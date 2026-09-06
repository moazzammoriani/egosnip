# EgoSnip V1

A local, browser-based tool for reviewing one GoPro recording once, marking any number of independent task clips, losslessly stream-copying them, and exporting synchronized ACCL/GYRO CSV files derived from the untouched source recording.

FastAPI stores browser-uploaded MP4s in identity-aware directories beneath `MEDIA_DIR/sources/` and also discovers existing MP4s directly inside `MEDIA_DIR`. By default this is the dedicated `media/` directory inside the project, rather than the repository root.

## Requirements

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/)
- `ffmpeg` and `ffprobe` on `PATH`
- A browser

Python dependencies, including `telemetrik`, are installed by `uv`.

## Start the app

From this repository:

```bash
uv sync
uv run python -m app
```

For development with reload:

```bash
MEDIA_DIR=/absolute/path/to/media uv run uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>. `python -m app` prints the URL at startup and creates `/path/to/egosnip/media/`. Set an absolute `MEDIA_DIR` only when you want the library somewhere else.

Use **Upload MP4s** or drop multiple MP4s onto the Media Library panel. Each raw video body is streamed to one temporary file, checked with FFprobe, assigned a physical device and recording identity from its original GoPro metadata, and atomically moved to `MEDIA_DIR/sources/<DEVICE_ID>/<SOURCE_ID>/<ORIGINAL_FILENAME>`. Same-named files from different cameras can coexist; existing files are never overwritten. Existing MP4s directly inside `MEDIA_DIR` are also discovered. Backend-generated IDs are used in URLs, and client-provided filesystem paths are never accepted.

## Operator workflow

1. Select an existing project or click **New Project**. Before this choice, all media, player, clip, and export controls remain hidden. The creation dialog defaults to `New Project`, stored on disk as `new_project`.
2. Upload/drop MP4s or select an existing source recording. Selecting a source assigns it to the active human-readable Project Name. The first selection queues one cached 720p H.264/30 fps/AAC proxy; later selections reuse it.
3. Play or scrub, enter a free-text task label, and set IN/OUT. Timestamps can also be typed as seconds, `MM:SS.mmm`, or `HH:MM:SS.mmm`.
4. Add as many clips as needed. Existing clips can be selected, jumped to, edited, or deleted. Deleting one never renumbers the others, and deleted high numbers are not reused in the project.
5. Optionally enable **Retain one pre-zero IMU sample** for the whole source project.
6. Export all clips. The UI polls the batch job and shows each clip as exporting, PASS, or FAIL. A failure does not stop later clips.

Shorter-than-two-minute clips show a warning but remain exportable for testing.

Keyboard shortcuts work unless focus is in an input, textarea, select, or editable element:

| Key | Action |
| --- | --- |
| Space | Play/pause |
| I | Set IN |
| O | Set OUT |
| Enter | Add/save current interval |
| Left / Right | Seek backward/forward one second |

## Output and persistence

Project JSON and expensive per-source artifacts are stored under:

```text
MEDIA_DIR/.snipper_cache/<SOURCE_ID>_<FILE_ID_PREFIX>/
├── project.json
├── proxy.mp4
├── source_metadata.json
├── telemetry_metadata.json
├── accelerometer.json.gz
└── gyroscope.json.gz
```

GoPro device and source identity metadata lives at:

```text
<repository>/devices.json
MEDIA_DIR/.egosnip/source_identities/
```

`devices.json` is intentionally version-controlled so another EgoSnip deployment receives the same serial-to-`GP-` assignments. The adjacent `devices.json.lock` is runtime-only and ignored. Coordinate registry changes through source control before onboarding new cameras on multiple installations; Git does not provide live allocation coordination.

EgoSnip reads the normalized `CASN` camera serial from the original MP4's GoPro GPMF header. A persistent registry assigns the first camera `GP-000001`, the next `GP-000002`, and so on. If CASN cannot be read, the source remains unresolved and cannot be exported. The recording ID is the first eight uppercase hex characters of a SHA-256 fingerprint built from stable GoPro metadata, file size, and fixed first/last source chunks—not filesystem mtime. For example:

```text
device_id    GP-000001
recording_id A72F91C3
source_id    GP-000001_GX010005_A72F91C3
clip_id      GP-000001_GX010005_A72F91C3_001
```

The two compressed sensor caches are generated together in one `telemetrik.extract_all_telemetry(..., streams=["ACCL", "GYRO"])` call against the original MP4. A source size/mtime fingerprint invalidates metadata, proxy, and telemetry caches when the source changes. Every clip reuses this master timeline.

Exports use:

```text
MEDIA_DIR/exports/claru_textile_pilot/
└── GP-000001_GX010005_A72F91C3/
    └── GP-000001_GX010005_A72F91C3_001/
        ├── GP-000001_GX010005_A72F91C3_001.mp4
        ├── GP-000001_GX010005_A72F91C3_001_imu.csv
        └── GP-000001_GX010005_A72F91C3_001.json
