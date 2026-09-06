from pathlib import Path

import pytest

from app import export


def test_find_first_source_frame_uses_decoded_timestamps(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_probe(path: Path, args: list[str]):
        assert "-show_frames" in args
        return {
            "frames": [
                {"media_type": "video", "best_effort_timestamp_time": "119.994875"},
                {"media_type": "video", "best_effort_timestamp_time": "120.003217"},
                {"media_type": "video", "best_effort_timestamp_time": "120.011559"},
            ]
        }

    monkeypatch.setattr(export, "ffprobe_json", fake_probe)
    assert export.find_first_source_video_frame_at_or_after(Path("source.mp4"), 120.0) == pytest.approx(120.003217)


def test_find_first_source_frame_prefers_best_effort_then_pts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        export,
        "ffprobe_json",
        lambda *_: {"frames": [{"media_type": "video", "pts_time": "2.025"}]},
    )
    assert export.find_first_source_video_frame_at_or_after(Path("source.mp4"), 2.0) == 2.025

