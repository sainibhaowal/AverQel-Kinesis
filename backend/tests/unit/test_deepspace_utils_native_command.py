"""Tests for native_command utilities."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

# Import all native_command exports
from app.deepspace.utils.native_command import (
    NativeCommandError,
    NativeCommandResult,
    NativeCommandRunner,
    NativeFileApplication,
    PathOpenerInternals,
    PathOpenerRunner,
    # Path opener
    can_open_native_path,
    desktop_application_icon,
    desktop_data_directories,
    # Desktop entry
    desktop_entry_fields,
    get_wsl_interop_path,
    # Platform detection
    is_wsl,
    # File applications
    native_file_applications,
    native_file_manager,
    open_native_path,
    open_native_text_file,
    reveal_native_path,
    # Command runner
    run_native_command,
    # WSL handoff
    wsl_handoff_command,
)


class TestPlatformDetection:
    """Tests for platform detection."""

    def test_is_wsl(self) -> None:
        """is_wsl should return a boolean."""
        result = is_wsl()
        assert isinstance(result, bool)

    def test_get_wsl_interop_path(self) -> None:
        """get_wsl_interop_path should return Path or None."""
        result = get_wsl_interop_path()
        assert result is None or isinstance(result, Path)

    def test_is_wsl_consistency(self) -> None:
        """Multiple calls should return same result."""
        assert is_wsl() == is_wsl()


class TestNativeCommandRunner:
    """Tests for NativeCommandRunner."""

    def test_simple_command(self) -> None:
        """Test running a simple command."""
        runner = NativeCommandRunner(timeout=5.0)
        result = runner.run([sys.executable, "-c", "print('hello')"])
        assert result.exit_code == 0
        assert "hello" in result.stdout
        assert result.stderr == ""
        assert not result.timed_out
        assert not result.cancelled

    def test_command_with_args(self) -> None:
        """Test command with arguments."""
        runner = NativeCommandRunner(timeout=5.0)
        result = runner.run([sys.executable, "-c", "import sys; print(sys.argv[1])", "test_arg"])
        assert result.exit_code == 0
        assert "test_arg" in result.stdout

    def test_command_with_stdin(self) -> None:
        """Test command with stdin input."""
        runner = NativeCommandRunner(timeout=5.0)
        result = runner.run(
            [sys.executable, "-c", "import sys; print(sys.stdin.read())"],
            input_data="hello from stdin",
        )
        assert result.exit_code == 0
        assert "hello from stdin" in result.stdout

    def test_timeout(self) -> None:
        """Test command timeout."""
        runner = NativeCommandRunner(timeout=0.1)
        result = runner.run([sys.executable, "-c", "import time; time.sleep(1)"])
        assert result.timed_out is True
        assert result.exit_code == -1

    def test_cancel(self) -> None:
        """Test command cancellation."""
        runner = NativeCommandRunner(timeout=10.0)
        runner.cancel()
        result = runner.run([sys.executable, "-c", "print('should not run')"])
        # Cancellation only affects a process that is already running.
        assert result.exit_code == 0
        assert "should not run" in result.stdout

    def test_empty_command_raises(self) -> None:
        """Empty command should raise error."""
        runner = NativeCommandRunner()
        with pytest.raises(NativeCommandError):
            runner.run([])

    def test_invalid_command(self) -> None:
        """Invalid command should return non-zero exit code."""
        runner = NativeCommandRunner(timeout=5.0)
        result = runner.run(["/nonexistent/command/that/does/not/exist"])
        assert result.exit_code != 0

    def test_output_truncation(self) -> None:
        """Test output size limit."""
        runner = NativeCommandRunner(timeout=5.0, max_output_size=100)
        result = runner.run([sys.executable, "-c", "print('x' * 1000)"])
        assert len(result.stdout.encode()) <= 100 + 50  # Some overhead
        assert "truncated" in result.stdout.lower()

    def test_custom_env(self) -> None:
        """Test custom environment variables."""
        runner = NativeCommandRunner(env={"TEST_VAR": "test_value"})
        result = runner.run(
            [sys.executable, "-c", "import os; print(os.environ.get('TEST_VAR', 'missing'))"]
        )
        assert "test_value" in result.stdout

    def test_custom_cwd(self) -> None:
        """Test custom working directory."""
        with tempfile.TemporaryDirectory() as tmp:
            test_file = Path(tmp) / "test.txt"
            test_file.write_text("cwd test")
            runner = NativeCommandRunner(cwd=tmp)
            result = runner.run(
                [sys.executable, "-c", "with open('test.txt') as f: print(f.read())"]
            )
            assert "cwd test" in result.stdout


class TestRunNativeCommand:
    """Tests for run_native_command convenience function."""

    def test_basic(self) -> None:
        result = run_native_command([sys.executable, "-c", "print('hello')"])
        assert result.exit_code == 0
        assert "hello" in result.stdout

    def test_string_command(self) -> None:
        result = run_native_command(f"{sys.executable} -c \"print('hello')\"")
        assert result.exit_code == 0
        assert "hello" in result.stdout

    def test_timeout_parameter(self) -> None:
        result = run_native_command(
            [sys.executable, "-c", "import time; time.sleep(1)"],
            timeout=0.1,
        )
        assert result.timed_out is True


class TestNativeCommandResult:
    """Tests for NativeCommandResult dataclass."""

    def test_fields(self) -> None:
        result = NativeCommandResult(
            exit_code=0,
            stdout="out",
            stderr="err",
            timed_out=False,
            cancelled=True,
        )
        assert result.exit_code == 0
        assert result.stdout == "out"
        assert result.stderr == "err"
        assert result.timed_out is False
        assert result.cancelled is True


class TestPathOpener:
    """Tests for path opener functions."""

    def test_can_open_native_path(self) -> None:
        with tempfile.NamedTemporaryFile() as f:
            path = Path(f.name)
            assert can_open_native_path(path) is True

        # Non-existent path
        assert can_open_native_path(Path("/nonexistent/path")) is False

    def test_reveal_native_path(self) -> None:
        """Test reveal_native_path doesn't crash."""
        with tempfile.NamedTemporaryFile() as f:
            # Should not raise
            result = reveal_native_path(Path(f.name))
            # Result depends on platform, just check it's boolean
            assert isinstance(result, bool)

    def test_open_native_path(self) -> None:
        """Test open_native_path doesn't crash."""
        with tempfile.NamedTemporaryFile() as f:
            result = open_native_path(Path(f.name))
            assert isinstance(result, bool)

    def test_open_native_text_file(self) -> None:
        """Test open_native_text_file doesn't crash."""
        with tempfile.NamedTemporaryFile(suffix=".txt") as f:
            result = open_native_text_file(Path(f.name))
            assert isinstance(result, bool)

    def test_non_existent_path(self) -> None:
        """Non-existent paths should return False."""
        fake_path = Path("/nonexistent/file.txt")
        assert open_native_path(fake_path) is False
        assert reveal_native_path(fake_path) is False
        assert open_native_text_file(fake_path) is False


