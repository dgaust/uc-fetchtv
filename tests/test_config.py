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

# A separate install gets its own.
other = tempfile.mkdtemp()
config.init(other)
assert config.get_device_id() != device_id and config.get_drm_id() != drm_id

print("CONFIG_TEST_PASSED")
