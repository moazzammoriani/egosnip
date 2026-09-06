from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable


class MediaError(RuntimeError):
    pass


def source_id_from_filename(filename: str) -> str:
    stem = Path(filename).stem
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_-")
    return safe or "source"


def safe_file_id(filename: str) -> str:
    return hashlib.sha256(filename.encode("utf-8")).hexdigest()[:20]


def parse_rate(value: str | None) -> float | None:
    if not value or value == "0/0":
        return None
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return None


def run_command(args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise MediaError(f"Required executable not found: {args[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise MediaError(f"{args[0]} failed: {detail[-8000:]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{args[0]} timed out") from exc


def ffprobe_json(path: Path, extra_args: list[str] | None = None) -> dict[str, Any]:
    args = ["ffprobe", "-v", "error"]
    if extra_args:
        args.extend(extra_args)
    args.extend(["-of", "json", os.fspath(path)])
    result = run_command(args)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaError(f"ffprobe returned invalid JSON for {path.name}") from exc


def detect_streams(probe: dict[str, Any]) -> dict[str, Any]:
    streams = probe.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    gpmd = next(
        (
            s
            for s in streams
            if str(s.get("codec_tag_string", "")).lower() == "gpmd"
        ),
        None,
    )
    return {"video": video, "audio": audio, "gpmd": gpmd}


def probe_media(path: Path) -> dict[str, Any]:
    probe = ffprobe_json(path, ["-show_streams", "-show_format"])
    selected = detect_streams(probe)
    video = selected["video"]
    if not video:
        raise MediaError(f"No video stream found in {path.name}")
    fmt = probe.get("format", {})
    raw_duration = video.get("duration") or fmt.get("duration")
    if raw_duration is None:
        raise MediaError(f"No duration reported for {path.name}")
    audio = selected["audio"]
    gpmd = selected["gpmd"]
    return {
        "duration": float(raw_duration),
        "width": int(video.get("width", 0)),
        "height": int(video.get("height", 0)),
        "fps": parse_rate(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name") if audio else None,
        "gpmd_present": gpmd is not None,
        "video_stream_index": int(video["index"]),
        "audio_stream_index": int(audio["index"]) if audio else None,
        "gpmd_stream_index": int(gpmd["index"]) if gpmd else None,
        "raw_probe": probe,
    }


@dataclass(frozen=True)
class Source:
    file_id: str
    filename: str
    source_id: str
    path: Path
    cache_dir: Path


class MediaLibrary:
    _proxy_locks_guard = threading.Lock()
    _proxy_locks: dict[str, threading.Lock] = {}

    def __init__(self, media_dir: Path):
        self.media_dir = media_dir.expanduser().resolve()
        self.media_dir.mkdir(parents=True, exist_ok=True)
        if not self.media_dir.is_dir():
            raise ValueError(f"MEDIA_DIR is not a directory: {self.media_dir}")
        self.cache_root = self.media_dir / ".snipper_cache"
        self.exports_root = self.media_dir / "exports"

    def _inside_media_dir(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.media_dir)
            return True
        except ValueError:
            return False

    def sources(self) -> list[Source]:
        found: list[Source] = []
        for path in sorted(self.media_dir.iterdir(), key=lambda p: p.name.lower()):
            if not path.is_file() or path.suffix.lower() != ".mp4":
                continue
            resolved = path.resolve()
            if not self._inside_media_dir(resolved):
                continue
            filename = path.name
            sid = source_id_from_filename(filename)
            fid = safe_file_id(filename)
            found.append(Source(fid, filename, sid, resolved, self.cache_root / f"{sid}_{fid[:8]}"))
        return found

    def get(self, file_id: str) -> Source:
        if not re.fullmatch(r"[a-f0-9]{20}", file_id):
            raise KeyError(file_id)
        source = next((item for item in self.sources() if item.file_id == file_id), None)
        if source is None or not self._inside_media_dir(source.path):
            raise KeyError(file_id)
        return source

    @staticmethod
    def fingerprint(source: Source) -> dict[str, int]:
        stat = source.path.stat()
        return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}

    def metadata(self, source: Source) -> dict[str, Any]:
        source.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = source.cache_dir / "source_metadata.json"
        fingerprint = self.fingerprint(source)
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if cached.get("fingerprint") == fingerprint:
                    return cached["metadata"]
            except (OSError, KeyError, json.JSONDecodeError):
                pass
        metadata = probe_media(source.path)
        cache_payload = {
            "fingerprint": fingerprint,
            "metadata": {key: value for key, value in metadata.items() if key != "raw_probe"},
        }
        temp = cache_path.with_suffix(".tmp")
        temp.write_text(json.dumps(cache_payload, indent=2), encoding="utf-8")
        temp.replace(cache_path)
        return cache_payload["metadata"]

    def public_metadata(self, source: Source) -> dict[str, Any]:
        metadata = self.metadata(source)
        return {
            "id": source.file_id,
            "filename": source.filename,
            "source_id": source.source_id,
            **{key: metadata[key] for key in (
                "duration", "width", "height", "fps", "video_codec",
                "audio_codec", "gpmd_present",
            )},
            "proxy_status": "ready" if self.proxy_ready(source) else "missing",
        }

    @classmethod
    def _proxy_lock_for(cls, key: str) -> threading.Lock:
        with cls._proxy_locks_guard:
            return cls._proxy_locks.setdefault(key, threading.Lock())

    def proxy_ready(self, source: Source) -> bool:
        proxy = source.cache_dir / "proxy.mp4"
        fingerprint_path = source.cache_dir / "proxy_source.json"
        if not (proxy.is_file() and proxy.stat().st_size > 0 and fingerprint_path.exists()):
            return False
        try:
            return json.loads(fingerprint_path.read_text(encoding="utf-8")) == self.fingerprint(source)
        except (OSError, json.JSONDecodeError):
            return False

    def ensure_proxy(
        self,
        source: Source,
        progress_callback: Callable[[float], None] | None = None,
    ) -> Path:
        with self._proxy_lock_for(source.file_id):
            return self._ensure_proxy_locked(source, progress_callback)

    def _ensure_proxy_locked(
        self,
        source: Source,
        progress_callback: Callable[[float], None] | None,
    ) -> Path:
        source.cache_dir.mkdir(parents=True, exist_ok=True)
        proxy = source.cache_dir / "proxy.mp4"
        fingerprint_path = source.cache_dir / "proxy_source.json"
        fingerprint = self.fingerprint(source)
        if self.proxy_ready(source):
            if progress_callback:
                progress_callback(1.0)
            return proxy
        temp = source.cache_dir / "proxy.partial.mp4"
        if temp.exists():
            temp.unlink()
        duration = self.metadata(source)["duration"]
        args = [
            "ffmpeg", "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
            "-i", os.fspath(source.path),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", "fps=30,scale=-2:720",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            os.fspath(temp),
        ]
        try:
            try:
                process = subprocess.Popen(
                    args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
            except FileNotFoundError as exc:
                raise MediaError("Required executable not found: ffmpeg") from exc
            assert process.stdout is not None
            for line in process.stdout:
                key, separator, raw_value = line.strip().partition("=")
                if not separator or key not in {"out_time_us", "out_time_ms"}:
                    continue
                try:
                    completed_s = int(raw_value) / 1_000_000
                except ValueError:
                    continue
                if progress_callback and duration > 0:
                    progress_callback(min(0.99, max(0.0, completed_s / duration)))
            stderr = process.stderr.read() if process.stderr else ""
            return_code = process.wait()
            if return_code != 0:
                raise MediaError(f"ffmpeg failed: {stderr.strip()[-8000:]}")
            temp.replace(proxy)
            fingerprint_path.write_text(json.dumps(fingerprint), encoding="utf-8")
            if progress_callback:
                progress_callback(1.0)
        finally:
            if temp.exists():
                temp.unlink()
        return proxy
