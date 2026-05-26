"""Entry point for ``python -m libgen_cli``."""

from __future__ import annotations

from libgen_cli.cli import app


def main() -> None:
    app()


if __name__ == "__main__":
    main()
