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
# Suggested pilot names. Pilots can also type in a custom name if they don't
# find themselves here -- edit this list to keep it current.
VALID_PILOTS = [
    "Zachariah Ellefson",
    "Anthony Pazzulla",
    "Timothy Powell",
    "Nathaniel Bailey",
    "Nick Mims",
    "Christian McAlister",
]
# Known feeder names. The wizard autocompletes against this list, but a pilot
# can type a custom feeder for troubleshooting / one-offs (warning is emitted
# if it doesn't match the LLLDDD pattern). Edit this list to keep it current.
VALID_FEEDERS = [
    "BRT060", "CNT061", "CNT071", "CUM081", "CUM082", "FLS062", "FLS074",
    "GRT311", "LAW312", "LAW321", "LAW322", "LCO061", "LCO062", "LCO063",
    "LCO071", "LCO072", "LCO073", "LOU061", "LOU062", "LOU071", "LOU072",
    "LUK021", "MHA061", "MHA062", "MHA063", "MHA072", "MHA073", "MRC021",
    "MRC022", "PSI020", "RCL031", "RTL021", "RTL022", "SAL310", "SCF031",
    "SCF032", "SCF033", "SOR060", "SOR061", "SOR062", "SOR063", "SOR070",
    "SOR071", "SOR072", "SOR073", "SOR080", "SOR081", "SOR082", "SOS070",
    "SOS071", "SOS072", "TAD081", "WSF062", "WSF065", "WSF073", "WSF074",
    "YLR081", "YLR082",
]
SENSOR_CHOICES = [("L3", True), ("TV540", True), ("TVGO", False)]

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
# TV540 folder naming: @@YYYY-MM-DD-HHMMSS. Note the feeder is NOT in the
# folder name for this sensor -- we have to trust the pilot's typed feeder
# for filing under the right cloud path.
TV540_MISSION_REGEX = re.compile(
    r"^@@(?P<Y>\d{4})-(?P<M>\d{2})-(?P<D>\d{2})-(?P<h>\d{2})(?P<m>\d{2})(?P<s>\d{2})$"
)
BASE_STATION_DAT_REGEX = re.compile(
    r"^DRTK\d+_\d+_(?P<Y>\d{4})(?P<M>\d{2})(?P<D>\d{2})\d{6}_.+\.dat$",
    re.IGNORECASE,
)
# RINEX observation-file extension. Accept both the short-name form
# (.YYo -- two-digit year + 'o', e.g. .26o, .25o) and the RINEX 3 long-name
# form (.rnx). Case-insensitive per RINEX convention.
RINEX_OBS_EXT_REGEX = re.compile(r"\.(?:\d{2}[oO]|rnx)$", re.IGNORECASE)
# Formats seen on the "PGM / RUN BY / DATE" line in RINEX 3 headers, tried
# in order:
#   1. YYYYMMDD HHMMSS <ZONE>   -- RINEX 3.02 / 3.04 spec
#   2. DD-MMM-YY HH:MM          -- some vendors, month as 3-letter name
#   3. DD-MM-YY HH:MM           -- CHC Navigation
RINEX_HEADER_DATE_YMD = re.compile(r"(\d{8})\s")
RINEX_HEADER_DATE_DMMMYY = re.compile(r"(\d{2})-([A-Za-z]{3})-(\d{2})")
RINEX_HEADER_DATE_DMY = re.compile(r"(\d{2})-(\d{2})-(\d{2})")
_RINEX_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
                 "jul", "aug", "sep", "oct", "nov", "dec"]
BASE_STATION_INDEX_NAMES = {"latest_index", "latest_index.txt"}

PROCESSED_EXTS = {".las", ".laz"}

DEFAULT_ADVANCED = {
    "local_retries": 50,          # X: per-item retries within a single session
    "timeout_seconds": 3600,      # T: azcopy per-call timeout
    "create_readmes": CREATE_DIR_READMES_DEFAULT,
    "zip_scratch_dir": "",        # "" -> system temp
    "retry_failed_forever": True, # after first pass, keep retrying items
                                  # left in status=failed forever, sleeping
                                  # retry_failed_interval_seconds between
                                  # passes
    "retry_failed_interval_seconds": 300,
    "zip_queue_depth": 5,         # max number of pre-zipped missions that
                                  # can sit in the queue waiting for the
                                  # uploader; higher = uploader never
                                  # starves on a fast link, but each ready
                                  # zip takes disk space in the scratch dir
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
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


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


def _run_azcopy_streaming(args: list[str], timeout: int,
                          on_progress=None) -> None:
    """Run azcopy with JSON-line stdout so we can surface live progress.

    ``on_progress(bytes_over_wire)`` is called each time azcopy emits a
    Progress message. Its argument is the cumulative bytes-over-wire count
    for the whole azcopy job (which, for our single-file copies, equals the
    bytes uploaded so far for this transfer).
    """
    exe = azcopy_path()
    if not exe.exists():
        raise AzcopyError(
            f"azcopy binary not found at {exe}. Place azcopy.exe next to this script."
        )
    env = os.environ.copy()
    env["AZCOPY_LOG_LEVEL"] = "ERROR"

    creationflags = 0
    if os.name == "nt":
        # avoid a black console window flashing when azcopy is spawned from Tk
        creationflags = 0x08000000  # CREATE_NO_WINDOW

    proc = subprocess.Popen(
        [str(exe)] + args + ["--output-type=json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        creationflags=creationflags,
        bufsize=1,
    )
    stderr_chunks: list[str] = []

    def _drain_stderr():
        try:
            assert proc.stderr is not None
            for line in proc.stderr:
                stderr_chunks.append(line)
        except Exception:
            pass

    threading.Thread(target=_drain_stderr, daemon=True).start()

    deadline = time.monotonic() + timeout
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if time.monotonic() > deadline:
                proc.kill()
                raise AzcopyError(f"azcopy timed out after {timeout}s")
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if on_progress and msg.get("MessageType") == "Progress":
                try:
                    content = json.loads(msg.get("MessageContent", "{}"))
                    bytes_over_wire = int(content.get("BytesOverWire", 0))
                    on_progress(bytes_over_wire)
                except Exception:
                    pass
    finally:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    if proc.returncode != 0:
        err = "".join(stderr_chunks).strip()
        raise AzcopyError(f"azcopy failed (rc={proc.returncode}): {err[:400]}")


def azcopy_upload_file(local: Path, dest_url: str, timeout: int,
                       put_md5: bool = True, on_progress=None) -> None:
    args = ["copy", str(local), dest_url, "--log-level=ERROR"]
    if put_md5:
        args.append("--put-md5")
    _run_azcopy_streaming(args, timeout=timeout, on_progress=on_progress)


def azcopy_download_file(src_url: str, local: Path, timeout: int,
                          on_progress=None) -> None:
    """Same wrapper, download direction. azcopy accepts (source, destination)
    with the source being the SAS-signed blob URL."""
    local.parent.mkdir(parents=True, exist_ok=True)
    args = ["copy", src_url, str(local), "--log-level=ERROR"]
    _run_azcopy_streaming(args, timeout=timeout, on_progress=on_progress)


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


def _container_list_url(prefix: str, marker: str = "") -> str:
    """URL for the Azure Blob List Blobs API restricted to one prefix. The
    SAS token must include list permission (usually sp=...l and srt=c)."""
    parts = (
        f"restype=container&comp=list&delimiter=/&prefix="
        f"{urllib.parse.quote(prefix, safe='/')}"
    )
    if marker:
        parts += f"&marker={urllib.parse.quote(marker)}"
    sas = SAS_TOKEN.lstrip("?")
    if sas:
        parts += "&" + sas
    return f"{BLOB_URL_BASE}?{parts}"


def list_blob_prefixes(prefix: str) -> list[str]:
    """Return the pseudo-directory names one level under ``prefix`` in the
    container. Uses the List Blobs API with delimiter='/' so BlobPrefix
    elements come back for each intermediate 'directory'."""
    import xml.etree.ElementTree as ET

    if prefix and not prefix.endswith("/"):
        prefix = prefix + "/"
    out: list[str] = []
    marker = ""
    while True:
        url = _container_list_url(prefix, marker)
        with urllib.request.urlopen(url, timeout=60) as r:
            xml = r.read().decode("utf-8")
        root = ET.fromstring(xml)
        for bp in root.findall(".//Blobs/BlobPrefix/Name"):
            name = bp.text or ""
            if name.startswith(prefix):
                sub = name[len(prefix):].rstrip("/")
                if sub:
                    out.append(sub)
        marker = (root.findtext("NextMarker") or "").strip()
        if not marker:
            break
    return out


def discover_dates_for_feeder(client: str, program: str,
                               feeder: str) -> dict[str, list[str]]:
    """For a feeder, return {kind -> [YYYY-MM-DD, ...]} discovered under both
    SENSOR_DATA/ and BASE_STATION/. Only directory-shaped date names are
    kept, so noise directories are ignored."""
    date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    result: dict[str, list[str]] = {KIND_SENSOR: [], KIND_BASE: []}
    for kind, subdir in ((KIND_SENSOR, DIR_SENSOR_DATA),
                          (KIND_BASE, DIR_BASE_STATION)):
        try:
            dates = list_blob_prefixes(f"{client}/{program}/{feeder}/{subdir}/")
        except Exception:
            dates = []
        result[kind] = sorted(d for d in dates if date_re.match(d))
    return result


# --- validators --------------------------------------------------------------


def _mission_regex_for(sensor: str) -> re.Pattern:
    if sensor == "TV540":
        return TV540_MISSION_REGEX
    return MISSION_REGEX  # L3 (and default)


def scan_missions(source_root: Path, expected_feeder: str,
                  sensor: str) -> tuple[list[dict], list[dict]]:
    """Sensor-aware mission-folder scan.

    L3:    strict feeder check via the DJI_..._FEEDER filename tail.
    TV540: no feeder in the folder name, so we trust the pilot's typed
           feeder and only validate the @@YYYY-MM-DD-HHMMSS shape. As
           requested, anything else in the source root (loose files,
           differently-named folders) is rejected instead of silently
           ignored.
    """
    regex = _mission_regex_for(sensor)
    check_feeder = sensor == "L3"
    strict_reject_loose = sensor == "TV540"
    valid: list[dict] = []
    invalid: list[dict] = []
    for entry in sorted(source_root.iterdir()):
        if entry.is_file():
            if strict_reject_loose:
                invalid.append({
                    "name": entry.name,
                    "reason": "loose file in source root -- only TV540 mission folders allowed",
                })
            continue
        if not entry.is_dir():
            continue
        m = regex.match(entry.name)
        if not m:
            if sensor == "TV540":
                reason = "does not match @@YYYY-MM-DD-HHMMSS"
            else:
                reason = "does not match DJI_YYYYMMDDHHMM_SEQ_FEEDER"
            invalid.append({"name": entry.name, "reason": reason})
            continue
        try:
            date = dt.date(int(m["Y"]), int(m["M"]), int(m["D"])).isoformat()
        except ValueError:
            invalid.append({"name": entry.name, "reason": "invalid date in folder name"})
            continue
        if check_feeder and m["feeder"] != expected_feeder:
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
            "seq": m["seq"] if "seq" in m.groupdict() else "",
            "feeder": expected_feeder if not check_feeder else m["feeder"],
            "file_count": file_count,
        })
    return valid, invalid


def detect_data_type(source_root: Path, sensor: str) -> str | None:
    regex = _mission_regex_for(sensor)
    has_missions = False
    has_processed = False
    for entry in source_root.iterdir():
        if entry.is_dir() and regex.match(entry.name):
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


def _parse_rinex_header_date(line: str) -> str | None:
    """Try the three date formats we've seen on the RINEX 3 PGM/RUN BY/DATE
    line, in order of specificity."""
    m = RINEX_HEADER_DATE_YMD.search(line)
    if m:
        s = m.group(1)
        try:
            return dt.date(int(s[:4]), int(s[4:6]), int(s[6:8])).isoformat()
        except ValueError:
            pass
    m = RINEX_HEADER_DATE_DMMMYY.search(line)
    if m:
        dd, mmm, yy = m.groups()
        try:
            mm = _RINEX_MONTHS.index(mmm.lower()) + 1
            return dt.date(2000 + int(yy), mm, int(dd)).isoformat()
        except (ValueError, IndexError):
            pass
    m = RINEX_HEADER_DATE_DMY.search(line)
    if m:
        dd, mm, yy = m.groups()
        try:
            return dt.date(2000 + int(yy), int(mm), int(dd)).isoformat()
        except ValueError:
            pass
    return None


