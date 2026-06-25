from soundsplitter.ui.app import _is_feedback_loop

# A capture source is a sink monitor ("<sink>.monitor"); routing output to that
# same sink loops audio back into capture and howls. These guard against it.


def test_routing_to_own_monitor_is_a_loop():
    sink = "alsa_output.usb-MCHOSE.analog-stereo"
    assert _is_feedback_loop(sink + ".monitor", sink) is True


def test_routing_to_a_different_device_is_fine():
    monitor = "alsa_output.usb-MCHOSE.analog-stereo.monitor"
    assert _is_feedback_loop(monitor, "bluez_output.3C_0B_4F_5F_DC_3B.1") is False


def test_no_source_is_never_a_loop():
    assert _is_feedback_loop(None, "any-sink") is False
    assert _is_feedback_loop("", "any-sink") is False
