import pytest

from app.telemetry import SensorData, SensorSample, rebase_samples, slice_sensor


def sensor(*timestamps: float) -> SensorData:
    return SensorData(
        key="ACCL",
        name="Accelerometer",
        units="m/s²",
        samples=[SensorSample(ts, (ts, ts + 1, ts + 2)) for ts in timestamps],
    )


def test_strict_half_open_slice_and_rebase() -> None:
    sliced = slice_sensor(sensor(9.9, 10.0, 10.5, 11.0), 10.0, 11.0, False)
    assert [item.source_timestamp_s for item in sliced.samples] == [10.0, 10.5]
    assert [row[0] for row in rebase_samples(sliced, 10.0)] == [0.0, 0.5]


def test_pre_zero_retains_exactly_closest_one() -> None:
    sliced = slice_sensor(sensor(9.0, 9.9, 10.1, 10.2), 10.0, 10.2, True)
    assert [item.source_timestamp_s for item in sliced.samples] == [9.9, 10.1]
    relative = [row[0] for row in rebase_samples(sliced, 10.0)]
    assert relative == pytest.approx([-0.1, 0.1])
    assert len([value for value in relative if value < 0]) == 1


def test_pre_zero_never_clamps_to_zero() -> None:
    sliced = slice_sensor(sensor(120.00313197969544, 120.008213), 120.003217, 121.0, True)
    first = rebase_samples(sliced, 120.003217)[0][0]
    assert first == pytest.approx(-0.00008502030456)
    assert first < 0


def test_no_pre_zero_when_no_earlier_sample() -> None:
    sliced = slice_sensor(sensor(10.0, 10.1), 10.0, 11.0, True)
    assert [item.source_timestamp_s for item in sliced.samples] == [10.0, 10.1]


def test_independent_sensor_clocks_are_not_resampled() -> None:
    sliced = slice_sensor(sensor(10.004, 10.009), 10.0, 11.0, False)
    assert rebase_samples(sliced, 10.0)[0][0] == pytest.approx(0.004)