def parse_rinex_obs_date(path: Path) -> str | None:
    """Read a RINEX 3 observation file's header and pull the run date off
    the "PGM / RUN BY / DATE" line. The RINEX 3.02/3.04 spec uses
    YYYYMMDD HHMMSS but real receivers (notably CHC Navigation) emit
    DD-MM-YY HH:MM. Returns 'YYYY-MM-DD' or None if we can't find it.
    Reads only the header (up to 60 lines or 'END OF HEADER')."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for _ in range(60):
                line = f.readline()
                if not line:
                    break
                if "PGM / RUN BY / DATE" in line:
                    return _parse_rinex_header_date(line)
                if "END OF HEADER" in line:
                    return None
    except Exception:
        return None
    return None


def scan_base_station(source: Path) -> tuple[list[Path], str | None, list[str]]:
    """Accept .dat (CHC RTK) and RINEX 3 observation files (.YYo / .rnx).
    Both require the pilot to trust the typed feeder since neither format
    encodes it.

    ``source`` may be either a directory of files (iterated) OR a single
    file (treated as a one-element listing) so a pilot with just one
    .dat / .26o can point at it directly. Returns (files, single-
    collection-date, warnings)."""
    files: list[Path] = []
    warnings: list[str] = []
    dates: set[str] = set()
    entries = [source] if source.is_file() else list(source.iterdir())
    for entry in entries:
        if entry.is_dir():
            warnings.append(f"skipping subfolder: {entry.name}")
            continue
        low = entry.name.lower()
        if low in BASE_STATION_INDEX_NAMES:
            # latest_index is a control file that isn't needed after the
            # fact. Silently drop it; only the .dat / RINEX files matter.
            continue
        m = BASE_STATION_DAT_REGEX.match(entry.name)
        if m:
            files.append(entry)
            try:
                dates.add(dt.date(int(m["Y"]), int(m["M"]), int(m["D"])).isoformat())
            except ValueError:
                pass
            continue
        if RINEX_OBS_EXT_REGEX.search(entry.name):
            date = parse_rinex_obs_date(entry)
            if date:
                files.append(entry)
                dates.add(date)
            else:
                warnings.append(
                    f"rejected (RINEX header date not readable): {entry.name}"
                )
            continue
        warnings.append(
            f"rejected (only .dat or RINEX .YYo files allowed): {entry.name}"
        )
    if len(dates) > 1:
        warnings.append(f"multiple collection dates seen: {sorted(dates)}")
    date = sorted(dates)[0] if dates else None
    return files, date, warnings


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


def mint_sensor_item(source_path: str, file_count: int, system_name: str,
                     pilot_name: str, session_id: str) -> dict:
    return {
        "kind": KIND_SENSOR,
        "original_path": source_path,
        "file_count": file_count,
        # Owner identity is stamped at mint time and never overwritten by a
        # merge, so a pilot's session can filter the deletion check to just
        # the items THEY uploaded even after another pilot has appended.
        "system_name": system_name,
        "pilot_name": pilot_name,
        "session_id": session_id,
        "disk_name": get_disk_name(Path(source_path)),
        "status": STATUS_PENDING,
        "attempts": 0,
        "zip_md5_b64": None,
        "zip_size_bytes": None,
        "uploaded_utc": None,
        "last_error": None,
    }


def mint_base_item(source_path: str, system_name: str,
                   pilot_name: str, session_id: str) -> dict:
    return {
        "kind": KIND_BASE,
        "original_path": source_path,
        "system_name": system_name,
        "pilot_name": pilot_name,
        "session_id": session_id,
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
    # Fire-and-forget master-index update so the Upload Tracker page has a
    # single-blob view of every session. Failure here is non-fatal; the
    # tracker's Refresh button rebuilds by re-enumerating.
    def _bg_index():
        try:
            upsert_master_index_entry(m)
        except Exception:
            pass
    threading.Thread(target=_bg_index, daemon=True).start()


def load_cloud_manifest_at(blob_path: str) -> dict | None:
    data = blob_get_bytes(blob_url_for_path(blob_path))
    return json.loads(data.decode("utf-8")) if data else None


# --- master upload index -----------------------------------------------------
#
# A single blob at {client}/{program}/_uploads_index.json enumerates every
# per-date manifest that has been written under that program, with pilot /
# system / progress / timing metadata pulled from the manifest. Session
# writes upsert an entry on every save; the tracker dialog can also fully
# rebuild by re-enumerating every VALID_FEEDERS. Concurrent writes are
# handled with an If-Match / ETag retry loop.


MASTER_INDEX_VERSION = 1


def master_index_blob_path(client: str, program: str) -> str:
    return f"{client}/{program}/_uploads_index.json"


def build_index_entry(m: dict) -> dict:
    items = m.get("items", {}) or {}
    pilots = sorted({it.get("pilot_name") for it in items.values()
                     if it.get("pilot_name")})
    systems = sorted({it.get("system_name") for it in items.values()
                      if it.get("system_name")})
    return {
        "feeder": m["feeder"],
        "kind": m["kind"],
        "sensor": m.get("sensor"),              # None for base_station
        "collection_date": m["collection_date"],
        "manifest_blob_path": m["manifest_blob_path"],
        "pilot_name": m.get("pilot_name"),      # top-level = last writer
        "system_name": m.get("system_name"),
        "session_id": m.get("session_id"),
        "created_utc": m.get("created_utc"),
        "updated_utc": m.get("updated_utc"),
        "total": len(items),
        "verified": sum(1 for it in items.values() if it.get("status") == STATUS_VERIFIED),
        "pilots": pilots,
        "systems": systems,
    }


def _fetch_index_with_etag(client: str, program: str) -> tuple[dict, str]:
    url = blob_url_for_path(master_index_blob_path(client, program))
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            data = r.read()
            etag = r.headers.get("ETag", "")
            return json.loads(data.decode("utf-8")), etag
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"version": MASTER_INDEX_VERSION, "entries": []}, ""
        raise


def _put_index(client: str, program: str, idx: dict, etag: str) -> bool:
    """Return True on success, False on ETag conflict (caller should retry)."""
    idx["version"] = MASTER_INDEX_VERSION
    idx["updated_utc"] = utcnow()
    body = json.dumps(idx, indent=2, sort_keys=True).encode("utf-8")
    url = blob_url_for_path(master_index_blob_path(client, program))
    req = urllib.request.Request(url, data=body, method="PUT")
    req.add_header("x-ms-blob-type", "BlockBlob")
    req.add_header("Content-Type", "application/json")
    req.add_header("Content-Length", str(len(body)))
    if etag:
        req.add_header("If-Match", etag)
    else:
        # First writer for this index: don't clobber if someone else beat us.
        req.add_header("If-None-Match", "*")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status in (200, 201)
    except urllib.error.HTTPError as e:
        if e.code in (409, 412):
            return False
        raise


def upsert_master_index_entry(m: dict) -> None:
    entry = build_index_entry(m)
    for _attempt in range(6):
        idx, etag = _fetch_index_with_etag(m["client"], m["program"])
        entries = idx.get("entries") or []
        prior = next((e for e in entries
                      if e.get("manifest_blob_path") == entry["manifest_blob_path"]),
                     None)
        if prior and prior.get("created_utc"):
            entry["created_utc"] = prior["created_utc"]
        entries = [e for e in entries
                   if e.get("manifest_blob_path") != entry["manifest_blob_path"]]
        entries.append(entry)
        idx["entries"] = entries
        if _put_index(m["client"], m["program"], idx, etag):
            return
    raise RuntimeError("upsert_master_index_entry: retries exhausted")


def load_master_index(client: str, program: str) -> dict:
    idx, _etag = _fetch_index_with_etag(client, program)
    return idx


def rebuild_master_index(client: str, program: str,
                         progress_cb=None) -> dict:
    """Re-enumerate every valid feeder and rewrite the index from scratch."""
    entries = []
    total = len(VALID_FEEDERS)
    for i, feeder in enumerate(VALID_FEEDERS):
        if progress_cb:
            try:
                progress_cb(i, total, feeder)
            except Exception:
                pass
        try:
            dates = discover_dates_for_feeder(client, program, feeder)
        except Exception:
            continue
        for kind, date_list in dates.items():
            for date in date_list:
                path = manifest_blob_path_for(kind, client, program, feeder, date)
                try:
                    m = load_cloud_manifest_at(path)
                except Exception:
                    m = None
                if not m:
                    continue
                entries.append(build_index_entry(m))
    if progress_cb:
        try:
            progress_cb(total, total, "")
        except Exception:
            pass
    idx = {"version": MASTER_INDEX_VERSION,
           "updated_utc": utcnow(), "entries": entries}
    body = json.dumps(idx, indent=2, sort_keys=True).encode("utf-8")
    blob_put_bytes(blob_url_for_path(master_index_blob_path(client, program)),
                   body, content_type="application/json")
    return idx


def save_local_manifest(m: dict, path: Path) -> None:
    _finalize_summary(m)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(m, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def load_local_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def registry_dir() -> Path:
    """Well-known local directory where every manifest this tool has ever
    written lives, so pilots can find them all from one place.

    On Windows: %LOCALAPPDATA%\\UULidarUpload\\manifests\\
    Elsewhere: <script_dir>/manifests/
    """
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
        d = base / "UULidarUpload" / "manifests"
    else:
        d = script_dir() / "manifests"
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_registered_manifests() -> list[Path]:
    return sorted(registry_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)


# --- upload queue ------------------------------------------------------------
#
# A persistent, ordered list of local-manifest paths so pilots can stack up
# multiple days / feeders / sensors, close the app, and pick back up. Lives
# in the same well-known place as the manifest registry so a system reboot
# doesn't lose it.

QUEUE_VERSION = 1


def queue_file_path() -> Path:
    """Where the queue lives on disk. Sibling of the manifest registry."""
    return registry_dir().parent / "queue.json"


class UploadQueue:
    """Ordered list of local manifest paths, persisted to queue.json.

    Deduplicates by path on load and save. Every mutating op saves
    immediately (atomic rename) so a crash mid-edit can't leave a half-
    written file.
    """

    def __init__(self):
        self.path = queue_file_path()
        self._paths: list[str] = []
        self.load()

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            raw = [p for p in (data.get("manifest_paths") or [])
                   if isinstance(p, str)]
        except (FileNotFoundError, json.JSONDecodeError):
            raw = []
        except Exception:
            raw = []
        seen: set[str] = set()
        self._paths = []
        for p in raw:
            if p in seen:
                continue
            seen.add(p)
            self._paths.append(p)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"version": QUEUE_VERSION,
                        "manifest_paths": self._paths},
                       indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def paths(self) -> list[str]:
        return list(self._paths)

    def append(self, mp: str):
        if mp not in self._paths:
            self._paths.append(mp)
            self.save()

    def remove(self, mp: str):
        if mp in self._paths:
            self._paths.remove(mp)
            self.save()

    def move(self, mp: str, delta: int):
        """Move mp up (delta<0) or down (delta>0) within the queue."""
        if mp not in self._paths or delta == 0:
            return
        idx = self._paths.index(mp)
        new_idx = max(0, min(len(self._paths) - 1, idx + delta))
        if new_idx == idx:
            return
        self._paths.pop(idx)
        self._paths.insert(new_idx, mp)
        self.save()

    def clear(self):
        self._paths.clear()
        self.save()


def manifest_availability(m: dict) -> dict:
    """Count how many non-verified items in ``m`` still have their source on
    disk. A manifest with pending > 0 and present == 0 is a queue item we
    can't work on right now (drive disconnected, moved, etc.) and should
    skip. Returns {"pending", "present", "missing", "verified"}."""
    pending = 0
    present = 0
    verified = 0
    for item in m.get("items", {}).values():
        if item.get("status") == STATUS_VERIFIED:
            verified += 1
            continue
        pending += 1
        try:
            if Path(item.get("original_path", "")).exists():
                present += 1
        except Exception:
            pass
    return {"pending": pending, "present": present,
            "missing": pending - present, "verified": verified}


def default_local_path(feeder: str, kind: str, collection_date: str, session_id: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    subdir = "sensor" if kind == KIND_SENSOR else "base"
    sid = (session_id or "")[:8] or "nosid"
    return registry_dir() / f"manifest_{feeder}_{subdir}_{collection_date}_{stamp}_{sid}.json"


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
        self._zip_queue: queue.Queue = queue.Queue(
            maxsize=int(advanced.get("zip_queue_depth", 5) or 5))
        self._thread: threading.Thread | None = None
        # rolling upload stats: list of (bytes, duration_seconds) for successful
        # verified uploads. Speed is measured only on actual azcopy upload time
        # (not including zip time, backoff sleeps, or manifest PUTs) so it
        # reflects real network throughput.
        self._recent_uploads: list[tuple[int, float]] = []
        self._recent_window = 8
        # live in-flight transfer state, updated by the azcopy JSON-stream
        # parser; the UI ticker samples it at 500ms cadence.
        self._current_lock = threading.Lock()
        self._current_transfer: dict = {
            "name": None,
            "size_bytes": 0,
            "bytes_over_wire": 0,
            "start_monotonic": None,
        }
        self._done_flag = threading.Event()

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

    def _start_transfer(self, name: str, size_bytes: int):
        with self._current_lock:
            self._current_transfer = {
                "name": name,
                "size_bytes": size_bytes,
                "bytes_over_wire": 0,
                "start_monotonic": time.monotonic(),
            }

    def _update_transfer(self, bytes_over_wire: int):
        with self._current_lock:
            # Ignore late callbacks that arrive after _end_transfer.
            if self._current_transfer.get("name") is None:
                return
            # azcopy reports raw bytes-over-wire, which on a bad link exceeds
            # the file size because of internal block retries. Cap at the
            # known file size so we never display > 100%.
            size = self._current_transfer.get("size_bytes") or 0
            if size and bytes_over_wire > size:
                bytes_over_wire = size
            self._current_transfer["bytes_over_wire"] = bytes_over_wire

    def _end_transfer(self):
        with self._current_lock:
            self._current_transfer = {
                "name": None,
                "size_bytes": 0,
                "bytes_over_wire": 0,
                "start_monotonic": None,
            }

    def _ticker(self):
        """Drive live progress updates while a session is running."""
        while not self._stop.is_set() and not self._done_flag.is_set():
            try:
                self._progress()
            except Exception:
                pass
            time.sleep(0.5)

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
            if known_sizes:
                mean = sum(known_sizes) / len(known_sizes)
                unknown_count = sum(
                    1 for m, _ in self.manifests
                    for it in m["items"].values()
                    if it.get("status") != STATUS_VERIFIED
                    and not (it.get("zip_size_bytes") or it.get("size_bytes"))
                )
                remaining_bytes += int(mean * unknown_count)

            # Live in-flight transfer
            with self._current_lock:
                ct = dict(self._current_transfer)
            in_flight_bytes = ct.get("bytes_over_wire", 0) or 0
            in_flight_start = ct.get("start_monotonic")
            in_flight_name = ct.get("name")
            in_flight_size = ct.get("size_bytes") or 0
            # bytes already up (verified) + partial in-flight
            bytes_uploaded_live = bytes_uploaded + in_flight_bytes
            # don't double-count the currently-uploading item in remaining
            remaining_live = max(0, remaining_bytes - in_flight_bytes)

            # Speed: prefer live rate (bytes_over_wire / elapsed of this transfer)
            # so the UI moves during a long upload; fall back to rolling average
            # of completed transfers when nothing is in flight.
            live_speed = 0.0
            if in_flight_start and in_flight_bytes > 0:
                elapsed = time.monotonic() - in_flight_start
                if elapsed > 0:
                    live_speed = in_flight_bytes / elapsed
            if live_speed > 0:
                speed_bps = live_speed
            elif self._recent_uploads:
                tb = sum(b for b, _ in self._recent_uploads)
                tt = sum(t for _, t in self._recent_uploads)
                speed_bps = tb / tt if tt > 0 else 0.0
            else:
                speed_bps = 0.0

            eta_s = (remaining_live / speed_bps) if speed_bps > 0 and remaining_live > 0 else None
            # While a transfer is in flight, show only its name -- top-row
            # Uploaded / Speed / ETA already carry the numeric progress, and
            # duplicating "1.7 GB / 1.1 GB" here was confusing when azcopy's
            # retry-bytes made the counter exceed the file size.
            if in_flight_name:
                display_current = f"Uploading: {in_flight_name}"
            elif current:
                display_current = f"Last: {current}"
            else:
                display_current = ""
            stats = {
                "done": done,
                "total": total,
                "failed": failed,
                "remaining": total - done - failed,
                "current": display_current,
                "bytes_uploaded": bytes_uploaded_live,
                "remaining_bytes": remaining_live,
                "speed_bps": speed_bps,
                "eta_seconds": eta_s,
            }
            self.progress_cb(stats)
        except Exception:
            pass

    def _run(self):
        ticker = threading.Thread(target=self._ticker, daemon=True)
        ticker.start()
        try:
            for m, path in self.manifests:
                self._prepare(m, path)
            self._progress()
            self._run_one_pass()

            # Optional: retry anything still status=failed forever, sleeping
            # RETRY_FAILED_INTERVAL_SECONDS between passes, until either every
            # item verifies or the user stops the session.
            if self.advanced.get("retry_failed_forever"):
                while not self._stop.is_set():
                    n_failed = self._count_failed()
                    if n_failed == 0:
                        break
                    interval = int(self.advanced.get("retry_failed_interval_seconds", 300) or 300)
                    self._log(f"[retry] {n_failed} item(s) still failed; "
                              f"sleeping {interval}s before next pass")
                    self._sleep_interruptible(interval)
                    if self._stop.is_set():
                        break
                    n_reset = self._reset_failed_to_pending()
                    self._log(f"[retry] reset {n_reset} item(s) to pending; starting next pass")
                    self._run_one_pass()

            total = sum(len(m["items"]) for m, _ in self.manifests)
            verified = sum(
                1 for m, _ in self.manifests
                for it in m["items"].values()
                if it.get("status") == STATUS_VERIFIED
            )
            failed = sum(
                1 for m, _ in self.manifests
                for it in m["items"].values()
                if it.get("status") == STATUS_FAILED
            )
            other = total - verified - failed
            if self._stop.is_set():
                self.done_cb(False,
                             f"Upload stopped early. {verified} verified, "
                             f"{failed} failed, {other} not attempted "
                             f"(of {total}).")
            elif failed or other:
                self.done_cb(False,
                             f"Upload finished with {failed} failed and "
                             f"{other} unfinished item(s) (of {total}). "
                             f"See the log.")
            else:
                self.done_cb(True,
                             f"All uploads finished. {verified} item(s) verified.")
        except Exception as e:
            self._log(f"FATAL: {e}\n{traceback.format_exc()}")
            self.done_cb(False, str(e))
        finally:
            self._done_flag.set()
            ticker.join(timeout=2)

    def _run_one_pass(self):
        """One pass through every manifest. SENSOR manifests get the
        zipper+uploader pipeline; BASE manifests get direct upload.
        VERIFIED items are skipped inside each pipeline."""
        sensor_manifests = [(m, p) for m, p in self.manifests if m["kind"] == KIND_SENSOR]
        base_manifests = [(m, p) for m, p in self.manifests if m["kind"] == KIND_BASE]

        if sensor_manifests and not self._stop.is_set():
            depth = int(self.advanced.get("zip_queue_depth", 5) or 5)
            self._zip_queue = queue.Queue(maxsize=depth)  # fresh queue each pass
            zipper = threading.Thread(target=self._zip_loop,
                                      args=(sensor_manifests,), daemon=True)
            zipper.start()
            self._sensor_upload_loop(sensor_manifests)
            zipper.join(timeout=5)

        for m, p in base_manifests:
            if self._stop.is_set():
                break
            self._base_upload(m, p)

    def _count_failed(self) -> int:
        return sum(1 for m, _ in self.manifests
                   for it in m["items"].values()
                   if it.get("status") == STATUS_FAILED)

    def _reset_failed_to_pending(self) -> int:
        n = 0
        for m, path in self.manifests:
            dirty = False
            for it in m["items"].values():
                if it.get("status") == STATUS_FAILED:
                    it["status"] = STATUS_PENDING
                    it["attempts"] = 0
                    n += 1
                    dirty = True
            if dirty:
                save_local_manifest(m, path)
        return n

    def _sleep_interruptible(self, seconds: int):
        for _ in range(seconds):
            if self._stop.is_set():
                return
            time.sleep(1)

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
                        # Multi-pilot / split-across-SD scenario: this item was
                        # added by another system. We can't upload it from
                        # here; leave it alone so the owning system can
                        # complete it later, rather than marking it FAILED
                        # (which would burn a slot in the forever-retry loop).
                        owner = item.get("system_name") or ""
                        if owner and owner != get_system_name():
                            self._log(f"[zip] {name}: owned by other system "
                                      f"({owner}); leaving alone")
                            continue
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
            self._start_transfer(name, item.get("zip_size_bytes") or 0)
            try:
                azcopy_upload_file(zip_path, dest, timeout=timeout, put_md5=True,
                                   on_progress=self._update_transfer)
            except Exception as e:
                self._end_transfer()
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
                self._end_transfer()
                self._log(f"[verify] {name}: OK ({remote_md5})")
                self._save_both(m, path)
                self._progress(name)
                return
            item["last_error"] = f"md5 mismatch: local={item['zip_md5_b64']} remote={remote_md5}"
            self._log(f"[verify] {name}: MISMATCH; retrying")
            self._sleep_backoff(attempt)
        item["status"] = STATUS_FAILED
        self._end_transfer()
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
                owner = item.get("system_name") or ""
                if owner and owner != get_system_name():
                    self._log(f"[base] {name}: owned by other system "
                              f"({owner}); leaving alone")
                    continue
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
                self._start_transfer(name, item.get("size_bytes") or 0)
                try:
                    azcopy_upload_file(src, dest, timeout=timeout, put_md5=True,
                                       on_progress=self._update_transfer)
                except Exception as e:
                    self._end_transfer()
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
                    self._end_transfer()
                    self._log(f"[base] {name}: verified")
                    self._save_both(m, path)
                    self._progress(name)
                    break
                item["last_error"] = "md5 mismatch"
                self._sleep_backoff(attempt)
            else:
                item["status"] = STATUS_FAILED
                self._end_transfer()
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


def verify_manifest_for_deletion(m: dict,
                                  log,
                                  cancel_event: threading.Event | None = None,
                                  item_filter=None) -> dict:
    """Cross-check every item in a manifest against the blob so the pilot can
    confirm it is safe to delete the local source data.

    Sensor items (folders zipped for upload):
      - status must be VERIFIED
      - blob HEAD must exist; blob Content-MD5 and Content-Length must match
        what the manifest recorded at upload time
      - if the local source folder still exists, its recursive file count
        must match the count recorded in the manifest (proxy for "no one
        modified the folder since upload"; re-zipping+re-hashing every
        mission would be prohibitively slow)

    Base station items (single files):
      - status must be VERIFIED
      - blob HEAD must match manifest md5/size
      - if the local file still exists, its md5 is recomputed and compared
        against the manifest md5 (cheap and definitive for single files)

    Returns {"ok": bool, "checked": n, "issues": [str, ...]}.
    """
    issues: list[str] = []
    item_results: dict[str, dict] = {}
    checked = 0
    skipped = 0
    subdir = DIR_SENSOR_DATA if m["kind"] == KIND_SENSOR else DIR_BASE_STATION

    def _record(name: str, category: str | None, detail: str = ""):
        item_results[name] = {"category": category, "detail": detail}
        if category is not None:
            issues.append(detail)

    for name, item in m["items"].items():
        if cancel_event is not None and cancel_event.is_set():
            issues.append("Cancelled before all items were checked.")
            break
        if item_filter is not None and not item_filter(item):
            skipped += 1
            continue
        checked += 1
        log(f"  checking {name} ...")

        if item.get("status") != STATUS_VERIFIED:
            _record(name, "not_verified",
                    f"{name}: status is {item.get('status')!r}, not verified")
            continue

        blob_name = f"{name}.zip" if m["kind"] == KIND_SENSOR else name
        url = blob_url(m["client"], m["program"], m["feeder"],
                       subdir, m["collection_date"], blob_name)
        try:
            headers = blob_head(url)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                _record(name, "blob_missing",
                        f"{name}: blob is missing on Azure ({blob_name})")
            else:
                _record(name, "blob_error",
                        f"{name}: blob HEAD failed ({e})")
            continue
        except Exception as e:
            _record(name, "blob_error", f"{name}: blob HEAD failed ({e})")
            continue

        remote_md5 = headers.get("content-md5")
        try:
            remote_size = int(headers.get("content-length", "0"))
        except ValueError:
            remote_size = 0
        expected_md5 = item.get("zip_md5_b64") if m["kind"] == KIND_SENSOR else item.get("md5_b64")
        expected_size = item.get("zip_size_bytes") if m["kind"] == KIND_SENSOR else item.get("size_bytes")

        if expected_md5 and remote_md5 != expected_md5:
            _record(name, "blob_mismatch",
                    f"{name}: blob md5 mismatch (blob={remote_md5}, manifest={expected_md5})")
            continue
        if expected_size and remote_size != expected_size:
            _record(name, "blob_mismatch",
                    f"{name}: blob size mismatch (blob={remote_size}, manifest={expected_size})")
            continue

        local_path = Path(item["original_path"])
        if not local_path.exists():
            # Blob is proven intact, but the recorded local source is gone.
            # Two possibilities we cannot tell apart from here: the pilot
            # already deleted the data (fine) OR they renamed / moved the
            # parent directory (the tool has no way to find the new
            # location and would silently miss content drift). Report as a
            # WARNING category so the verdict distinguishes it from a full
            # verified pass; the dialog offers a prefix remap to retarget
            # the check at the new location.
            _record(name, "local_missing",
                    f"{name}: local source not found at {local_path}")
            continue

        if m["kind"] == KIND_SENSOR:
            try:
                count = sum(1 for _ in local_path.rglob("*") if _.is_file())
            except Exception as e:
                _record(name, "local_error",
                        f"{name}: could not count local files ({e})")
                continue
            if count != item.get("file_count"):
                _record(name, "local_mismatch",
                        f"{name}: local file count changed since upload "
                        f"(now {count}, was {item.get('file_count')})")
                continue
        else:
            try:
                _hex, b64, size = md5_file(local_path)
            except Exception as e:
                _record(name, "local_error",
                        f"{name}: could not hash local file ({e})")
                continue
            if b64 != expected_md5:
                _record(name, "local_mismatch",
                        f"{name}: local md5 differs from blob md5 "
                        f"(local={b64}, blob={remote_md5})")
                continue

        _record(name, None)
    return {
        "ok": len(issues) == 0,
        "checked": checked,
        "skipped": skipped,
        "issues": issues,
        "item_results": item_results,
    }


# Categories that mean "the cloud copy is bad, and we can fix it by
# re-uploading the item". Anything else (local drift, not-verified,
# transient HEAD errors) is not auto-repairable here.
REPAIRABLE_CATEGORIES = {"blob_missing", "blob_mismatch"}


def repair_manifest_for_reupload(m: dict, local_path: Path,
                                  item_results: dict[str, dict]) -> list[str]:
    """Reset items whose blob copy was found missing / mismatched back to
    STATUS_PENDING so the next Resume picks them up and re-uploads them.

    Returns the list of item names that were repaired.
    """
    repaired: list[str] = []
    for name, res in item_results.items():
        if res.get("category") not in REPAIRABLE_CATEGORIES:
            continue
        item = m["items"].get(name)
        if not item:
            continue
        item["status"] = STATUS_PENDING
        item["attempts"] = 0
        item["uploaded_utc"] = None
        item["last_error"] = f"Repaired by validation: {res.get('detail', res.get('category'))}"
        repaired.append(name)
    if repaired:
        save_cloud_manifest(m)
        save_local_manifest(m, local_path)
    return repaired


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


def _fmt_local_time(ts_utc: str) -> str:
    """Render a stored '...Z' UTC timestamp in the local system timezone,
    12-hour with AM/PM. Falls back to the raw '[:19]' UTC form if parsing
    fails so the column never goes blank."""
    if not ts_utc:
        return ""
    try:
        core = ts_utc.rstrip("Z")
        d = dt.datetime.fromisoformat(core).replace(tzinfo=dt.timezone.utc)
        return d.astimezone().strftime("%Y-%m-%d %I:%M:%S %p")
    except Exception:
        return ts_utc[:19].replace("T", " ")


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


# =============================================================================
# Download session
# =============================================================================


class DownloadSession:
    """Download a set of manifests' VERIFIED items back to local disk.

    For SENSOR items: fetch the zip, md5-verify, then extract into place so
    the mission folder appears exactly as it was uploaded. For BASE items:
    fetch the file, md5-verify. Only verified items in the manifest are
    downloaded -- anything still pending / in-flight is skipped.

    Emits progress via the same stats dict shape as UploadSession, so the
    App's progress screen can render it without special-casing.
    """

    def __init__(self,
                 manifests: list[dict],
                 destination: Path,
                 advanced: dict,
                 log_cb, progress_cb, done_cb):
        self.manifests = manifests
        self.destination = destination
        self.advanced = advanced
        self.log_cb = log_cb
        self.progress_cb = progress_cb
        self.done_cb = done_cb
        self._stop = threading.Event()
        self._done_flag = threading.Event()
        self._current_lock = threading.Lock()
        self._current_transfer: dict = {
            "name": None, "size_bytes": 0,
            "bytes_over_wire": 0, "start_monotonic": None,
        }
        # Keyed by (manifest_blob_path, item_name) rather than just name so
        # that duplicated filenames across dates (e.g. an identical
        # base-station control file appearing in every day's folder) don't
        # collapse to a single entry -- which would make the total count
        # look permanently short.
        self._verified: set[tuple[str, str]] = set()
        self._failed: set[tuple[str, str]] = set()
        self._recent: list[tuple[int, float]] = []
        self._recent_window = 8

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()

    # ---- transfer state (mirrors UploadSession) ----

    def _start_transfer(self, name: str, size_bytes: int):
        with self._current_lock:
            self._current_transfer = {
                "name": name, "size_bytes": size_bytes,
                "bytes_over_wire": 0, "start_monotonic": time.monotonic(),
            }

    def _update_transfer(self, bytes_over_wire: int):
        with self._current_lock:
            if self._current_transfer.get("name") is None:
                return
            size = self._current_transfer.get("size_bytes") or 0
            if size and bytes_over_wire > size:
                bytes_over_wire = size
            self._current_transfer["bytes_over_wire"] = bytes_over_wire

    def _end_transfer(self):
        with self._current_lock:
            self._current_transfer = {
                "name": None, "size_bytes": 0,
                "bytes_over_wire": 0, "start_monotonic": None,
            }

    def _record(self, size_bytes: int, dur: float):
        if size_bytes <= 0 or dur <= 0:
            return
        self._recent.append((size_bytes, dur))
        if len(self._recent) > self._recent_window:
            self._recent.pop(0)

    def _sleep_backoff(self, attempt: int):
        delay = min(60, 2 ** attempt)
        for _ in range(delay):
            if self._stop.is_set():
                return
            time.sleep(1)

    def _log(self, msg: str):
        try:
            self.log_cb(msg)
        except Exception:
            pass

    def _ticker(self):
        while not self._stop.is_set() and not self._done_flag.is_set():
            try:
                self._progress()
            except Exception:
                pass
            time.sleep(0.5)

    def _run(self):
        ticker = threading.Thread(target=self._ticker, daemon=True)
        ticker.start()
        try:
            for m in self.manifests:
                if self._stop.is_set():
                    break
                self._download_manifest(m)
            total = 0
            for m in self.manifests:
                for it in m["items"].values():
                    if it.get("status") == STATUS_VERIFIED:
                        total += 1
            done = len(self._verified)
            failed = len(self._failed)
            interrupted = total - done - failed
            if self._stop.is_set():
                self.done_cb(False,
                             f"Download stopped early. {done} verified, "
                             f"{failed} failed, {interrupted} not attempted "
                             f"(of {total}).")
            elif failed or interrupted:
                self.done_cb(False,
                             f"Download finished with problems. "
                             f"{done} verified, {failed} failed, "
                             f"{interrupted} unaccounted for (of {total}). "
                             f"See the log.")
            else:
                self.done_cb(True,
                             f"Download complete. All {done} item(s) verified.")
        except Exception as e:
            self._log(f"FATAL: {e}\n{traceback.format_exc()}")
            self.done_cb(False, str(e))
        finally:
            self._done_flag.set()
            ticker.join(timeout=2)

    def _download_manifest(self, m: dict):
        for name, item in m["items"].items():
            if self._stop.is_set():
                return
            if item.get("status") != STATUS_VERIFIED:
                continue
            self._download_one(m, name, item)

    def _download_one(self, m: dict, name: str, item: dict):
        key = (m.get("manifest_blob_path", ""), name)
        feeder_dir = (self.destination / m["client"] / m["program"] /
                      m["feeder"])
        subdir = DIR_SENSOR_DATA if m["kind"] == KIND_SENSOR else DIR_BASE_STATION
        date_dir = feeder_dir / subdir / m["collection_date"]
        date_dir.mkdir(parents=True, exist_ok=True)

        if m["kind"] == KIND_SENSOR:
            blob_name = f"{name}.zip"
            src_url = blob_url(m["client"], m["program"], m["feeder"],
                                DIR_SENSOR_DATA, m["collection_date"], blob_name)
            expected_md5 = item.get("zip_md5_b64")
            size = item.get("zip_size_bytes") or 0
            zip_dest = date_dir / blob_name
        else:
            src_url = blob_url(m["client"], m["program"], m["feeder"],
                                DIR_BASE_STATION, m["collection_date"], name)
            expected_md5 = item.get("md5_b64")
            size = item.get("size_bytes") or 0
            zip_dest = date_dir / name

        timeout = self.advanced["timeout_seconds"]
        for attempt in range(1, self.advanced["local_retries"] + 1):
            if self._stop.is_set():
                return
            self._log(f"[dl] {name}: attempt {attempt}/{self.advanced['local_retries']}")
            self._start_transfer(name, size)
            try:
                t0 = time.monotonic()
                # azcopy refuses to overwrite by default; nuke any partial
                # from a prior attempt.
                try:
                    zip_dest.unlink(missing_ok=True)
                except Exception:
                    pass
                azcopy_download_file(src_url, zip_dest, timeout=timeout,
                                     on_progress=self._update_transfer)
                _hex, got_md5, got_size = md5_file(zip_dest)
                if expected_md5 and got_md5 != expected_md5:
                    raise RuntimeError(
                        f"md5 mismatch after download "
                        f"(got={got_md5}, expected={expected_md5})"
                    )
                if m["kind"] == KIND_SENSOR:
                    self._log(f"[dl] {name}: verified; extracting")
                    with zipfile.ZipFile(zip_dest) as zf:
                        zf.extractall(date_dir)
                    try:
                        zip_dest.unlink(missing_ok=True)
                    except Exception:
                        pass
                self._record(size, time.monotonic() - t0)
                self._verified.add(key)
                self._end_transfer()
                self._log(f"[dl] {name}: OK")
                self._progress(name)
                return
            except Exception as e:
                self._log(f"[dl] {name}: attempt {attempt} failed: {e}")
                self._end_transfer()
                self._sleep_backoff(attempt)
        self._failed.add(key)
        self._log(f"[dl] {name}: exhausted retries")
        self._progress(name)

    def _progress(self, current: str = ""):
        try:
            total = 0
            done = len(self._verified)
            failed = len(self._failed)
            bytes_done = 0
            remaining_bytes = 0
            for m in self.manifests:
                mpath = m.get("manifest_blob_path", "")
                for name, item in m["items"].items():
                    if item.get("status") != STATUS_VERIFIED:
                        continue
                    total += 1
                    size = item.get("zip_size_bytes") or item.get("size_bytes") or 0
                    key = (mpath, name)
                    if key in self._verified:
                        bytes_done += size
                    elif key not in self._failed and size:
                        remaining_bytes += size

            with self._current_lock:
                ct = dict(self._current_transfer)
            in_flight_bytes = ct.get("bytes_over_wire") or 0
            in_flight_start = ct.get("start_monotonic")
            in_flight_name = ct.get("name")
            bytes_done_live = bytes_done + in_flight_bytes
            remaining_live = max(0, remaining_bytes - in_flight_bytes)

            live_speed = 0.0
            if in_flight_start and in_flight_bytes > 0:
                elapsed = time.monotonic() - in_flight_start
                if elapsed > 0:
                    live_speed = in_flight_bytes / elapsed
            if live_speed > 0:
                speed_bps = live_speed
            elif self._recent:
                tb = sum(b for b, _ in self._recent)
                tt = sum(t for _, t in self._recent)
                speed_bps = tb / tt if tt > 0 else 0.0
            else:
                speed_bps = 0.0
            eta_s = (remaining_live / speed_bps) if speed_bps > 0 and remaining_live > 0 else None

            if in_flight_name:
                display = f"Downloading: {in_flight_name}"
            elif current:
                display = f"Last: {current}"
            else:
                display = ""

            self.progress_cb({
                "done": done, "total": total, "failed": failed,
                "remaining": total - done - failed,
                "current": display,
                "bytes_uploaded": bytes_done_live,
                "remaining_bytes": remaining_live,
                "speed_bps": speed_bps,
                "eta_seconds": eta_s,
            })
        except Exception:
            pass


NO_COMPRESS_EXTS = {".jpg", ".jpeg"}


def _make_zip(src_dir: Path, out_path: Path) -> None:
    """Zip a mission folder for upload.

    Everything gets DEFLATE at level 9 except already-compressed formats
    (.jpg / .jpeg), which we ZIP_STORE because re-compressing them just
    burns CPU without shrinking the payload. On the slow / intermittent
    links this tool targets, a smaller zip is worth the CPU cost.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".part")
    with zipfile.ZipFile(tmp, "w", allowZip64=True) as zf:
        for p in sorted(src_dir.rglob("*")):
            if not p.is_file():
                continue
            arcname = p.relative_to(src_dir.parent).as_posix()
            if p.suffix.lower() in NO_COMPRESS_EXTS:
                zf.write(p, arcname=arcname, compress_type=zipfile.ZIP_STORED)
            else:
                zf.write(p, arcname=arcname,
                         compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
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


class AutocompleteCombobox(ttk.Combobox):
    """Combobox with inline prefix autocomplete.

    As the user types, the widget (a) filters its dropdown values to prefix
    matches and (b) inline-completes the field to the first match with the
    auto-inserted tail highlighted, so the next keystroke replaces it and
    pressing Right / End accepts. Set ``uppercase=True`` to force-uppercase
    typing (used for feeder names). Pilots can still type a custom value
    that doesn't appear in the list -- the completion is a hint, not a
    constraint.
    """

    _NAV_KEYS = {"BackSpace", "Delete", "Left", "Right", "Up", "Down",
                 "Home", "End", "Escape", "Return", "Tab",
                 "Shift_L", "Shift_R", "Control_L", "Control_R",
                 "Alt_L", "Alt_R"}

    def __init__(self, master, completion_values, textvariable=None,
                 uppercase: bool = False, **kw):
        super().__init__(master, values=completion_values,
                         textvariable=textvariable, **kw)
        self._all_values = sorted(set(completion_values), key=str.casefold)
        self._uppercase = uppercase
        self.bind("<KeyRelease>", self._on_keyrelease)

    def _on_keyrelease(self, event):
        if event.keysym in self._NAV_KEYS:
            return
        typed = self.get()
        if self._uppercase and typed != typed.upper():
            cursor = self.index("insert")
            self.delete(0, "end")
            self.insert(0, typed.upper())
            self.icursor(cursor)
            typed = typed.upper()
        if not typed:
            self.configure(values=self._all_values)
            return
        low = typed.lower()
        matches = [v for v in self._all_values if v.lower().startswith(low)]
        self.configure(values=matches or self._all_values)
        if matches:
            first = matches[0]
            if len(first) > len(typed):
                # Inline-complete: fill the tail and select it so the next
                # keystroke replaces the auto-inserted portion.
                self.delete(0, "end")
                self.insert(0, first)
                self.select_range(len(typed), "end")
                self.icursor(len(typed))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("UU LiDAR Upload Tool")
        self.geometry("900x680")
        self.advanced = dict(DEFAULT_ADVANCED)
        self.session: UploadSession | None = None
        self.queue = UploadQueue()
        # Paths of manifests handed to the currently-running session; used
        # to prune the queue on completion (verified manifests get removed).
        self._session_paths: list[Path] = []
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
        ttk.Button(frame, text="New upload (adds to queue)", width=40,
                   command=self._start_new_wizard).pack(pady=6)
        # Bound to a StringVar so it stays fresh as items are added / removed
        # without needing to rebuild the whole start frame.
        self.queue_btn_var = tk.StringVar(
            value=f"Upload queue ({len(self.queue.paths())} pending)")
        ttk.Button(frame, textvariable=self.queue_btn_var, width=40,
                   command=self._open_queue).pack(pady=6)
        ttk.Button(frame, text="Uploads (browse / download / resume)", width=40,
                   command=self._open_uploads).pack(pady=6)
        ttk.Button(frame, text="Am I Good To Delete? / Validate cloud", width=40,
                   command=self._open_deletion_check).pack(pady=6)
        ttk.Button(frame, text="Advanced options...", width=40,
                   command=self._open_advanced).pack(pady=6)
        ttk.Button(frame, text="Exit", width=40, command=self.destroy).pack(pady=6)

    def _open_advanced(self):
        AdvancedDialog(self, self.advanced)

    def _open_deletion_check(self):
        DeletionCheckDialog(self)

    def _open_uploads(self):
        UploadsBrowserDialog(self)

    def _open_queue(self):
        QueueDialog(self)

    def _refresh_queue_count(self):
        """Keep the main-menu Upload Queue button in sync with the queue
        whenever items are added, removed, or pruned. No-op if the start
        frame isn't currently on screen."""
        try:
            self.queue_btn_var.set(
                f"Upload queue ({len(self.queue.paths())} pending)")
        except Exception:
            pass

    def _enqueue(self, manifests: list[tuple[dict, Path]]):
        """Add manifests to the queue (paths only; the manifest JSON is on
        disk in the registry). Offer to open the queue or start processing
        right away."""
        for _m, p in manifests:
            self.queue.append(str(p))
        self._refresh_queue_count()
        n = len(manifests)
        total = len(self.queue.paths())
        if messagebox.askyesno(
            "Added to queue",
            f"Added {n} manifest(s) to the upload queue "
            f"({total} in queue total).\n\nOpen the queue now?",
        ):
            self._open_queue()

    def _process_queue(self, manifests: list[tuple[dict, Path]]):
        """Kick off an UploadSession with a pre-validated list. Called from
        QueueDialog after it's checked availability."""
        self._start_session(manifests)

    def _start_download(self, manifests: list[dict], destination: Path):
        self._mode = "download"
        self._build_progress()
        session = DownloadSession(
            manifests=manifests,
            destination=destination,
            advanced=dict(self.advanced),
            log_cb=self._log,
            progress_cb=self._progress,
            done_cb=self._done,
        )
        self.session = session
        session.start()

    def _start_new_wizard(self):
        NewUploadWizard(self, on_ready=self._enqueue)

    def _start_session(self, manifests: list[tuple[dict, Path]]):
        self._mode = "upload"
        # Remember what we handed off so _done can prune the queue of any
        # manifests that finished fully verified.
        self._session_paths = [p for _m, p in manifests]
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

    # --- progress screen ---

    def _build_progress(self):
        self._clear()
        frame = ttk.Frame(self, padding=10)
        frame.pack(fill="both", expand=True)
        title = ("Download in progress"
                 if getattr(self, "_mode", "upload") == "download"
                 else "Upload in progress")
        ttk.Label(frame, text=title, font=("Segoe UI", 14, "bold")).pack(anchor="w")
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
        self.stop_btn = ttk.Button(btns, text="Stop", command=self._stop_session)
        self.stop_btn.pack(side="right")
        self.back_btn = ttk.Button(btns, text="Back to home",
                                   command=self._back_to_home)
        self.back_btn.pack(side="right", padx=6)
        self.back_btn.state(["disabled"])

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
            verb = "Downloaded" if getattr(self, "_mode", "upload") == "download" else "Uploaded"
            self.bytes_var.set(f"{verb}: {_fmt_bytes(stats['bytes_uploaded'])}")
            if stats["current"]:
                self.current_item.set(stats["current"])
        self.after(0, apply)

    def _done(self, ok: bool, msg: str):
        def apply():
            self._append_log(("DONE: " if ok else "FAILED: ") + msg)
            # Session is over -- swap the buttons so the pilot can leave.
            try:
                self.stop_btn.state(["disabled"])
                self.stop_btn.config(text="Stopped" if not ok else "Finished")
                self.back_btn.state(["!disabled"])
            except Exception:
                pass
            # Prune the queue: any manifest that came out of this upload
            # session fully verified is done and can leave the queue.
            # Partial / failed manifests stay so the pilot can re-run.
            if getattr(self, "_mode", "upload") == "upload":
                for p in getattr(self, "_session_paths", []):
                    try:
                        m = load_local_manifest(p)
                    except Exception:
                        continue
                    items = m.get("items", {}) or {}
                    if items and all(it.get("status") == STATUS_VERIFIED
                                     for it in items.values()):
                        self.queue.remove(str(p))
                self._refresh_queue_count()
            messagebox.showinfo("Finished" if ok else "Stopped", msg)
        self.after(0, apply)

    def _stop_session(self):
        if self.session:
            self.session.stop()
            self._append_log("Stop requested; will halt after current item.")

    def _back_to_home(self):
        # If a session is somehow still running, ask it to stop before we
        # tear down the progress UI; the daemon thread will exit on its own.
        if self.session:
            try:
                self.session.stop()
            except Exception:
                pass
            self.session = None
        self._build_start()


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
            ("Retry-failed interval seconds (between passes)", "retry_failed_interval_seconds", int),
            ("Zip queue depth (missions pre-zipped ahead of upload)", "zip_queue_depth", int),
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

        retry_var = tk.BooleanVar(value=bool(advanced.get("retry_failed_forever", True)))
        ttk.Checkbutton(self,
                        text="After first pass, keep retrying failed uploads forever "
                             "(uses the interval above)",
                        variable=retry_var).grid(row=len(rows) + 1, column=0, columnspan=2,
                                                 sticky="w", padx=8, pady=(0, 8))

        def save():
            try:
                for key, (v, t) in vars_.items():
                    val = v.get().strip()
                    advanced[key] = t(val) if val or t is str else t(0)
                advanced["create_readmes"] = readme_var.get()
                advanced["retry_failed_forever"] = retry_var.get()
            except Exception as e:
                messagebox.showerror("Bad value", str(e))
                return
            self.destroy()

        ttk.Button(self, text="Save", command=save).grid(row=len(rows) + 2, column=1, sticky="e", padx=8, pady=8)
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
        # state="normal" (not "readonly") + AutocompleteCombobox so pilots
        # see suggestions as they type but can still enter a custom name.
        AutocompleteCombobox(self, VALID_PILOTS, textvariable=self.pilot_var,
                             state="normal").grid(row=row, column=1, sticky="ew", **pad)
        row += 1
        ttk.Label(self, text="Feeder (e.g. LAW322)").grid(row=row, column=0, sticky="w", **pad)
        AutocompleteCombobox(self, VALID_FEEDERS, textvariable=self.feeder_var,
                             state="normal", uppercase=True).grid(row=row, column=1, sticky="ew", **pad)
        row += 1
        ttk.Label(self, text="Re-type feeder to confirm").grid(row=row, column=0, sticky="w", **pad)
        AutocompleteCombobox(self, VALID_FEEDERS, textvariable=self.feeder_confirm_var,
                             state="normal", uppercase=True).grid(row=row, column=1, sticky="ew", **pad)
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
        ttk.Checkbutton(self, text="Also upload BASE_STATION (folder or single file)",
                        variable=self.include_base_var).grid(
            row=row, column=0, columnspan=2, sticky="w", **pad)
        row += 1
        ttk.Label(self, text="Base station folder / file").grid(row=row, column=0, sticky="w", **pad)
        ttk.Entry(self, textvariable=self.base_source_var).grid(row=row, column=1, sticky="ew", **pad)
        base_btns = ttk.Frame(self)
        base_btns.grid(row=row, column=2, sticky="w", **pad)
        ttk.Button(base_btns, text="Folder...",
                    command=self._pick_base_folder).pack(side="left")
        ttk.Button(base_btns, text="File...",
                    command=self._pick_base_file).pack(side="left", padx=(4, 0))
        row += 1

        self.columnconfigure(1, weight=1)

        self.report = tk.Text(self, height=14, wrap="word")
        self.report.grid(row=row, column=0, columnspan=3, sticky="nsew", padx=8, pady=8)
        self.rowconfigure(row, weight=1)
        row += 1

        btns = ttk.Frame(self)
        btns.grid(row=row, column=0, columnspan=3, sticky="ew", padx=8, pady=8)
        ttk.Button(btns, text="Scan / validate", command=self._scan).pack(side="left")
        ttk.Button(btns, text="Add to queue", command=self._confirm_start).pack(side="right")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=6)

        self.scanned: dict | None = None

    def _pick_source(self):
        d = filedialog.askdirectory(title="Select sensor source folder")
        if d:
            self.source_var.set(d)

    def _pick_base_folder(self):
        d = filedialog.askdirectory(title="Select base station folder")
        if d:
            self.base_source_var.set(d)

    def _pick_base_file(self):
        f = filedialog.askopenfilename(
            title="Select a single base station file",
            filetypes=[
                ("Base station files", "*.dat *.rnx *.[0-9][0-9]o *.[0-9][0-9]O"),
                ("All files", "*.*"),
            ],
        )
        if f:
            self.base_source_var.set(f)

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
        sensor = self.sensor_var.get()
        if self.upload_sensor_var.get():
            src = self.source_var.get().strip()
            if not src or not Path(src).is_dir():
                messagebox.showerror("Bad source", "Pick a valid sensor source folder.")
                return
            root = Path(src)
            data_type = detect_data_type(root, sensor)
            if data_type is None:
                mission_shape = ("@@YYYY-MM-DD-HHMMSS folders" if sensor == "TV540"
                                 else "DJI_* mission folders")
                messagebox.showerror(
                    "Mixed / empty",
                    f"Sensor source must be either raw {mission_shape} OR .las/.laz files, not both.",
                )
                return
            self._report(f"Detected data type: {data_type} (sensor={sensor})")
            if sensor == "TV540":
                # No feeder in the folder name for TV540 -- extra reminder
                # since we can't cross-check what the pilot typed.
                self._report(
                    "NOTE: TV540 folder names don't encode the feeder. "
                    f"All missions will be filed under '{feeder}' -- verify it's right."
                )
            if data_type == DATA_TYPE_RAW:
                valid, invalid = scan_missions(root, feeder, sensor)
                if invalid:
                    self._report("Invalid entries in source root -- fix, rename, or remove and re-scan:")
                    for i in invalid:
                        self._report(f"  {i['name']}: {i['reason']}")
                    messagebox.showwarning("Invalid entries",
                        "One or more entries in the source root are invalid. "
                        "See report; fix and re-scan.")
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
            src_path = Path(bp) if bp else None
            if not src_path or not src_path.exists():
                messagebox.showerror(
                    "Bad base source",
                    "Pick a valid base station folder OR a single .dat / RINEX file."
                )
                return
            dats, bdate, warnings = scan_base_station(src_path)
            for w in warnings:
                self._report(f"[base] {w}")
            if not dats:
                messagebox.showerror(
                    "No usable files",
                    "No .dat / RINEX .YYo / .rnx files found at that path."
                )
                return
            if not bdate:
                messagebox.showerror(
                    "No date",
                    "Could not determine collection date from the base station file(s)."
                )
                return
            self._report(f"Base station: {len(dats)} file(s), date {bdate}")
            # 'folder' key kept for backward compat with the manifest schema;
            # for a single-file source this is the parent directory.
            folder_str = str(src_path if src_path.is_dir() else src_path.parent)
            base_info = {"folder": folder_str, "date": bdate,
                         "files": [str(p) for p in dats]}

        if not sensor_by_date and not base_info:
            messagebox.showerror("Nothing to do", "Enable at least one of SENSOR_DATA or BASE_STATION.")
            return

        # Preview any existing cloud manifests so the pilot knows what will be
        # merged versus added. Multi-pilot or split-across-SD-cards scenarios
        # always merge into the existing per-(feeder,kind,date) cloud manifest.
        client = self.client_var.get()
        program = self.program_var.get()
        my_system = get_system_name()
        for date, items in sorted(sensor_by_date.items()):
            real_date = items[0]["date"] if date == "__processed__" else date
            self._preview_cloud_merge(KIND_SENSOR, client, program, feeder, real_date,
                                      new_names={it["name"] for it in items},
                                      my_system=my_system)
        if base_info:
            self._preview_cloud_merge(KIND_BASE, client, program, feeder, base_info["date"],
                                      new_names={Path(p).name for p in base_info["files"]},
                                      my_system=my_system)

        self.scanned = {
            "feeder": feeder,
            "data_type": data_type,
            "sensor_by_date": sensor_by_date,
            "base_info": base_info,
        }
        self._report("Scan OK. Press 'Add to queue' to add these manifests.")

    def _preview_cloud_merge(self, kind: str, client: str, program: str,
                              feeder: str, collection_date: str,
                              new_names: set[str], my_system: str):
        """Fetch any existing cloud manifest and report how the merge will
        look, so the pilot knows what they're adding to before pressing Start.
        Failure to reach Azure is non-fatal; we log a warning and move on."""
        try:
            existing = load_cloud_manifest_at(
                manifest_blob_path_for(kind, client, program, feeder, collection_date))
        except Exception as e:
            self._report(f"[cloud] {kind}/{collection_date}: could not check cloud "
                         f"for existing manifest ({e}); will attempt merge at Start.")
            return
        if not existing:
            self._report(f"[cloud] {kind}/{collection_date}: no existing cloud manifest; "
                         f"a fresh one will be created.")
            return
        existing_items = existing.get("items", {}) or {}
        verified = sum(1 for it in existing_items.values() if it.get("status") == STATUS_VERIFIED)
        pending = len(existing_items) - verified
        same_name = new_names & set(existing_items.keys())
        truly_new = new_names - same_name
        other_system_items = [
            n for n, it in existing_items.items()
            if it.get("system_name") and it.get("system_name") != my_system
        ]
        self._report(
            f"[cloud] {kind}/{collection_date}: existing manifest has "
            f"{len(existing_items)} item(s) "
            f"({verified} verified, {pending} pending).")
        if same_name:
            self._report(f"    - {len(same_name)} of your items already exist by name "
                         f"in the cloud; already-verified ones will be skipped, "
                         f"any not-yet-verified will re-upload.")
        if truly_new:
            self._report(f"    - {len(truly_new)} brand-new item(s) from your scan "
                         f"will be ADDED to the existing manifest.")
        if other_system_items:
            self._report(f"    - {len(other_system_items)} item(s) belong to another "
                         f"system; those are left alone by this session.")

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
        # Extra confirmation for base station: neither .dat filenames nor
        # RINEX headers encode the feeder, so we can only cross-check by
        # asking the pilot to confirm their records.
        bi = self.scanned.get("base_info")
        if bi:
            n = len(bi["files"])
            if not messagebox.askyesno(
                "Confirm base station",
                f"{n} base station file(s) (.dat / RINEX .YYo) will be filed "
                f"under feeder '{feeder}' at collection date {bi['date']}.\n\n"
                f"Neither format embeds the feeder in its filename, so this "
                f"can't be validated by the tool -- confirm the pilot's "
                f"records say these files belong to this feeder. Continue?"
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
                    pilot_name=pilot_name,
                    session_id=m["session_id"],
                )
            path = default_local_path(feeder, KIND_SENSOR, date, m["session_id"])
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
                m["items"][pp.name] = mint_base_item(
                    source_path=str(pp),
                    system_name=system_name,
                    pilot_name=pilot_name,
                    session_id=m["session_id"],
                )
            path = default_local_path(feeder, KIND_BASE, bi["date"], m["session_id"])
            save_local_manifest(m, path)
            manifests.append((m, path))
            self._report(f"Wrote local manifest: {path}")

        messagebox.showinfo(
            "Manifests saved",
            f"Local manifests saved to:\n{registry_dir()}\n\n"
            "Any manifest in that folder is a valid resume handle, and can be "
            "checked later via 'Am I Good To Delete?' before you delete data "
            "off the drive. The cloud copy at each manifest_blob_path is "
            "byte-identical to the local file and is also a valid resume handle.",
        )
        self.destroy()
        self.on_ready(manifests)


