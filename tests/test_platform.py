import pytest
from PIL import Image

from raven import protocol
from raven.platform import power
from raven.platform.capture import Monitor, encode_jpeg
from raven.platform.inputs import is_valid_key
from raven.wol import magic_packet


@pytest.mark.parametrize("os_name", ["windows", "macos", "linux"])
@pytest.mark.parametrize("action", power.ACTIONS)
def test_every_action_has_a_plan(os_name, action):
    assert power.plan(action, os_name)


def test_unknown_power_action():
    with pytest.raises(ValueError):
        power.plan("format-disk", "linux")


def test_monitor_mapping():
    m = Monitor(2, left=-1920, top=0, width=1920, height=1080)
    assert m.denormalize(0, 0) == (-1920, 0)
    assert m.denormalize(1, 1) == (-1, 1079)
    assert m.denormalize(5, -3) == (-1, 0)  # clamped
    assert m.normalize(-960, 540) == pytest.approx((0.5, 0.5))
    assert m.normalize(100, 100) == (-1.0, -1.0)


def test_jpeg_is_downscaled():
    jpeg = encode_jpeg(Image.new("RGB", (3840, 2160), "red"), max_width=1280, quality=50)
    import io
    assert Image.open(io.BytesIO(jpeg)).size == (1280, 720)


def test_frame_packing():
    data = protocol.pack_frame(b"\xff\xd8jpeg", (0.25, 0.5))
    assert protocol.unpack_frame(data) == (b"\xff\xd8jpeg", (0.25, 0.5))


def test_key_validation():
    assert is_valid_key("a") and is_valid_key("enter") and is_valid_key("f12") and is_valid_key("cmd")
    assert not is_valid_key("") and not is_valid_key("rm -rf") and not is_valid_key("\n")


def test_magic_packet():
    pkt = magic_packet("AA-BB-CC-DD-EE-FF")
    assert len(pkt) == 102 and pkt[:6] == b"\xff" * 6 and pkt[6:12] == bytes.fromhex("aabbccddeeff")
    with pytest.raises(ValueError):
        magic_packet("not-a-mac")
