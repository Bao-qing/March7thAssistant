from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from collections.abc import Mapping
from typing import Any


DEFAULT_CORE_CONFIG: dict[str, dict[str, Any]] = {
    "device_profile": {
        "device_id": "88631514-ee79-4cd7-820d-48c70d9a222d",
        "os": "Linux undefined",
        "model": "Unknown",
        "cpu_cores": 16,
        "cpu_freq": 16,
        "cpu_type": "Unknown",
        "memory_gb": 8,
        "soc": "Unknown",
        "gpu_model": "amd radeon vega series / radeon vega mobile",
        "screen_width": 1920,
        "screen_height": 1080,
        "dpi": 96,
        "device_name": "Unknown",
        "sys_version": "Linux undefined",
    },
    "browser_profile": {
        "user_agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        ),
        "sec_ch_ua": '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
        "app_version": "4.3.0",
        "web_device_name": "Chrome",
        "web_device_model": "Chrome%20145.0.0.0",
        "web_device_os": "Linux%2064-bit",
    },
    "protocol_profile": {
        "client_lib": "python-aiortc",
        "sdk_webview_ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome Safari/537.36",
    },
    "session_profile": {
        "graphics_mode": 0,
        "bitrate_multiplier": 1.875,
        "resolution": "1920x1080",
        "fps": 30,
        "bit_rate": 10240000,
    },
}


def _deep_update(base: dict[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in updates.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def _to_namespace(value: Any) -> Any:
    if isinstance(value, Mapping):
        return SimpleNamespace(**{key: _to_namespace(item) for key, item in value.items()})
    return value


def strip_json_comments(text: str) -> str:
    """Remove // comments while preserving string contents."""
    out: list[str] = []
    in_string = False
    escaped = False
    i = 0
    while i < len(text):
        char = text[i]
        next_char = text[i + 1] if i + 1 < len(text) else ""
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "\"":
                in_string = False
            i += 1
            continue
        if char == "\"":
            in_string = True
            out.append(char)
            i += 1
            continue
        if char == "/" and next_char == "/":
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue
        out.append(char)
        i += 1
    return "".join(out)


def loads_json_with_comments(text: str) -> dict[str, Any]:
    data = json.loads(strip_json_comments(text))
    if not isinstance(data, dict):
        raise ValueError("core config must be a JSON object")
    return data


class CoreConfig:
    """Core runtime profile with attribute access."""

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        merged = deepcopy(DEFAULT_CORE_CONFIG)
        if data:
            _deep_update(merged, data)
        self.raw = merged
        for key, value in merged.items():
            setattr(self, key, _to_namespace(value))


def normalize_core_config(value: CoreConfig | Mapping[str, Any] | None) -> CoreConfig:
    if isinstance(value, CoreConfig):
        return value
    return CoreConfig(value)


__all__ = [
    "CoreConfig",
    "DEFAULT_CORE_CONFIG",
    "loads_json_with_comments",
    "normalize_core_config",
    "strip_json_comments",
]
