# Repository Guidelines

## Project Structure & Module Organization

- `app/` contains the FastAPI backend. `main.py` defines HTTP endpoints and background jobs; `identity.py` parses GoPro identity and maintains the device registry; `migrate_identity.py` is the explicit one-time legacy naming migration; `media.py` handles source storage/discovery, FFprobe, and proxies; `export.py` performs stream-copy cuts and QC; `telemetry.py` extracts/caches IMU data; `projects.py` persists per-source state and catalogs export-project folders; `models.py` defines Pydantic schemas.
- `static/` contains the vanilla HTML, CSS, and JavaScript interface served by FastAPI.
- `tests/` contains pytest unit, synthetic-media, and optional GoPro integration tests.
- `devices.json` is the repository-level physical-camera registry and is version-controlled so deployments share stable `GP-` assignments. `devices.json.lock` is transient and ignored. `media/` is the default runtime library; its `sources/`, `.egosnip/`, `.snipper_cache/`, and `exports/` contents are generated data and must not be committed.

## Build, Test, and Development Commands

```bash
uv sync
uv run python -m app
uv run uvicorn app.main:app --reload
uv run pytest -q
MEDIA_DIR=media uv run pytest -q -m integration
```

`uv sync` creates the Python environment from `pyproject.toml` and `uv.lock`. The application defaults to `media/` and listens on `http://127.0.0.1:8000`. Use `MEDIA_DIR=/absolute/path` to select another library. FFmpeg and FFprobe must be available on `PATH`.

## Coding Style & Naming Conventions

Use four-space indentation and type hints in Python. Prefer small, direct modules and subprocess argument arrays; never build shell commands from filenames. Use `snake_case` for Python names, `camelCase` for browser JavaScript, and descriptive DOM IDs. Source IDs follow `<DEVICE_ID>_<ORIGINAL_STEM>_<RECORDING_ID>`; clip output names append `<NNN>` beneath `exports/<project_slug>/<SOURCE_ID>/`. No formatter is currently enforced, so keep changes consistent with surrounding code and run Python compilation plus tests.

## Testing Guidelines

Use pytest and name files/functions `test_*.py`. Keep normal tests independent of real GoPro footage by using synthetic samples or generated videos. Mark hardware fixtures with `@pytest.mark.integration` and skip cleanly when absent. Timing tests must cover half-open `[T0, T1)` slicing, stable IDs, pre-zero behavior, dynamic stream detection, manifests, and QC.

## Commit & Pull Request Guidelines

No established commit convention exists in the current history. Use short imperative subjects, such as `Add deduplicated proxy jobs`. Keep commits focused. Pull requests should explain operator-visible behavior, list verification commands, note cache/output compatibility, and include screenshots for UI changes. Link relevant issues and call out any changes affecting source-file safety or telemetry synchronization.

## Security & Data Safety

Never modify source MP4s. Resolve paths beneath `MEDIA_DIR`, reject overwrites, and keep uploads atomic. Do not commit recordings, proxies, telemetry caches, exports, or machine-specific paths.
