import numpy as np
import pytest

from soundsplitter.audio.ring_buffer import RingBuffer


def _ramp(n: int, channels: int = 2, start: int = 0) -> np.ndarray:
    values = np.arange(start, start + n, dtype=np.float32)
    return np.repeat(values[:, None], channels, axis=1)


def test_write_then_read_is_fifo():
    rb = RingBuffer(capacity=16, channels=2)
    rb.write(_ramp(4))
    assert rb.available == 4
    out = rb.read(4)
    assert np.array_equal(out, _ramp(4))
    assert rb.available == 0


def test_underflow_returns_silence():
    rb = RingBuffer(capacity=16, channels=2)
    rb.write(_ramp(2))
    out = rb.read(4)
    assert out.shape == (4, 2)
    assert np.array_equal(out[:2], _ramp(2))
    assert np.all(out[2:] == 0.0)


def test_wraparound_across_capacity_boundary():
    rb = RingBuffer(capacity=8, channels=1)
    rb.write(_ramp(6, channels=1))
    rb.read(6)  # advance read head near the end
    rb.write(_ramp(5, channels=1, start=100))  # wraps around
    out = rb.read(5)
    assert np.array_equal(out, _ramp(5, channels=1, start=100))


def test_overflow_drops_oldest():
    rb = RingBuffer(capacity=4, channels=1)
    rb.write(_ramp(6, channels=1))  # only the last 4 frames survive
    assert rb.available == 4
    out = rb.read(4)
    assert np.array_equal(out, _ramp(4, channels=1, start=2))


def test_prefill_silence_creates_delay():
    rb = RingBuffer(capacity=16, channels=2)
    rb.prefill_silence(3)
    rb.write(_ramp(2, start=1))
    out = rb.read(5)
    assert np.all(out[:3] == 0.0)
    assert np.array_equal(out[3:], _ramp(2, start=1))


def test_read_into_fills_and_zero_pads():
    rb = RingBuffer(capacity=16, channels=2)
    rb.write(_ramp(3))
    out = np.full((5, 2), 99.0, dtype=np.float32)
    n = rb.read_into(out)
    assert n == 3
    assert np.array_equal(out[:3], _ramp(3))
    assert np.all(out[3:] == 0.0)


def test_constructor_validates_args():
    with pytest.raises(ValueError):
        RingBuffer(capacity=0, channels=2)
    with pytest.raises(ValueError):
        RingBuffer(capacity=8, channels=0)
