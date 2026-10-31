"""How long the FetchTV session outlives the Remote: 10 minutes on a server, none in the on-Remote package, overridable."""
import os
import sys
import tempfile

os.environ["UC_DISABLE_MDNS_PUBLISH"] = "true"
os.environ["UC_CONFIG_HOME"] = tempfile.mkdtemp()

import _path  # noqa: E402 - puts intg-fetchtv on sys.path
os.chdir(_path.INTG_DIR)

import box_manager  # noqa: E402


def grace(env=None, frozen=False):
    os.environ.pop("UC_SESSION_GRACE_S", None)
    if env is not None:
        os.environ["UC_SESSION_GRACE_S"] = env
    if frozen:
        sys.frozen = True
    try:
        return box_manager._grace_from_environment()
    finally:
        if hasattr(sys, "frozen"):
            del sys.frozen
        os.environ.pop("UC_SESSION_GRACE_S", None)


# The container image runs plain Python; the on-Remote package is built with PyInstaller.
assert grace() == 600.0
assert grace(frozen=True) == 0.0

# The setting wins over either default; 0 means close at once.
assert grace("120") == 120.0 and grace("120", frozen=True) == 120.0
assert grace("0") == 0.0 and grace("0.5") == 0.5

# Anything that is not a number of seconds is ignored, falling back to the default.
for bad in ("", "soon", "-5", "nan", "inf", "-inf"):
    assert grace(bad) == 600.0, bad
    assert grace(bad, frozen=True) == 0.0, bad

print("SESSION_GRACE_TEST_PASSED")
