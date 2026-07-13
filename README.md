# UU LiDAR Upload Tool

A Windows GUI (Tkinter) tool for drone pilots to upload LiDAR data to the
`wpbstorageaccount / lidardata` Azure Blob Storage container, using a
portable `azcopy.exe` bundled alongside the script.

## Layout produced on blob

```
lidardata/{client}/{program}/{feeder}/
    BASE_STATION/{YYYY-MM-DD}/*.dat
    SENSOR_DATA/{YYYY-MM-DD}/manifest.json
    SENSOR_DATA/{YYYY-MM-DD}/{DJI_...}.zip
    CLIENT_DELIEVERABLES/
```

Currently supported:

- **client**: `xcel`
- **program**: `2026`
- **sensor**: `L3` (TV540 / TVGO disabled)
- **data type**: `RAW_SENSOR` (DJI mission folders); `PROCESSED_SENSOR`
  (flat `.las` / `.laz`) is experimental.

## Setup

1. Drop `lidar_upload.py` and `azcopy.exe` into the same folder.
2. Open `lidar_upload.py` and paste your container SAS token into the
   `SAS_TOKEN = "?PASTE_YOUR_SAS_TOKEN_HERE"` line at the top.
   The token must grant read + write + create + list on the `lidardata` container.
3. Run: `python lidar_upload.py` (Python 3.10+; Windows only).

## Flow

1. **Start**: choose *New upload* or *Resume from local manifest*.
2. **New upload**: pick client / program, type the feeder name and re-type
   to confirm, pick sensor (L3), pick the sensor source folder and
   (optionally) a base station folder, and press **Scan / validate**.
   Any DJI mission folders with names that don't match
   `DJI_YYYYMMDDHHMM_SEQ_FEEDER` are listed for you to fix.
3. **Start upload**: opens a confirmation dialog with the feeder name.
   The tool then, per date:
   - Creates / merges a `SENSOR_DATA/{date}/manifest.json` on blob.
   - Zips missions one at a time (background thread) and uploads them
     one at a time (foreground thread) so the pipe stays saturated.
   - After each mission: verifies blob MD5, updates the manifest, and
     re-uploads the manifest.
   - Retries with backoff (X local per session, Y global across sessions,
     both configurable in Advanced options).
4. **Resume**: pick the local `lidar_session_*.json` file. Fix any drive
   letter changes with a single "old prefix -> new prefix" remap.

## Advanced options

- Local retries (X) per mission per session (missions that exhaust
  X retries stay in the manifest and will be retried on the next
  session; there is no permanent give-up cap)
- azcopy per-call timeout (T seconds)
- Zip scratch directory (blank = system temp)
- Whether to write tiny `README.txt` placeholders so empty feeder
  subdirectories persist in the flat namespace

## Notes / caveats

- **Double-circuit feeders** (e.g. `LCO071` and `LCO072`) are not
  elegantly handled. Every mission gets filed under exactly one feeder.
  Split or duplicate outside this tool if you need both.
- **Base station folders** must contain only `.dat` and `latest_index`;
  other files are refused with warnings.
- Manifest upload failure is treated as fatal (per spec) and stops the
  session so the cloud never diverges from local state.

## Files

- `lidar_upload.py` -- the tool
- `azcopy.exe` -- bring your own, place next to the script
