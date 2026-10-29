"""Run the DeepSpace command line with ``python -m app.deepspace.cli``."""

from __future__ import annotations

import sys

from app.deepspace.cli.main import main

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "headless":
        from app.deepspace.cli.headless import cli_entry

        del sys.argv[1]
        sys.exit(cli_entry())
    sys.exit(main())
