"""
LiDAR Azure Upload Tool
=======================

Windows GUI for drone pilots to upload LiDAR data to Azure Blob Storage
(wpbstorageaccount / lidardata) via a portable azcopy.exe that must live in
the same directory as this script.

Layout on blob:
    lidardata/{client}/{program}/{feeder}/
        BASE_STATION/{collection_date}/
        SENSOR_DATA/{collection_date}/
            manifest.json
            {mission_folder}.zip
        CLIENT_DELIEVERABLES/

Robustness:
 - Single-mission-at-a-time upload (max the pipe on bad networks).
 - Background zipper prepares the next mission while one uploads.
 - Per-mission md5 + azcopy --put-md5 + REST HEAD verify against blob.
 - Cloud manifest is the source of truth; re-uploaded after every mission.
 - Local session manifest is a single-file resume handle. Drive letters
   can be remapped on resume (external SD readers change letters).

Note on double-circuit feeders (e.g. LCO071 / LCO072): a mission may
physically belong to both feeders. This tool does NOT elegantly handle
that case -- it forces every mission into exactly one feeder based on
the feeder name typed at session start. Pilots must currently duplicate
or manually split such data outside this tool.
"""

# =============================================================================
# CONFIG -- PASTE YOUR SAS TOKEN BELOW
# =============================================================================

# Container-scoped SAS token for the "lidardata" container.
# Must begin with "?" and include read+add+create+write+list permissions,
# e.g. "?sv=2023-11-03&ss=b&srt=sco&sp=rwdlac&se=...&sig=..."
SAS_TOKEN = "?PASTE_YOUR_SAS_TOKEN_HERE"

STORAGE_ACCOUNT = "wpbstorageaccount"
CONTAINER = "lidardata"

# Toggle default for creating tiny per-directory README placeholders so that
# empty directories persist in a non-hierarchical namespace. Also exposed in
# the Advanced Options dialog at runtime.
CREATE_DIR_READMES_DEFAULT = True

# =============================================================================

import base64
import ctypes
import ctypes.wintypes
import datetime as dt
import hashlib
import json
import os
import platform
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# --- constants ---------------------------------------------------------------

BLOB_URL_BASE = f"https://{STORAGE_ACCOUNT}.blob.core.windows.net/{CONTAINER}"

VALID_CLIENTS = ["xcel"]
VALID_PROGRAMS = ["2026"]
# Only L3 is wired up right now.
SENSOR_CHOICES = [("L3", True), ("TV540", False), ("TVGO", False)]

DATA_TYPE_RAW = "RAW_SENSOR"
DATA_TYPE_PROCESSED = "PROCESSED_SENSOR"

DIR_BASE_STATION = "BASE_STATION"
DIR_SENSOR_DATA = "SENSOR_DATA"
DIR_CLIENT_DELIVERABLES = "CLIENT_DELIEVERABLES"  # spelled per spec

FEEDER_WARN_REGEX = re.compile(r"^[A-Z]{3}\d{3}$")  # warn-only
MISSION_REGEX = re.compile(
    r"^DJI_(?P<Y>\d{4})(?P<M>\d{2})(?P<D>\d{2})(?P<h>\d{2})(?P<m>\d{2})"
    r"_(?P<seq>\d+)_(?P<feeder>[A-Z0-9]+)$"
)
BASE_STATION_DAT_REGEX = re.compile(
    r"^DRTK\d+_\d+_(?P<Y>\d{4})(?P<M>\d{2})(?P<D>\d{2})\d{6}_.+\.dat$",
    re.IGNORECASE,
)
BASE_STATION_INDEX_NAMES = {"latest_index", "latest_index.txt"}

PROCESSED_EXTS = {".las", ".laz"}

DEFAULT_ADVANCED = {
    "local_retries": 3,          # X: per-mission local retries within a session
    "global_retries": 2,         # Y: global retries per mission across sessions
    "timeout_seconds": 3600,     # T: azcopy per-call timeout
    "create_readmes": CREATE_DIR_READMES_DEFAULT,
    "zip_scratch_dir": "",       # "" -> system temp
}

STATUS_PENDING = "pending"
STATUS_ZIPPING = "zipping"
STATUS_ZIPPED = "zipped"
STATUS_UPLOADING = "uploading"
STATUS_UPLOADED = "uploaded"     # bytes in blob, not yet verified
STATUS_VERIFIED = "verified"     # md5 match confirmed
STATUS_FAILED = "failed"

TERMINAL_OK = {STATUS_VERIFIED}


# --- system / environment helpers -------------------------------------------


def script_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent.resolve()


def azcopy_path() -> Path:
    exe = "azcopy.exe" if os.name == "nt" else "azcopy"
    return script_dir() / exe


def get_system_name() -> str:
    return os.environ.get("COMPUTERNAME") or platform.node() or "unknown"


def get_disk_name(path: Path) -> str:
    """Return the Windows volume label for the drive that owns ``path``.

    Falls back to the drive letter (or "unknown" off-Windows) so this
    script still imports and runs sanity checks on non-Windows dev boxes.
    """
    if os.name != "nt":
        return "non-windows"
    try:
        drive = os.path.splitdrive(os.path.abspath(str(path)))[0]
        if not drive:
            return "unknown"
        root = drive + "\\"
        vol_name_buf = ctypes.create_unicode_buffer(261)
        fs_name_buf = ctypes.create_unicode_buffer(261)
        serial = ctypes.wintypes.DWORD()
        max_comp = ctypes.wintypes.DWORD()
        flags = ctypes.wintypes.DWORD()
        ok = ctypes.windll.kernel32.GetVolumeInformationW(  # type: ignore[attr-defined]
            ctypes.c_wchar_p(root),
            vol_name_buf, ctypes.sizeof(vol_name_buf),
            ctypes.byref(serial),
            ctypes.byref(max_comp),
            ctypes.byref(flags),
            fs_name_buf, ctypes.sizeof(fs_name_buf),
        )
        if not ok:
            return drive
        return vol_name_buf.value or drive
    except Exception:
        return "unknown"


