"""``python -m attune`` — start the always-on process (design doc 4.6).

Deliberately thin: all the wiring logic lives in ``runtime.py`` and is
independently tested there. This file just configures logging (from
``ATTUNE_LOG_LEVEL`` / ``ATTUNE_LOG_JSON``) and calls it.
"""

import os

from dotenv import load_dotenv

from .logging_setup import configure
from .runtime import build_runtime

if __name__ == "__main__":  # pragma: no cover - requires live services
    load_dotenv()
    configure(
        level=os.environ.get("ATTUNE_LOG_LEVEL", "INFO"),
        json_mode=os.environ.get("ATTUNE_LOG_JSON", "") == "1",
    )
    build_runtime().run()
