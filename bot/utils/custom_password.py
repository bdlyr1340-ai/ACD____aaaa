"""Per-user custom password store (JSON file).

Lets a user pin a fixed new password that will be used for every rotation
instead of a random one. Persists across restarts.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

_LOCK = threading.Lock()
_PATH = Path(os.getenv("CUSTOM_PWD_FILE", "/tmp/custom_passwords.json"))


def _load() -> dict:
    if not _PATH.exists():
        return {}
    try:
        return json.loads(_PATH.read_text("utf-8") or "{}")
    except Exception:
        return {}


def _save(data: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    tmp.replace(_PATH)


def get(user_id: int) -> Optional[str]:
    with _LOCK:
        return _load().get(str(user_id)) or None


def set_password(user_id: int, password: str) -> None:
    with _LOCK:
        d = _load()
        d[str(user_id)] = password
        _save(d)


def clear(user_id: int) -> None:
    with _LOCK:
        d = _load()
        d.pop(str(user_id), None)
        _save(d)
