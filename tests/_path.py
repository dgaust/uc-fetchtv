"""Make the intg-fetchtv package importable from the tests directory.

The tests are standalone scripts (each spins up its own event loop and fake
servers, and some rely on module-global driver state), so they are run one
per process - `python tests/test_x.py` - rather than under a shared pytest
session. Importing this module first puts intg-fetchtv on sys.path.
"""

import pathlib
import sys

INTG_DIR = pathlib.Path(__file__).resolve().parents[1] / "intg-fetchtv"
sys.path.insert(0, str(INTG_DIR))
