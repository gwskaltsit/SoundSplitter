"""A built-in virtual output sink — the cross-platform "virtual cable".

On Linux/PipeWire this loads a ``module-null-sink``: a sink that plays to
nothing. Routing system audio into it and capturing its monitor lets the router
fan that audio out to several real devices, each with its own delay — including
the device you actually listen on.

Why it is needed: if you instead capture a *real* device's monitor, that device
keeps playing the original stream at zero latency, straight past the app. You
can only ever *add* delay, so you can never slow that device down to line up
with a slower one (e.g. a Bluetooth speaker). A silent virtual sink has no such
zero-latency reference — every real device sits downstream of the app and can be
delayed independently.

Windows/macOS have no built-in equivalent, so there the feature reports itself
unavailable and the UI hides it (a third-party cable like VB-Audio is needed).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)

SINK_NAME = "soundsplitter"
SINK_DESCRIPTION = "SoundSplitter"


def _pactl(*args: str, check: bool = True) -> str:
    proc = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=5, check=check)
    return proc.stdout.strip()


class VirtualSink:
    """Lifecycle of the app's null-sink: create, optionally make default, remove."""

    def __init__(self) -> None:
        self._module_id: str | None = None
        self._prev_default: str | None = None
        self._is_default = False
        self._moved: list[str] = []  # sink-input ids we relocated, to restore later

    @property
    def available(self) -> bool:
        return sys.platform.startswith("linux") and shutil.which("pactl") is not None

    @property
    def created(self) -> bool:
        return self._module_id is not None

    @property
    def sink_name(self) -> str:
        return SINK_NAME

    @property
    def monitor_source(self) -> str:
        return f"{SINK_NAME}.monitor"

    @property
    def is_default(self) -> bool:
        return self._is_default

    def create(self) -> bool:
        if not self.available:
            return False
        self._unload_stale()
        try:
            self._module_id = _pactl(
                "load-module", "module-null-sink",
                "media.class=Audio/Sink",
                f"sink_name={SINK_NAME}",
                f"sink_properties=device.description={SINK_DESCRIPTION}",
            )
        except (subprocess.SubprocessError, OSError):
            logger.exception("failed to create virtual sink")
            return False
        logger.info("virtual sink created (module %s)", self._module_id)
        return True

    def engage(self) -> str | None:
        """Route the *current* output's audio through the virtual sink.

        Two moves, so nothing is left behind: make the virtual sink the default
        (new streams follow it) and relocate every stream already playing on the
        old default sink onto it (existing audio follows too), skipping our own
        playback. Returns the previous default sink name so the caller can keep
        that device as an output target — otherwise its audio would vanish.
        """
        if not self.created:
            return None
        try:
            self._prev_default = _pactl("get-default-sink") or None
            name_to_idx = self._sink_index_by_name()
            prev_idx = name_to_idx.get(self._prev_default or "")
            _pactl("set-default-sink", SINK_NAME)
            self._is_default = True
            self._moved = []
            if prev_idx is not None:
                for input_id, sink_idx, label in self._sink_inputs():
                    if label == SINK_DESCRIPTION:  # never grab our own playback streams
                        continue
                    if sink_idx == prev_idx:
                        _pactl("move-sink-input", input_id, SINK_NAME, check=False)
                        self._moved.append(input_id)
            logger.info(
                "engaged: default=%s, moved %d live stream(s) off %s",
                SINK_NAME, len(self._moved), self._prev_default,
            )
            return self._prev_default
        except (subprocess.SubprocessError, OSError):
            logger.exception("failed to engage virtual sink")
            return None

    def disengage(self) -> None:
        """Undo :meth:`engage`: move streams back and restore the old default."""
        if not self.created or not self._is_default:
            return
        target = self._prev_default
        try:
            for input_id in self._moved:
                if target:
                    _pactl("move-sink-input", input_id, target, check=False)
            if target:
                _pactl("set-default-sink", target, check=False)
            logger.info("disengaged: restored default=%s, moved %d stream(s) back", target, len(self._moved))
        except (subprocess.SubprocessError, OSError):
            logger.exception("failed to disengage virtual sink")
        finally:
            self._moved = []
            self._is_default = False

    def _sink_index_by_name(self) -> dict[str, int]:
        out = _pactl("list", "short", "sinks", check=False)
        result: dict[str, int] = {}
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0].strip().isdigit():
                result[parts[1].strip()] = int(parts[0].strip())
        return result

    def _sink_inputs(self) -> list[tuple[str, int, str]]:
        """(input id, current sink index, identifying label) for every stream."""
        out = _pactl("list", "sink-inputs", check=False)
        items: list[tuple[str, int, str]] = []
        cur_id: str | None = None
        cur_sink: int | None = None
        cur_app = ""
        cur_media = ""

        def flush() -> None:
            if cur_id is not None and cur_sink is not None:
                items.append((cur_id, cur_sink, cur_media or cur_app))

        for raw in out.splitlines():
            line = raw.strip()
            if line.startswith("Sink Input #"):
                flush()
                cur_id, cur_sink, cur_app, cur_media = line.split("#", 1)[1].strip(), None, "", ""
            elif line.startswith("Sink:"):
                m = re.search(r"\d+", line)
                cur_sink = int(m.group()) if m else None
            elif line.startswith("application.name"):
                m = re.search(r'=\s*"(.*)"', line)
                cur_app = m.group(1) if m else cur_app
            elif line.startswith("media.name"):
                m = re.search(r'=\s*"(.*)"', line)
                cur_media = m.group(1) if m else cur_media
        flush()
        return items

    def destroy(self) -> None:
        """Restore routing (if engaged) and unload the module."""
        if self._is_default:
            self.disengage()
        if self._module_id is not None:
            try:
                _pactl("unload-module", self._module_id, check=False)
                logger.info("virtual sink removed (module %s)", self._module_id)
            except (subprocess.SubprocessError, OSError):
                logger.exception("failed to remove virtual sink")
            self._module_id = None

    def _unload_stale(self) -> None:
        # A crashed run can leave our sink loaded; reusing the same name would
        # otherwise create 'soundsplitter.2', '.3', … Remove any leftover first.
        try:
            out = _pactl("list", "short", "modules", check=False)
        except (subprocess.SubprocessError, OSError):
            return
        for line in out.splitlines():
            if "module-null-sink" in line and f"sink_name={SINK_NAME}" in line:
                mid = line.split("\t", 1)[0].strip()
                if mid:
                    _pactl("unload-module", mid, check=False)
                    logger.info("removed stale virtual sink (module %s)", mid)
