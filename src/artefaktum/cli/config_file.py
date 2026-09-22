from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

KEYS = ("api_key", "base_url", "project")


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def config_dir(env: Mapping[str, str] | None = None, platform: str = sys.platform) -> Path:
    e = _env(env)
    if platform == "win32" and e.get("APPDATA"):
        return Path(e["APPDATA"]) / "artefaktum"
    base = e.get("XDG_CONFIG_HOME") or str(Path(e.get("HOME") or Path.home()) / ".config")
    return Path(base) / "artefaktum"


def config_path(env: Mapping[str, str] | None = None, platform: str = sys.platform) -> Path:
    return config_dir(env, platform) / "config.json"


def load(env: Mapping[str, str] | None = None) -> dict[str, str]:
    try:
        data = json.loads(config_path(env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: str(data[k]) for k in KEYS if data.get(k)}


def save(values: Mapping[str, str], env: Mapping[str, str] | None = None) -> Path:
    path = config_path(env)
    # mkdir's `mode` is ignored when the directory already exists and is masked by the
    # process umask on creation either way, so the chmod below still does the real work.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    body = json.dumps({k: values[k] for k in KEYS if values.get(k)}, indent=2) + "\n"
    # Create with 0600 from the start; chmod afterwards too, because O_CREAT's mode is
    # ignored when the file already exists with looser permissions. O_NOFOLLOW (where the
    # platform has it) refuses a pre-planted symlink at the config path instead of writing
    # through it - callers must be ready for save() to raise OSError in that case.
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(path, 0o600)
    return path


def clear(env: Mapping[str, str] | None = None) -> bool:
    try:
        config_path(env).unlink()
    except FileNotFoundError:
        return False
    return True
