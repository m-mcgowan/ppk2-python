import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ppk2._daemon_lib import service


def test_get_service_manager_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    sm = service.get_service_manager()
    assert isinstance(sm, service.LaunchdManager)


def test_get_service_manager_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    sm = service.get_service_manager()
    assert isinstance(sm, service.SystemdManager)


def test_get_service_manager_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    sm = service.get_service_manager()
    assert isinstance(sm, service.WinServiceManager)


def test_systemd_install_raises_not_implemented():
    sm = service.SystemdManager()
    with pytest.raises(NotImplementedError):
        sm.install("ABC")


def test_winservice_install_raises_not_implemented():
    sm = service.WinServiceManager()
    with pytest.raises(NotImplementedError):
        sm.install("ABC")


def test_launchd_label_format():
    sm = service.LaunchdManager()
    assert sm._label("ABC123") == "com.ppk2.daemon.ABC123"


def test_launchd_plist_path(tmp_path, monkeypatch):
    sm = service.LaunchdManager()
    monkeypatch.setattr(sm, "PLIST_DIR", tmp_path / "LaunchAgents")
    p = sm._plist_path("ABC123")
    assert p == tmp_path / "LaunchAgents" / "com.ppk2.daemon.ABC123.plist"


def test_launchd_install_writes_plist_and_bootstraps(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    sm = service.LaunchdManager()
    monkeypatch.setattr(sm, "PLIST_DIR", tmp_path / "LaunchAgents")

    completed = MagicMock()
    completed.returncode = 0
    with patch("subprocess.run", return_value=completed) as run:
        sm.install("ABC123")

    plist_path = tmp_path / "LaunchAgents" / "com.ppk2.daemon.ABC123.plist"
    assert plist_path.exists()

    import plistlib
    payload = plistlib.loads(plist_path.read_bytes())
    assert payload["Label"] == "com.ppk2.daemon.ABC123"
    assert payload["KeepAlive"] is True
    assert payload["RunAtLoad"] is True
    assert payload["ThrottleInterval"] == 10
    assert payload["ProgramArguments"][1] == "-u"
    # bootout (cleanup) + bootstrap = two calls
    assert run.call_count == 2


def test_launchd_uninstall_removes_plist(tmp_path, monkeypatch):
    sm = service.LaunchdManager()
    monkeypatch.setattr(sm, "PLIST_DIR", tmp_path / "LaunchAgents")
    plist_path = tmp_path / "LaunchAgents" / "com.ppk2.daemon.ABC123.plist"
    plist_path.parent.mkdir(parents=True)
    plist_path.write_bytes(b"<?xml version='1.0'?><plist/>")

    completed = MagicMock(); completed.returncode = 0
    with patch("subprocess.run", return_value=completed):
        sm.uninstall("ABC123")

    assert not plist_path.exists()


def test_launchd_list_installed(tmp_path, monkeypatch):
    sm = service.LaunchdManager()
    monkeypatch.setattr(sm, "PLIST_DIR", tmp_path / "LaunchAgents")
    sm.PLIST_DIR.mkdir(parents=True)
    (sm.PLIST_DIR / "com.ppk2.daemon.AAA.plist").write_bytes(b"")
    (sm.PLIST_DIR / "com.ppk2.daemon.BBB.plist").write_bytes(b"")
    (sm.PLIST_DIR / "com.something.else.plist").write_bytes(b"")
    assert sorted(sm.list_installed()) == ["AAA", "BBB"]
