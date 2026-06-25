"""Cross-platform audio device discovery, all via soundcard.

Both capture sources and output devices come from soundcard, so the names shown
match what the OS shows (PipeWire/PulseAudio sinks on Linux, WASAPI endpoints on
Windows, CoreAudio devices on macOS) — not the cryptic low-level ALSA names that
PortAudio exposes.
"""

from __future__ import annotations

from dataclasses import dataclass

import soundcard as sc


@dataclass(frozen=True)
class OutputDevice:
    id: str
    name: str
    channels: int


@dataclass(frozen=True)
class CaptureSource:
    id: str
    name: str
    is_loopback: bool


def list_output_devices() -> list[OutputDevice]:
    return [OutputDevice(id=str(s.id), name=s.name, channels=s.channels) for s in sc.all_speakers()]


def list_capture_sources() -> list[CaptureSource]:
    """Capture sources, including loopbacks (record what's playing)."""
    return [
        CaptureSource(id=str(mic.id), name=mic.name, is_loopback=bool(mic.isloopback))
        for mic in sc.all_microphones(include_loopback=True)
    ]


def default_microphone() -> CaptureSource | None:
    """A real (non-loopback) microphone for acoustic calibration."""
    try:
        mic = sc.default_microphone()
        if mic is not None and not mic.isloopback:
            return CaptureSource(id=str(mic.id), name=mic.name, is_loopback=False)
    except (RuntimeError, IndexError):
        pass
    return next((s for s in list_capture_sources() if not s.is_loopback), None)


def default_loopback_source() -> CaptureSource | None:
    """Loopback of the default output, falling back to the first loopback found."""
    try:
        speaker = sc.default_speaker()
        mic = sc.get_microphone(speaker.id, include_loopback=True)
        if mic is not None and mic.isloopback:
            return CaptureSource(id=str(mic.id), name=mic.name, is_loopback=True)
    except (RuntimeError, IndexError):
        pass
    return next((s for s in list_capture_sources() if s.is_loopback), None)
