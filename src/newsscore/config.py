"""Per-user store of configured news sources.

Location (first match wins):
    1. ``$NEWSSCORE_CONFIG``
    2. ``platformdirs.user_config_dir("newsscore")/sources.json``
       (``%APPDATA%\\newsscore`` on Windows, ``~/.config/newsscore`` on Linux,
       ``~/Library/Application Support/newsscore`` on macOS)

Shape::

    {
      "sources": {
        "finnhub":  {"type": "finnhub", "api_key": "abc", "options": {}},
        "yahoo":    {"type": "yahoo",   "api_key": null,  "options": {}}
      }
    }

``api_key`` may be null; the source then falls back to its environment variable.

API keys can also live in a ``.env`` file (``KEY=VALUE`` lines, ``#`` comments,
optional quotes). :func:`load_env` is called by the CLI and by
``NewsScorer.from_config()``; it never overrides variables already set in the
environment. Lookup: ``$NEWSSCORE_ENV`` alone if set, else ``./.env``, then ``<config dir>/.env``.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir


def env_candidates() -> list[Path]:
    """Where :func:`load_env` looks. ``NEWSSCORE_ENV``, when set, is the only candidate."""
    override = os.environ.get("NEWSSCORE_ENV")
    if override:
        return [Path(override).expanduser()]
    return [Path.cwd() / ".env", Path(user_config_dir("newsscore")) / ".env"]


def parse_env(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines. Supports ``export KEY=...``, quotes and ``#`` comments."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif value.startswith("#"):
            value = ""  # blank value followed by a comment
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def load_env(path: Path | str | None = None, *, override: bool = False) -> Path | None:
    """Load the first ``.env`` found into ``os.environ``. Returns the path used, or ``None``.

    Empty values are skipped so a template with blank keys is harmless. Existing
    environment variables win unless ``override=True``.
    """
    candidates = [Path(path).expanduser()] if path else env_candidates()
    for candidate in candidates:
        if not candidate.is_file():
            continue
        for key, value in parse_env(candidate.read_text(encoding="utf-8")).items():
            if value and (override or key not in os.environ):
                os.environ[key] = value
        return candidate
    return None


def config_path() -> Path:
    override = os.environ.get("NEWSSCORE_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path(user_config_dir("newsscore")) / "sources.json"


@dataclass(slots=True)
class SourceSpec:
    type: str
    name: str
    api_key: str | None = None
    options: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("name")
        return data


class SourceStore:
    """Read and write the JSON file. Every method reloads from disk, so the store
    is always consistent with what other processes wrote."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else config_path()

    def load(self) -> dict[str, SourceSpec]:
        if not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8") as fh:
            data = json.load(fh) or {}
        specs: dict[str, SourceSpec] = {}
        for name, item in (data.get("sources") or {}).items():
            specs[name] = SourceSpec(
                type=item["type"],
                name=name,
                api_key=item.get("api_key"),
                options=dict(item.get("options") or {}),
            )
        return specs

    def save(self, specs: dict[str, SourceSpec]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"sources": {name: spec.to_dict() for name, spec in specs.items()}}
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, self.path)
        try:  # keys live here; tighten permissions where the OS honours them
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def add(self, spec: SourceSpec, *, replace: bool = False) -> None:
        specs = self.load()
        if spec.name in specs and not replace:
            raise KeyError(f"source {spec.name!r} already exists (use replace)")
        specs[spec.name] = spec
        self.save(specs)

    def remove(self, name: str) -> None:
        specs = self.load()
        if name not in specs:
            raise KeyError(f"no source named {name!r}")
        del specs[name]
        self.save(specs)