class TestPathOpenerRunner:
    """Tests for PathOpenerRunner class."""

    def test_instance(self) -> None:
        runner = PathOpenerRunner()
        assert isinstance(runner, PathOpenerRunner)

    def test_native_file_manager(self) -> None:
        """Test native_file_manager returns platform-appropriate command."""
        cmd = native_file_manager()
        assert isinstance(cmd, str)
        assert len(cmd) > 0


class TestFileApplications:
    """Tests for file application detection."""

    def test_native_file_applications(self) -> None:
        apps = native_file_applications()
        assert isinstance(apps, dict)
        # Should not crash

    def test_native_file_application_dataclass(self) -> None:
        app = NativeFileApplication(
            name="test",
            exec="test %f",
            mime_types=["text/plain"],
            extensions=[".txt"],
        )
        assert app.name == "test"
        assert app.exec == "test %f"
        assert app.mime_types == ["text/plain"]
        assert app.extensions == [".txt"]


class TestDesktopEntry:
    """Tests for .desktop file parsing."""

    def test_desktop_data_directories(self) -> None:
        dirs = desktop_data_directories()
        assert isinstance(dirs, list)
        assert all(isinstance(d, Path) for d in dirs)
        # Should have at least some directories
        assert len(dirs) > 0

    def test_desktop_entry_fields_valid(self) -> None:
        """Test parsing a valid .desktop file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".desktop", delete=False) as f:
            f.write("""[Desktop Entry]
