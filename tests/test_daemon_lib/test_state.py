import os
import sys
from pathlib import Path

import pytest

from ppk2._daemon_lib import state


def test_state_dir_unix(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    d = state.state_dir()
    assert d == tmp_path / ".local" / "state" / "ppk2"
    assert d.is_dir()


def test_state_dir_macos(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    d = state.state_dir()
    assert d == tmp_path / ".local" / "state" / "ppk2"
    assert d.is_dir()


def test_state_dir_windows_with_localappdata(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    d = state.state_dir()
    assert d == tmp_path / "AppData" / "Local" / "ppk2"
    assert d.is_dir()


def test_state_dir_windows_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    d = state.state_dir()
    assert d == tmp_path / "AppData" / "Local" / "ppk2"
