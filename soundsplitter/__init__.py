"""SoundSplitter — a cross-platform real-time audio router.

Captures system audio (loopback) and forwards it to several output devices at
once, with per-device delay and volume. Works on Windows (WASAPI), Linux
(PulseAudio) and macOS (CoreAudio).
"""

__version__ = "1.0.0"
