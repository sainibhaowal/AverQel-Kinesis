from __future__ import annotations

import sys
from typing import TextIO


def _use_backslashreplace(stream: TextIO | None) -> None:
    if stream is None:
        return

    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return

    try:
        reconfigure(errors="backslashreplace")
    except (OSError, TypeError, ValueError):
        # Some embedded or test streams cannot be reconfigured. Do not make
        # application startup depend on that optional stream capability.
        return


def configure_console_output() -> None:
    """Keep unencodable console text from aborting application startup."""
    _use_backslashreplace(sys.stdout)
    _use_backslashreplace(sys.stderr)
