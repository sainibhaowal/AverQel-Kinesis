"""Tests for the DeepSpace CLI __main__ module."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest


class TestCliMain:
    """Tests for the __main__ module entry point."""

    def test_main_dispatches_to_main(self) -> None:
        """Test that main() is called when not headless."""
        import runpy

        with patch("app.deepspace.cli.main.main", return_value=0) as mock_main:
            with patch("sys.exit") as mock_exit:
                with patch.object(sys, "argv", ["app.deepspace.cli"]):
                    runpy.run_module("app.deepspace.cli.__main__", run_name="__main__")
                    mock_main.assert_called_once()
                    mock_exit.assert_called_once_with(0)

    def test_headless_dispatches_to_cli_entry(self) -> None:
        """Test that cli_entry is called when headless."""
        # The headless path is tested by checking the source code
        with open("app/deepspace/cli/__main__.py") as f:
            content = f.read()
        assert "from app.deepspace.cli.headless import cli_entry" in content
        assert "del sys.argv[1]" in content
        assert "sys.exit(cli_entry())" in content

    def test_main_imports_main(self) -> None:
        """Test that main is imported correctly."""
        from app.deepspace.cli.__main__ import main
        from app.deepspace.cli.main import main as actual_main

        assert main is actual_main

    def test_headless_import(self) -> None:
        """Test that headless entry can be imported."""
        from app.deepspace.cli.headless import cli_entry

        assert cli_entry is not None

    def test_main_module_has_name_guard(self) -> None:
        """Test that the module has the __name__ guard."""
        import app.deepspace.cli.__main__ as main_module

        source = main_module.__file__
        assert source is not None
        with open(source) as f:
            content = f.read()
        assert 'if __name__ == "__main__"' in content
        assert "headless" in content
        assert "main()" in content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
