from soundsplitter.audio.latency import align_offsets


def test_align_offsets_delays_faster_devices_to_the_slowest():
    # Bluetooth at 134 ms is the slowest; the 10 ms USB device is delayed to match.
    offsets = align_offsets({"usb": 10.0, "bt": 134.0})
    assert offsets["bt"] == 0.0
    assert round(offsets["usb"], 1) == 124.0


def test_align_offsets_empty_is_empty():
    assert align_offsets({}) == {}


def test_align_offsets_single_device_is_zero():
    assert align_offsets({"only": 42.0}) == {"only": 0.0}
