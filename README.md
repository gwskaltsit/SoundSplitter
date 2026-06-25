# SoundSplitter

**Play your computer's sound on several devices at once — and keep them in sync.**

Sending the same audio to two outputs sounds trivial until you try it. Your USB
headphones and a Bluetooth speaker don't play in step: the speaker trails the
headphones by a fraction of a second, and that lag *changes* every time
Bluetooth reconnects. You can delay the fast device to catch up to the slow one,
but you can't speed the slow one up — and the moment you've tuned the delay by
hand, a reconnect throws it off again.

SoundSplitter captures whatever is playing and fans it out to as many outputs as
you want, each with its own volume and delay, and it handles the tedious part —
measuring the latency and keeping the devices lined up — for you.

It's a ground-up rewrite of an old tool of mine that leaned on a proprietary
"virtual audio cable" and never really solved the sync problem. This version
drops the cable (on Linux it makes its own) and treats staying-in-sync as the
actual job.

> Status: works on Linux (PipeWire / PulseAudio), which is what I run and test.
> The Windows and macOS audio backends are wired up but untested. ~1.8k lines of
> engine plus ~300 of tests. Solo project.

---

## What's interesting

**You can only ever *add* delay, so the trick is making everything go through
you.** If you tap a real device's monitor, that device is already playing the
sound at zero delay and you've lost — there's no way to push it later. So
SoundSplitter makes a silent virtual sink, lets the system play into *that*, and
then it owns every output and can delay each one independently. On Linux it
creates that sink itself through PipeWire; there's nothing to install.

**The OS knows each device's real latency — you just have to ask the right
tool.** The obvious one, `pactl`, reports zero latency for every device on
PipeWire, which is useless. But PipeWire's own node data has the true number: a
Bluetooth speaker shows up as ~135 ms. SoundSplitter reads that and lines the
devices up automatically when you start, then re-checks every so often, so a
Bluetooth reconnect doesn't mean re-tuning by hand. Your manual delay is just a
small trim on top.

**Clocks drift, so alignment has to be held, not set once.** Two devices run on
separate crystals ticking at very slightly different rates, so the buffer between
them slowly fills or empties — you hear the alignment slide by tens of
milliseconds over a minute. A small control loop watches the buffer and drops or
duplicates a *single* sample now and then to hold it steady. One sample is
1/48000 of a second; you never hear the correction, you just stop hearing the
drift. (Bluetooth goes through a lossy codec where even that can smear, so it's
nudged far more gently and leans on the latency re-check instead.)

**The audio path does almost nothing.** One thread captures and drops each chunk
into a small lock-free queue per device; each output drains its own queue on its
own thread. Nothing on the hot path allocates memory or waits on a lock, and a
stuttering Bluetooth device can only back up its own queue — it can't stall the
others or the capture.

**Starting twice gives the same result.** Capture starts first, and each output
waits until it has buffered a fixed cushion of real audio before it plays a note.
That makes the latency identical on every start and stop — without it the
devices' alignment jumped around between runs and you'd be re-tuning constantly.

## Stack

Python, [Flet](https://flet.dev) for the UI, NumPy for the DSP. On Linux audio
goes through PipeWire's `pacat` / `pw-dump`; elsewhere it falls back to
`soundcard` and `sounddevice` (PortAudio). No build step, no framework on the
audio side.

## Run

Needs Python 3.10+ and, on Linux, PipeWire or PulseAudio (standard on desktops).

```bash
pip install -r requirements.txt
python -m soundsplitter
```

Pick a capture source (a device's *Monitor*, or the built-in *SoundSplitter
virtual cable*), add the outputs you want, set volume and delay per device, and
hit Start. Leave **Auto-align device latency** on and it keeps things in sync on
its own.

```bash
pip install pytest && pytest   # engine, ring buffer, drift, latency — no hardware needed
```

## Layout

```
soundsplitter/
  audio/
    engine.py        capture → per-device ring buffers → outputs, with drift control
    ring_buffer.py   lock-free single-producer / single-consumer queue
    capture.py       loopback capture  (pacat on Linux · soundcard elsewhere)
    output.py        per-device player  (pacat on Linux · sounddevice elsewhere)
    virtual_sink.py  the self-made PipeWire virtual cable
    latency.py       reads real device latency from PipeWire for auto-alignment
    dsp.py           gain, soft-clip, delay — pure functions
    devices.py       device discovery
  config/settings.py typed settings, saved atomically
  ui/app.py          Flet front-end
tests/               DSP · ring buffer · engine · drift · latency · settings
```

## License

[MIT](LICENSE)
