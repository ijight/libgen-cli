"""User configuration: XDG-respecting storage of mirror ranking + overrides.

The on-disk format is a tiny TOML document::

    mirrors = [
      "https://libgen.li",
      "https://libgen.la",
    ]

We deliberately avoid a TOML writer dependency; the file is structured enough
that hand-rolled emission is trivial and bug-free.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from pathlib import Path

from libgen_cli.errors import ConfigError

APP_NAME = "libgen-cli"
ENV_MIRROR_VAR = "LIBGEN_MIRROR"


def config_dir() -> Path:
    """Return the XDG-respecting config directory for the app."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


def config_path() -> Path:
    return config_dir() / "config.toml"


def load_mirror_overrides() -> list[str]:
    """Load persisted mirror ranking from the config file.

    Returns an empty list if no config exists. Raises :class:`ConfigError` only
    on malformed TOML — missing keys are tolerated.
    """
    path = config_path()
    if not path.exists():
        return []
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"failed to read {path}: {exc}") from exc

    raw = data.get("mirrors", [])
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str) and item.strip()]


def save_mirror_ranking(mirrors: list[str]) -> Path:
    """Persist the given mirror ranking, atomically. Returns the path written."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    body_lines = ["# libgen-cli config (auto-generated; safe to edit)\n", "mirrors = [\n"]
    body_lines.extend(f'    "{m}",\n' for m in mirrors)
    body_lines.append("]\n")
    body = "".join(body_lines)

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
        prefix=".config-",
        suffix=".toml.tmp",
    ) as tmp:
        tmp.write(body)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)
    return path


def env_mirror_overrides() -> list[str]:
    """Parse ``LIBGEN_MIRROR`` (comma-separated) into a list of URLs."""
    raw = os.environ.get(ENV_MIRROR_VAR)
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]
