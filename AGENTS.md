# Repository Guidelines

## Project Structure & Module Organization

- `app/` contains the FastAPI backend. `main.py` defines HTTP endpoints and background jobs; `media.py` handles safe file discovery, FFprobe, and proxies; `export.py` performs stream-copy cuts and QC; `telemetry.py` extracts/caches IMU data; `projects.py` persists project JSON; `models.py` defines Pydantic schemas.
- `static/` contains the vanilla HTML, CSS, and JavaScript interface served by FastAPI.
- `tests/` contains pytest unit, synthetic-media, and optional GoPro integration tests.
- `media/` is the default runtime library. Its recordings, `.snipper_cache/`, and `exports/` are generated data and must not be committed.

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

Use four-space indentation and type hints in Python. Prefer small, direct modules and subprocess argument arrays; never build shell commands from filenames. Use `snake_case` for Python names, `camelCase` for browser JavaScript, and descriptive DOM IDs. Clip output names follow `<SOURCE_ID>_<NNN>_<sanitized_task_label>`. No formatter is currently enforced, so keep changes consistent with surrounding code and run Python compilation plus tests.

## Testing Guidelines

Use pytest and name files/functions `test_*.py`. Keep normal tests independent of real GoPro footage by using synthetic samples or generated videos. Mark hardware fixtures with `@pytest.mark.integration` and skip cleanly when absent. Timing tests must cover half-open `[T0, T1)` slicing, stable IDs, pre-zero behavior, dynamic stream detection, manifests, and QC.

## Commit & Pull Request Guidelines

No established commit convention exists in the current history. Use short imperative subjects, such as `Add deduplicated proxy jobs`. Keep commits focused. Pull requests should explain operator-visible behavior, list verification commands, note cache/output compatibility, and include screenshots for UI changes. Link relevant issues and call out any changes affecting source-file safety or telemetry synchronization.

## Security & Data Safety

Never modify source MP4s. Resolve paths beneath `MEDIA_DIR`, reject overwrites, and keep uploads atomic. Do not commit recordings, proxies, telemetry caches, exports, or machine-specific paths.