# --- "Am I Good To Delete?" dialog -------------------------------------------


class DeletionCheckDialog(tk.Toplevel):
    """Browse the manifest registry, filter to the one you want, and
    cross-check every item against the blob before deleting local data.
    """

    COLUMNS = ("feeder", "kind", "date", "pilot", "verified", "updated")

    def __init__(self, parent: App):
        super().__init__(parent)
        self.title("Am I Good To Delete?")
        self.geometry("1000x680")

        # Modal-on-top
        self.transient(parent)
        self.lift()
        self.attributes("-topmost", True)
        self.after(300, lambda: self.attributes("-topmost", False))
        self.focus_force()
        self.grab_set()

        self._paths: list[Path] = []
        self._manifests: dict[str, dict] = {}  # path -> manifest
        self._cancel = threading.Event()
        # Last verify result kept so Repair can act on it without re-running.
        self._last_result: dict | None = None
        self._last_result_path: Path | None = None

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text=f"Manifest registry:  {registry_dir()}").pack(anchor="w")

        search = ttk.Frame(self, padding=(8, 4))
        search.pack(fill="x")
        ttk.Label(search, text="Search (feeder / date / pilot / kind):").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._refill())
        ttk.Entry(search, textvariable=self.search_var, width=40).pack(side="left", padx=6)
        ttk.Button(search, text="Refresh", command=self._reload).pack(side="left", padx=4)
        ttk.Button(search, text="Open registry folder",
                   command=self._open_registry_folder).pack(side="left", padx=4)

        # Ownership scope for the check: default to items uploaded by THIS
        # pilot/session so a multi-pilot merged manifest doesn't flag another
        # pilot's items as local_missing just because the sources aren't on
        # this machine.
        scope = ttk.Frame(self, padding=(8, 0))
        scope.pack(fill="x")
        self.only_mine_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(scope,
                        text="Only check items uploaded by me (my session or my system)",
                        variable=self.only_mine_var).pack(side="left")

        # Optional local-prefix remap so a check can retarget when the pilot
        # renamed / moved the parent directory holding the raw data. This is
        # applied to a COPY of the manifest for the check only; nothing is
        # written back to disk (unlike the Resume drive-remap flow).
        remap = ttk.Frame(self, padding=(8, 0))
        remap.pack(fill="x")
        ttk.Label(remap, text="Local prefix remap (optional):").pack(side="left")
        ttk.Label(remap, text="  Old").pack(side="left")
        self.old_prefix_var = tk.StringVar()
        ttk.Entry(remap, textvariable=self.old_prefix_var, width=28).pack(side="left", padx=4)
        ttk.Label(remap, text="New").pack(side="left")
        self.new_prefix_var = tk.StringVar()
        ttk.Entry(remap, textvariable=self.new_prefix_var, width=28).pack(side="left", padx=4)

        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill="both", expand=True, padx=8, pady=4)
        self.tree = ttk.Treeview(tree_frame, columns=self.COLUMNS, show="headings",
                                 selectmode="browse", height=12)
        for col, w in zip(self.COLUMNS, (100, 90, 110, 140, 100, 170)):
            self.tree.heading(col, text=col.title())
            self.tree.column(col, width=w, anchor="w")
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        btns = ttk.Frame(self, padding=8)
        btns.pack(fill="x")
        ttk.Button(btns, text="Verify selected", command=self._verify).pack(side="left")
        self.stop_btn = ttk.Button(btns, text="Stop check", command=self._cancel.set)
        self.stop_btn.pack(side="left", padx=6)
        self.stop_btn.state(["disabled"])
        self.repair_btn = ttk.Button(btns, text="Repair manifest for re-upload",
                                     command=self._repair)
        self.repair_btn.pack(side="left", padx=6)
        self.repair_btn.state(["disabled"])
        ttk.Button(btns, text="Close", command=self.destroy).pack(side="right")

        result_frame = ttk.LabelFrame(self, text="Result")
        result_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.verdict_var = tk.StringVar(value="Pick a manifest and press 'Verify selected'.")
        ttk.Label(result_frame, textvariable=self.verdict_var, font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=6, pady=4)
        self.result_text = tk.Text(result_frame, height=12, wrap="word")
        self.result_text.pack(fill="both", expand=True, padx=6, pady=4)

        self._reload()

    def _reload(self):
        self._paths = list_registered_manifests()
        self._manifests.clear()
        for p in self._paths:
            try:
                self._manifests[str(p)] = load_local_manifest(p)
            except Exception:
                self._manifests[str(p)] = {}
        self._refill()

    def _row_matches(self, m: dict, query: str) -> bool:
        if not query:
            return True
        blob = " ".join(str(m.get(k, "")) for k in
                        ("feeder", "kind", "collection_date", "pilot_name",
                         "sensor", "data_type", "client", "program")).lower()
        return query.lower() in blob

    def _refill(self):
        q = self.search_var.get().strip()
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for p in self._paths:
            m = self._manifests.get(str(p), {})
            if not self._row_matches(m, q):
                continue
            summary = m.get("summary", {}) or {}
            verified = f"{summary.get('verified', 0)} / {summary.get('total', 0)}"
            self.tree.insert("", "end", iid=str(p), values=(
                m.get("feeder", "?"),
                m.get("kind", "?"),
                m.get("collection_date", "?"),
                m.get("pilot_name", "?"),
                verified,
                _fmt_local_time(m.get("updated_utc", "")),
            ))

    def _open_registry_folder(self):
        try:
            if os.name == "nt":
                os.startfile(str(registry_dir()))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(registry_dir())])
        except Exception as e:
            messagebox.showerror("Could not open folder", str(e))

    def _verify(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showerror("Pick one", "Select a manifest row first.")
            return
        path = Path(sel[0])
        m = self._manifests.get(str(path))
        if not m:
            messagebox.showerror("Load failed", "Could not read that manifest.")
            return

        # Optional prefix remap for the CHECK ONLY. Deep-copy the manifest so
        # we don't mutate the registry copy. If the pilot presses Repair later
        # we use the original manifest (repair should touch cloud-side items,
        # which are independent of local paths).
        old = self.old_prefix_var.get()
        new = self.new_prefix_var.get()
        if (old and not new) or (new and not old):
            messagebox.showerror("Incomplete remap",
                                 "Fill both Old and New prefix, or leave both empty.")
            return
        m_check = json.loads(json.dumps(m))  # deep copy
        if old and new:
            _remap_paths(m_check, old, new)

        # Build the ownership filter. An item is "mine" if either its
        # per-item session_id matches this manifest's top-level session_id
        # (the strictest form), or (legacy items with no per-item session_id)
        # its system_name matches. When the checkbox is off we check every
        # item in the manifest.
        my_session = m_check.get("session_id")
        my_system = m_check.get("system_name")
        only_mine = self.only_mine_var.get()

        def item_filter(item):
            if not only_mine:
                return True
            item_session = item.get("session_id")
            if item_session and my_session:
                return item_session == my_session
            return item.get("system_name") == my_system

        self.result_text.delete("1.0", "end")
        self.verdict_var.set("Verifying against blob... please wait.")
        self._cancel.clear()
        self.stop_btn.state(["!disabled"])
        self.repair_btn.state(["disabled"])
        self._last_result = None
        self._last_result_path = path
        threading.Thread(target=self._verify_worker,
                         args=(m_check, path, item_filter), daemon=True).start()

    def _verify_worker(self, m: dict, path: Path, item_filter=None):
        def log(msg: str):
            self.after(0, lambda: self._append(msg))
        try:
            self.after(0, lambda: self._append(
                f"Checking items in {m.get('feeder')} / {m.get('kind')} / "
                f"{m.get('collection_date')} ...\n"
            ))
            result = verify_manifest_for_deletion(m, log, self._cancel, item_filter)
        except Exception as e:
            self.after(0, lambda: self._append(f"\nERROR: {e}\n"))
            self.after(0, lambda: self.verdict_var.set("Check failed."))
            self.after(0, lambda: self.stop_btn.state(["disabled"]))
            return

        def finish():
            self.stop_btn.state(["disabled"])
            self._last_result = result
            skipped = result.get("skipped", 0)
            if skipped:
                self._append(
                    f"(Skipped {skipped} item(s) that belong to other "
                    f"pilots / systems -- uncheck 'Only check items uploaded "
                    f"by me' to include them.)\n"
                )
            item_results = result.get("item_results") or {}
            hard_issues = [
                n for n, r in item_results.items()
                if r.get("category") not in (None, "local_missing")
            ]
            local_missing = [
                n for n, r in item_results.items()
                if r.get("category") == "local_missing"
            ]
            if self._cancel.is_set():
                self.verdict_var.set("Cancelled.")
            elif hard_issues:
                self.verdict_var.set(
                    f"NOT SAFE  --  {len(hard_issues)} issue(s) across "
                    f"{result['checked']} checked item(s). See details below."
                )
                for n in hard_issues:
                    self._append(f"  !! {item_results[n]['detail']}\n")
                if local_missing:
                    self._append(
                        f"\n{len(local_missing)} additional item(s) had a missing "
                        f"local source (see below).\n"
                    )
                    for n in local_missing:
                        self._append(f"  ?? {item_results[n]['detail']}\n")
            elif local_missing:
                # Cloud is intact for every item, but the local sources are
                # not where the manifest says they should be. Could mean the
                # pilot already deleted them (safe) OR renamed/moved the
                # parent directory (we cannot tell). Do NOT declare SAFE.
                self.verdict_var.set(
                    f"CLOUD INTACT, LOCAL UNKNOWN  --  blob verified for all "
                    f"{result['checked']} item(s), but {len(local_missing)} local "
                    f"source(s) not found at the recorded path. If you already "
                    f"deleted the data, this is expected -- deletion is safe. "
                    f"If you renamed / moved the parent folder, use the Local "
                    f"prefix remap above to retarget the check."
                )
                for n in local_missing:
                    self._append(f"  ?? {item_results[n]['detail']}\n")
            else:
                self.verdict_var.set(
                    f"SAFE TO DELETE  --  all {result['checked']} item(s) verified "
                    f"against the blob AND locally."
                )
            # If any items are repairable (blob missing/mismatched), enable
            # the Repair button so the pilot can reset them to pending
            # and re-upload.
            repairable = [
                n for n, r in (result.get("item_results") or {}).items()
                if r.get("category") in REPAIRABLE_CATEGORIES
            ]
            if repairable:
                self.repair_btn.config(text=f"Repair manifest ({len(repairable)} to re-upload)")
                self.repair_btn.state(["!disabled"])
                self._append(
                    f"\n{len(repairable)} item(s) can be repaired (blob missing or "
                    f"mismatched). Click 'Repair manifest' to mark them for re-upload, "
                    f"then use Resume to send them again.\n"
                )
            else:
                self.repair_btn.config(text="Repair manifest for re-upload")
                self.repair_btn.state(["disabled"])
            self._append(f"\nManifest file: {path}\n")
        self.after(0, finish)

    def _repair(self):
        if not self._last_result or not self._last_result_path:
            messagebox.showerror("No result", "Run 'Verify selected' first.")
            return
        path = self._last_result_path
        m = self._manifests.get(str(path))
        if not m:
            messagebox.showerror("Load failed", "Could not read that manifest.")
            return
        item_results = self._last_result.get("item_results") or {}
        repairable = [n for n, r in item_results.items()
                      if r.get("category") in REPAIRABLE_CATEGORIES]
        if not repairable:
            messagebox.showinfo("Nothing to repair",
                                "No repairable items in the last check result.")
            return
        if not messagebox.askyesno(
            "Confirm repair",
            f"Mark {len(repairable)} item(s) as pending so they will be re-uploaded "
            f"the next time you Resume this manifest?\n\n"
            + "\n".join(f"  - {n}" for n in repairable[:10])
            + ("\n  ..." if len(repairable) > 10 else "")
        ):
            return
        self.repair_btn.state(["disabled"])
        self.verdict_var.set("Repairing manifest...")

        def worker():
            try:
                names = repair_manifest_for_reupload(m, path, item_results)
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Repair failed", str(e)))
                return
            def done():
                self._append(f"\nRepaired {len(names)} item(s); reset to pending:\n")
                for n in names:
                    self._append(f"  * {n}\n")
                self.verdict_var.set(
                    f"Manifest repaired. Resume this manifest to re-upload "
                    f"{len(names)} item(s)."
                )
                # Refresh the cached manifest so the tree row shows the new
                # verified/total split.
                try:
                    self._manifests[str(path)] = load_local_manifest(path)
                except Exception:
                    pass
                self._refill()
                # Clear the last result so the button won't be re-enabled without
                # another Verify pass.
                self._last_result = None
                self.repair_btn.config(text="Repair manifest for re-upload")
            self.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    def _append(self, s: str):
        self.result_text.insert("end", s)
        self.result_text.see("end")


# --- uploads browser dialog -------------------------------------------------
#
# Merged replacement for the old separate DownloadDialog and
# UploadTrackerDialog. Layout follows the Upload Tracker style (grouped
# tree, colored rows) with a Date-under-Feeder second grouping level, a
# base-station availability flag on each sensor row, and both actions on
# the same page: Download selected (verified items -> local disk, zips
# auto-extracted) and Resume selected (take-over via TakeoverDialog for
# any in-progress upload).


class UploadsBrowserDialog(tk.Toplevel):
    """Single browser for every upload the master index knows about.

    Rows are three-level: Feeder > Date > Entry (one per kind). Sensor
    rows also show the sensor type (L3/TV540/TVGO) and a Base column
    that flags whether a matching base-station manifest exists for the
    same (feeder, date), so you can spot a pilot who's forgotten to
    upload their base station data at a glance.
    """

    ENTRY_COLS = ("kind", "sensor", "pilot", "systems", "progress",
                  "base", "updated")

    def __init__(self, parent: App):
        super().__init__(parent)
        self.title("Uploads")
        self.geometry("1180x760")
        self.parent_app = parent

        self.transient(parent)
        self.lift()
        self.attributes("-topmost", True)
        self.after(300, lambda: self.attributes("-topmost", False))
        self.focus_force()
        self.grab_set()

        self.client_var = tk.StringVar(value=VALID_CLIENTS[0])
        self.program_var = tk.StringVar(value=VALID_PROGRAMS[0])
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._refill())
        self.dest_var = tk.StringVar()
        self._entries: list[dict] = []
        # (feeder, date) -> dict with base_entry (or None)
        self._base_index: dict[tuple[str, str], dict | None] = {}

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Client").pack(side="left")
        ttk.Combobox(top, textvariable=self.client_var, values=VALID_CLIENTS,
                     state="readonly", width=10).pack(side="left", padx=4)
        ttk.Label(top, text="Program").pack(side="left", padx=(10, 0))
        ttk.Combobox(top, textvariable=self.program_var, values=VALID_PROGRAMS,
                     state="readonly", width=8).pack(side="left", padx=4)
        ttk.Button(top, text="Load", command=self._load).pack(side="left", padx=(10, 4))
        ttk.Button(top, text="Refresh from cloud (rescan all feeders)",
                   command=self._rebuild).pack(side="left", padx=4)
        ttk.Label(top, text="Search").pack(side="left", padx=(20, 4))
        ttk.Entry(top, textvariable=self.search_var, width=40).pack(side="left")

        # Persistent warning + help block above the table.
        info = tk.Message(
            self,
            width=1140, padx=8, pady=4,
            text=(
                "WARNING: 'Progress' comes from cloud manifests. If a pilot "
                "hasn't started their upload for a date, their items are "
                "absent from the manifest -- a complete row does NOT guarantee "
                "ALL data for that day is present, especially when multiple "
                "pilots share a day. Coordinate before treating a row as final.\n\n"
                "HELP: pick a leaf row (not a feeder/date header), then:\n"
                "  * DOWNLOAD SELECTED pulls every verified item in that "
                "manifest to the Destination folder below. Zips are md5-"
                "verified and unzipped so the mission folder appears exactly "
                "as it was uploaded.\n"
                "  * RESUME SELECTED opens the take-over dialog. Give the "
                "current pilot's name and point at where the local files are "
                "on THIS machine; any unfinished items whose folder / file "
                "matches by name will be adopted and uploaded from here."
            ),
            foreground="#8a4b00",
        )
        info.pack(fill="x", padx=8, pady=(2, 4))

        # Row-color legend
        legend = ttk.Frame(self, padding=(8, 0))
        legend.pack(fill="x")
        for text, color in [("Complete", "#c9f2c9"),
                             ("In progress", "#fff2c9"),
                             ("Empty / no items", "#f2c9c9"),
                             ("Missing base station", "#fddede")]:
            tk.Label(legend, text=f"  {text}  ", background=color,
                     relief="solid", borderwidth=1).pack(side="left", padx=4)

        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill="both", expand=True, padx=8, pady=6)
        self.tree = ttk.Treeview(tree_frame, columns=self.ENTRY_COLS,
                                  show="tree headings", height=20)
        self.tree.heading("#0", text="Feeder / Date")
        self.tree.column("#0", width=180, anchor="w")
        widths = (70, 70, 140, 180, 90, 130, 160)
        for col, w in zip(self.ENTRY_COLS, widths):
            self.tree.heading(col, text=col.title())
            self.tree.column(col, width=w, anchor="w")
        vsb = ttk.Scrollbar(tree_frame, orient="vertical",
                             command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("complete", background="#c9f2c9")
        self.tree.tag_configure("partial", background="#fff2c9")
        self.tree.tag_configure("empty", background="#f2c9c9")
        self.tree.tag_configure("missing_base", background="#fddede")
        self.tree.bind("<Double-1>", lambda _e: self._download_or_resume())

        # Destination folder for downloads
        dest = ttk.Frame(self, padding=(8, 4))
        dest.pack(fill="x")
        ttk.Label(dest, text="Destination folder (for Download)").pack(side="left")
        ttk.Entry(dest, textvariable=self.dest_var, width=60).pack(
            side="left", padx=6, fill="x", expand=True)
        ttk.Button(dest, text="Browse", command=self._pick_dest).pack(side="left")

        btns = ttk.Frame(self, padding=8)
        btns.pack(fill="x")
        ttk.Button(btns, text="Download selected",
                    command=self._download).pack(side="left")
        self.resume_btn = ttk.Button(btns, text="Resume selected",
                                       command=self._resume)
        self.resume_btn.pack(side="left", padx=6)
        self.resume_btn.state(["disabled"])
        ttk.Button(btns, text="Close", command=self.destroy).pack(side="right")

        # Enable Resume only for rows that actually have work left to do.
        self.tree.bind("<<TreeviewSelect>>",
                        lambda _e: self._refresh_action_states())

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var).pack(anchor="w", padx=8, pady=(0, 6))

        self._load()

    # ---- data ----

    def _load(self):
        self.status_var.set("Loading master index...")
        self.tree.delete(*self.tree.get_children())
        client = self.client_var.get()
        program = self.program_var.get()

        def worker():
            try:
                idx = load_master_index(client, program)
            except Exception as e:
                self.after(0, lambda: self.status_var.set(f"Failed to load index: {e}"))
                return
            self.after(0, lambda: self._apply(idx.get("entries") or []))
        threading.Thread(target=worker, daemon=True).start()

    def _rebuild(self):
        if not messagebox.askyesno(
            "Rebuild index",
            f"Rescan every valid feeder ({len(VALID_FEEDERS)}) and rewrite "
            f"the master index from scratch? This can be slow on a bad link.",
        ):
            return
        client = self.client_var.get()
        program = self.program_var.get()
        self.status_var.set("Rebuilding: scanning feeders...")

        def prog(i, total, feeder):
            self.after(0, lambda: self.status_var.set(
                f"Rebuilding {i}/{total}: {feeder}"))

        def worker():
            try:
                idx = rebuild_master_index(client, program, progress_cb=prog)
            except Exception as e:
                self.after(0, lambda: self.status_var.set(f"Rebuild failed: {e}"))
                return
            entries = idx.get("entries") or []
            self.after(0, lambda: (
                self._apply(entries),
                self.status_var.set(f"Rebuilt. {len(entries)} manifest(s) indexed."),
            ))
        threading.Thread(target=worker, daemon=True).start()

    def _apply(self, entries: list[dict]):
        self._entries = entries
        # Precompute base-station map so sensor rows can flag missing bases.
        self._base_index = {}
        for e in entries:
            if e.get("kind") == KIND_BASE:
                self._base_index[(e["feeder"], e["collection_date"])] = e
        self._refill()

    def _row_matches(self, e: dict, q: str) -> bool:
        if not q:
            return True
        blob = " ".join(str(e.get(k, "")) for k in
                        ("feeder", "kind", "sensor", "collection_date",
                         "pilot_name", "system_name")).lower()
        blob += " " + " ".join(e.get("pilots") or []).lower()
        blob += " " + " ".join(e.get("systems") or []).lower()
        return q in blob

    def _refill(self):
        self.tree.delete(*self.tree.get_children())
        q = self.search_var.get().strip().lower()

        # Group: feeder -> date -> [entries]
        by_feeder: dict[str, dict[str, list[dict]]] = {}
        for e in self._entries:
            if not self._row_matches(e, q):
                continue
            by_feeder.setdefault(e["feeder"], {}) \
                     .setdefault(e["collection_date"], []).append(e)

        def _max_upd(nested_dates):
            return max(
                (e.get("updated_utc") or ""
                 for date_entries in nested_dates.values()
                 for e in date_entries),
                default="")

        # Sort feeders by most-recent activity desc
        feeders = sorted(by_feeder.items(),
                          key=lambda kv: _max_upd(kv[1]), reverse=True)
        shown = 0
        for i, (feeder, date_map) in enumerate(feeders):
            if i > 0:
                # Blank spacer row between feeders so the boundary is obvious.
                self.tree.insert("", "end", text="",
                                  values=("",) * len(self.ENTRY_COLS))
            feeder_node = self.tree.insert("", "end",
                                            text=f"{feeder}  ({len(date_map)} date(s))",
                                            open=True)
            # Sort dates within a feeder by date desc
            for date in sorted(date_map.keys(), reverse=True):
                entries = date_map[date]
                # Sort entries within date: sensor first, then base
                entries.sort(key=lambda e: (
                    0 if e.get("kind") == KIND_SENSOR else 1,
                    e.get("updated_utc") or ""
                ))
                date_node = self.tree.insert(feeder_node, "end",
                                              text=f"{date}  ({len(entries)} entry)",
                                              open=True)
                for e in entries:
                    total = int(e.get("total") or 0)
                    verified = int(e.get("verified") or 0)
                    if total == 0:
                        row_tag = "empty"
                        progress = "empty"
                    elif verified == total:
                        row_tag = "complete"
                        progress = f"{verified}/{total}"
                    else:
                        row_tag = "partial"
                        progress = f"{verified}/{total}"

                    # Base-station flag column (only meaningful for sensor rows)
                    if e.get("kind") == KIND_SENSOR:
                        base = self._base_index.get((feeder, date))
                        if not base:
                            base_text = "MISSING base"
                            # Override row color to hot-pink if the base is
                            # missing, so pilots see it before anything else.
                            row_tag = "missing_base"
                        else:
                            bt = int(base.get("total") or 0)
                            bv = int(base.get("verified") or 0)
                            if bt > 0 and bv == bt:
                                base_text = f"yes ({bv}/{bt})"
                            else:
                                base_text = f"partial ({bv}/{bt})"
                    else:
                        base_text = "-"

                    systems = ", ".join(e.get("systems") or [])
                    pilots_join = ", ".join(e.get("pilots") or [])
                    pilot = e.get("pilot_name") or ""
                    if pilots_join and pilots_join != pilot:
                        pilot = f"{pilot} (+ {pilots_join})"

                    iid = e["manifest_blob_path"]
                    self.tree.insert(date_node, "end", iid=iid,
                                      tags=(row_tag,), values=(
                        e.get("kind") or "",
                        e.get("sensor") or ("" if e.get("kind") == KIND_BASE else "?"),
                        pilot,
                        systems,
                        progress,
                        base_text,
                        _fmt_local_time(e.get("updated_utc") or ""),
                    ))
                    shown += 1
        self.status_var.set(
            f"{shown} manifest(s) across {len(feeders)} feeder(s)."
        )

    def _pick_dest(self):
        d = filedialog.askdirectory(title="Select destination folder")
        if d:
            self.dest_var.set(d)

    # ---- actions ----

    def _refresh_action_states(self):
        """Only enable Resume when EXACTLY ONE leaf with unfinished work is
        selected (take-over is per-manifest). Download tolerates any number."""
        entries = self._selected_entries(silent=True)
        can_resume = False
        if len(entries) == 1:
            e = entries[0]
            total = int(e.get("total") or 0)
            verified = int(e.get("verified") or 0)
            # Resume makes sense whenever there's something not yet verified.
            # total==0 also qualifies -- a placeholder cloud manifest with
            # no items can be taken over and repopulated.
            can_resume = (total == 0) or (verified < total)
        if can_resume:
            self.resume_btn.state(["!disabled"])
        else:
            self.resume_btn.state(["disabled"])

    def _selected_entries(self, silent: bool = False) -> list[dict]:
        """Return every leaf-entry currently selected (group headers are
        silently skipped since they aren't in _entries)."""
        entries: list[dict] = []
        for iid in self.tree.selection():
            e = next((e for e in self._entries
                      if e["manifest_blob_path"] == iid), None)
            if e:
                entries.append(e)
        if not entries and not silent:
            messagebox.showerror("Pick a row",
                                 "Select one or more leaf rows (not a feeder or date header).")
        return entries

    def _selected_entry(self) -> dict | None:
        entries = self._selected_entries()
        if not entries:
            return None
        if len(entries) > 1:
            messagebox.showerror(
                "One at a time",
                "Resume takes over one manifest at a time. Select just one row."
            )
            return None
        return entries[0]

    def _download_or_resume(self):
        # Double-click default = Download if the row is complete, else Resume.
        entry = self._selected_entry()
        if not entry:
            return
        if (entry.get("total") or 0) > 0 and \
                entry.get("verified") == entry.get("total"):
            self._download()
        else:
            self._resume()

    def _download(self):
        entries = self._selected_entries()
        if not entries:
            return
        dest = self.dest_var.get().strip()
        if not dest:
            messagebox.showerror("No destination",
                                 "Pick a destination folder first.")
            return
        dest_path = Path(dest)
        try:
            dest_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror("Bad destination", f"Cannot create: {e}")
            return

        loaded: list[dict] = []
        load_errors: list[str] = []
        for entry in entries:
            try:
                m = load_cloud_manifest_at(entry["manifest_blob_path"])
            except Exception as e:
                load_errors.append(f"{entry['manifest_blob_path']}: {e}")
                continue
            if not m:
                load_errors.append(f"{entry['manifest_blob_path']}: not found on cloud")
                continue
            loaded.append(m)
        if load_errors:
            messagebox.showwarning(
                "Some manifests could not be loaded",
                "The following selected manifests were skipped:\n\n" +
                "\n".join(load_errors),
            )
        if not loaded:
            return

        verified_total = sum(
            1 for m in loaded for it in m.get("items", {}).values()
            if it.get("status") == STATUS_VERIFIED
        )
        if verified_total == 0:
            messagebox.showerror("Nothing to download",
                                 "None of the selected manifests have verified items yet.")
            return
        preview_rows = "\n".join(
            f"  - {m.get('feeder')} / {m.get('kind')} / {m.get('collection_date')}  "
            f"({sum(1 for it in m.get('items', {}).values() if it.get('status') == STATUS_VERIFIED)}"
            f"/{len(m.get('items', {}))} verified)"
            for m in loaded
        )
        if not messagebox.askyesno(
            "Confirm download",
            f"Download {verified_total} verified item(s) across "
            f"{len(loaded)} manifest(s):\n\n{preview_rows}\n\n"
            f"Destination:\n{dest_path}\n\nOnly verified items are pulled. "
            f"Zips are md5-verified and unzipped in place.",
        ):
            return
        self.destroy()
        self.parent_app._start_download(loaded, dest_path)

    def _resume(self):
        entry = self._selected_entry()
        if not entry:
            return
        total = int(entry.get("total") or 0)
        verified = int(entry.get("verified") or 0)
        if total > 0 and verified >= total:
            messagebox.showerror(
                "Nothing to resume",
                "That upload is already complete -- every item is verified. "
                "Use Download instead if you want to pull it down.",
            )
            return
        TakeoverDialog(self, entry, on_ready=self._start_after_takeover)

    def _start_after_takeover(self, m: dict, path: Path):
        self.destroy()
        self.parent_app._enqueue([(m, path)])