# --- blob URL helpers --------------------------------------------------------


def blob_url(*path_parts: str) -> str:
    """Join path parts to the container base, appending the SAS token."""
    quoted = "/".join(urllib.parse.quote(p, safe="") for p in path_parts if p)
    return f"{BLOB_URL_BASE}/{quoted}{SAS_TOKEN}"


def blob_url_no_sas(*path_parts: str) -> str:
    quoted = "/".join(urllib.parse.quote(p, safe="") for p in path_parts if p)
    return f"{BLOB_URL_BASE}/{quoted}"


def feeder_prefix(client: str, program: str, feeder: str) -> str:
    return f"{client}/{program}/{feeder}"


# --- hashing -----------------------------------------------------------------


def md5_file(path: Path, chunk: int = 4 * 1024 * 1024) -> tuple[str, str, int]:
    """Return (hex, base64, size_bytes) MD5 of a file."""
    h = hashlib.md5()
    size = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
            size += len(b)
    return h.hexdigest(), base64.b64encode(h.digest()).decode("ascii"), size


# --- azcopy wrapper ----------------------------------------------------------


class AzcopyError(RuntimeError):
    pass


def _run_azcopy(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    exe = azcopy_path()
    if not exe.exists():
        raise AzcopyError(
            f"azcopy binary not found at {exe}. Place azcopy.exe next to this script."
        )
    env = os.environ.copy()
    # Only log errors, as spec'd.
    env["AZCOPY_LOG_LEVEL"] = "ERROR"
    try:
        return subprocess.run(
            [str(exe)] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        raise AzcopyError(f"azcopy timed out after {timeout}s") from e


def azcopy_upload_file(local: Path, dest_url: str, timeout: int, put_md5: bool = True) -> None:
    args = ["copy", str(local), dest_url, "--log-level=ERROR", "--output-level=essential"]
    if put_md5:
        args.append("--put-md5")
    r = _run_azcopy(args, timeout)
    if r.returncode != 0:
        raise AzcopyError(
            f"azcopy failed (rc={r.returncode}): {r.stderr.strip() or r.stdout.strip()}"
        )


# --- blob REST (manifest + verify) -------------------------------------------


def blob_head(url: str) -> dict[str, str]:
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as r:
        return {k.lower(): v for k, v in r.headers.items()}


def blob_get_bytes(url: str) -> bytes | None:
    """Return blob bytes, or None on 404."""
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def blob_put_bytes(url: str, data: bytes, content_type: str = "application/json") -> None:
    req = urllib.request.Request(url, data=data, method="PUT")
    req.add_header("x-ms-blob-type", "BlockBlob")
    req.add_header("Content-Type", content_type)
    req.add_header("Content-Length", str(len(data)))
    with urllib.request.urlopen(req, timeout=120) as r:
        if r.status not in (200, 201):
            raise RuntimeError(f"manifest PUT returned {r.status}")


def blob_md5_b64(url: str) -> str | None:
    headers = blob_head(url)
    md5 = headers.get("content-md5")
    return md5


# --- validators --------------------------------------------------------------


def scan_missions(source_root: Path, expected_feeder: str) -> tuple[list[dict], list[dict]]:
    """Return (valid_missions, invalid_entries).

    Each valid entry: {"name","path","date","seq","feeder","file_count"}.
    Invalid: {"name","reason"}.
    """
    valid: list[dict] = []
    invalid: list[dict] = []
    for entry in sorted(source_root.iterdir()):
        if not entry.is_dir():
            continue
        m = MISSION_REGEX.match(entry.name)
        if not m:
            invalid.append({"name": entry.name, "reason": "does not match DJI_YYYYMMDDHHMM_SEQ_FEEDER"})
            continue
        try:
            date = dt.date(int(m["Y"]), int(m["M"]), int(m["D"])).isoformat()
        except ValueError:
            invalid.append({"name": entry.name, "reason": "invalid date in folder name"})
            continue
        feeder_in_name = m["feeder"]
        if feeder_in_name != expected_feeder:
            invalid.append({
                "name": entry.name,
                "reason": f"feeder in folder ({feeder_in_name}) != entered feeder ({expected_feeder})",
            })
            continue
        file_count = sum(1 for _ in entry.rglob("*") if _.is_file())
        valid.append({
            "name": entry.name,
            "path": str(entry),
            "date": date,
            "seq": m["seq"],
            "feeder": feeder_in_name,
            "file_count": file_count,
        })
    return valid, invalid


def detect_data_type(source_root: Path) -> str | None:
    """Sniff whether a source folder is RAW mission folders or PROCESSED .las/.laz.

    Returns DATA_TYPE_RAW, DATA_TYPE_PROCESSED, or None if a mix / empty.
    """
    has_missions = False
    has_processed = False
    has_other = False
    for entry in source_root.iterdir():
        if entry.is_dir() and MISSION_REGEX.match(entry.name):
            has_missions = True
        elif entry.is_file() and entry.suffix.lower() in PROCESSED_EXTS:
            has_processed = True
        else:
            has_other = True  # noqa: F841 -- informational only
    if has_missions and has_processed:
        return None  # mix -> caller must refuse
    if has_missions:
        return DATA_TYPE_RAW
    if has_processed:
        return DATA_TYPE_PROCESSED
    return None


def scan_base_station(folder: Path) -> tuple[list[Path], str | None, list[str]]:
    """Return (dat_files, collection_date, warnings).

    Refuses non-.dat / non-latest_index files (warnings list them).
    """
    dats: list[Path] = []
    warnings: list[str] = []
    dates: set[str] = set()
    for entry in folder.iterdir():
        if entry.is_dir():
            warnings.append(f"skipping subfolder: {entry.name}")
            continue
        low = entry.name.lower()
        if low in BASE_STATION_INDEX_NAMES:
            dats.append(entry)
            continue
        m = BASE_STATION_DAT_REGEX.match(entry.name)
        if m:
            dats.append(entry)
            try:
                dates.add(dt.date(int(m["Y"]), int(m["M"]), int(m["D"])).isoformat())
            except ValueError:
                pass
            continue
        warnings.append(f"rejected (only .dat + latest_index allowed): {entry.name}")
    if len(dates) > 1:
        warnings.append(f"multiple collection dates in .dat filenames: {sorted(dates)}")
    date = sorted(dates)[0] if dates else None
    return dats, date, warnings


# --- manifest ----------------------------------------------------------------


MANIFEST_VERSION = 1


def new_cloud_manifest(client, program, feeder, sensor, data_type,
                      collection_date, system_name) -> dict:
    return {
        "version": MANIFEST_VERSION,
        "client": client,
        "program": program,
        "feeder": feeder,
        "sensor": sensor,
        "data_type": data_type,
        "collection_date": collection_date,
        "system_name": system_name,
        "created_utc": dt.datetime.utcnow().isoformat() + "Z",
        "missions": {},   # name -> mission dict
        "summary": {"total": 0, "verified": 0},
    }


def cloud_manifest_blob_path(client, program, feeder, collection_date) -> str:
    return f"{client}/{program}/{feeder}/{DIR_SENSOR_DATA}/{collection_date}/manifest.json"


def load_cloud_manifest(client, program, feeder, collection_date) -> dict | None:
    path = cloud_manifest_blob_path(client, program, feeder, collection_date)
    data = blob_get_bytes(blob_url(path))
    if data is None:
        return None
    return json.loads(data.decode("utf-8"))


def save_cloud_manifest(manifest: dict) -> None:
    path = cloud_manifest_blob_path(
        manifest["client"], manifest["program"],
        manifest["feeder"], manifest["collection_date"],
    )
    manifest["summary"] = {
        "total": len(manifest["missions"]),
        "verified": sum(1 for m in manifest["missions"].values() if m.get("status") == STATUS_VERIFIED),
        "updated_utc": dt.datetime.utcnow().isoformat() + "Z",
    }
    data = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    blob_put_bytes(blob_url(path), data, content_type="application/json")


def local_manifest_default_path(feeder: str, scratch_dir: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return scratch_dir / f"lidar_session_{feeder}_{stamp}.json"


def save_local_manifest(local: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(local, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def load_local_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --- upload session ----------------------------------------------------------


class UploadSession:
    """Owns state + threads for one running upload session.

    Emits progress via ``log_cb(str)`` and ``progress_cb(done, total, mission_name)``
    which are called on the worker thread; GUI callers should marshal to Tk.
    """

    def __init__(self,
                 local_manifest_path: Path,
                 local_manifest: dict,
                 advanced: dict,
                 log_cb, progress_cb, done_cb):
        self.local_manifest_path = local_manifest_path
        self.local_manifest = local_manifest
        self.advanced = advanced
        self.log_cb = log_cb
        self.progress_cb = progress_cb
        self.done_cb = done_cb

        self._stop = threading.Event()
        self._pause_pipeline = threading.Event()  # set when we must halt
        self._zip_queue: queue.Queue = queue.Queue(maxsize=1)  # (mission_name, zip_path, md5_b64, size)
        self._thread: threading.Thread | None = None

    # ---- public control ----

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ---- run loop ----

    def _log(self, msg: str):
        try:
            self.log_cb(msg)
        except Exception:
            pass

    def _progress(self, mission: str = ""):
        try:
            missions_by_date = self._collect_missions()
            total = sum(len(m) for m in missions_by_date.values())
            done = sum(1 for missions in missions_by_date.values()
                       for m in missions.values() if m.get("status") == STATUS_VERIFIED)
            self.progress_cb(done, total, mission)
        except Exception:
            pass

    def _collect_missions(self) -> dict[str, dict]:
        """date -> {mission_name -> mission_dict} loaded from cloud manifests cache."""
        return self._cloud_cache

    def _run(self):
        try:
            self._cloud_cache: dict[str, dict] = {}  # date -> mission dict
            self._cloud_manifests: dict[str, dict] = {}  # date -> full manifest
            self._prepare_all_dates()
            self._progress()

            # Zipper thread
            zipper = threading.Thread(target=self._zip_loop, daemon=True)
            zipper.start()

            # Uploader loop (this thread)
            self._upload_loop()

            zipper.join(timeout=5)

            # Optional base station leg
            if self.local_manifest.get("base_station"):
                self._upload_base_station()

            self.done_cb(True, "All uploads finished.")
        except Exception as e:
            self._log(f"FATAL: {e}\n{traceback.format_exc()}")
            self.done_cb(False, str(e))

    # ---- date prep ----

    def _prepare_all_dates(self):
        lm = self.local_manifest
        for date, entry in lm["dates"].items():
            cloud_path = entry["cloud_manifest_blob_path"]
            self._log(f"Fetching cloud manifest for {date}...")
            existing_bytes = blob_get_bytes(blob_url(cloud_path))
            if existing_bytes:
                manifest = json.loads(existing_bytes.decode("utf-8"))
                self._log(f"  found existing manifest with {len(manifest['missions'])} missions")
            else:
                manifest = new_cloud_manifest(
                    client=lm["client"], program=lm["program"],
                    feeder=lm["feeder"], sensor=lm["sensor"],
                    data_type=lm["data_type"], collection_date=date,
                    system_name=lm["system_name"],
                )
                self._log("  no existing manifest, will create fresh")

            for m in entry.get("missions", []):
                name = m["name"]
                if name in manifest["missions"]:
                    mission = manifest["missions"][name]
                    mission["original_path"] = m["path"]  # refresh path (drive remap)
                    mission["file_count"] = m["file_count"]
                else:
                    manifest["missions"][name] = {
                        "original_path": m["path"],
                        "file_count": m["file_count"],
                        "system_name": lm["system_name"],
                        "disk_name": get_disk_name(Path(m["path"])),
                        "status": STATUS_PENDING,
                        "attempts": 0,
                        "global_attempts": 0,
                        "zip_md5_b64": None,
                        "zip_size_bytes": None,
                        "last_error": None,
                    }
            # bump global_attempts for any that were mid-flight or failed
            for name, mission in manifest["missions"].items():
                if mission.get("status") in (STATUS_ZIPPING, STATUS_UPLOADING,
                                              STATUS_UPLOADED, STATUS_FAILED):
                    mission["global_attempts"] = mission.get("global_attempts", 0) + 1
                    mission["attempts"] = 0
                    mission["status"] = STATUS_PENDING

            save_cloud_manifest(manifest)
            self._cloud_manifests[date] = manifest
            self._cloud_cache[date] = manifest["missions"]

    # ---- zipper thread ----

    def _zip_loop(self):
        try:
            for date, manifest in self._cloud_manifests.items():
                for name, mission in manifest["missions"].items():
                    if self._stop.is_set():
                        return
                    if mission["status"] == STATUS_VERIFIED:
                        continue
                    if mission.get("global_attempts", 0) >= self.advanced["global_retries"]:
                        self._log(f"[zip] {name}: global retries exhausted, skipping")
                        continue
                    src = Path(mission["original_path"])
                    if not src.exists():
                        self._log(f"[zip] {name}: source path missing: {src}")
                        mission["status"] = STATUS_FAILED
                        mission["last_error"] = f"source path missing: {src}"
                        continue
                    scratch = self._scratch_dir()
                    zip_path = scratch / f"{name}.zip"
                    self._log(f"[zip] {name}: zipping -> {zip_path}")
                    mission["status"] = STATUS_ZIPPING
                    try:
                        _make_zip(src, zip_path)
                    except Exception as e:
                        self._log(f"[zip] {name}: FAILED {e}")
                        mission["status"] = STATUS_FAILED
                        mission["last_error"] = f"zip failed: {e}"
                        continue
                    _hex, b64, size = md5_file(zip_path)
                    mission["zip_md5_b64"] = b64
                    mission["zip_size_bytes"] = size
                    mission["status"] = STATUS_ZIPPED
                    self._log(f"[zip] {name}: ready ({size / 1e6:.1f} MB, md5={b64})")
                    self._zip_queue.put((date, name, zip_path))
            self._zip_queue.put(None)  # sentinel
        except Exception as e:
            self._log(f"[zip] fatal: {e}")
            self._zip_queue.put(None)

    def _scratch_dir(self) -> Path:
        d = self.advanced.get("zip_scratch_dir") or ""
        base = Path(d) if d else Path(tempfile.gettempdir()) / "lidar_upload_scratch"
        base.mkdir(parents=True, exist_ok=True)
        return base

    # ---- uploader loop ----

    def _upload_loop(self):
        while not self._stop.is_set():
            item = self._zip_queue.get()
            if item is None:
                return
            date, name, zip_path = item
            manifest = self._cloud_manifests[date]
            mission = manifest["missions"][name]
            ok = self._upload_one(date, name, zip_path, mission, manifest)
            try:
                zip_path.unlink(missing_ok=True)
            except Exception:
                pass
            if not ok and self._stop.is_set():
                return

    def _upload_one(self, date: str, name: str, zip_path: Path,
                    mission: dict, manifest: dict) -> bool:
        lm = self.local_manifest
        dest = blob_url(
            lm["client"], lm["program"], lm["feeder"],
            DIR_SENSOR_DATA, date, f"{name}.zip",
        )
        dest_no_sas = blob_url_no_sas(
            lm["client"], lm["program"], lm["feeder"],
            DIR_SENSOR_DATA, date, f"{name}.zip",
        )
        timeout = self.advanced["timeout_seconds"]
        local_retries = self.advanced["local_retries"]

        for attempt in range(1, local_retries + 1):
            if self._stop.is_set():
                return False
            mission["status"] = STATUS_UPLOADING
            mission["attempts"] = attempt
            self._log(f"[upload] {name}: attempt {attempt}/{local_retries}")
            try:
                azcopy_upload_file(zip_path, dest, timeout=timeout, put_md5=True)
            except Exception as e:
                mission["last_error"] = f"upload attempt {attempt}: {e}"
                self._log(f"[upload] {name}: {e}")
                self._sleep_backoff(attempt)
                continue
            mission["status"] = STATUS_UPLOADED
            # verify md5 via HEAD
            try:
                remote_md5 = blob_md5_b64(blob_url(
                    lm["client"], lm["program"], lm["feeder"],
                    DIR_SENSOR_DATA, date, f"{name}.zip",
                ))
            except Exception as e:
                mission["last_error"] = f"HEAD failed: {e}"
                self._log(f"[verify] {name}: HEAD failed: {e}")
                self._sleep_backoff(attempt)
                continue
            if remote_md5 and remote_md5 == mission["zip_md5_b64"]:
                mission["status"] = STATUS_VERIFIED
                mission["uploaded_utc"] = dt.datetime.utcnow().isoformat() + "Z"
                mission["last_error"] = None
                self._log(f"[verify] {name}: OK ({remote_md5})")
                self._save_and_upload_manifest(date, manifest)
                self._progress(name)
                return True
            mission["last_error"] = (
                f"md5 mismatch: local={mission['zip_md5_b64']} remote={remote_md5}"
            )
            self._log(f"[verify] {name}: MISMATCH; retrying")
            self._sleep_backoff(attempt)

        mission["status"] = STATUS_FAILED
        mission["global_attempts"] = mission.get("global_attempts", 0) + 1
        self._save_and_upload_manifest(date, manifest)
        self._log(f"[upload] {name}: exhausted local retries. Global attempts={mission['global_attempts']}")
        # Continue to next mission rather than halting the whole session.
        return False

    def _sleep_backoff(self, attempt: int):
        delay = min(60, 2 ** attempt)
        for _ in range(delay):
            if self._stop.is_set():
                return
            time.sleep(1)

    def _save_and_upload_manifest(self, date: str, manifest: dict):
        try:
            save_cloud_manifest(manifest)
        except Exception as e:
            # Per spec: if manifest upload fails, stop and error.
            self._log(f"FATAL: manifest upload failed for {date}: {e}")
            self._stop.set()
            raise
        save_local_manifest(self.local_manifest, self.local_manifest_path)

    # ---- base station ----

    def _upload_base_station(self):
        lm = self.local_manifest
        bs = lm["base_station"]
        date = bs["collection_date"]
        self._log(f"[base] uploading {len(bs['files'])} files to BASE_STATION/{date}/")
        timeout = self.advanced["timeout_seconds"]
        for file_entry in bs["files"]:
            if self._stop.is_set():
                return
            src = Path(file_entry["path"])
            if not src.exists():
                self._log(f"[base] missing: {src}")
                continue
            dest = blob_url(lm["client"], lm["program"], lm["feeder"],
                            DIR_BASE_STATION, date, src.name)
            for attempt in range(1, self.advanced["local_retries"] + 1):
                try:
                    azcopy_upload_file(src, dest, timeout=timeout, put_md5=True)
                    self._log(f"[base] uploaded {src.name}")
                    file_entry["status"] = STATUS_VERIFIED
                    save_local_manifest(self.local_manifest, self.local_manifest_path)
                    break
                except Exception as e:
                    self._log(f"[base] {src.name} attempt {attempt}: {e}")
                    self._sleep_backoff(attempt)
            else:
                self._log(f"[base] {src.name}: gave up")


def _make_zip(src_dir: Path, out_path: Path) -> None:
    """Deterministic-ish zip of a directory. ZIP_STORED (no compression) since
    LiDAR files are already compact/binary and we care about throughput."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".part")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for p in sorted(src_dir.rglob("*")):
            if p.is_file():
                zf.write(p, arcname=p.relative_to(src_dir.parent).as_posix())
    tmp.replace(out_path)


# --- README placeholders -----------------------------------------------------


README_TEXTS = {
    DIR_BASE_STATION: (
        "This directory holds base station files (.dat and latest_index) captured "
        "during the LiDAR mission. One subdirectory per collection date (YYYY-MM-DD).\n"
    ),
    DIR_SENSOR_DATA: (
        "This directory holds sensor data uploads. One subdirectory per collection "
        "date (YYYY-MM-DD), containing a manifest.json and one .zip per DJI mission "
        "folder.\n"
    ),
    DIR_CLIENT_DELIVERABLES: (
        "This directory is reserved for client deliverables (processed products, "
        "reports, exports).\n"
    ),
}


def create_feeder_readmes(client: str, program: str, feeder: str):
    for sub, text in README_TEXTS.items():
        path = f"{client}/{program}/{feeder}/{sub}/README.txt"
        blob_put_bytes(blob_url(path), text.encode("utf-8"), content_type="text/plain")


# =============================================================================
# GUI
# =============================================================================


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("UU LiDAR Upload Tool")
        self.geometry("900x680")
        self.advanced = dict(DEFAULT_ADVANCED)
        self.session: UploadSession | None = None
        self._build_start()

    def _clear(self):
        for w in self.winfo_children():
            w.destroy()

    def _build_start(self):
        self._clear()
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="UU LiDAR Upload Tool", font=("Segoe UI", 20, "bold")).pack(pady=(0, 6))
        ttk.Label(frame, text=f"Target: {BLOB_URL_BASE}").pack()
        if SAS_TOKEN.startswith("?PASTE"):
            ttk.Label(frame, text="WARNING: SAS token not set in lidar_upload.py",
                      foreground="red").pack(pady=6)
        ttk.Label(frame, text=f"azcopy: {azcopy_path()}  "
                              f"({'found' if azcopy_path().exists() else 'MISSING'})",
                  foreground="black" if azcopy_path().exists() else "red").pack(pady=2)

        ttk.Separator(frame).pack(fill="x", pady=14)
        ttk.Button(frame, text="New upload", width=30,
                   command=self._start_new_wizard).pack(pady=6)
        ttk.Button(frame, text="Resume from local manifest...", width=30,
                   command=self._start_resume).pack(pady=6)
        ttk.Button(frame, text="Advanced options...", width=30,
                   command=self._open_advanced).pack(pady=6)
        ttk.Button(frame, text="Exit", width=30, command=self.destroy).pack(pady=6)

    def _open_advanced(self):
        AdvancedDialog(self, self.advanced)

    # --- new upload wizard ---

    def _start_new_wizard(self):
        NewUploadWizard(self, on_ready=self._start_session_from_wizard)

    def _start_session_from_wizard(self, local_manifest: dict, local_path: Path):
        self._build_progress()
        session = UploadSession(
            local_manifest_path=local_path,
            local_manifest=local_manifest,
            advanced=dict(self.advanced),
            log_cb=self._log,
            progress_cb=self._progress,
            done_cb=self._done,
        )
        self.session = session
        session.start()

    # --- resume ---

    def _start_resume(self):
        path = filedialog.askopenfilename(
            title="Select local session manifest (.json)",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            local = load_local_manifest(Path(path))
        except Exception as e:
            messagebox.showerror("Load failed", str(e))
            return
        # Offer drive remap
        remapped = DriveRemapDialog(self, local).result
        if remapped is False:
            return
        save_local_manifest(local, Path(path))
        self._build_progress()
        session = UploadSession(
            local_manifest_path=Path(path),
            local_manifest=local,
            advanced=dict(self.advanced),
            log_cb=self._log,
            progress_cb=self._progress,
            done_cb=self._done,
        )
        self.session = session
        session.start()

    # --- progress screen ---

    def _build_progress(self):
        self._clear()
        frame = ttk.Frame(self, padding=10)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Upload in progress", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        self.progress_var = tk.StringVar(value="0 / 0")
        ttk.Label(frame, textvariable=self.progress_var).pack(anchor="w")
        self.progress_bar = ttk.Progressbar(frame, maximum=1, value=0)
        self.progress_bar.pack(fill="x", pady=4)
        self.current_mission = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.current_mission).pack(anchor="w")

        log_frame = ttk.LabelFrame(frame, text="Log")
        log_frame.pack(fill="both", expand=True, pady=6)
        self.log_widget = tk.Text(log_frame, height=20, wrap="word")
        self.log_widget.pack(fill="both", expand=True)

        btns = ttk.Frame(frame)
        btns.pack(fill="x")
        ttk.Button(btns, text="Stop", command=self._stop_session).pack(side="right")

    def _log(self, msg: str):
        # Called from worker thread -> marshal
        self.after(0, lambda: self._append_log(msg))

    def _append_log(self, msg: str):
        self.log_widget.insert("end", f"{dt.datetime.now().strftime('%H:%M:%S')} {msg}\n")
        self.log_widget.see("end")

    def _progress(self, done: int, total: int, mission: str):
        def apply():
            self.progress_bar["maximum"] = max(total, 1)
            self.progress_bar["value"] = done
            self.progress_var.set(f"{done} / {total} missions verified")
            if mission:
                self.current_mission.set(f"Last: {mission}")
        self.after(0, apply)

    def _done(self, ok: bool, msg: str):
        def apply():
            self._append_log(("DONE: " if ok else "FAILED: ") + msg)
            messagebox.showinfo("Finished" if ok else "Stopped", msg)
        self.after(0, apply)

    def _stop_session(self):
        if self.session:
            self.session.stop()
            self._append_log("Stop requested; will halt after current mission.")


# --- advanced options dialog -------------------------------------------------


class AdvancedDialog(tk.Toplevel):
    def __init__(self, parent: App, advanced: dict):
        super().__init__(parent)
        self.title("Advanced options")
        self.advanced = advanced
        self.resizable(False, False)

        vars_ = {}
        rows = [
            ("Local retries per mission (X)", "local_retries", int),
            ("Global retries per mission (Y)", "global_retries", int),
            ("azcopy per-call timeout seconds (T)", "timeout_seconds", int),
            ("Zip scratch dir (blank = temp)", "zip_scratch_dir", str),
        ]
        for i, (label, key, _t) in enumerate(rows):
            ttk.Label(self, text=label).grid(row=i, column=0, sticky="w", padx=8, pady=4)
            v = tk.StringVar(value=str(advanced.get(key, "")))
            ttk.Entry(self, textvariable=v, width=40).grid(row=i, column=1, padx=8, pady=4)
            vars_[key] = (v, _t)

        readme_var = tk.BooleanVar(value=bool(advanced.get("create_readmes", True)))
        ttk.Checkbutton(self, text="Create tiny README.txt per feeder subdirectory (so empty dirs persist)",
                        variable=readme_var).grid(row=len(rows), column=0, columnspan=2, sticky="w", padx=8, pady=8)

        def save():
            try:
                for key, (v, t) in vars_.items():
                    val = v.get().strip()
                    advanced[key] = t(val) if val or t is str else t(0)
                advanced["create_readmes"] = readme_var.get()
            except Exception as e:
                messagebox.showerror("Bad value", str(e))
                return
            self.destroy()

        ttk.Button(self, text="Save", command=save).grid(row=len(rows) + 1, column=1, sticky="e", padx=8, pady=8)
        self.transient(parent)
        self.grab_set()


# --- new upload wizard -------------------------------------------------------


class NewUploadWizard(tk.Toplevel):
    def __init__(self, parent: App, on_ready):
        super().__init__(parent)
        self.title("New upload")
        self.geometry("780x640")
        self.parent_app = parent
        self.on_ready = on_ready

        self.client_var = tk.StringVar(value=VALID_CLIENTS[0])
        self.program_var = tk.StringVar(value=VALID_PROGRAMS[0])
        self.feeder_var = tk.StringVar()
        self.feeder_confirm_var = tk.StringVar()
        self.sensor_var = tk.StringVar(value="L3")
        self.source_var = tk.StringVar()
        self.base_source_var = tk.StringVar()
        self.include_base_var = tk.BooleanVar(value=False)
        self.upload_sensor_var = tk.BooleanVar(value=True)

        pad = {"padx": 8, "pady": 4}
        row = 0
        ttk.Label(self, text="Client").grid(row=row, column=0, sticky="w", **pad)
        ttk.Combobox(self, values=VALID_CLIENTS, textvariable=self.client_var,
                     state="readonly").grid(row=row, column=1, sticky="ew", **pad)
        row += 1
        ttk.Label(self, text="Program").grid(row=row, column=0, sticky="w", **pad)
        ttk.Combobox(self, values=VALID_PROGRAMS, textvariable=self.program_var,
                     state="readonly").grid(row=row, column=1, sticky="ew", **pad)
        row += 1
        ttk.Label(self, text="Feeder (e.g. LAW322)").grid(row=row, column=0, sticky="w", **pad)
        e = ttk.Entry(self, textvariable=self.feeder_var)
        e.grid(row=row, column=1, sticky="ew", **pad)
        e.bind("<KeyRelease>", lambda _e: self.feeder_var.set(self.feeder_var.get().upper()))
        row += 1
        ttk.Label(self, text="Re-type feeder to confirm").grid(row=row, column=0, sticky="w", **pad)
        e2 = ttk.Entry(self, textvariable=self.feeder_confirm_var)
        e2.grid(row=row, column=1, sticky="ew", **pad)
        e2.bind("<KeyRelease>", lambda _e: self.feeder_confirm_var.set(self.feeder_confirm_var.get().upper()))
        row += 1
        ttk.Label(self, text="Sensor").grid(row=row, column=0, sticky="w", **pad)
        sensor_frame = ttk.Frame(self)
        sensor_frame.grid(row=row, column=1, sticky="w", **pad)
        for name, enabled in SENSOR_CHOICES:
            rb = ttk.Radiobutton(sensor_frame, text=name, value=name, variable=self.sensor_var)
            if not enabled:
                rb.state(["disabled"])
            rb.pack(side="left", padx=4)
        row += 1

        ttk.Separator(self, orient="horizontal").grid(row=row, column=0, columnspan=3, sticky="ew", pady=6)
        row += 1
        ttk.Checkbutton(self, text="Upload SENSOR_DATA", variable=self.upload_sensor_var).grid(
            row=row, column=0, columnspan=2, sticky="w", **pad)
        row += 1
        ttk.Label(self, text="Sensor source folder").grid(row=row, column=0, sticky="w", **pad)
        ttk.Entry(self, textvariable=self.source_var).grid(row=row, column=1, sticky="ew", **pad)
        ttk.Button(self, text="Browse", command=self._pick_source).grid(row=row, column=2, **pad)
        row += 1

        ttk.Separator(self, orient="horizontal").grid(row=row, column=0, columnspan=3, sticky="ew", pady=6)
        row += 1
        ttk.Checkbutton(self, text="Also upload BASE_STATION folder", variable=self.include_base_var).grid(
            row=row, column=0, columnspan=2, sticky="w", **pad)
        row += 1
        ttk.Label(self, text="Base station folder").grid(row=row, column=0, sticky="w", **pad)
        ttk.Entry(self, textvariable=self.base_source_var).grid(row=row, column=1, sticky="ew", **pad)
        ttk.Button(self, text="Browse", command=self._pick_base).grid(row=row, column=2, **pad)
        row += 1

        self.columnconfigure(1, weight=1)

        self.report = tk.Text(self, height=14, wrap="word")
        self.report.grid(row=row, column=0, columnspan=3, sticky="nsew", padx=8, pady=8)
        self.rowconfigure(row, weight=1)
        row += 1

        btns = ttk.Frame(self)
        btns.grid(row=row, column=0, columnspan=3, sticky="ew", padx=8, pady=8)
        ttk.Button(btns, text="Scan / validate", command=self._scan).pack(side="left")
        ttk.Button(btns, text="Start upload", command=self._confirm_start).pack(side="right")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=6)

        self.scanned: dict | None = None

    def _pick_source(self):
        d = filedialog.askdirectory(title="Select sensor source folder")
        if d:
            self.source_var.set(d)

    def _pick_base(self):
        d = filedialog.askdirectory(title="Select base station folder")
        if d:
            self.base_source_var.set(d)

    def _report(self, s: str):
        self.report.insert("end", s + "\n")
        self.report.see("end")

    def _scan(self):
        self.report.delete("1.0", "end")
        feeder = self.feeder_var.get().strip().upper()
        confirm = self.feeder_confirm_var.get().strip().upper()
        if not feeder or feeder != confirm:
            messagebox.showerror("Feeder mismatch", "Feeder name and confirmation must match.")
            return
        if not FEEDER_WARN_REGEX.match(feeder):
            self._report(f"WARNING: feeder '{feeder}' does not match typical format LLLDDD (3 letters + 3 digits).")

        sensor_missions: dict[str, list[dict]] = {}
        data_type: str | None = None
        if self.upload_sensor_var.get():
            src = self.source_var.get().strip()
            if not src or not Path(src).is_dir():
                messagebox.showerror("Bad source", "Pick a valid sensor source folder.")
                return
            root = Path(src)
            data_type = detect_data_type(root)
            if data_type is None:
                messagebox.showerror(
                    "Mixed / empty",
                    "Sensor source must be either raw DJI_* mission folders OR .las/.laz files, not both.",
                )
                return
            self._report(f"Detected data type: {data_type}")
            if data_type == DATA_TYPE_RAW:
                valid, invalid = scan_missions(root, feeder)
                if invalid:
                    self._report("Invalid mission folder names -- fix, rename, or remove and re-scan:")
                    for i in invalid:
                        self._report(f"  {i['name']}: {i['reason']}")
                    messagebox.showwarning("Invalid names",
                        "One or more mission folders are invalid. See report; fix and re-scan.")
                    return
                for m in valid:
                    sensor_missions.setdefault(m["date"], []).append(m)
                self._report(f"Valid missions: {len(valid)} across {len(sensor_missions)} date(s).")
                for date, items in sorted(sensor_missions.items()):
                    self._report(f"  {date}: {len(items)} missions")
            else:
                # PROCESSED: flat set of .las/.laz, needs a date from the user or file mtime
                messagebox.showinfo(
                    "PROCESSED_SENSOR",
                    "PROCESSED_SENSOR support is experimental. Files will upload flat under "
                    "SENSOR_DATA/{collection_date}/. You'll be prompted for the date at Start.",
                )
                # collect files -> single 'processed' pseudo-mission per date; date = today unless overridden
                # store for later; the wizard will use a StringVar-populated date at Start.
                self._report(f"Processed files found: "
                             f"{sum(1 for p in root.iterdir() if p.suffix.lower() in PROCESSED_EXTS)}")
                sensor_missions["__processed__"] = [{
                    "name": f"processed_{dt.date.today().isoformat()}",
                    "path": str(root),
                    "date": dt.date.today().isoformat(),
                    "file_count": sum(1 for p in root.iterdir() if p.suffix.lower() in PROCESSED_EXTS),
                }]

        base_info = None
        if self.include_base_var.get():
            bp = self.base_source_var.get().strip()
            if not bp or not Path(bp).is_dir():
                messagebox.showerror("Bad base folder", "Pick a valid base station folder.")
                return
            dats, bdate, warnings = scan_base_station(Path(bp))
            for w in warnings:
                self._report(f"[base] {w}")
            if not dats:
                messagebox.showerror("Empty base folder", "No .dat / latest_index files found.")
                return
            if not bdate:
                messagebox.showerror("No date", "Could not determine collection date from .dat filenames.")
                return
            self._report(f"Base station: {len(dats)} files, date {bdate}")
            base_info = {"folder": bp, "date": bdate, "files": [str(p) for p in dats]}

        if not sensor_missions and not base_info:
            messagebox.showerror("Nothing to do", "Enable at least one of SENSOR_DATA or BASE_STATION.")
            return

        self.scanned = {
            "feeder": feeder,
            "data_type": data_type,
            "sensor_missions": sensor_missions,
            "base_info": base_info,
        }
        self._report("Scan OK. Press 'Start upload' to begin.")

    def _confirm_start(self):
        if not self.scanned:
            messagebox.showerror("Scan first", "Run 'Scan / validate' before starting.")
            return
        feeder = self.scanned["feeder"]
        if not messagebox.askyesno(
            "Confirm feeder",
            f"About to upload for feeder '{feeder}'.\n"
            f"Is that correct? All data will be filed under this feeder."
        ):
            return
        self._start()

    def _start(self):
        parent = self.parent_app
        adv = parent.advanced
        s = self.scanned
        client = self.client_var.get()
        program = self.program_var.get()
        feeder = s["feeder"]
        sensor = self.sensor_var.get()
        data_type = s["data_type"] or DATA_TYPE_RAW

        if adv.get("create_readmes", True):
            try:
                create_feeder_readmes(client, program, feeder)
                self._report("Created feeder README placeholders.")
            except Exception as e:
                self._report(f"WARNING: failed to write READMEs: {e}")

        # Build local manifest
        system_name = get_system_name()
        source_roots = []
        dates: dict[str, dict] = {}
        for date, missions in s["sensor_missions"].items():
            if date == "__processed__":
                real_date = missions[0]["date"]
            else:
                real_date = date
            dates[real_date] = {
                "cloud_manifest_blob_path": cloud_manifest_blob_path(client, program, feeder, real_date),
                "missions": missions,
            }
            for m in missions:
                source_roots.append(str(Path(m["path"]).parent))

        base_station = None
        if s["base_info"]:
            files_meta = []
            for p in s["base_info"]["files"]:
                pp = Path(p)
                files_meta.append({
                    "path": str(pp),
                    "name": pp.name,
                    "status": STATUS_PENDING,
                    "system_name": system_name,
                    "disk_name": get_disk_name(pp),
                })
                source_roots.append(str(pp.parent))
            base_station = {
                "source_folder": s["base_info"]["folder"],
                "collection_date": s["base_info"]["date"],
                "files": files_meta,
            }

        local_manifest = {
            "session_id": str(uuid.uuid4()),
            "created_utc": dt.datetime.utcnow().isoformat() + "Z",
            "system_name": system_name,
            "client": client,
            "program": program,
            "feeder": feeder,
            "sensor": sensor,
            "data_type": data_type,
            "source_roots": sorted(set(source_roots)),
            "zip_scratch_dir": adv.get("zip_scratch_dir") or "",
            "dates": dates,
            "base_station": base_station,
        }

        scratch = Path(adv.get("zip_scratch_dir") or tempfile.gettempdir()) / "lidar_upload_scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        local_path = local_manifest_default_path(feeder, scratch)
        save_local_manifest(local_manifest, local_path)

        messagebox.showinfo(
            "Session saved",
            f"Local session manifest saved to:\n{local_path}\n\n"
            "Keep this file! It's your resume handle.",
        )
        self.destroy()
        self.on_ready(local_manifest, local_path)


# --- drive remap dialog ------------------------------------------------------


class DriveRemapDialog(tk.Toplevel):
    def __init__(self, parent: App, local_manifest: dict):
        super().__init__(parent)
        self.title("Resume: verify source paths")
        self.local_manifest = local_manifest
        self.result: bool | None = None

        roots = sorted(set(local_manifest.get("source_roots", [])))
        # Also include mission original_paths' drive letters
        drives = sorted({os.path.splitdrive(r)[0] for r in roots if os.path.splitdrive(r)[0]})

        ttk.Label(self, text="Source drives referenced by this session:").pack(anchor="w", padx=10, pady=6)
        for r in roots:
            exists = Path(r).exists()
            ttk.Label(self, text=f"  {r}   {'(found)' if exists else '(MISSING)'}",
                      foreground="green" if exists else "red").pack(anchor="w", padx=10)

        ttk.Separator(self).pack(fill="x", pady=6)
        ttk.Label(self, text="Optional: remap a path prefix (e.g. E:\\ -> F:\\)").pack(anchor="w", padx=10)
        rowf = ttk.Frame(self); rowf.pack(fill="x", padx=10, pady=4)
        self.old_var = tk.StringVar()
        self.new_var = tk.StringVar()
        ttk.Label(rowf, text="Old prefix").grid(row=0, column=0, sticky="w")
        ttk.Entry(rowf, textvariable=self.old_var, width=30).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Label(rowf, text="New prefix").grid(row=1, column=0, sticky="w")
        ttk.Entry(rowf, textvariable=self.new_var, width=30).grid(row=1, column=1, sticky="ew", padx=4)
        rowf.columnconfigure(1, weight=1)

        btns = ttk.Frame(self); btns.pack(fill="x", padx=10, pady=10)
        ttk.Button(btns, text="Apply remap", command=self._apply).pack(side="left")
        ttk.Button(btns, text="Continue without remap", command=self._skip).pack(side="left", padx=6)
        ttk.Button(btns, text="Cancel", command=self._cancel).pack(side="right")

        self.transient(parent); self.grab_set(); self.wait_window(self)

    def _apply(self):
        old = self.old_var.get()
        new = self.new_var.get()
        if not old or not new:
            messagebox.showerror("Missing", "Enter both old and new prefixes.")
            return
        _remap_paths(self.local_manifest, old, new)
        self.result = True
        self.destroy()

    def _skip(self):
        self.result = True
        self.destroy()

    def _cancel(self):
        self.result = False
        self.destroy()


def _remap_paths(local_manifest: dict, old: str, new: str):
    def swap(p: str) -> str:
        if p.lower().startswith(old.lower()):
            return new + p[len(old):]
        return p
    for date_entry in local_manifest.get("dates", {}).values():
        for m in date_entry.get("missions", []):
            m["path"] = swap(m["path"])
    bs = local_manifest.get("base_station")
    if bs:
        bs["source_folder"] = swap(bs["source_folder"])
        for f in bs.get("files", []):
            f["path"] = swap(f["path"])
    local_manifest["source_roots"] = [swap(r) for r in local_manifest.get("source_roots", [])]


# --- entrypoint --------------------------------------------------------------


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
