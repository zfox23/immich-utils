"""Settings loading shared by the immich-utils scripts.

Settings are merged from these sources, later ones winning:
  1. <repo root>/config.json       shared settings (server_url, api_key)
  2. <script folder>/config.json   script-specific settings (or the file given with --config)
  3. environment variables
  4. command-line flags
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT_CONFIG = REPO_ROOT / "config.json"

SHARED_ENV = {"server_url": "IMMICH_URL", "api_key": "IMMICH_API_KEY"}


class ConfigError(Exception):
    pass


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path} is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    return data


def normalize_server_url(value: Any) -> str:
    url = str(value).strip().rstrip("/")
    if url.endswith("/api"):
        url = url[:-4]
    if not url.startswith(("http://", "https://")):
        raise ConfigError(f"server_url must start with http:// or https:// (got {url!r})")
    return url


def load_config(
    script_dir: Path,
    config_arg: Optional[str] = None,
    cli_values: Optional[Mapping[str, Any]] = None,
    required: Iterable[str] = ("server_url", "api_key"),
    extra_env: Optional[Mapping[str, str]] = None,
    root_config: Optional[Path] = None,
) -> Dict[str, Any]:
    """Merge settings from the shared config, the script config, env vars and CLI values."""
    cfg: Dict[str, Any] = {}
    root_path = root_config if root_config is not None else ROOT_CONFIG
    if root_path.exists():
        cfg.update(_read_json(root_path))

    script_path = Path(config_arg) if config_arg else Path(script_dir) / "config.json"
    if script_path.exists():
        cfg.update(_read_json(script_path))
    elif config_arg:
        raise ConfigError(f"Config file not found: {script_path}")

    env_map = dict(SHARED_ENV)
    env_map.update(extra_env or {})
    for key, env in env_map.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    for key, value in (cli_values or {}).items():
        if value:
            cfg[key] = value

    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise ConfigError(
            "Missing setting(s): " + ", ".join(missing) + f". Set them in {root_path} (shared) or "
            f"{script_path} (this script only), via environment variables, or on the command line. "
            "See the config.example.json files."
        )
    if cfg.get("server_url"):
        cfg["server_url"] = normalize_server_url(cfg["server_url"])
    return cfg
