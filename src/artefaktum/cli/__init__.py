"""The artefaktum command-line interface. Needs the `cli` extra: pip install "artefaktum[cli]"."""

from __future__ import annotations

import sys


def main() -> None:
    try:
        import click  # noqa: F401 - only checking that the extra is installed
    except ModuleNotFoundError:
        sys.stderr.write('the CLI needs an extra: pip install "artefaktum[cli]"\n')
        raise SystemExit(1) from None
    from .app import app

    app()
