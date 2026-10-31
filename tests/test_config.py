"""Saved configuration: the account, the box cache, and this install's device id."""
import re
import tempfile

import _path  # noqa: F401 - puts intg-fetchtv on sys.path

import config

folder = tempfile.mkdtemp()
config.init(folder)
assert not config.is_configured()

# The device id looks like an Android id (16 hex characters), is made up once,
# and is kept across restarts and when the account is saved.
device_id = config.get_device_id()
assert re.fullmatch(r"[0-9a-f]{16}", device_id), device_id
config.init(folder)
assert config.get_device_id() == device_id
config.set_account("code", "1234", [{"terminal_id": "1|aa", "name": "Lounge"}])
config.init(folder)
assert config.get_device_id() == device_id
assert config.get_credentials() == ("code", "1234") and config.get_boxes()[0]["name"] == "Lounge"

# The DRM id is a UUID, made once and kept.
import uuid  # noqa: E402
drm_id = config.get_drm_id()
assert str(uuid.UUID(drm_id)) == drm_id
config.init(folder)
assert config.get_drm_id() == drm_id and config.get_device_id() == device_id

# The Debug logging option from the setup screen is kept across restarts, and
# saving the account does not lose it.
assert config.get_debug_logging() is False
config.set_debug_logging(True)
config.init(folder)
assert config.get_debug_logging() is True and config.get_device_id() == device_id
config.set_account("code", "1234", [{"terminal_id": "1|aa", "name": "Lounge"}])
config.init(folder)
assert config.get_debug_logging() is True
config.set_debug_logging(False)
config.init(folder)
assert config.get_debug_logging() is False

# A separate install gets its own.
other = tempfile.mkdtemp()
config.init(other)
assert config.get_device_id() != device_id and config.get_drm_id() != drm_id

# Saving never leaves the settings half written: a failure part-way keeps the old
# file and tidies up after itself, and no stray temporary file is left behind.
import json  # noqa: E402
import os  # noqa: E402
import stat  # noqa: E402
from unittest import mock  # noqa: E402

path = os.path.join(other, "config.json")
config.set_account("code", "1234", [{"terminal_id": "1|aa", "name": "Lounge"}])
before = open(path, "rb").read()
with mock.patch.object(config.json, "dump", side_effect=OSError("disk full")):
    config.set_boxes([{"terminal_id": "2|bb", "name": "Bedroom"}])
assert open(path, "rb").read() == before, "a failed save damaged the saved settings"
assert os.listdir(other) == ["config.json"], os.listdir(other)
config.init(other)
assert config.get_boxes()[0]["terminal_id"] == "1|aa" and config.get_credentials() == ("code", "1234")
if os.name == "posix":
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600, "the saved PIN is readable by other users"

# A settings file in an unexpected shape is ignored, not a crash at start-up.
for content in ("[]", "null", '"x"', "{", "", '{"boxes": 5, "pin": 3, "activation_code": []}',
                '{"boxes": [null, 3, {"name": "no id"}, {"terminal_id": "1|aa", "name": "Ok"}], "device_id": 5}'):
    folder = tempfile.mkdtemp()
    with open(os.path.join(folder, "config.json"), "w", encoding="utf-8") as f:
        f.write(content)
    config.init(folder)
    assert not config.is_configured()
    assert [b["terminal_id"] for b in config.get_boxes()] == (["1|aa"] if "Ok" in content else []), content
    assert re.fullmatch(r"[0-9a-f]{16}", config.get_device_id())
    assert json.load(open(os.path.join(folder, "config.json"), encoding="utf-8"))["device_id"] == config.get_device_id()

print("CONFIG_TEST_PASSED")
