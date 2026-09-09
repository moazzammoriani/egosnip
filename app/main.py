from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .export import ClipExporter
from .identity import IdentityError
from .media import MediaError, MediaLibrary, configured_media_dir, probe_media
from .models import CreateProjectRequest, ExportRequest, ProjectState
from .projects import ExportProjectCatalog, ProjectStore, project_slug
from .telemetry import TelemetryCache, TelemetryError


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class ExportJobs:
    def __init__(self, library: MediaLibrary, projects: ProjectStore):
        self.library = library
        self.projects = projects
        self.telemetry = TelemetryCache(library)
        self.exporter = ClipExporter(library, self.telemetry)
        self._jobs: dict[str, dict[str, Any]] = {}
        self._active_by_source: dict[str, str] = {}
        self._lock = threading.Lock()

    def start(self, source_file_id: str, project: ProjectState, clip_ids: list[str] | None) -> str:
        source = self.library.get(source_file_id)
        project_slug(project.project_name)
        selected = [clip for clip in project.clips if clip_ids is None or clip.id in clip_ids]
        if clip_ids is not None:
            missing = set(clip_ids) - {clip.id for clip in selected}
            if missing:
                raise ValueError(f"Unknown clip IDs: {', '.join(sorted(missing))}")
        if not selected:
            raise ValueError("The project has no selected clips to export")
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "source_id": source.source_id,
            "state": "queued",
            "message": "Queued",
            "completed": 0,
            "total": len(selected),
            "clips": [
                {
                    "clip_id": clip.id,
                    "clip_index": clip.clip_index,
                    "task_label": clip.task_label,
                    "status": "pending",
                    "message": "Pending",
                }
                for clip in selected
            ],
        }
        with self._lock:
            active_id = self._active_by_source.get(source_file_id)
            if active_id is not None:
                active = self._jobs.get(active_id)
                if active and active["state"] in {"queued", "running"}:
                    return active_id
            self._jobs[job_id] = job
            self._active_by_source[source_file_id] = job_id
        worker = threading.Thread(
            target=self._run,
            args=(job_id, source_file_id, project, selected),
            daemon=True,
            name=f"export-{job_id[:8]}",
        )
        worker.start()
        return job_id

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(job_id)
            job = self._jobs[job_id]
            return {
                **{key: value for key, value in job.items() if key != "clips"},
                "clips": [dict(item) for item in job["clips"]],
            }

    def _update(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            self._jobs[job_id].update(changes)

    def _update_clip(self, job_id: str, clip_id: str, **changes: Any) -> None:
        with self._lock:
            item = next(row for row in self._jobs[job_id]["clips"] if row["clip_id"] == clip_id)
            item.update(changes)

    def _run(self, job_id: str, source_file_id: str, project: ProjectState, clips: list[Any]) -> None:
        try:
            source = self.library.get(source_file_id)
            metadata = self.library.metadata(source)
            master = None
            telemetry_error = None
            self._update(
                job_id,
                state="running",
                message="Extracting master IMU (first export can take a few minutes)",
            )
            if not metadata.get("gpmd_present"):
                telemetry_error = "Source has no gpmd stream; external ACCL/GYRO telemetry is unavailable"
            else:
                try:
                    master = self.telemetry.load_or_extract(source)
                except TelemetryError as exc:
                    telemetry_error = str(exc)

            for position, clip in enumerate(clips, start=1):
                self._update(
                    job_id,
                    message=f"Exporting {position} / {len(clips)}",
                )
                self._update_clip(job_id, clip.id, status="exporting", message="Exporting")
                try:
                    result = self.exporter.export_clip(
                        source,
                        project,
                        clip,
                        master=master,
                        telemetry_error=telemetry_error,
                    )
                except Exception as exc:
                    message = str(exc)
                    result = self.exporter.write_failure_manifest(source, project, clip, message)
                self._update_clip(
                    job_id,
                    clip.id,
                    status=result["status"],
                    message=result["message"],
                    output_dir=result["output_dir"],
                )
                self._update(job_id, completed=position)
            final_job = self.get(job_id)
            failures = sum(item["status"] == "FAIL" for item in final_job["clips"])
            message = "All clips passed QC" if failures == 0 else f"Finished with {failures} failed clip(s)"
            self._update(job_id, state="complete", message=message)
        except Exception as exc:
            self._update(job_id, state="failed", message=str(exc))
        finally:
            with self._lock:
                if self._active_by_source.get(source_file_id) == job_id:
                    del self._active_by_source[source_file_id]


class ProxyJobs:
    """Deduplicated, single-worker proxy queue with observable progress."""

    def __init__(self, library: MediaLibrary):
        self.library = library
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="proxy")

    def start(self, file_id: str) -> dict[str, Any]:
        source = self.library.get(file_id)
        if self.library.proxy_ready(source):
            return self._ready(source.file_id)
        with self._lock:
            current = self._jobs.get(file_id)
            if current and current["state"] in {"queued", "generating"}:
                return dict(current)
            job = {
                "source_file_id": file_id,
                "state": "queued",
                "progress": 0.0,
                "message": "Queued for proxy generation",
            }
            self._jobs[file_id] = job
        self._executor.submit(self._run, file_id)
        return dict(job)

    def get(self, file_id: str) -> dict[str, Any]:
        source = self.library.get(file_id)
        if self.library.proxy_ready(source):
            return self._ready(file_id)
        with self._lock:
            return dict(self._jobs.get(file_id, {
                "source_file_id": file_id,
                "state": "missing",
                "progress": 0.0,
                "message": "Proxy has not been generated",
            }))

    @staticmethod
    def _ready(file_id: str) -> dict[str, Any]:
        return {
            "source_file_id": file_id,
            "state": "ready",
            "progress": 1.0,
            "message": "Proxy ready",
            "url": f"/api/sources/{file_id}/proxy",
        }

    def _update(self, file_id: str, **changes: Any) -> None:
        with self._lock:
            self._jobs[file_id].update(changes)

    def _run(self, file_id: str) -> None:
        try:
            source = self.library.get(file_id)
            self._update(file_id, state="generating", message="Generating review proxy")

            def progress(value: float) -> None:
                self._update(
                    file_id,
                    progress=value,
                    message=f"Generating review proxy · {value * 100:.0f}%",
                )

            self.library.ensure_proxy(source, progress)
            self._update(file_id, **self._ready(file_id))
        except Exception as exc:
            self._update(file_id, state="failed", message=str(exc))


