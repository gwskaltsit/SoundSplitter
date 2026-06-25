"""The audio router.

One capture thread reads system audio and fans each block out into a per-device
ring buffer. Each output device has its own thread that drains its ring buffer,
applies gain, and plays the block. The ring buffer decouples the single producer
(capture) from each consumer (output), so a slow or Bluetooth device only backs
up its own buffer — it never stalls capture or the other devices.

The list of targets is published as an immutable tuple snapshot, so the capture
thread reads it with a single atomic attribute load and never holds a lock.

Capture goes through soundcard (cross-platform loopback). Output goes through a
per-device backend (``output.open_player``): on Linux each device is its own
``pacat`` subprocess targeting one sink by name, which is the only mechanism
that reliably reaches every USB/Bluetooth sink independently. No virtual audio
cable is needed.

Latency is deterministic by construction. Capture is started first, then each
output thread waits until its ring has filled to a fixed cushion of *real*
captured audio before it plays a single frame. The steady-state buffering is
therefore the same on every start/stop/resume — it is not a function of thread
or subprocess startup races, which is what used to make the alignment drift
between runs. The cushion is the one latency knob: large enough to ride out
scheduling jitter, small enough to keep end-to-end delay low.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .capture import LoopbackCapture
from .dsp import db_to_gain, delay_samples
from .output import open_player
from .ring_buffer import RingBuffer

logger = logging.getLogger(__name__)

# The shared output cushion, in milliseconds of real captured audio buffered
# ahead before playback begins. It must exceed one playback burst (the player's
# own buffer, filled in one shot at start) plus thread-scheduling jitter, so the
# stream never underflows once primed. Per-device delay is layered on top and
# does not affect this baseline.
BASE_CUSHION_MS = 150.0
# How long to wait for capture to prime the cushion before giving up (capture
# emits real-time frames even on a silent source, so this completes in well
# under the cushion duration in practice).
PRIME_TIMEOUT_S = 3.0

# Clock-drift compensation. Capture and each output device run on independent
# crystals, so the buffer between them slowly fills or empties (~hundreds of ppm)
# — heard as the alignment sliding by tens of ms over a minute. We track a slow
# moving average of the buffer level and, when it strays past a small deadband,
# drop or duplicate a single frame per block to pull it back. One frame is 1/48
# of a millisecond — inaudible — and the correction rate dwarfs any real drift.
DRIFT_TAU_S = 3.0          # smoothing time constant for the level average
DRIFT_DEADBAND_MS = 5.0    # wired: tight, single-frame edits are inaudible on PCM
# Bluetooth runs through a lossy codec, where a single dropped/duplicated frame
# smears into an audible artifact instead of vanishing. So correct it far more
# rarely — its alignment only needs to be loose, and auto-align re-snaps it
# every 30 s anyway.
DRIFT_DEADBAND_MS_BLUETOOTH = 25.0
DRIFT_SETTLE_S = 2.0       # measure the naturally-settled buffer level over this long first


@dataclass
class _Target:
    id: str
    name: str
    ring: RingBuffer
    gain: float  # linear; updated live for volume changes (atomic float store)
    delay_ms: float
    volume_db: float
    delay_frames: int = 0  # desired delay; the output thread chases this live (atomic int store)
    underflows: int = 0
    thread: threading.Thread | None = field(default=None)
    stop_event: threading.Event | None = field(default=None)


class _DriftState:
    """Per-device clock-drift controller for the output thread.

    It first measures the buffer level the ring *naturally* settles at (the
    player keeps its own fixed buffer inside pacat, so the ring sits below
    base+delay by that amount). Assuming the theoretical level instead made the
    controller think the buffer was permanently too empty and stuff a frame every
    block — an audible crackle. After the settle window it nudges one frame per
    block when the smoothed level strays past the deadband. Everything is weighted
    by elapsed wall time, so a device whose writes wake the thread in bursts
    (Bluetooth) doesn't bias the average.
    """

    def __init__(self, now: float) -> None:
        self._start = now
        self._last = now
        self._sum = 0.0           # time-weighted accumulators for the settle mean
        self._weight = 0.0
        self._reference: float | None = None
        self._ema = 0.0
        self._have_ema = False

    def update(self, now: float, level: int, deadband: int) -> int:
        dt = now - self._last
        self._last = now
        if dt < 0.0:
            dt = 0.0
        if not self._have_ema:
            self._ema = float(level)
            self._have_ema = True
        else:
            w = dt / DRIFT_TAU_S
            self._ema += min(w, 1.0) * (level - self._ema)
        if self._reference is None:
            self._sum += level * dt
            self._weight += dt
            if now - self._start >= DRIFT_SETTLE_S and self._weight > 0.0:
                self._reference = self._sum / self._weight
            return 0  # never correct while still learning the settled level
        if self._ema > self._reference + deadband:
            return 1
        if self._ema < self._reference - deadband:
            return -1
        return 0


class AudioRouter:
    def __init__(
        self,
        samplerate: int = 48000,
        blocksize: int = 256,
        channels: int = 2,
        max_delay_ms: float = 3000.0,
    ) -> None:
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.channels = channels
        # A fixed output cushion so the device never starves on real-time jitter.
        # Per-device delay is added on top of this; the common base doesn't affect
        # relative alignment between devices.
        self._base_frames = int(BASE_CUSHION_MS / 1000.0 * samplerate)
        self._capacity = int(samplerate * (max_delay_ms / 1000.0 + 1.0)) + self._base_frames
        self._source_id: str | None = None
        self._targets: dict[str, _Target] = {}
        self._snapshot: tuple[_Target, ...] = ()
        self._capture: LoopbackCapture | None = None
        self._lock = threading.Lock()  # guards structural changes to _targets only
        self._running = False
        self._error: Exception | None = None
        self._blocks_seen = 0
        self._rms_log_every = max(1, int(2.0 * samplerate / blocksize))  # ~every 2 s

    @property
    def running(self) -> bool:
        return self._running

    @property
    def error(self) -> Exception | None:
        return self._error

    def set_source(self, source_id: str) -> None:
        self._source_id = source_id

    def add_target(self, device_id: str, name: str, delay_ms: float = 0.0, volume_db: float = 0.0) -> None:
        ring = RingBuffer(self._capacity, self.channels)
        # Only the per-device relative delay is seeded as silence. The shared base
        # cushion is filled at start with real captured audio (see _output_loop),
        # which is what makes the steady-state latency deterministic.
        ring.prefill_silence(delay_samples(delay_ms, self.samplerate))
        target = _Target(
            id=device_id,
            name=name,
            ring=ring,
            gain=db_to_gain(volume_db),
            delay_ms=delay_ms,
            volume_db=volume_db,
            delay_frames=delay_samples(delay_ms, self.samplerate),
        )
        with self._lock:
            self._targets[device_id] = target
            self._publish_snapshot()
        if self._running:
            self._start_output(target)

    def remove_target(self, device_id: str) -> None:
        with self._lock:
            target = self._targets.pop(device_id, None)
            self._publish_snapshot()
        if target is not None:
            self._stop_output(target)

    def set_volume(self, device_id: str, volume_db: float) -> None:
        target = self._targets.get(device_id)
        if target is not None:
            target.volume_db = volume_db
            target.gain = db_to_gain(volume_db)  # single float store, read live by the output thread

    def set_delay(self, device_id: str, delay_ms: float) -> None:
        # Live, seamless: just publish the new desired delay. The output thread
        # converges its buffer to it (inserting silence to grow it, dropping
        # frames to shrink it) without ever restarting the stream — no dropout.
        target = self._targets.get(device_id)
        if target is None:
            return
        target.delay_ms = delay_ms
        target.delay_frames = delay_samples(delay_ms, self.samplerate)  # single atomic int store

    def start(self) -> None:
        if self._running:
            return
        if self._source_id is None:
            raise RuntimeError("no capture source set")
        if not self._targets:
            raise RuntimeError("no output devices added")
        self._error = None
        self._blocks_seen = 0
        self._running = True
        logger.info(
            "starting router: source=%s, %d output(s) @ %d Hz / %d frames",
            self._source_id, len(self._snapshot), self.samplerate, self.blocksize,
        )
        # Reset rings first, then start capture, then the outputs. Capture must be
        # flowing before any output drains, so each output's priming wait sees a
        # ring filling at real-time — that is what locks the latency per start.
        for target in self._snapshot:
            self._reset_ring(target)
        self._capture = LoopbackCapture(
            source_id=self._source_id,
            samplerate=self.samplerate,
            blocksize=self.blocksize,
            on_block=self._on_capture_block,
            channels=self.channels,
            on_error=self._on_capture_error,
        )
        self._capture.start()
        for target in self._snapshot:
            self._start_output(target)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._capture is not None:
            self._capture.stop()
            self._capture = None
        for target in self._snapshot:
            self._stop_output(target)

    def get_stats(self) -> list[dict[str, object]]:
        return [
            {
                "name": t.name,
                "volume_db": t.volume_db,
                "delay_ms": t.delay_ms,
                "underflows": t.underflows,
                "buffered_ms": 1000.0 * t.ring.available / self.samplerate,
            }
            for t in self._snapshot
        ]

    # --- internals -------------------------------------------------------

    @staticmethod
    def _apply_gain(block: np.ndarray, gain: float) -> np.ndarray:
        if gain == 1.0:
            return block
        block = block * np.float32(gain)
        if gain > 1.0:
            np.tanh(block, out=block)  # soft-clip only when amplifying
        return block

    def _on_capture_block(self, block: np.ndarray) -> None:
        # Producer side: one atomic load of the snapshot, then fan out. No lock.
        # rms is sampled periodically (not just once) so the log reflects the
        # live signal — a lasting rms~0 means the chosen source is truly silent.
        self._blocks_seen += 1
        if self._blocks_seen == 1 or self._blocks_seen % self._rms_log_every == 0:
            rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))
            logger.info("capture rms=%.5f (block #%d; rms~0 means the source is silent)", rms, self._blocks_seen)
        for target in self._snapshot:
            target.ring.write(block)

    def _on_capture_error(self, exc: Exception) -> None:
        self._error = exc
        self._running = False

    def _output_loop(self, target: _Target) -> None:
        player = None
        try:
            player = open_player(target.id, self.samplerate, self.channels, self.blocksize)
            logger.info("output started: %s (id=%s)", target.name, target.id)
            if not self._prime(target):
                return  # stopped (or capture never flowed) before priming finished
            # The cushion primed above already realises the initial delay, so the
            # thread starts already "caught up" to the desired delay.
            applied = target.delay_frames
            drift_deadband_ms = DRIFT_DEADBAND_MS_BLUETOOTH if target.id.startswith("bluez") else DRIFT_DEADBAND_MS
            deadband = int(drift_deadband_ms / 1000.0 * self.samplerate)
            # The drift corrector targets the level the ring *actually* settles at
            # (lower than base+delay by the player's own buffer, which lives in
            # pacat, not the ring). It measures that reference over a settle window
            # rather than assuming it — assuming it caused a permanent offset that
            # made the corrector stuff frames forever (audible crackle). Everything
            # is time-weighted so a device that wakes the thread in bursts (Bluetooth)
            # doesn't bias the average.
            drift = _DriftState(time.monotonic())
            played = False
            while target.stop_event is not None and not target.stop_event.is_set():
                desired = target.delay_frames  # atomic read; the UI may change it live
                if applied < desired:
                    # Grow the delay: emit silence and hold the ring back so the
                    # buffered audio (= this device's delay) increases. Smooth, no
                    # restart — the gap is only the few ms of delay being added.
                    n = min(desired - applied, self.blocksize)
                    block = np.zeros((self.blocksize, self.channels), dtype=np.float32)
                    if n < self.blocksize:
                        block[n:] = target.ring.read(self.blocksize - n)
                    applied += n
                    drift = _DriftState(time.monotonic())  # re-measure after a deliberate change
                elif applied > desired:
                    # Shrink the delay: drop buffered frames, then play normally.
                    drop = min(applied - desired, self.blocksize)
                    target.ring.read(drop)
                    applied -= drop
                    block = target.ring.read(self.blocksize)
                    drift = _DriftState(time.monotonic())
                else:
                    # Steady state: gently counter clock drift so the delay holds.
                    extra = drift.update(time.monotonic(), target.ring.available, deadband)
                    if extra > 0:  # buffer trending full — consume one extra frame
                        target.ring.read(1)
                        block = target.ring.read(self.blocksize)
                    elif extra < 0:  # buffer trending empty — stuff one frame
                        block = np.empty((self.blocksize, self.channels), dtype=np.float32)
                        block[:-1] = target.ring.read(self.blocksize - 1)
                        block[-1] = block[-2]  # duplicate last frame (inaudible)
                    else:
                        if target.ring.available < self.blocksize:
                            target.underflows += 1
                        block = target.ring.read(self.blocksize)
                # write() blocks until the device is ready, pacing this thread.
                player.write(self._apply_gain(block, target.gain))
                if not played:
                    logger.info("audio reaching %s", target.name)
                    played = True
        except Exception as exc:
            logger.exception("output thread failed for %s", target.name)
            self._error = exc
        finally:
            if player is not None:
                player.close()

    def _prime(self, target: _Target) -> bool:
        """Block until the ring holds the full cushion, locking the start latency.

        Returns False if the router was stopped or capture never produced audio
        within the timeout, in which case the output thread should just exit.
        """
        wanted = self._base_frames + target.delay_frames
        deadline = time.monotonic() + PRIME_TIMEOUT_S
        while target.stop_event is not None and not target.stop_event.is_set():
            if target.ring.available >= wanted:
                logger.info("output primed: %s (%.0f ms cushion)", target.name, BASE_CUSHION_MS)
                return True
            if time.monotonic() > deadline:
                logger.warning("priming timed out for %s — capture not flowing?", target.name)
                return False
            time.sleep(0.001)
        return False

    def _reset_ring(self, target: _Target) -> None:
        # Seed only the relative delay; the base cushion is primed from real
        # capture in _output_loop so the start latency is deterministic.
        target.ring.clear()
        target.ring.prefill_silence(delay_samples(target.delay_ms, self.samplerate))
        target.underflows = 0

    def _start_output(self, target: _Target) -> None:
        target.stop_event = threading.Event()
        target.thread = threading.Thread(
            target=self._output_loop, args=(target,), name=f"out-{target.name}", daemon=True
        )
        target.thread.start()

    def _stop_output(self, target: _Target) -> None:
        if target.stop_event is not None:
            target.stop_event.set()
        if target.thread is not None:
            target.thread.join(timeout=2.0)
        target.thread = None
        target.stop_event = None

    def _publish_snapshot(self) -> None:
        # Assignment of the new tuple is atomic; readers see old or new, never torn.
        self._snapshot = tuple(self._targets.values())
