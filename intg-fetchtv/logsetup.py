"""
Log level selection.

The level comes from ``UC_LOG_LEVEL`` when that is set (Docker, Cosmos). The
Remote cannot set environment variables for an installed driver and has no log
level setting of its own - it only stores what a driver prints and lets you
download it - so there the "Debug logging" tick box on the setup screen, saved
in the config file, turns DEBUG on. Otherwise the level is INFO.

:license: MPL-2.0, see LICENSE for more details.
"""

import logging
import os

import config

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
# The driver's own loggers (the library's are set from the same level, below).
_DRIVER_LOGGERS = (
    "driver", "box_manager", "setup_flow", "entities", "remote_entity", "sensors", "config", "fetchtv", "logsetup",
)

_LOG = logging.getLogger(__name__)


def environment_level() -> tuple[str | None, bool]:
    """``UC_LOG_LEVEL``, and whether it was set to something that is not a level (then it is ignored)."""
    raw = os.getenv("UC_LOG_LEVEL")
    if raw is None or not raw.strip():
        return None, False
    level = raw.strip().upper()
    return (level, False) if level in LEVELS else (None, True)


def choose() -> str:
    """The level to log at: ``UC_LOG_LEVEL``, else DEBUG if the setup option is ticked, else INFO."""
    level, _bad = environment_level()
    if level:
        return level
    return "DEBUG" if config.get_debug_logging() else "INFO"


def source() -> str:
    """Where the chosen level comes from, for the log."""
    level, _bad = environment_level()
    if level:
        return "UC_LOG_LEVEL"
    return "the Debug logging setting" if config.get_debug_logging() else "the default"


def apply(level: str) -> None:
    """Set the driver's loggers, and the library's (which logs every websocket message, so only at DEBUG)."""
    for name in _DRIVER_LOGGERS:
        logging.getLogger(name).setLevel(level)
    library = "DEBUG" if level == "DEBUG" else "INFO"
    logging.getLogger("ucapi").setLevel(library)
    logging.getLogger("ucapi.api").setLevel(library)