# --- upload queue dialog -----------------------------------------------------


class QueueDialog(tk.Toplevel):
    """Manage the persistent upload queue: view, reorder, remove, and
    start processing. Each row shows the local manifest's identity plus a
    per-manifest data-availability check (drive present? sources on
    disk?) so a queue item added yesterday can be flagged today if the
    drive is disconnected. Start Processing re-checks availability just
    before handing manifests to the upload session; anything with no
    reachable data is skipped with a warning listing what was skipped."""

    COLS = ("pos", "feeder", "kind", "sensor", "date", "pilot",
            "progress", "sources", "path")

    def __init__(self, parent: App):
        super().__init__(parent)
        self.title("Upload queue")
        self.geometry("1120x620")
        self.parent_app = parent
        self.queue = parent.queue

        self.transient(parent)
        self.lift()
        self.attributes("-topmost", True)
        self.after(300, lambda: self.attributes("-topmost", False))
        self.focus_force()
        self.grab_set()

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text=f"Queue file:  {self.queue.path}").pack(anchor="w")

        info = tk.Message(
            self, width=1080, padx=8, pady=4,
            text=(
                "The queue uploads manifests in the order shown. Use Up "
                "/ Down to reorder, Remove to drop an item, Refresh to "
                "re-check whether the source data is reachable right now. "
                "On Start Uploads, each item's data availability is "
                "re-checked one more time; items whose sources aren't "
                "present (drive disconnected, files moved) are skipped "
                "and the queue continues with the next item."
            ),
            foreground="#555555",
        )
        info.pack(fill="x", padx=8, pady=(0, 4))

        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill="both", expand=True, padx=8, pady=6)
        self.tree = ttk.Treeview(tree_frame, columns=self.COLS,
                                  show="headings", selectmode="browse",
                                  height=14)
        widths = (40, 90, 70, 70, 100, 130, 90, 130, 260)
        for col, w in zip(self.COLS, widths):
            self.tree.heading(col, text=col.title())
            self.tree.column(col, width=w, anchor="w")
        vsb = ttk.Scrollbar(tree_frame, orient="vertical",
                             command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("complete", background="#c9f2c9")
        self.tree.tag_configure("partial", background="#fff2c9")
        self.tree.tag_configure("no_data", background="#fddede")
        self.tree.tag_configure("missing_file", background="#f2c9c9")

        btns = ttk.Frame(self, padding=8)
        btns.pack(fill="x")
        ttk.Button(btns, text="Move up", command=lambda: self._move(-1)).pack(side="left")
        ttk.Button(btns, text="Move down", command=lambda: self._move(1)).pack(side="left", padx=6)
        ttk.Button(btns, text="Remove selected", command=self._remove).pack(side="left", padx=6)
        ttk.Button(btns, text="Refresh", command=self._reload).pack(side="left", padx=6)
        ttk.Button(btns, text="Start uploads", command=self._start).pack(side="left", padx=18)
        ttk.Button(btns, text="Close", command=self.destroy).pack(side="right")

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var).pack(anchor="w", padx=8, pady=(0, 6))

        self._reload()

    def _reload(self):
        self.tree.delete(*self.tree.get_children())
        paths = self.queue.paths()
        n_ok = 0
        for i, p in enumerate(paths, start=1):
            pp = Path(p)
            if not pp.exists():
                self.tree.insert("", "end", iid=p, tags=("missing_file",),
                                 values=(i, "?", "?", "?", "?", "?",
                                          "?", "manifest file missing", p))
                continue
            try:
                m = load_local_manifest(pp)
            except Exception as e:
                self.tree.insert("", "end", iid=p, tags=("missing_file",),
                                 values=(i, "?", "?", "?", "?", "?",
                                          "?", f"load failed: {e}", p))
                continue
            items = m.get("items", {}) or {}
            avail = manifest_availability(m)
            total = len(items)
            verified = avail["verified"]
            if total == 0:
                progress = "empty"
                row_tag = "no_data"
            elif verified == total:
                progress = f"{verified}/{total}"
                row_tag = "complete"
            else:
                progress = f"{verified}/{total}"
                row_tag = "partial"
            if avail["pending"] > 0 and avail["present"] == 0:
                sources = f"NONE present (0/{avail['pending']})"
                row_tag = "no_data"
            elif avail["pending"] > 0 and avail["missing"] > 0:
                sources = f"partial ({avail['present']}/{avail['pending']})"
                if row_tag != "no_data":
                    row_tag = "partial"
                n_ok += 1
            elif avail["pending"] == 0:
                sources = "-"
                n_ok += 1
            else:
                sources = f"all present ({avail['present']}/{avail['pending']})"
                n_ok += 1
            self.tree.insert("", "end", iid=p, tags=(row_tag,), values=(
                i,
                m.get("feeder", "?"),
                m.get("kind", "?"),
                m.get("sensor") or "-",
                m.get("collection_date", "?"),
                m.get("pilot_name") or "?",
                progress,
                sources,
                p,
            ))
        self.status_var.set(
            f"{len(paths)} item(s) in queue; {n_ok} ready to process."
        )

    def _selected_path(self) -> str | None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showerror("Pick a row", "Select a queue row first.")
            return None
        return sel[0]

    def _move(self, delta: int):
        p = self._selected_path()
        if not p:
            return
        self.queue.move(p, delta)
        self.parent_app._refresh_queue_count()
        self._reload()
        # keep the moved row selected so the pilot can spam the button
        if p in self.tree.get_children():
            self.tree.selection_set(p)
            self.tree.see(p)

    def _remove(self):
        p = self._selected_path()
        if not p:
            return
        if not messagebox.askyesno(
            "Remove from queue",
            f"Remove this manifest from the upload queue?\n\n{p}\n\n"
            f"The manifest file on disk is NOT deleted."
        ):
            return
        self.queue.remove(p)
        self.parent_app._refresh_queue_count()
        self._reload()

    def _start(self):
        # Re-check availability at start-time (drive may have popped in/out
        # since Reload). Load each manifest; skip anything missing or with
        # zero reachable sources.
        ready: list[tuple[dict, Path]] = []
        skipped: list[str] = []
        for p in self.queue.paths():
            pp = Path(p)
            if not pp.exists():
                skipped.append(f"{p}: manifest file missing")
                continue
            try:
                m = load_local_manifest(pp)
            except Exception as e:
                skipped.append(f"{p}: load failed ({e})")
                continue
            items = m.get("items", {}) or {}
            if items and all(it.get("status") == STATUS_VERIFIED for it in items.values()):
                # already complete; drop it from the queue silently
                self.queue.remove(p)
                self.parent_app._refresh_queue_count()
                continue
            avail = manifest_availability(m)
            if avail["pending"] > 0 and avail["present"] == 0:
                skipped.append(
                    f"{p}: no source data reachable "
                    f"(0/{avail['pending']} present)"
                )
                continue
            ready.append((m, pp))
        if skipped:
            messagebox.showwarning(
                "Some items will be skipped",
                "These queue items will be skipped this run:\n\n" +
                "\n".join(f"  - {s}" for s in skipped) +
                "\n\nThey stay in the queue and can be re-tried later."
            )
        if not ready:
            messagebox.showerror(
                "Nothing to process",
                "No queue items have reachable data right now."
            )
            return
        if not messagebox.askyesno(
            "Confirm uploads",
            f"Upload {len(ready)} queue item(s) now?\n\nThe upload session "
            f"will run them in order; you can Stop at any time."
        ):
            return
        self.destroy()
        self.parent_app._process_queue(ready)


