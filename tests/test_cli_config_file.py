from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from artefaktum.cli import config_file as cf


def test_paths_follow_xdg_then_home_then_appdata(tmp_path):
    assert (
        cf.config_path({"XDG_CONFIG_HOME": "/x", "HOME": "/h"}, "linux").as_posix()
        == "/x/artefaktum/config.json"
    )
    assert (
        cf.config_path({"HOME": "/h"}, "darwin").as_posix() == "/h/.config/artefaktum/config.json"
    )
    win = cf.config_path({"APPDATA": r"C:\Users\t\AppData\Roaming"}, "win32")
    assert win.name == "config.json" and win.parent.name == "artefaktum"


def test_save_load_roundtrip_keeps_only_known_keys(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    path = cf.save({"api_key": "ak_1", "base_url": "http://x", "project": "p", "junk": "no"}, env)
    assert path == tmp_path / "artefaktum" / "config.json"
    assert json.loads(path.read_text()) == {
        "api_key": "ak_1",
        "base_url": "http://x",
        "project": "p",
    }
    assert cf.load(env) == {"api_key": "ak_1", "base_url": "http://x", "project": "p"}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_file_is_0600_and_dir_0700_even_when_the_file_already_existed_loose(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    d = tmp_path / "artefaktum"
    d.mkdir()
    p = d / "config.json"
    p.write_text("{}")
    os.chmod(p, 0o644)
    cf.save({"api_key": "ak_1"}, env)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_load_is_empty_when_missing_or_corrupt(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    assert cf.load(env) == {}
    (tmp_path / "artefaktum").mkdir()
    (tmp_path / "artefaktum" / "config.json").write_text("{not json")
    assert cf.load(env) == {}


def test_clear_removes_the_file_and_reports_it(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    assert cf.clear(env) is False
    cf.save({"api_key": "ak_1"}, env)
    assert cf.clear(env) is True and cf.load(env) == {}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks / O_NOFOLLOW")
def test_save_refuses_a_symlinked_config_path(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    d = tmp_path / "artefaktum"
    d.mkdir()
    target = tmp_path / "elsewhere.txt"
    target.write_text("untouched")
    (d / "config.json").symlink_to(target)
    with pytest.raises(OSError):
        cf.save({"api_key": "ak_1"}, env)
    assert target.read_text() == "untouched"
