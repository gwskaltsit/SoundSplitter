"""A single-producer / single-consumer ring buffer for audio frames.

The capture thread (one producer) calls :meth:`write`; one output callback
(one consumer) calls :meth:`read`. Index updates rely on the GIL for
atomicity, which is enough for exactly one producer and one consumer — no lock
is taken on the audio hot path.

Overflow policy is "keep the freshest": if the consumer falls behind, the
oldest frames are dropped so latency stays bounded instead of growing without
limit. Underflow returns silence, which is what an output device should play
when it momentarily has nothing to render.
"""

from __future__ import annotations

import numpy as np


class RingBuffer:
    def __init__(self, capacity: int, channels: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if channels <= 0:
            raise ValueError("channels must be positive")
        self._buf = np.zeros((capacity, channels), dtype=np.float32)
        self._capacity = capacity
        self._channels = channels
        # Monotonic frame counters; the difference is the fill level.
        self._written = 0
        self._read = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def channels(self) -> int:
        return self._channels

    @property
    def available(self) -> int:
        """Frames currently readable."""
        return self._written - self._read

    def write(self, block: np.ndarray) -> int:
        """Append frames. If the buffer would overflow, drop the oldest frames.

        ``block`` is shaped ``(frames, channels)``. Returns frames accepted.
        """
        n = int(block.shape[0])
        if n == 0:
            return 0
        # A single write larger than the buffer can only keep the newest tail.
        if n > self._capacity:
            block = block[-self._capacity:]
            n = self._capacity

        start = self._written % self._capacity
        end = start + n
        if end <= self._capacity:
            self._buf[start:end] = block
        else:
            split = self._capacity - start
            self._buf[start:] = block[:split]
            self._buf[: end - self._capacity] = block[split:]
        self._written += n

        overflow = self.available - self._capacity
        if overflow > 0:
            self._read += overflow
        return n

    def read_into(self, out: np.ndarray) -> int:
        """Fill ``out`` (shape ``(frames, channels)``) with the next frames.

        Zero-pads on underflow. Returns the number of real (non-padded) frames.
        Reading straight into the output device's buffer avoids allocating on
        the audio hot path.
        """
        frames = int(out.shape[0])
        n = min(self.available, frames)
        if n > 0:
            start = self._read % self._capacity
            end = start + n
            if end <= self._capacity:
                out[:n] = self._buf[start:end]
            else:
                split = self._capacity - start
                out[:split] = self._buf[start:]
                out[split:n] = self._buf[: end - self._capacity]
            self._read += n
        if n < frames:
            out[n:] = 0.0
        return n

    def read(self, frames: int) -> np.ndarray:
        """Return ``frames`` frames as a new array, zero-padding on underflow."""
        if frames <= 0:
            return np.zeros((0, self._channels), dtype=np.float32)
        out = np.zeros((frames, self._channels), dtype=np.float32)
        self.read_into(out)
        return out

    def prefill_silence(self, frames: int) -> None:
        """Seed the buffer with silence — used to implement an output delay."""
        if frames > 0:
            self.write(np.zeros((frames, self._channels), dtype=np.float32))

    def clear(self) -> None:
        self._read = self._written
