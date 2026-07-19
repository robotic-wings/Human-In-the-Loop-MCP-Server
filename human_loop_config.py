#!/usr/bin/env python3
"""
Shared configuration store for the Human-in-the-Loop MCP Server.

A tiny, stdlib-only module imported by BOTH the server (human_loop_server.py)
and the Management Console (management_console.py). It persists operator settings
to a single JSON file so a non-technical operator can configure everything from
the console GUI and the server picks it up.

Location: ~/.human_loop_config.json (override with $HUMAN_LOOP_CONFIG), matching
the flat ~/.human_loop_outbox convention.

Precedence for a given knob is up to each reader, but the established pattern is:
    explicit environment variable > this config file > hardcoded default.

The schema is versioned and read tolerantly (missing keys are filled from
defaults), so it can grow toward the enterprise multi-user roadmap without
breaking older files:
  - `profile` is the single-operator seed of a future `users: [...]` list.
  - Reserved (NOT implemented yet): an `auth` block (an operator secret_key the
    AI must present) and an `org` block (super account + per-user editable-setting
    ACL). New sections just need entries in DEFAULT_CONFIG.
"""

import json
import os
import tempfile

CONFIG_ENV_VAR = "HUMAN_LOOP_CONFIG"
DEFAULT_CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".human_loop_config.json")

SCHEMA_VERSION = 1

DEFAULT_CONFIG = {
    "schema_version": SCHEMA_VERSION,
    "profile": {
        "name": "",
        "role": "",
        "responsibilities": "",
        "communication": "",
    },
    "server": {
        "http_port": 8000,
        "http_host": "127.0.0.1",
        "https_enabled": False,   # serve over HTTPS (required by newer clients)
        "https_certfile": "",     # PEM cert path (empty = auto-managed self-signed)
        "https_keyfile": "",      # PEM private-key path (empty = auto-managed self-signed)
        "https_san_names": [],    # extra IPs/hosts the cert must cover (SAN)
    },
    "task_defaults": {
        "timeout_seconds": 240,
        "max_result_bytes": 1_000_000,
        "attachments_enabled": True,
    },
    "notification": {
        "ringtone_path": "",   # empty = use the bundled notify.wav
        "muted": False,
    },
}


def get_config_path():
    """Resolve the config file path (env override, else the default)."""
    return os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG_PATH


def _deep_merge(base, override):
    """Return base deep-merged with override (override wins for scalars)."""
    result = dict(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _defaults():
    # Fresh deep copy so callers can't mutate DEFAULT_CONFIG.
    return json.loads(json.dumps(DEFAULT_CONFIG))


def load_config():
    """Load the config, deep-merged over defaults. Tolerant: a missing or
    corrupt file yields the defaults (never raises)."""
    path = get_config_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _defaults()
        return _deep_merge(_defaults(), data)
    except (OSError, ValueError):
        return _defaults()


def save_config(cfg):
    """Persist the config atomically (temp file + os.replace). Returns the path.

    Failures are raised to the caller (the console) so it can surface them.
    """
    path = get_config_path()
    cfg = dict(cfg or {})
    cfg.setdefault("schema_version", SCHEMA_VERSION)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".hl_cfg_", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


# --- Small typed getters used by the server (all tolerant of a missing file) --- #

def get_profile():
    return load_config().get("profile", _defaults()["profile"])


def get_task_defaults():
    return load_config().get("task_defaults", _defaults()["task_defaults"])


def get_notification():
    return load_config().get("notification", _defaults()["notification"])


def get_server_settings():
    return load_config().get("server", _defaults()["server"])
