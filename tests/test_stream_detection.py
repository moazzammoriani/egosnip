from app.media import detect_streams


def test_detects_dynamic_stream_indices() -> None:
    probe = {
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "hevc"},
            {"index": 1, "codec_type": "data", "codec_tag_string": "tmcd"},
            {"index": 3, "codec_type": "audio", "codec_name": "aac"},
            {"index": 7, "codec_type": "data", "codec_tag_string": "gpmd"},
        ]
    }
    streams = detect_streams(probe)
    assert streams["video"]["index"] == 0
    assert streams["audio"]["index"] == 3
    assert streams["gpmd"]["index"] == 7


def test_missing_audio_and_gpmd_are_graceful() -> None:
    streams = detect_streams({"streams": [{"index": 2, "codec_type": "video"}]})
    assert streams["video"]["index"] == 2
    assert streams["audio"] is None
    assert streams["gpmd"] is None

