"""Per-device audio output backends.

Each output device gets its own independent streaming player. On Linux that is
a ``pacat`` subprocess targeting a specific PipeWire/PulseAudio sink by name —
the canonical way to route audio to one device, and the only one that reliably
reaches USB and Bluetooth sinks (PortAudio only sees the default sink, and the
``soundcard`` player goes silent when more than one is open at once). On Windows
and macOS, where ``soundcard``/PortAudio address endpoints directly, a
``sounddevice`` stream is used instead.

The contract is tiny: :func:`open_player` returns something with ``write(block)``
and ``close()``. ``write`` is fed a contiguous ``(frames, channels)`` float32
block and blocks until the device is ready for more, which paces the output
thread at real-time without any explicit sleep.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Protocol

import numpy as np

# Shrink the kernel pipe to one small buffer so we never queue audio ahead of
# the player by more than this. The default 64 KiB pipe (~170 ms at 48k stereo
# float) would otherwise hide a large, variable backlog and inflate latency.
_PIPE_BYTES = 65536


def _shrink_pipe(fd: int) -> None:
    try:
        import fcntl

        fcntl.fcntl(fd, getattr(fcntl, "F_SETPIPE_SZ", 1031), _PIPE_BYTES)
    except (OSError, ImportError, AttributeError):
        pass  # best effort; falls back to the default pipe size


# Playback buffer per sink type. Bluetooth delivers audio in jittery bursts and
# needs a generous buffer or it crackles; a wired sink stays smooth on a small
# one, which keeps its latency low. BT is high-latency anyway and auto-alignment
# compensates for the difference, so the extra buffer is free.
_LATENCY_MS_BLUETOOTH = 350
_LATENCY_MS_DEFAULT = 60


def _playback_latency_ms(sink: str) -> int:
    return _LATENCY_MS_BLUETOOTH if sink.startswith("bluez") else _LATENCY_MS_DEFAULT


class Player(Protocol):
    def write(self, block: np.ndarray) -> None: ...
    def close(self) -> None: ...


class _PacatPlayer:
    """Streams raw float32 frames to one PipeWire/PulseAudio sink via ``pacat``.

    Writing to the pipe blocks once pacat's own buffer (``--latency-msec``) is
    full, so the producer is paced at the sink's real-time rate. Connecting also
    resumes a suspended sink (e.g. an idle Bluetooth speaker).
    """

    def __init__(self, sink: str, samplerate: int, channels: int) -> None:
        self._proc = subprocess.Popen(
            [
                "pacat",
                "--playback",
                f"--device={sink}",
                f"--rate={samplerate}",
                f"--channels={channels}",
                "--format=float32le",
                # Playback buffer: small for wired sinks (low latency), large for
                # Bluetooth (rides out its jittery delivery without crackling).
                f"--latency-msec={_playback_latency_ms(sink)}",
                "--stream-name=SoundSplitter",
            ],
            stdin=subprocess.PIPE,
        )
        if self._proc.stdin is not None:
            _shrink_pipe(self._proc.stdin.fileno())

    def write(self, block: np.ndarray) -> None:
        if self._proc.stdin is None:
            return
        self._proc.stdin.write(np.ascontiguousarray(block, dtype=np.float32).tobytes())

    def close(self) -> None:
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        self._proc.terminate()
        try:
            self._proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            self._proc.kill()


class _SoundDevicePlayer:
    """PortAudio output stream for Windows/macOS, addressed by device id."""

    def __init__(self, device_id: str, samplerate: int, channels: int, blocksize: int) -> None:
        import sounddevice as sd

        self._stream = sd.OutputStream(
            device=int(device_id) if str(device_id).isdigit() else device_id,
            samplerate=samplerate,
            channels=channels,
            blocksize=blocksize,
            dtype="float32",
        )
        self._stream.start()

    def write(self, block: np.ndarray) -> None:
        self._stream.write(np.ascontiguousarray(block, dtype=np.float32))

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()


def open_player(device_id: str, samplerate: int, channels: int, blocksize: int) -> Player:
    """Open a streaming player for one output device, picking the OS backend."""
    if sys.platform.startswith("linux"):
        return _PacatPlayer(device_id, samplerate, channels)
    return _SoundDevicePlayer(device_id, samplerate, channels, blocksize)
