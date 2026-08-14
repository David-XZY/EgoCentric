from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PACKAGE_ROOT / "config" / "default.yaml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """读取默认配置，并按需覆盖用户配置。"""
    with DEFAULT_CONFIG_PATH.open(encoding="utf-8") as file:
        config = yaml.safe_load(file) or {}
    if path is None:
        return config
    with Path(path).expanduser().open(encoding="utf-8") as file:
        override = yaml.safe_load(file) or {}
    return _deep_merge(config, override)


def output_root(config: dict[str, Any]) -> Path:
    value = str(config.get("workspace", {}).get("output_root", "~/EgoCentricData"))
    return Path(value).expanduser().resolve()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result
