"""The DeepSpace command-line interface.

The package holds the client for the DeepSpace HTTP API and the command that
drives it, so a terminal can do what the web application does.
"""

from app.deepspace.cli.main import main

__all__ = ["main"]