class TakeoverDialog(tk.Toplevel):
    """Show the manifest header, ask for a local source root + pilot name,
    then adopt any pending items that match under that root and start the
    session in resume mode. Items whose sources still can't be found stay
    marked with their original system_name and get skipped by the zipper
    ('owned by other system')."""

    def __init__(self, parent, entry: dict, on_ready):
        super().__init__(parent)
        self.title("Take over upload")
        self.geometry("720x360")
        self.on_ready = on_ready
        self.entry = entry

        self.transient(parent)
        self.lift()
        self.attributes("-topmost", True)
        self.after(200, lambda: self.attributes("-topmost", False))
        self.focus_force()
        self.grab_set()

        header = ttk.LabelFrame(self, text="Manifest")
        header.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(header, text=f"Feeder:  {entry.get('feeder')}").pack(anchor="w", padx=6)
        ttk.Label(header, text=f"Kind:  {entry.get('kind')}").pack(anchor="w", padx=6)
        ttk.Label(header, text=f"Collection date:  {entry.get('collection_date')}").pack(anchor="w", padx=6)
        ttk.Label(header, text=f"Progress:  {entry.get('verified')}/{entry.get('total')} verified").pack(anchor="w", padx=6)
        ttk.Label(header, text=f"Original pilot:  {entry.get('pilot_name')}").pack(anchor="w", padx=6)
        ttk.Label(header, text=f"Manifest path:  {entry.get('manifest_blob_path')}").pack(anchor="w", padx=6, pady=(0, 4))

        form = ttk.Frame(self, padding=10)
        form.pack(fill="x")
        ttk.Label(form, text="Your name").grid(row=0, column=0, sticky="w", pady=4)
        self.pilot_var = tk.StringVar()
        AutocompleteCombobox(form, VALID_PILOTS, textvariable=self.pilot_var,
                             state="normal").grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Label(form, text="Local source root").grid(row=1, column=0, sticky="w", pady=4)
        self.src_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.src_var).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(form, text="Browse", command=self._browse).grid(row=1, column=2)
        form.columnconfigure(1, weight=1)
        ttk.Label(form, foreground="#666666", wraplength=680,
                  text=("For sensor manifests, point at the folder that "
                        "contains the mission subfolders (e.g. the SD card "
                        "root). For base station, point at the folder that "
                        "contains the .dat files. Items whose folder / file "
                        "isn't found under this root will be left untouched; "
                        "you can Take over later from another machine.")
                  ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        btns = ttk.Frame(self, padding=10)
        btns.pack(fill="x")
        ttk.Button(btns, text="Take over and resume", command=self._go).pack(side="right")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=6)

    def _browse(self):
        d = filedialog.askdirectory(title="Select the folder that contains the mission subfolders / .dat files")
        if d:
            self.src_var.set(d)

    def _go(self):
        pilot = self.pilot_var.get().strip()
        if not pilot:
            messagebox.showerror("Missing pilot", "Enter your name first.")
            return
        src = self.src_var.get().strip()
        try:
            m = load_cloud_manifest_at(self.entry["manifest_blob_path"])
        except Exception as e:
            messagebox.showerror("Load failed", str(e))
            return
        if not m:
            messagebox.showerror("Not found",
                                 "That manifest no longer exists on the cloud.")
            return
        adopted = _adopt_items(m, Path(src) if src else None, pilot)
        m["pilot_name"] = pilot  # top-level = new writer
        # Fresh top-level session_id so tracker + Am-I-Good-To-Delete? see
        # THIS pilot's slice; existing items retain their original per-item
        # session_id / pilot_name for provenance.
        m["session_id"] = str(uuid.uuid4())
        path = default_local_path(m["feeder"], m["kind"], m["collection_date"], m["session_id"])
        save_local_manifest(m, path)
        n_left = sum(1 for it in m["items"].values()
                     if it.get("status") != STATUS_VERIFIED)
        messagebox.showinfo(
            "Ready to resume",
            f"Adopted {adopted} of {n_left} unfinished item(s) at:\n{src or '(none)'}\n\n"
            f"Local manifest saved to:\n{path}\n\n"
            f"Starting the upload session now."
        )
        self.destroy()
        self.on_ready(m, path)


def _adopt_items(m: dict, source_root: Path | None, current_pilot: str) -> int:
    """Rewrite item paths in ``m`` to point under ``source_root`` where a
    matching name is found there, and update system_name to the current
    machine so the session zipper actually picks them up. Returns the
    number of items adopted. Verified items are not touched."""
    if source_root is None or not source_root.exists():
        return 0
    if m["kind"] == KIND_SENSOR:
        candidates = {p.name: p for p in source_root.iterdir() if p.is_dir()}
    else:
        candidates = {p.name: p for p in source_root.iterdir() if p.is_file()}
    current_system = get_system_name()
    adopted = 0
    for name, item in m["items"].items():
        if item.get("status") == STATUS_VERIFIED:
            continue
        match = candidates.get(name)
        if not match:
            continue
        item["original_path"] = str(match)
        item["system_name"] = current_system
        item["disk_name"] = get_disk_name(match)
        adopted += 1
    return adopted


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