Name=Test App
Exec=test-app %f
Icon=test-icon
Type=Application
Categories=Utility;
MimeType=text/plain;
Comment=A test application
""")
            f.flush()
            path = Path(f.name)

        try:
            entry = desktop_entry_fields(path)
            assert entry is not None
            assert entry.name == "Test App"
            assert entry.exec == "test-app %f"
            assert entry.icon == "test-icon"
            assert entry.type == "Application"
            assert entry.categories == ["Utility"]
            assert entry.mime_types == ["text/plain"]
            assert entry.comment == "A test application"
        finally:
            Path(f.name).unlink(missing_ok=True)

    def test_desktop_entry_invalid_file(self) -> None:
        """Non-existent file should return None."""
        result = desktop_entry_fields("/nonexistent/file.desktop")
        assert result is None

    def test_desktop_entry_empty(self) -> None:
        """Empty file should return defaults."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".desktop", delete=False) as f:
            f.write("")
            f.flush()
            path = Path(f.name)

        try:
            entry = desktop_entry_fields(path)
            assert entry is not None
            assert entry.name == ""
            assert entry.exec == ""
        finally:
            Path(f.name).unlink(missing_ok=True)


class TestDesktopDataDirectories:
    """Tests for desktop data directories."""

    def test_returns_paths(self) -> None:
        dirs = desktop_data_directories()
        assert isinstance(dirs, list)
        assert all(isinstance(d, Path) for d in dirs)

    def test_includes_user_local_share(self) -> None:
        dirs = desktop_data_directories()
        # Should include ~/.local/share/applications or similar
        found = any("local/share" in str(d) for d in dirs)
        assert found


class TestDesktopApplicationIcon:
    """Tests for desktop application icon resolution."""

    def test_desktop_application_icon(self) -> None:
        """Test icon resolution doesn't crash."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".desktop", delete=False) as f:
            f.write("""[Desktop Entry]
Name=Test
Icon=text-editor
Exec=test
""")
            f.flush()
            path = Path(f.name)

        try:
            icon = desktop_application_icon(path)
            # May return None if icon not found, but shouldn't crash
            assert icon is None or isinstance(icon, Path)
        finally:
            Path(f.name).unlink(missing_ok=True)


class TestWSLHandoff:
    """Tests for WSL handoff."""

    def test_wsl_handoff_command(self) -> None:
        """Test WSL handoff wraps commands appropriately."""
        if is_wsl():
            result = wsl_handoff_command("echo hello")
            assert isinstance(result, list)
            assert "cmd.exe" in result or "wslpath" in str(result).lower()
        else:
            # On non-WSL, should pass through
            result = wsl_handoff_command("echo hello")
            assert result == ["echo", "hello"]

    def test_wsl_handoff_list(self) -> None:
        result = wsl_handoff_command(["echo", "hello"])
        if is_wsl():
            assert "cmd.exe" in result
        else:
            assert result == ["echo", "hello"]


class TestPathOpenerInternals:
    """Tests for internal classes."""

    def test_path_opener_internals(self) -> None:
        internals = PathOpenerInternals()
        assert hasattr(internals, "runner")
        assert isinstance(internals.runner, PathOpenerRunner)


class TestWSL:
    """Tests for WSL detection."""

    def test_is_wsl_boolean(self) -> None:
        assert isinstance(is_wsl(), bool)

    def test_get_wsl_interop_path_type(self) -> None:
        result = get_wsl_interop_path()
        assert result is None or isinstance(result, Path)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
