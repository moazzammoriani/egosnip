from io import BytesIO

from app.identity import _find_mp4_boxes
from app.telemetry import _telemetrik_get_samples


def mp4_box(key: str, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + key.encode("ascii") + payload


def full_box(key: str, payload: bytes) -> bytes:
    return mp4_box(key, b"\0\0\0\0" + payload)


def test_telemetry_samples_support_64_bit_offsets_and_multi_sample_chunks() -> None:
    sample_sizes = [4, 5, 6]
    stsz = full_box(
        "stsz",
        (0).to_bytes(4, "big")
        + len(sample_sizes).to_bytes(4, "big")
        + b"".join(size.to_bytes(4, "big") for size in sample_sizes),
    )
    co64 = full_box(
        "co64",
        (2).to_bytes(4, "big")
        + (2**32 + 100).to_bytes(8, "big")
        + (2**32 + 200).to_bytes(8, "big"),
    )
    stsc = full_box(
        "stsc",
        (2).to_bytes(4, "big")
        + (1).to_bytes(4, "big") + (2).to_bytes(4, "big") + (1).to_bytes(4, "big")
        + (2).to_bytes(4, "big") + (1).to_bytes(4, "big") + (1).to_bytes(4, "big"),
    )
    stts = full_box(
        "stts",
        (1).to_bytes(4, "big")
        + (3).to_bytes(4, "big")
        + (10).to_bytes(4, "big"),
    )
    encoded = mp4_box("stbl", stsz + co64 + stsc + stts)
    handle = BytesIO(encoded)
    stbl = _find_mp4_boxes(handle, 0, len(encoded), ["stbl"])[0]

    samples = _telemetrik_get_samples(handle, stbl)

    assert [sample.offset for sample in samples] == [2**32 + 100, 2**32 + 104, 2**32 + 200]
    assert [sample.size for sample in samples] == sample_sizes
    assert [sample.pts for sample in samples] == [0, 10, 20]
