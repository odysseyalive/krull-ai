# proxy/session_meta.py
# Helper for reading host-side session meta files and exposing them via contextvar
import contextvars
import json
import os
from pathlib import Path
from time import time
from typing import Dict

# Request-local session meta
_current_session_meta = contextvars.ContextVar("krull_session_meta", default={})
# Simple in-process cache with TTL
_cache: Dict[str, Dict] = {}
_CACHE_TTL = 5.0  # seconds

def load_session_meta_for(session_id: str, host_home: str = None) -> dict:
    """
    Read the session-meta file from the host home path, cache briefly, and return a dict.
    Expects files named: $HOME/.krull-session-meta-<session_id>.json or .krull-session-meta.json
    """
    host_home = host_home or os.environ.get("KRULL_HOST_HOME") or os.environ.get("HOME")
    if not host_home:
        return {}
    key1 = f"{host_home}/.krull-session-meta-{session_id}.json"
    key2 = f"{host_home}/.krull-session-meta.json"
    for path in (key1, key2):
        # caching
        c = _cache.get(path)
        if c and (time() - c.get("_ts", 0) < _CACHE_TTL):
            return c.get("data", {})
        p = Path(path)
        if p.exists():
            try:
                data = json.loads(p.read_text())
                _cache[path] = {"data": data, "_ts": time()}
                return data
            except Exception:
                return {}
    return {}

def set_current_session_meta(meta: dict):
    _current_session_meta.set(meta)

def get_current_session_meta(default=None):
    return _current_session_meta.get(default or {})

# Convenience: load & set in one call
def load_and_set(session_id: str, host_home: str = None):
    meta = load_session_meta_for(session_id, host_home=host_home)
    set_current_session_meta(meta)
    return meta
