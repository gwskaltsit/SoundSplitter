"""Read each output device's real latency from PipeWire, for auto-alignment.

PulseAudio's ``pactl`` reports 0 latency on PipeWire, but PipeWire's own per-node
``Latency`` param carries the true figure. This matters most for Bluetooth: its
latency (often 100-200 ms) is renegotiated on every reconnect, which is the usual
reason a hand-tuned delay drifts between sessions. Reading it lets us re-align
automatically on each start instead of by hand.

The reported latency does not capture everything (a headset's own internal buffer
is invisible to the host), so it gets alignment *close*, not perfect — the user's
manual delay stays as a small constant trim on top. But because the variable part
(Bluetooth) is tracked here, that trim no longer needs re-tuning every restart.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys

logger = logging.getLogger(__name__)


def read_latencies_ms(sink_names: list[str], samplerate: int = 48000) -> dict[str, float]:
    """Map each given sink name to its PipeWire-reported output latency in ms.

    Missing/0 entries mean the device wasn't active yet — read again once the
    stream has been running for a moment.
    """
    if not sink_names or not sys.platform.startswith("linux"):
        return {}
    try:
        out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout
        objects = json.loads(out)
    except (FileNotFoundError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}
    wanted = set(sink_names)
    result: dict[str, float] = {}
    for obj in objects:
        info = obj.get("info") or {}
        name = (info.get("props") or {}).get("node.name")
        if name not in wanted:
            continue
        entries = (info.get("params") or {}).get("Latency") or []
        ns = max((e.get("maxNs") or 0 for e in entries), default=0)
        rate = max((e.get("maxRate") or 0 for e in entries), default=0)
        # Some devices express latency in nanoseconds, others in frames (rate).
        result[name] = max(ns / 1e6, rate / samplerate * 1000.0)
    return result


def align_offsets(latencies_ms: dict[str, float]) -> dict[str, float]:
    """Per-device delay to add so all line up: the slowest gets 0, faster ones
    are delayed by how much sooner they would otherwise play."""
    if not latencies_ms:
        return {}
    slowest = max(latencies_ms.values())
    return {name: slowest - lat for name, lat in latencies_ms.items()}
