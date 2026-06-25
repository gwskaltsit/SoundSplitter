"""Loopback capture backend.

A dedicated thread pulls fixed-size blocks of system audio from a loopback
source and hands each block to a callback. Running it off the real-time output
path keeps the output threads free of any blocking work.

The backend is OS-specific. On Linux the source is a PipeWire/PulseAudio sink
monitor read through ``pacat --record`` — soundcard's recorder returns silence
on PipeWire (same breakage as its player), so the native tool is the only thing
that actually captures audio. On Windows/macOS soundcard's WASAPI/CoreAudio
loopback works and is used directly.

Note: on Windows/WASAPI, single-channel loopback returns garbage, so capture is
always done in stereo (and the rest of the engine assumes stereo too).
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from typing import Callable

import numpy as np
import soundcard as sc

logger = logging.getLogger(__name__)

BlockCallback = Callable[[np.ndarray], None]
ErrorCallback = Callable[[Exception], None]


class LoopbackCapture:
    def __init__(
        self,
        source_id: str,
        samplerate: int,
        blocksize: int,
        on_block: BlockCallback,
        channels: int = 2,
        on_error: ErrorCallback | None = None,
    ) -> None:
        self._source_id = source_id
        self._samplerate = samplerate
        self._blocksize = blocksize
        self._channels = channels
        self._on_block = on_block
        self._on_error = on_error
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="loopback-capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        try:
            if sys.platform.startswith("linux"):
                self._run_pacat()
            else:
                self._run_soundcard()
        except Exception as exc:  # surface to the engine instead of dying silently
            logger.exception("capture thread failed")
            if self._on_error is not None:
                self._on_error(exc)

    def _run_pacat(self) -> None:
        """Read raw float32 frames from a sink monitor via ``pacat --record``."""
        bytes_per_block = self._blocksize * self._channels * 4  # float32 = 4 bytes
        proc = subprocess.Popen(
            [
                "pacat",
                "--record",
                f"--device={self._source_id}",
                f"--rate={self._samplerate}",
                f"--channels={self._channels}",
                "--format=float32le",
                # Small capture buffer -> small, frequent bursts instead of one
                # big 40 ms dump every 40 ms, which keeps the ring fill smooth.
                "--latency-msec=15",
                "--stream-name=SoundSplitter-capture",
            ],
            stdout=subprocess.PIPE,
        )
        try:
            assert proc.stdout is not None
            # Bound the kernel pipe so capture can't queue a large, variable
            # backlog ahead of us — keeps end-to-end latency low and steady.
            try:
                import fcntl

                fcntl.fcntl(proc.stdout.fileno(), getattr(fcntl, "F_SETPIPE_SZ", 1031), 65536)
            except (OSError, ImportError, AttributeError):
                pass
            while not self._stop.is_set():
                raw = proc.stdout.read(bytes_per_block)  # blocks until a full block is ready
                if not raw or len(raw) < bytes_per_block:
                    break
                block = np.frombuffer(raw, dtype=np.float32).reshape(self._blocksize, self._channels)
                self._on_block(block)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _run_soundcard(self) -> None:
        mic = sc.get_microphone(self._source_id, include_loopback=True)
        with mic.recorder(
            samplerate=self._samplerate,
            channels=self._channels,
            blocksize=self._blocksize,
        ) as recorder:
            while not self._stop.is_set():
                data = recorder.record(numframes=self._blocksize)
                block = np.ascontiguousarray(data, dtype=np.float32)
                self._on_block(block)