```

The existing-project selector is populated from valid slug folders directly beneath `MEDIA_DIR/exports/`. A hidden `.project.json` inside each newly created namespace preserves its human-readable name; existing folders without that metadata receive a title-cased display name derived from the slug. The source assignment remains in its per-source `project.json`.

Project slugs are ASCII-normalized, lowercase, underscore-separated, and capped at 80 characters. For example, `Claru Textile Pilot` becomes `claru_textile_pilot`. Sources using the same Project Name export beneath the same namespace. Changing the name affects future exports only.

The full task label remains in `project.json`, the browser UI, and the clip manifest, but it is never used in export paths. Editing a task label therefore does not change the stable `<SOURCE_ID>_<NNN>` directory or filenames.

### One-time legacy identity migration

To move pre-device-ID caches, project state, and exports into the current naming convention, stop the server and run:

```bash
uv run python -m app.migrate_identity --media-dir media
```

The migration renames cache/export directories and artifacts, updates project and manifest identity fields, and preserves task labels. Exported MP4 and CSV payloads are moved without being rewritten. It refuses destination collisions, ambiguous manifests, unexpected artifacts, and symlinked legacy paths.

## FFmpeg stream-copy pattern

Indices come from FFprobe; none are hard-coded and `-map 0` is never used:

```bash
ffmpeg -y \
  -ss START \
  -i SOURCE.MP4 \
  -map 0:VIDEO_INDEX \
  -map 0:AUDIO_INDEX \
  -map 0:GPMD_INDEX \
  -c copy \
  -copy_unknown \
  -tag:d:0 gpmd \
  -t DURATION \
  OUTPUT.MP4
```

The audio and `gpmd` mappings are omitted when absent. The source is never an output target. FFmpeg's negative, discard-marked video preroll packets are preserved as normal stream-copy decoder support; the app does not snap the operator's requested point to a keyframe or rewrite the GOP.

## Synchronization details

For requested start `S`, the backend asks FFprobe for decoded video frames in a small interval around `S`, reads `best_effort_timestamp_time` (falling back to `pts_time`), and chooses the earliest displayed source frame timestamp at or after `S`. That timestamp is `T0`; it is not computed from nominal FPS.

After export, the backend reads the output **video stream** duration `Dv`, not container duration. The represented source interval is `[T0, T0 + Dv)`. Original `pts_data` ACCL and GYRO samples in that half-open interval are retained and rebased as:

```text
clip_timestamp = source_sample_timestamp - T0
```

The first positive sample is not forced to zero. With pre-zero mode enabled, each sensor additionally receives exactly the closest original sample below `T0`; its genuinely negative relative timestamp is preserved. It is never clamped, and no other negative sample is kept.

ACCL and GYRO are combined only when counts match and every corresponding timestamp differs by no more than 0.1 ms. The combined timestamp column uses the ACCL timestamp and the manifest records that fact plus the maximum clock delta. No interpolation occurs. Otherwise, separate accelerometer and gyroscope CSV files are written.

## QC and manifests

Every successful FFmpeg cut receives a manifest containing schema version, Project Name and slug, device ID, camera serial/model/firmware, recording/source/clip identity, requested and actual timing, unmodified task label, sensor metadata/counts/rates, codecs, resolution, GPMD state, CSV layout, and individual QC checks. QC covers stream existence/preservation, codec and resolution equality, plausible duration, stream-copy use, sensor presence, strict monotonicity, interval bounds, negative-sample count, and verification that a retained pre-zero sample is the closest source sample.

If FFmpeg itself fails, a failure manifest is still written. If source GPMD or external telemetry is unavailable, the video is still stream-copied but QC is FAIL with the reason shown in the UI.

## Tests

```bash
uv run pytest -q
```

The normal suite uses synthetic data and a generated H.264/AAC MP4. If `GX010005.MP4` exists directly in `MEDIA_DIR`, the marked integration test also checks the known 120-second HERO13 frame/IMU boundary:

```bash
MEDIA_DIR=/path/to/media uv run pytest -q -m integration
```

## V1 limitations and assumptions

- Export jobs are in memory; restarting the server loses live job progress, but already written files and project JSON remain.
- Browser upload copies the source into `MEDIA_DIR`; this needs enough free space for the selected file and its library copy, but the server does not create an additional multipart spool copy.
- Embedded GPMF is preserved as-is. Boundary packet overlap is intentionally not rewritten; clean external IMU comes from the original master timeline.
- Physical sensor-axis orientation is not independently validated. Parser axis order, sensor name, and units are preserved and documented.
- V1 assumes source video PTS corresponds to the source/proxy review timeline, as on the validated HERO13 files. Sources with unusual non-zero edit-list offsets should be checked before production use.
- There is no authentication because the service is intended for loopback/local use only.
- Eight hex characters are used for the operator-facing recording ID. EgoSnip retains a longer internal fingerprint and blocks rather than overwrites if the short ID collides.
- Sequential `GP-` numbers are coordinated only by `<repository>/devices.json`; separate installations need a shared registry if globally consistent numbering is required.
