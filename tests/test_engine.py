import numpy as np

from soundsplitter.audio.engine import AudioRouter

# These tests exercise the routing/gain/lifecycle logic directly, without
# starting any output threads or touching audio hardware.


def test_add_target_prefills_only_delay_silence():
    # The base cushion is primed from real capture at start, so add_target seeds
    # only the per-device relative delay as silence — nothing for a zero delay.
    router = AudioRouter(samplerate=48000, blocksize=4, channels=2)
    router.add_target("spk-a", "A", delay_ms=10.0)  # 10 ms @ 48k = 480 frames
    assert router._snapshot[0].ring.available == 480
    router.add_target("spk-b", "B")  # no delay -> no prefill
    assert router._snapshot[1].ring.available == 0


def test_capture_block_fans_out_to_all_targets():
    router = AudioRouter(samplerate=48000, blocksize=4, channels=2)
    router.add_target("spk-a", "A")
    router.add_target("spk-b", "B")
    block = np.ones((4, 2), dtype=np.float32)
    router._on_capture_block(block)
    for target in router._snapshot:
        assert np.array_equal(target.ring.read(4), block)


def test_apply_gain_unity_is_passthrough():
    block = np.full((4, 2), 0.5, dtype=np.float32)
    assert np.array_equal(AudioRouter._apply_gain(block, 1.0), block)


def test_apply_gain_amplifies_and_soft_clips():
    block = np.full((4, 2), 0.9, dtype=np.float32)
    out = AudioRouter._apply_gain(block, 4.0)  # 3.6 -> tanh -> < 1
    assert out.max() < 1.0
    assert out.max() > 0.9


def test_set_volume_updates_gain_live():
    router = AudioRouter()
    router.add_target("spk-a", "A", volume_db=0.0)
    assert router._snapshot[0].gain == 1.0
    router.set_volume("spk-a", 6.0)
    assert router._snapshot[0].gain > 1.9


def test_drift_state_stable_level_never_corrects():
    # A device sitting at a constant level (whatever it settled at) must never be
    # "corrected" — the old bug stuffed frames forever because it assumed a level.
    from soundsplitter.audio.engine import _DriftState
    d = _DriftState(now=0.0)
    extras = [d.update(now=i * 0.05, level=1234, deadband=240) for i in range(120)]
    assert set(extras) == {0}


def test_drift_state_corrects_after_settle_when_level_strays():
    from soundsplitter.audio.engine import _DriftState
    d = _DriftState(now=0.0)
    t = 0.0
    while t < 2.5:  # settle on 5000
        d.update(now=t, level=5000, deadband=240)
        t += 0.05
    # level now persistently higher -> controller should eventually signal a drop
    saw = set()
    for _ in range(400):
        t += 0.05
        saw.add(d.update(now=t, level=6000, deadband=240))
    assert 1 in saw


def test_set_delay_is_seamless_not_a_rebuild():
    # Changing delay must not tear down the device's ring/stream — it only
    # publishes a new desired delay for the output thread to converge to.
    router = AudioRouter(samplerate=48000, blocksize=4, channels=2)
    router.add_target("spk-a", "A", delay_ms=10.0)
    ring_before = router._snapshot[0].ring
    router.set_delay("spk-a", 50.0)
    target = router._snapshot[0]
    assert target.ring is ring_before  # same object: not rebuilt
    assert target.delay_ms == 50.0
    assert target.delay_frames == 2400  # 50 ms @ 48 kHz


def test_remove_target_drops_from_snapshot():
    router = AudioRouter()
    router.add_target("spk-a", "A")
    router.add_target("spk-b", "B")
    router.remove_target("spk-a")
    assert {t.name for t in router._snapshot} == {"B"}


def test_start_without_source_raises():
    router = AudioRouter()
    router.add_target("spk-a", "A")
    try:
        router.start()
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass
