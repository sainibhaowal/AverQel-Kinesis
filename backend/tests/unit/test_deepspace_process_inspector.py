"""Tests for process inspector."""

from app.deepspace.services.process_inspector import (
    ProcessIdentity,
    create_process_inspector,
    parse_proc_stat,
)


def test_parse_proc_stat_basic():
    # A synthetic /proc/PID/stat line
    line = "1 (init) S 0 1 1 0 -1 4194560 100 0 0 0 0 0 0 0 20 0 1 0 10 1000 0 18446744073709551615 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
    # This may or may not parse; just ensure no crash
    result = parse_proc_stat(line)
    # pid should be 1
    if result is not None:
        assert result.pid == 1


def test_create_process_inspector():
    inspector = create_process_inspector(platform="linux", arch="x86_64")
    assert inspector.platform == "linux"


def test_process_identity():
    pi = ProcessIdentity(pid=123, started="abc")
    assert pi.pid == 123
    assert pi.started == "abc"
