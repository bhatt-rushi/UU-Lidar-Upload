"""
LiDAR Azure Upload Tool
=======================

Windows GUI for drone pilots to upload LiDAR data to Azure Blob Storage
(wpbstorageaccount / lidardata) via a portable azcopy.exe that must live in
the same directory as this script.

Layout on blob:
    lidardata/{client}/{program}/{feeder}/
        BASE_STATION/{collection_date}/
            manifest.json
            <flat .dat files>
        SENSOR_DATA/{collection_date}/
            manifest.json
            {mission_folder}.zip
        CLIENT_DELIEVERABLES/

Manifest schema (identical on cloud AND local) -- one file per
(kind, collection_date) pair. Downloading the cloud manifest is a valid
local resume handle; no format translation is required.

Robustness:
 - Single-item-at-a-time upload (max the pipe on bad networks).
 - Background zipper prepares the next mission while one uploads.
 - Per-mission md5 + azcopy --put-md5 + REST HEAD verify against blob.
 - Cloud manifest is the source of truth; re-uploaded after every item.
 - Drive letters can be remapped on resume via a single "old prefix ->
   new prefix" swap.

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
SENSOR_CHOICES = [("L3", True), ("TV540", False), ("TVGO", False)]

DATA_TYPE_RAW = "RAW_SENSOR"
DATA_TYPE_PROCESSED = "PROCESSED_SENSOR"

DIR_BASE_STATION = "BASE_STATION"
DIR_SENSOR_DATA = "SENSOR_DATA"
DIR_CLIENT_DELIVERABLES = "CLIENT_DELIEVERABLES"  # spelled per spec

KIND_SENSOR = "sensor"
KIND_BASE = "base_station"

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
    "local_retries": 3,          # X: per-item retries within a single session
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

MANIFEST_VERSION = 2


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


def utcnow() -> str:
    return dt.datetime.utcnow().isoformat() + "Z"


# --- blob URL helpers --------------------------------------------------------


def blob_url(*path_parts: str) -> str:
    """Join path parts to the container base, appending the SAS token."""
    quoted = "/".join(urllib.parse.quote(p, safe="") for p in path_parts if p)
    return f"{BLOB_URL_BASE}/{quoted}{SAS_TOKEN}"


def blob_url_for_path(blob_path: str) -> str:
    """Blob URL for a slash-joined path, appending SAS."""
    parts = [p for p in blob_path.split("/") if p]
    return blob_url(*parts)


def feeder_prefix(client: str, program: str, feeder: str) -> str:
    return f"{client}/{program}/{feeder}"


def manifest_blob_path_for(kind: str, client: str, program: str,
                           feeder: str, collection_date: str) -> str:
    subdir = DIR_SENSOR_DATA if kind == KIND_SENSOR else DIR_BASE_STATION
    return f"{client}/{program}/{feeder}/{subdir}/{collection_date}/manifest.json"


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
    return blob_head(url).get("content-md5")


# --- validators --------------------------------------------------------------


def scan_missions(source_root: Path, expected_feeder: str) -> tuple[list[dict], list[dict]]:
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
        if m["feeder"] != expected_feeder:
            invalid.append({
                "name": entry.name,
                "reason": f"feeder in folder ({m['feeder']}) != entered feeder ({expected_feeder})",
            })
            continue
        file_count = sum(1 for _ in entry.rglob("*") if _.is_file())
        valid.append({
            "name": entry.name,
            "path": str(entry),
            "date": date,
            "seq": m["seq"],
            "feeder": m["feeder"],
            "file_count": file_count,
        })
    return valid, invalid


def detect_data_type(source_root: Path) -> str | None:
    has_missions = False
    has_processed = False
    for entry in source_root.iterdir():
        if entry.is_dir() and MISSION_REGEX.match(entry.name):
            has_missions = True
        elif entry.is_file() and entry.suffix.lower() in PROCESSED_EXTS:
            has_processed = True
    if has_missions and has_processed:
        return None
    if has_missions:
        return DATA_TYPE_RAW
    if has_processed:
        return DATA_TYPE_PROCESSED
    return None


def scan_base_station(folder: Path) -> tuple[list[Path], str | None, list[str]]:
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
#
# ONE unified schema, ONE file per (kind, collection_date). The cloud file at
# manifest_blob_path is byte-identical to the local file. To resume purely
# from the cloud, download the manifest and pick it in "Resume".


def new_manifest(kind: str,
                 client: str, program: str, feeder: str,
                 sensor: str | None, data_type: str | None,
                 collection_date: str,
                 system_name: str,
                 pilot_name: str,
                 source_roots: list[str]) -> dict:
    now = utcnow()
    return {
        "version": MANIFEST_VERSION,
        "kind": kind,
        "session_id": str(uuid.uuid4()),
        "client": client,
        "program": program,
        "feeder": feeder,
        "sensor": sensor,
        "data_type": data_type,
        "collection_date": collection_date,
        "system_name": system_name,
        "pilot_name": pilot_name,
        "source_roots": list(source_roots),
        "manifest_blob_path": manifest_blob_path_for(kind, client, program, feeder, collection_date),
        "created_utc": now,
        "updated_utc": now,
        # items: name -> {status, attempts, ...} (see mint_* below)
        "items": {},
        "summary": {"total": 0, "verified": 0},
    }


def mint_sensor_item(source_path: str, file_count: int, system_name: str) -> dict:
    return {
        "kind": KIND_SENSOR,
        "original_path": source_path,
        "file_count": file_count,
        "system_name": system_name,
        "disk_name": get_disk_name(Path(source_path)),
        "status": STATUS_PENDING,
        "attempts": 0,
        "zip_md5_b64": None,
        "zip_size_bytes": None,
        "uploaded_utc": None,
        "last_error": None,
    }


def mint_base_item(source_path: str, system_name: str) -> dict:
    return {
        "kind": KIND_BASE,
        "original_path": source_path,
        "system_name": system_name,
        "disk_name": get_disk_name(Path(source_path)),
        "status": STATUS_PENDING,
        "attempts": 0,
        "md5_b64": None,
        "size_bytes": None,
        "uploaded_utc": None,
        "last_error": None,
    }


def _finalize_summary(m: dict) -> None:
    m["summary"] = {
        "total": len(m["items"]),
        "verified": sum(1 for it in m["items"].values() if it.get("status") == STATUS_VERIFIED),
    }
    m["updated_utc"] = utcnow()


def save_cloud_manifest(m: dict) -> None:
    _finalize_summary(m)
    data = json.dumps(m, indent=2, sort_keys=True).encode("utf-8")
    blob_put_bytes(blob_url_for_path(m["manifest_blob_path"]), data, content_type="application/json")


def load_cloud_manifest_at(blob_path: str) -> dict | None:
    data = blob_get_bytes(blob_url_for_path(blob_path))
    return json.loads(data.decode("utf-8")) if data else None


def save_local_manifest(m: dict, path: Path) -> None:
    _finalize_summary(m)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(m, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def load_local_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def default_local_path(scratch: Path, feeder: str, kind: str, collection_date: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    subdir = "sensor" if kind == KIND_SENSOR else "base"
    return scratch / f"manifest_{feeder}_{subdir}_{collection_date}_{stamp}.json"


def merge_cloud_into(local: dict, cloud: dict) -> dict:
    """Update local's item statuses from cloud (cloud wins on status/attempts).

    Item paths on local are authoritative (they reflect the current drive
    letter); we do not overwrite ``original_path`` from cloud.
    """
    for name, litem in local["items"].items():
        citem = cloud.get("items", {}).get(name)
        if not citem:
            continue
        for k in ("status", "zip_md5_b64", "zip_size_bytes",
                  "md5_b64", "size_bytes", "uploaded_utc", "last_error"):
            if k in citem and citem[k] is not None:
                litem[k] = citem[k]
    # any items that exist ONLY in cloud (e.g. this local was rebuilt from a
    # partial cloud manifest) get added
    for name, citem in cloud.get("items", {}).items():
        if name not in local["items"]:
            local["items"][name] = citem
    return local


# --- upload session ----------------------------------------------------------


class UploadSession:
    """Runs a list of manifests (one per (kind, date)) sequentially.

    Each manifest is written to disk at its ``local_path`` after every item,
    and pushed to blob at ``manifest_blob_path`` after every item, so cloud
    and local stay byte-identical.
    """

    def __init__(self,
                 manifests: list[tuple[dict, Path]],
                 advanced: dict,
                 log_cb, progress_cb, done_cb):
        self.manifests = manifests
        self.advanced = advanced
        self.log_cb = log_cb
        self.progress_cb = progress_cb
        self.done_cb = done_cb
        self._stop = threading.Event()
        self._zip_queue: queue.Queue = queue.Queue(maxsize=1)
        self._thread: threading.Thread | None = None
        # rolling upload stats: list of (bytes, duration_seconds) for successful
        # verified uploads. Speed is measured only on actual azcopy upload time
        # (not including zip time, backoff sleeps, or manifest PUTs) so it
        # reflects real network throughput.
        self._recent_uploads: list[tuple[int, float]] = []
        self._recent_window = 8

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ---- lifecycle ----

    def _log(self, msg: str):
        try:
            self.log_cb(msg)
        except Exception:
            pass

    def _record_upload(self, size_bytes: int, duration_s: float):
        if size_bytes <= 0 or duration_s <= 0:
            return
        self._recent_uploads.append((size_bytes, duration_s))
        if len(self._recent_uploads) > self._recent_window:
            self._recent_uploads.pop(0)

    def _progress(self, current: str = ""):
        try:
            total = 0
            done = 0
            failed = 0
            bytes_uploaded = 0
            remaining_bytes = 0
            known_sizes: list[int] = []
            for m, _ in self.manifests:
                for it in m["items"].values():
                    total += 1
                    size = it.get("zip_size_bytes") or it.get("size_bytes") or 0
                    status = it.get("status")
                    if status == STATUS_VERIFIED:
                        done += 1
                        bytes_uploaded += size
                        known_sizes.append(size)
                    else:
                        if status == STATUS_FAILED:
                            failed += 1
                        if size:
                            remaining_bytes += size
            # fill in unknown sizes with the mean of what we've seen
            if known_sizes:
                mean = sum(known_sizes) / len(known_sizes)
                unknown_count = sum(
                    1 for m, _ in self.manifests
                    for it in m["items"].values()
                    if it.get("status") != STATUS_VERIFIED
                    and not (it.get("zip_size_bytes") or it.get("size_bytes"))
                )
                remaining_bytes += int(mean * unknown_count)
            # speed from recent uploads
            if self._recent_uploads:
                total_bytes = sum(b for b, _ in self._recent_uploads)
                total_time = sum(t for _, t in self._recent_uploads)
                speed_bps = total_bytes / total_time if total_time > 0 else 0.0
            else:
                speed_bps = 0.0
            eta_s = (remaining_bytes / speed_bps) if speed_bps > 0 and remaining_bytes > 0 else None
            stats = {
                "done": done,
                "total": total,
                "failed": failed,
                "remaining": total - done - failed,
                "current": current,
                "bytes_uploaded": bytes_uploaded,
                "remaining_bytes": remaining_bytes,
                "speed_bps": speed_bps,
                "eta_seconds": eta_s,
            }
            self.progress_cb(stats)
        except Exception:
            pass

    def _run(self):
        try:
            for m, path in self.manifests:
                self._prepare(m, path)
            self._progress()

            # SENSOR manifests get zipper+uploader; BASE do direct upload.
            sensor_manifests = [(m, p) for m, p in self.manifests if m["kind"] == KIND_SENSOR]
            base_manifests = [(m, p) for m, p in self.manifests if m["kind"] == KIND_BASE]

            if sensor_manifests:
                zipper = threading.Thread(target=self._zip_loop,
                                          args=(sensor_manifests,), daemon=True)
                zipper.start()
                self._sensor_upload_loop(sensor_manifests)
                zipper.join(timeout=5)

            for m, p in base_manifests:
                if self._stop.is_set():
                    break
                self._base_upload(m, p)

            self.done_cb(True, "All uploads finished.")
        except Exception as e:
            self._log(f"FATAL: {e}\n{traceback.format_exc()}")
            self.done_cb(False, str(e))

    # ---- prepare (merge with cloud) ----

    def _prepare(self, local: dict, path: Path):
        blob_path = local["manifest_blob_path"]
        self._log(f"Fetching cloud manifest: {blob_path}")
        cloud = load_cloud_manifest_at(blob_path)
        if cloud:
            self._log(f"  found existing cloud manifest, merging state")
            merge_cloud_into(local, cloud)
        else:
            self._log("  no existing cloud manifest, creating fresh")

        # Any item that was mid-flight or failed previously -> reset to pending
        # so this session gets a fresh X-attempt budget for it. There's no
        # global cap, so items are retried indefinitely across sessions until
        # they verify or the pilot removes them from the manifest.
        for it in local["items"].values():
            if it.get("status") in (STATUS_ZIPPING, STATUS_UPLOADING,
                                     STATUS_UPLOADED, STATUS_FAILED):
                it["attempts"] = 0
                it["status"] = STATUS_PENDING

        save_cloud_manifest(local)
        save_local_manifest(local, path)

    # ---- sensor pipeline ----

    def _scratch_dir(self) -> Path:
        d = self.advanced.get("zip_scratch_dir") or ""
        base = Path(d) if d else Path(tempfile.gettempdir()) / "lidar_upload_scratch"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _zip_loop(self, sensor_manifests: list[tuple[dict, Path]]):
        try:
            for m, path in sensor_manifests:
                for name, item in m["items"].items():
                    if self._stop.is_set():
                        return
                    if item["status"] == STATUS_VERIFIED:
                        continue
                    src = Path(item["original_path"])
                    if not src.exists():
                        self._log(f"[zip] {name}: source missing: {src}")
                        item["status"] = STATUS_FAILED
                        item["last_error"] = f"source path missing: {src}"
                        continue
                    zip_path = self._scratch_dir() / f"{name}.zip"
                    self._log(f"[zip] {name}: zipping -> {zip_path}")
                    item["status"] = STATUS_ZIPPING
                    try:
                        _make_zip(src, zip_path)
                    except Exception as e:
                        self._log(f"[zip] {name}: FAILED {e}")
                        item["status"] = STATUS_FAILED
                        item["last_error"] = f"zip failed: {e}"
                        continue
                    _hex, b64, size = md5_file(zip_path)
                    item["zip_md5_b64"] = b64
                    item["zip_size_bytes"] = size
                    item["status"] = STATUS_ZIPPED
                    self._log(f"[zip] {name}: ready ({size / 1e6:.1f} MB, md5={b64})")
                    self._zip_queue.put((m, path, name, zip_path))
            self._zip_queue.put(None)
        except Exception as e:
            self._log(f"[zip] fatal: {e}")
            self._zip_queue.put(None)

    def _sensor_upload_loop(self, _sensor_manifests):
        while not self._stop.is_set():
            item = self._zip_queue.get()
            if item is None:
                return
            m, path, name, zip_path = item
            self._upload_sensor_item(m, path, name, zip_path)
            try:
                zip_path.unlink(missing_ok=True)
            except Exception:
                pass

    def _upload_sensor_item(self, m: dict, path: Path, name: str, zip_path: Path):
        item = m["items"][name]
        dest = blob_url(m["client"], m["program"], m["feeder"],
                        DIR_SENSOR_DATA, m["collection_date"], f"{name}.zip")
        head_url = dest  # HEAD with SAS
        timeout = self.advanced["timeout_seconds"]
        for attempt in range(1, self.advanced["local_retries"] + 1):
            if self._stop.is_set():
                return
            item["status"] = STATUS_UPLOADING
            item["attempts"] = attempt
            self._log(f"[upload] {name}: attempt {attempt}/{self.advanced['local_retries']}")
            up_start = time.monotonic()
            try:
                azcopy_upload_file(zip_path, dest, timeout=timeout, put_md5=True)
            except Exception as e:
                item["last_error"] = f"upload attempt {attempt}: {e}"
                self._log(f"[upload] {name}: {e}")
                self._sleep_backoff(attempt)
                continue
            item["status"] = STATUS_UPLOADED
            try:
                remote_md5 = blob_md5_b64(head_url)
            except Exception as e:
                item["last_error"] = f"HEAD failed: {e}"
                self._log(f"[verify] {name}: HEAD failed: {e}")
                self._sleep_backoff(attempt)
                continue
            if remote_md5 and remote_md5 == item["zip_md5_b64"]:
                item["status"] = STATUS_VERIFIED
                item["uploaded_utc"] = utcnow()
                item["last_error"] = None
                self._record_upload(item.get("zip_size_bytes") or 0,
                                    time.monotonic() - up_start)
                self._log(f"[verify] {name}: OK ({remote_md5})")
                self._save_both(m, path)
                self._progress(name)
                return
            item["last_error"] = f"md5 mismatch: local={item['zip_md5_b64']} remote={remote_md5}"
            self._log(f"[verify] {name}: MISMATCH; retrying")
            self._sleep_backoff(attempt)
        item["status"] = STATUS_FAILED
        self._save_both(m, path)
        self._log(f"[upload] {name}: exhausted {self.advanced['local_retries']} local retries; will retry on next session")

    # ---- base station ----

    def _base_upload(self, m: dict, path: Path):
        self._log(f"[base] uploading {len(m['items'])} files for {m['collection_date']}")
        timeout = self.advanced["timeout_seconds"]
        for name, item in m["items"].items():
            if self._stop.is_set():
                return
            if item["status"] == STATUS_VERIFIED:
                continue
            src = Path(item["original_path"])
            if not src.exists():
                self._log(f"[base] {name}: missing source: {src}")
                item["status"] = STATUS_FAILED
                item["last_error"] = f"source missing: {src}"
                continue
            _hex, b64, size = md5_file(src)
            item["md5_b64"] = b64
            item["size_bytes"] = size
            dest = blob_url(m["client"], m["program"], m["feeder"],
                            DIR_BASE_STATION, m["collection_date"], name)
            for attempt in range(1, self.advanced["local_retries"] + 1):
                if self._stop.is_set():
                    return
                item["status"] = STATUS_UPLOADING
                item["attempts"] = attempt
                self._log(f"[base] {name}: attempt {attempt}/{self.advanced['local_retries']}")
                up_start = time.monotonic()
                try:
                    azcopy_upload_file(src, dest, timeout=timeout, put_md5=True)
                except Exception as e:
                    item["last_error"] = f"upload attempt {attempt}: {e}"
                    self._log(f"[base] {name}: {e}")
                    self._sleep_backoff(attempt)
                    continue
                item["status"] = STATUS_UPLOADED
                try:
                    remote_md5 = blob_md5_b64(dest)
                except Exception as e:
                    item["last_error"] = f"HEAD failed: {e}"
                    self._sleep_backoff(attempt)
                    continue
                if remote_md5 and remote_md5 == item["md5_b64"]:
                    item["status"] = STATUS_VERIFIED
                    item["uploaded_utc"] = utcnow()
                    item["last_error"] = None
                    self._record_upload(item.get("size_bytes") or 0,
                                        time.monotonic() - up_start)
                    self._log(f"[base] {name}: verified")
                    self._save_both(m, path)
                    self._progress(name)
                    break
                item["last_error"] = "md5 mismatch"
                self._sleep_backoff(attempt)
            else:
                item["status"] = STATUS_FAILED
                self._save_both(m, path)

    # ---- helpers ----

    def _sleep_backoff(self, attempt: int):
        delay = min(60, 2 ** attempt)
        for _ in range(delay):
            if self._stop.is_set():
                return
            time.sleep(1)

    def _save_both(self, m: dict, path: Path):
        try:
            save_cloud_manifest(m)
        except Exception as e:
            self._log(f"FATAL: manifest upload failed: {e}")
            self._stop.set()
            raise
        save_local_manifest(m, path)


def _fmt_bytes(n: int) -> str:
    if n <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    f = float(n)
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.1f} {units[i]}"


def _fmt_rate(bps: float) -> str:
    if not bps or bps <= 0:
        return "--"
    # Show as B/s or KB/s or MB/s
    return _fmt_bytes(int(bps)) + "/s"


def _fmt_eta(secs: float | None) -> str:
    if secs is None or secs <= 0:
        return "--"
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _make_zip(src_dir: Path, out_path: Path) -> None:
    """ZIP_STORED (no compression) -- LiDAR files are already binary/compact
    and we care about throughput on a bad link, not disk savings."""
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
        blob_put_bytes(blob_url_for_path(path), text.encode("utf-8"), content_type="text/plain")


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
        ttk.Button(frame, text="Resume from manifest...", width=30,
                   command=self._start_resume).pack(pady=6)
        ttk.Button(frame, text="Advanced options...", width=30,
                   command=self._open_advanced).pack(pady=6)
        ttk.Button(frame, text="Exit", width=30, command=self.destroy).pack(pady=6)

    def _open_advanced(self):
        AdvancedDialog(self, self.advanced)

    def _start_new_wizard(self):
        NewUploadWizard(self, on_ready=self._start_session)

    def _start_session(self, manifests: list[tuple[dict, Path]]):
        self._build_progress()
        session = UploadSession(
            manifests=manifests,
            advanced=dict(self.advanced),
            log_cb=self._log,
            progress_cb=self._progress,
            done_cb=self._done,
        )
        self.session = session
        session.start()

    def _start_resume(self):
        path = filedialog.askopenfilename(
            title="Select manifest.json (local file or one you downloaded from blob)",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            m = load_local_manifest(Path(path))
        except Exception as e:
            messagebox.showerror("Load failed", str(e))
            return
        if m.get("version") != MANIFEST_VERSION:
            messagebox.showerror("Bad manifest", f"Unsupported manifest version: {m.get('version')}")
            return
        remapped = DriveRemapDialog(self, m).result
        if remapped is False:
            return
        save_local_manifest(m, Path(path))
        self._start_session([(m, Path(path))])

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

        stats_frame = ttk.Frame(frame)
        stats_frame.pack(fill="x", pady=2)
        self.speed_var = tk.StringVar(value="Speed: --")
        self.eta_var = tk.StringVar(value="ETA: --")
        self.failed_var = tk.StringVar(value="Failed: 0")
        self.bytes_var = tk.StringVar(value="Uploaded: 0 B")
        ttk.Label(stats_frame, textvariable=self.speed_var, width=22).pack(side="left")
        ttk.Label(stats_frame, textvariable=self.eta_var, width=22).pack(side="left")
        ttk.Label(stats_frame, textvariable=self.failed_var, width=16).pack(side="left")
        ttk.Label(stats_frame, textvariable=self.bytes_var, width=26).pack(side="left")

        self.current_item = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.current_item).pack(anchor="w")

        log_frame = ttk.LabelFrame(frame, text="Log")
        log_frame.pack(fill="both", expand=True, pady=6)
        self.log_widget = tk.Text(log_frame, height=20, wrap="word")
        self.log_widget.pack(fill="both", expand=True)

        btns = ttk.Frame(frame)
        btns.pack(fill="x")
        ttk.Button(btns, text="Stop", command=self._stop_session).pack(side="right")

    def _log(self, msg: str):
        self.after(0, lambda: self._append_log(msg))

    def _append_log(self, msg: str):
        self.log_widget.insert("end", f"{dt.datetime.now().strftime('%H:%M:%S')} {msg}\n")
        self.log_widget.see("end")

    def _progress(self, stats: dict):
        def apply():
            total = stats["total"]
            done = stats["done"]
            failed = stats["failed"]
            remaining = stats["remaining"]
            self.progress_bar["maximum"] = max(total, 1)
            self.progress_bar["value"] = done
            self.progress_var.set(f"{done} verified / {remaining} remaining / {total} total")
            self.failed_var.set(f"Failed: {failed}")
            self.speed_var.set(f"Speed: {_fmt_rate(stats['speed_bps'])}")
            self.eta_var.set(f"ETA: {_fmt_eta(stats['eta_seconds'])}")
            self.bytes_var.set(f"Uploaded: {_fmt_bytes(stats['bytes_uploaded'])}")
            if stats["current"]:
                self.current_item.set(f"Last: {stats['current']}")
        self.after(0, apply)

    def _done(self, ok: bool, msg: str):
        def apply():
            self._append_log(("DONE: " if ok else "FAILED: ") + msg)
            messagebox.showinfo("Finished" if ok else "Stopped", msg)
        self.after(0, apply)

    def _stop_session(self):
        if self.session:
            self.session.stop()
            self._append_log("Stop requested; will halt after current item.")


# --- advanced options dialog -------------------------------------------------


class AdvancedDialog(tk.Toplevel):
    def __init__(self, parent: App, advanced: dict):
        super().__init__(parent)
        self.title("Advanced options")
        self.advanced = advanced
        self.resizable(False, False)

        vars_ = {}
        rows = [
            ("Local retries per item (X)", "local_retries", int),
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

        # ensure the wizard opens ON TOP of the main window rather than behind it
        self.transient(parent)
        self.lift()
        self.attributes("-topmost", True)
        self.after(300, lambda: self.attributes("-topmost", False))
        self.focus_force()
        self.grab_set()

        self.client_var = tk.StringVar(value=VALID_CLIENTS[0])
        self.program_var = tk.StringVar(value=VALID_PROGRAMS[0])
        self.pilot_var = tk.StringVar()
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
        ttk.Label(self, text="Pilot name").grid(row=row, column=0, sticky="w", **pad)
        ttk.Entry(self, textvariable=self.pilot_var).grid(row=row, column=1, sticky="ew", **pad)
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
        pilot = self.pilot_var.get().strip()
        if not pilot:
            messagebox.showerror("Missing pilot", "Enter the pilot's name before scanning.")
            return
        feeder = self.feeder_var.get().strip().upper()
        confirm = self.feeder_confirm_var.get().strip().upper()
        if not feeder or feeder != confirm:
            messagebox.showerror("Feeder mismatch", "Feeder name and confirmation must match.")
            return
        if not FEEDER_WARN_REGEX.match(feeder):
            self._report(f"WARNING: feeder '{feeder}' does not match typical format LLLDDD.")

        sensor_by_date: dict[str, list[dict]] = {}
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
                    sensor_by_date.setdefault(m["date"], []).append(m)
                self._report(f"Valid missions: {len(valid)} across {len(sensor_by_date)} date(s).")
                for date, items in sorted(sensor_by_date.items()):
                    self._report(f"  {date}: {len(items)} missions")
            else:
                messagebox.showinfo(
                    "PROCESSED_SENSOR",
                    "PROCESSED_SENSOR support is experimental. Files will upload flat under "
                    "SENSOR_DATA/{collection_date}/. Today's date is used as the collection date.",
                )
                files = [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in PROCESSED_EXTS]
                today = dt.date.today().isoformat()
                sensor_by_date[today] = [{
                    "name": p.name, "path": str(p), "date": today,
                    "file_count": 1, "processed": True,
                } for p in files]
                self._report(f"Processed files found: {len(files)}")

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

        if not sensor_by_date and not base_info:
            messagebox.showerror("Nothing to do", "Enable at least one of SENSOR_DATA or BASE_STATION.")
            return

        self.scanned = {
            "feeder": feeder,
            "data_type": data_type,
            "sensor_by_date": sensor_by_date,
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
        system_name = get_system_name()
        pilot_name = self.pilot_var.get().strip()

        if adv.get("create_readmes", True):
            try:
                create_feeder_readmes(client, program, feeder)
                self._report("Created feeder README placeholders.")
            except Exception as e:
                self._report(f"WARNING: failed to write READMEs: {e}")

        scratch = Path(adv.get("zip_scratch_dir") or tempfile.gettempdir()) / "lidar_upload_scratch"
        scratch.mkdir(parents=True, exist_ok=True)

        manifests: list[tuple[dict, Path]] = []

        # Build one SENSOR manifest per date.
        for date, items in sorted(s["sensor_by_date"].items()):
            source_roots = sorted({str(Path(it["path"]).parent) for it in items})
            m = new_manifest(
                kind=KIND_SENSOR,
                client=client, program=program, feeder=feeder,
                sensor=sensor, data_type=data_type,
                collection_date=date, system_name=system_name,
                pilot_name=pilot_name,
                source_roots=source_roots,
            )
            for it in items:
                m["items"][it["name"]] = mint_sensor_item(
                    source_path=it["path"],
                    file_count=it.get("file_count", 0),
                    system_name=system_name,
                )
            path = default_local_path(scratch, feeder, KIND_SENSOR, date)
            save_local_manifest(m, path)
            manifests.append((m, path))
            self._report(f"Wrote local manifest: {path}")

        # Build ONE BASE_STATION manifest if requested.
        if s["base_info"]:
            bi = s["base_info"]
            source_roots = [str(Path(bi["folder"]))]
            m = new_manifest(
                kind=KIND_BASE,
                client=client, program=program, feeder=feeder,
                sensor=None, data_type=None,
                collection_date=bi["date"], system_name=system_name,
                pilot_name=pilot_name,
                source_roots=source_roots,
            )
            for p in bi["files"]:
                pp = Path(p)
                m["items"][pp.name] = mint_base_item(str(pp), system_name)
            path = default_local_path(scratch, feeder, KIND_BASE, bi["date"])
            save_local_manifest(m, path)
            manifests.append((m, path))
            self._report(f"Wrote local manifest: {path}")

        messagebox.showinfo(
            "Manifests saved",
            "Local manifests saved to the scratch dir. Keep them! Any one of them "
            "is a valid resume handle. The cloud copy at each manifest_blob_path is "
            "byte-identical to the local file and is also a valid resume handle.",
        )
        self.destroy()
        self.on_ready(manifests)


# --- drive remap dialog ------------------------------------------------------


class DriveRemapDialog(tk.Toplevel):
    def __init__(self, parent: App, manifest: dict):
        super().__init__(parent)
        self.title("Resume: verify source paths")
        self.manifest = manifest
        self.result: bool | None = None

        roots = sorted(set(manifest.get("source_roots", [])))

        ttk.Label(self, text=f"Manifest: {manifest['kind']} / {manifest['feeder']} / {manifest['collection_date']}",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=10, pady=6)
        ttk.Label(self, text="Source paths referenced by this manifest:").pack(anchor="w", padx=10)
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
        _remap_paths(self.manifest, old, new)
        self.result = True
        self.destroy()

    def _skip(self):
        self.result = True
        self.destroy()

    def _cancel(self):
        self.result = False
        self.destroy()


def _remap_paths(m: dict, old: str, new: str):
    def swap(p: str) -> str:
        if p.lower().startswith(old.lower()):
            return new + p[len(old):]
        return p
    m["source_roots"] = [swap(r) for r in m.get("source_roots", [])]
    for item in m["items"].values():
        item["original_path"] = swap(item["original_path"])


# --- entrypoint --------------------------------------------------------------


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
