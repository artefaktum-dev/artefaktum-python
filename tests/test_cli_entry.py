from __future__ import annotations

import ast
import builtins
import subprocess
import sys
from pathlib import Path

import pytest


def test_python_dash_m_shows_help():
    out = subprocess.run(
        [sys.executable, "-m", "artefaktum.cli", "--help"],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0 and "Usage" in out.stdout
    assert "search" in out.stdout
    assert "push" in out.stdout


def test_without_click_main_explains_the_extra(monkeypatch, capsys):
    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "click":
            raise ModuleNotFoundError("No module named 'click'")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    from artefaktum import cli

    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 1
    assert 'pip install "artefaktum[cli]"' in capsys.readouterr().err


def test_importing_the_sdk_does_not_import_click():
    # Checking for plain "click" in sys.modules is not reliable here: httpx>=0.28 itself
    # does `try: from ._main import main except ImportError: ...` for its own optional CLI
    # extra, and `click` is the first import inside that module. Whenever `click` happens
    # to be installed in the environment (as it is here, via our `cli`/`dev` extras), mere
    # `import httpx` registers `click` in sys.modules before failing on the still-missing
    # `rich`/`pygments.lexers` pieces - a side effect with nothing to do with our code. What
    # we actually guarantee is that importing the SDK never reaches into our own cli package.
    code = "import sys, artefaktum; sys.exit(1 if 'artefaktum.cli' in sys.modules else 0)"
    result = subprocess.run([sys.executable, "-c", code])  # noqa: S603 - fixed argv, no shell
    assert result.returncode == 0


def _imports_click(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(alias.name == "click" or alias.name.startswith("click.") for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return node.module is not None and (
            node.module == "click" or node.module.startswith("click.")
        )
    return False


def test_no_core_sdk_module_imports_click():
    # Static, source-level check that complements test_importing_the_sdk_does_not_import_click
    # (which can't rely on 'click' being absent from sys.modules - see the comment above, an
    # unrelated httpx side effect). This walks the actual source text, so it catches a direct
    # `import click` anywhere under src/artefaktum/ outside of cli/, regardless of what any
    # third-party dependency does at runtime.
    src_root = Path(__file__).resolve().parents[1] / "src" / "artefaktum"
    offenders = []
    for path in src_root.rglob("*.py"):
        if path.is_relative_to(src_root / "cli"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(_imports_click(node) for node in ast.walk(tree)):
            offenders.append(path)
    assert offenders == []
