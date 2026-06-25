"""Stateless DSP helpers.

Every function operates on numpy float32 blocks shaped ``(frames, channels)``
and returns a new array. Keeping them pure makes them cheap to unit-test and
safe to call from a real-time audio callback.
"""

from __future__ import annotations

import numpy as np


def db_to_gain(db: float) -> float:
    """Convert a decibel level to a linear amplitude factor (0 dB -> 1.0)."""
    return float(10.0 ** (db / 20.0))


def apply_gain(block: np.ndarray, gain: float) -> np.ndarray:
    """Scale a block by a linear gain factor."""
    return block * np.float32(gain)


def soft_clip(block: np.ndarray) -> np.ndarray:
    """Bound samples to (-1, 1) with a smooth tanh knee.

    Used after positive gain to avoid the harsh artifacts of hard clipping.
    It is non-linear, so callers should only apply it when the signal can
    actually exceed unity — otherwise it colours quiet audio for no reason.
    """
    return np.tanh(block).astype(np.float32, copy=False)


def delay_samples(ms: float, sample_rate: int) -> int:
    """Convert a delay in milliseconds to a whole number of frames."""
    if ms < 0:
        raise ValueError("delay must be non-negative")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    return int(round(ms * sample_rate / 1000.0))
