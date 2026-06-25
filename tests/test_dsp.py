import numpy as np
import pytest

from soundsplitter.audio import dsp


def test_db_to_gain_reference_points():
    assert dsp.db_to_gain(0.0) == pytest.approx(1.0)
    assert dsp.db_to_gain(6.0) == pytest.approx(1.995, abs=1e-3)
    assert dsp.db_to_gain(-6.0) == pytest.approx(0.501, abs=1e-3)
    assert dsp.db_to_gain(20.0) == pytest.approx(10.0)


def test_apply_gain_scales_block():
    block = np.ones((4, 2), dtype=np.float32)
    out = dsp.apply_gain(block, 0.5)
    assert np.allclose(out, 0.5)
    assert out.dtype == np.float32


def test_soft_clip_bounds_to_unit_range():
    block = np.array([[-5.0], [5.0], [0.0]], dtype=np.float32)
    out = dsp.soft_clip(block)
    assert out.max() < 1.0
    assert out.min() > -1.0
    assert out[2, 0] == pytest.approx(0.0)


def test_delay_samples():
    assert dsp.delay_samples(1000.0, 48000) == 48000
    assert dsp.delay_samples(0.0, 48000) == 0
    assert dsp.delay_samples(10.0, 48000) == 480


def test_delay_samples_rejects_bad_input():
    with pytest.raises(ValueError):
        dsp.delay_samples(-1.0, 48000)
    with pytest.raises(ValueError):
        dsp.delay_samples(10.0, 0)
