"""Which level the driver logs at: UC_LOG_LEVEL, else the setup screen's Debug logging option, else INFO."""
import logging
import os
import tempfile

import _path  # noqa: F401 - puts intg-fetchtv on sys.path

import config
import logsetup


def level(env=None):
    os.environ.pop("UC_LOG_LEVEL", None)
    if env is not None:
        os.environ["UC_LOG_LEVEL"] = env
    try:
        return logsetup.choose(), logsetup.source()
    finally:
        os.environ.pop("UC_LOG_LEVEL", None)


config.init(tempfile.mkdtemp())

# Default: INFO. The tick box (saved in the config) turns DEBUG on where there are no environment variables.
assert level() == ("INFO", "the default")
config.set_debug_logging(True)
assert level() == ("DEBUG", "the Debug logging setting")

# UC_LOG_LEVEL wins over the tick box, in either direction, in any case and with stray spaces.
assert level("warning") == ("WARNING", "UC_LOG_LEVEL")
assert level(" error ") == ("ERROR", "UC_LOG_LEVEL")
config.set_debug_logging(False)
assert level("debug") == ("DEBUG", "UC_LOG_LEVEL")

# Something that is not a level is ignored (and reported), not a crash.
for bad in ("verbose", "10", "TRACE"):
    os.environ["UC_LOG_LEVEL"] = bad
    assert logsetup.environment_level() == (None, True), bad
os.environ["UC_LOG_LEVEL"] = "  "
assert logsetup.environment_level() == (None, False)
os.environ.pop("UC_LOG_LEVEL")
assert logsetup.environment_level() == (None, False)

# Applying a level sets the driver's loggers; the library's messages (every websocket
# message, with the setup request) only show at DEBUG.
logsetup.apply("DEBUG")
assert logging.getLogger("fetchtv").level == logging.DEBUG and logging.getLogger("ucapi.api").level == logging.DEBUG
logsetup.apply("WARNING")
assert logging.getLogger("fetchtv").level == logging.WARNING and logging.getLogger("ucapi.api").level == logging.INFO

print("LOGSETUP_TEST_PASSED")