def create_app(media_dir: Path | None = None) -> FastAPI:
    library = MediaLibrary(configured_media_dir(media_dir))
    projects = ProjectStore()
    project_catalog = ExportProjectCatalog(library.exports_root)
    jobs = ExportJobs(library, projects)
    proxy_jobs = ProxyJobs(library)
    app = FastAPI(title="EgoSnip", version="0.1.0")
    app.state.library = library
    app.state.projects = projects
    app.state.jobs = jobs
    app.state.proxy_jobs = proxy_jobs
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def source_or_404(file_id: str):
        try:
            return library.get(file_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown source ID")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/files")
    def list_files() -> dict[str, Any]:
        files = []
        errors = []
        for source in library.sources():
            try:
                files.append(library.public_metadata(source))
            except MediaError as exc:
                errors.append({"filename": source.filename, "error": str(exc)})
        errors.extend(library.discovery_errors)
        return {"files": files, "errors": errors, "media_dir": os.fspath(library.media_dir)}

    @app.post("/api/uploads", status_code=201)
    async def upload_media(request: Request, filename: str) -> dict[str, Any]:
        if (
            not filename
            or filename in {".", ".."}
            or Path(filename).name != filename
            or Path(filename).suffix.lower() != ".mp4"
        ):
            raise HTTPException(status_code=422, detail="Only .mp4 files can be uploaded")
        uploads_dir = library.media_dir / ".uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        partial = uploads_dir / f"{uuid.uuid4().hex}.partial"
        try:
            with partial.open("wb") as output:
                async for chunk in request.stream():
                    output.write(chunk)
            try:
                await run_in_threadpool(probe_media, partial)
            except MediaError as exc:
                raise HTTPException(status_code=422, detail=f"{filename} is not a readable video: {exc}")
            try:
                source = await run_in_threadpool(library.ingest_upload, partial, filename)
            except IdentityError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
            except MediaError as exc:
                raise HTTPException(status_code=409, detail=str(exc))
            metadata = library.public_metadata(source)
            return {"status": "uploaded", "file": metadata}
        finally:
            if partial.exists():
                partial.unlink()

    @app.post("/api/sources/{file_id}/proxy", status_code=202)
    def create_proxy(file_id: str) -> dict[str, Any]:
        source_or_404(file_id)
        try:
            return proxy_jobs.start(file_id)
        except (KeyError, MediaError) as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.get("/api/sources/{file_id}/proxy/status")
    def proxy_status(file_id: str) -> dict[str, Any]:
        source_or_404(file_id)
        return proxy_jobs.get(file_id)

    @app.get("/api/sources/{file_id}/proxy")
    def serve_proxy(file_id: str) -> FileResponse:
        source = source_or_404(file_id)
        proxy = source.cache_dir / "proxy.mp4"
        if not proxy.is_file():
            raise HTTPException(status_code=404, detail="Proxy has not been generated")
        return FileResponse(proxy, media_type="video/mp4")

    @app.get("/api/projects")
    def list_projects() -> dict[str, Any]:
        return {"projects": project_catalog.list()}

    @app.post("/api/projects", status_code=201)
    def create_project(request: CreateProjectRequest) -> dict[str, str]:
        try:
            return project_catalog.ensure(request.project_name, require_new=True)
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.get("/api/projects/{file_id}", response_model=ProjectState)
    def get_project(file_id: str) -> ProjectState:
        source = source_or_404(file_id)
        try:
            return projects.load(source)
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.put("/api/projects/{file_id}", response_model=ProjectState)
    def put_project(file_id: str, project: ProjectState) -> ProjectState:
        source = source_or_404(file_id)
        try:
            duration = library.metadata(source)["duration"]
            return projects.save(source, project, duration)
        except (ValueError, MediaError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.post("/api/projects/{file_id}/export", status_code=202)
    def start_export(file_id: str, request: ExportRequest) -> dict[str, str]:
        source = source_or_404(file_id)
        try:
            project = projects.load(source)
            duration = library.metadata(source)["duration"]
            projects.save(source, project, duration)
            job_id = jobs.start(file_id, project, request.clip_ids)
        except (ValueError, MediaError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {"job_id": job_id, "status_url": f"/api/exports/{job_id}"}

    @app.get("/api/exports/{job_id}")
    def export_status(job_id: str) -> dict[str, Any]:
        try:
            return jobs.get(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown export job")

    return app


app = create_app()
