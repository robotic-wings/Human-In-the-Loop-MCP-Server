"""Portable, GUI-free desktop terminal client boundary.

The central service remains authoritative.  This class deliberately owns only
local presentation lifecycle and an authenticated, injected notification
listener seam; it neither starts nor proxies an MCP server.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


class DesktopTerminalClient:
    """Coordinate injected desktop presentation adapters with central state."""

    def __init__(
        self,
        *,
        config: Mapping[str, object],
        central: Any,
        listener: Any,
        gui_capability: Callable[[], bool],
        ring: Any,
        presenter: Any,
    ) -> None:
        self._config = dict(config)
        self._central = central
        self.listener = listener
        self._gui_capability = gui_capability
        self.ring = ring
        self.presenter = presenter
        self._session_id: str | None = None
        self._online = False
        self._gui_ready = False
        self._shown: dict[str, int] = {}
        self._operations: dict[tuple[str, str, str], dict[str, object]] = {}

    def start(self) -> dict[str, object]:
        if self._callback_credential() is None:
            self._online = False
            self._gui_ready = False
            return {"success": False, "status": "invalid_config"}
        self._gui_ready = bool(self._gui_capability())
        if not self._gui_ready:
            self._online = False
            return {"success": False, "status": "gui_unavailable"}
        try:
            authenticated = self._central.authenticate_desktop()
        except (ConnectionError, OSError):
            return self._central_lost()
        if not authenticated.get("success") or not isinstance(authenticated.get("session_id"), str):
            self._online = False
            return {"success": False, "status": "central_unavailable"}
        self._session_id = authenticated["session_id"]
        self._online = True
        return {"success": True, "status": "ready"}

    def status(self) -> dict[str, object]:
        return {"gui_ready": self._gui_ready, "online": self._online}

    def recover(self) -> dict[str, object]:
        """Re-register after the central service restarted or the session was lost."""
        self._online = False
        self._session_id = None
        central = self._central
        central._session_id = None
        session_file = getattr(central, "_session_file", lambda: None)()
        if session_file is not None:
            try:
                session_file.unlink()
            except OSError:
                pass
        return self.start()

    def receive_notification(
        self, event: Mapping[str, object], *, callback_credential: str | None
    ) -> dict[str, object]:
        expected_credential = self._callback_credential()
        if expected_credential is None or callback_credential != expected_credential:
            return {"success": False, "status": "notification_not_authenticated"}
        call_id, version = event.get("call_id"), event.get("version")
        if not isinstance(call_id, str) or not isinstance(version, int) or not self._online or self._session_id is None:
            return {"success": False, "status": "session_not_found"}
        if self._shown.get(call_id, -1) >= version:
            return {"success": True, "status": "duplicate", "call_id": call_id}
        try:
            state = self._central.read_call(call_id, self._session_id)
        except (ConnectionError, OSError):
            return self._central_lost()
        if not state.get("success"):
            if event.get("call_state") == "accepted" and event.get("handled_on") == "iphone_web":
                self.listener.acknowledge(dict(event))
                show_hint = getattr(self.presenter, "show_handled_hint", None)
                if callable(show_hint):
                    show_hint(call_id, "已在手机端接听")
                self._close(call_id)
                return {"success": True, "status": "synchronized", "call_id": call_id}
            return {"success": False, "status": "session_not_found"}
        self.listener.acknowledge(dict(event))
        if state.get("call_state") != "ringing":
            if state.get("call_state") == "accepted" and state.get("handled_on") == "iphone_web":
                show_hint = getattr(self.presenter, "show_handled_hint", None)
                if callable(show_hint):
                    show_hint(call_id, "已在手机端接听")
            self._close(call_id)
            return {"success": True, "status": "synchronized", "call_id": call_id}
        self._shown[call_id] = version
        self.presenter.show(state)
        quiet_mode = event.get("quiet_mode")
        if quiet_mode == "short":
            self.ring.start(call_id, "short")
        elif quiet_mode != "silent":
            self.ring.start(call_id, "long")
        return {"success": True, "status": "presented", "call_id": call_id}

    def accept(self, *, call_id: str, operation_id: str) -> dict[str, object]:
        return self._act("accept", call_id, operation_id)

    def decline(self, *, call_id: str, operation_id: str) -> dict[str, object]:
        return self._act("decline", call_id, operation_id)

    def dismiss(self, *, call_id: str, operation_id: str) -> dict[str, object]:
        """Closing a ringing desktop alert explicitly declines the whole call."""

        return self.decline(call_id=call_id, operation_id=operation_id)

    def sync(self) -> dict[str, object]:
        if not self._online or self._session_id is None:
            return {"success": False, "status": "central_unavailable"}
        call_ids = list(self._shown)
        try:
            for call_id in call_ids:
                state = self._central.read_call(call_id, self._session_id)
                if not state.get("success") or state.get("call_state") != "ringing":
                    self._close(call_id)
        except (ConnectionError, OSError):
            return self._central_lost()
        return {"success": True, "status": "synchronized", "call_ids": call_ids}

    def _act(self, action: str, call_id: str, operation_id: str) -> dict[str, object]:
        # Operation identifiers are idempotent only within their call.  A
        # terminal can receive another ringing call after the first ends, and
        # that new call must always reach central authority even when a client
        # reuses a local operation identifier.
        cache_key = (action, call_id, operation_id)
        if cache_key in self._operations:
            return dict(self._operations[cache_key])
        if not self._online or self._session_id is None or call_id not in self._shown:
            return {"success": False, "status": "session_not_found"}
        try:
            result = self._central.act(action, call_id, self._session_id, operation_id)
        except (ConnectionError, OSError):
            return self._central_lost()
        if not result.get("success"):
            return {"success": False, "status": "session_not_found"}
        self._close(call_id)
        if action == "accept":
            try:
                state = self._central.read_call(call_id, self._session_id)
            except (ConnectionError, OSError):
                return self._central_lost()
            result = {**result, **{key: state[key] for key in ("handled_on", "terminal_platform") if key in state}}
        self._operations[cache_key] = dict(result)
        return result

    def _close(self, call_id: str) -> None:
        if call_id in self._shown:
            del self._shown[call_id]
            self.ring.stop(call_id)
            self.presenter.close(call_id)

    def _central_lost(self) -> dict[str, object]:
        self._online = False
        for call_id in list(self._shown):
            self._close(call_id)
        return {"success": False, "status": "central_unavailable"}

    def _callback_credential(self) -> str | None:
        """Return the separately configured callback secret, never a fallback."""

        credential = self._config.get("callback_credential")
        return credential if isinstance(credential, str) and credential else None


@dataclass(frozen=True)
class DesktopTerminalConfig:
    """Configuration owned by the independently installed terminal process."""

    central_url: str
    callsign: str
    operator_password: str
    device_id: str
    callback_credential: str
    listener_host: str
    listener_port: int
    terminal_platform: str = "macos"
    terminal_credential: str = ""  # legacy device secret; unused in operator login
    listener_scheme: str = "http"
    listener_tls_cert_file: str | None = None
    listener_tls_key_file: str | None = None
    verify_tls: bool | str = True
    heartbeat_seconds: float = 10.0
    ringtone_path: str = "ringtone.mp3"
    ringtone_alt_path: str | None = None
    ringtones_dir: str = "ringtones"
    short_ringtone_path: str = ""
    session_store_path: str | None = None

    @property
    def listener_endpoint(self) -> str:
        return f"{self.listener_scheme}://{self.listener_host}:{self.listener_port}/terminal/events"

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "DesktopTerminalConfig":
        # ``listener_address`` is accepted as the explicit address spelling
        # used by the earlier terminal-client boundary.
        normalized = dict(value)
        if "listener_host" not in normalized and isinstance(normalized.get("listener_address"), str):
            normalized["listener_host"] = normalized["listener_address"]
        required = ("central_url", "callsign", "operator_password", "device_id", "callback_credential", "listener_host", "listener_port")
        missing = [name for name in required if not normalized.get(name)]
        if missing:
            raise ValueError(f"missing desktop terminal configuration: {', '.join(missing)}")
        port = normalized["listener_port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("listener_port must be an integer between 1 and 65535")
        central_url = normalized["central_url"]
        if not isinstance(central_url, str) or not central_url.startswith(("https://", "http://")):
            raise ValueError("central_url must be an HTTP(S) URL")
        scheme = normalized.get("listener_scheme", "http")
        if scheme not in {"http", "https"}:
            raise ValueError("listener_scheme must be http or https")
        interval = normalized.get("heartbeat_seconds", 10.0)
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval <= 0:
            raise ValueError("heartbeat_seconds must be positive")
        verify_tls = normalized.get("verify_tls", True)
        if not isinstance(verify_tls, (bool, str)):
            raise ValueError("verify_tls must be true, false, or a CA bundle path")
        if normalized.get("terminal_credential") and normalized["terminal_credential"] == normalized["callback_credential"]:
            raise ValueError("callback_credential must differ from terminal_credential")
        ringtone_path = normalized.get("ringtone_path", "ringtone.mp3")
        if not isinstance(ringtone_path, str) or not ringtone_path:
            ringtone_path = "ringtone.mp3"
        ringtone_alt = normalized.get("ringtone_alt_path")
        ringtone_alt_path = ringtone_alt if isinstance(ringtone_alt, str) and ringtone_alt else None
        ringtones_dir = normalized.get("ringtones_dir", "ringtones")
        if not isinstance(ringtones_dir, str) or not ringtones_dir:
            ringtones_dir = "ringtones"
        short_ringtone = normalized.get("short_ringtone_path")
        short_ringtone_path = short_ringtone if isinstance(short_ringtone, str) else ""
        session_store = normalized.get("session_store_path")
        session_store_path = session_store if isinstance(session_store, str) and session_store else None
        return cls(
            central_url=central_url.rstrip("/"),
            callback_credential=str(normalized["callback_credential"]), listener_host=str(normalized["listener_host"]),
            callsign=str(normalized.get("callsign", "")),
            operator_password=str(normalized.get("operator_password", "")),
            device_id=str(normalized.get("device_id", "")),
            terminal_platform=str(normalized.get("platform") or normalized.get("terminal_platform") or "macos"),
            listener_port=port, listener_scheme=str(scheme), verify_tls=verify_tls,
            listener_tls_cert_file=(str(normalized["listener_tls_cert_file"]) if normalized.get("listener_tls_cert_file") else None),
            listener_tls_key_file=(str(normalized["listener_tls_key_file"]) if normalized.get("listener_tls_key_file") else None),
            heartbeat_seconds=float(interval),
            ringtone_path=ringtone_path, ringtone_alt_path=ringtone_alt_path,
            ringtones_dir=ringtones_dir,
            short_ringtone_path=short_ringtone_path,
            session_store_path=session_store_path,
        )


def scan_ringtones(directory: str) -> list[str]:
    """Return the sorted ``*.mp3`` filenames present in a directory."""
    path = Path(directory)
    if not path.is_dir():
        return []
    return sorted(entry.name for entry in path.glob("*.mp3") if entry.is_file())


def preferences_file(config_path: str) -> str:
    """Panel preferences live beside the config as preferences.json."""
    return str(Path(config_path).with_name("preferences.json"))


def load_preferences(prefs_file: str) -> dict[str, str]:
    """Load panel preferences, migrating the legacy terminal-state.json."""
    path = Path(prefs_file)
    legacy = path.with_name("terminal-state.json")
    source = path if path.exists() else (legacy if legacy.exists() else None)
    if source is None:
        return {}
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if isinstance(data, str):
        data = {"short_ringtone_path": data}
    if not isinstance(data, dict):
        return {}
    prefs = {str(k): str(v) for k, v in data.items()
             if isinstance(k, str) and isinstance(v, str) and v}
    if source == legacy and prefs:
        save_preferences(prefs_file, prefs)
        try:
            legacy.unlink()
        except OSError:
            pass
    return prefs


def save_preferences(prefs_file: str, prefs: Mapping[str, str]) -> None:
    target = Path(prefs_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(prefs), encoding="utf-8")


def save_short_ringtone(state_file: str, path: str) -> None:
    """Persist the chosen short ringtone into the preferences file."""
    prefs = load_preferences(state_file)
    prefs["short_ringtone_path"] = path
    save_preferences(state_file, prefs)


def load_short_ringtone(state_file: str) -> str:
    """Read the chosen short ringtone from preferences, '' when absent."""
    return load_preferences(state_file).get("short_ringtone_path", "")


class _PreviewControls:
    """Preview state-machine UI: button labels are a pure function of player state.

    Every toggle or ringtone change funnels through here; ``refresh`` derives
    each label from the player's actual preview state, so mutual exclusion and
    channel resets can never drift out of sync.
    """

    def __init__(self, ring: Any, long_button: Any, short_button: Any) -> None:
        self._ring = ring
        self._long_button = long_button
        self._short_button = short_button

    def toggle_long(self) -> None:
        self._ring.toggle_preview()
        self.refresh()

    def toggle_short(self) -> None:
        toggle = getattr(self._ring, "toggle_short_preview", None)
        if callable(toggle):
            toggle()
        self.refresh()

    def channel_changed(self, channel: str) -> None:
        """A ringtone selection changed: stop that channel's preview if playing."""
        if channel == "long":
            self._ring.stop_preview()
        else:
            stop = getattr(self._ring, "stop_short_preview", None)
            if callable(stop):
                stop()
        self.refresh()

    def refresh(self) -> None:
        long_playing = bool(getattr(self._ring, "previewing_long", False))
        short_playing = bool(getattr(self._ring, "previewing_short", False))
        self._long_button.config(text="停止播放" if long_playing else "测试播放")
        self._short_button.config(text="停止" if short_playing else "试听")


def build_settings_panel(root: Any, ring: Any, central: Any, ringtones_dir: str,
                         mp3s: list[str], initial_ringtone: str, tk: Any,
                         short_ringtone: str = "", short_state_file: str = "") -> None:
    """Build the settings panel as classic framed groups with aligned controls."""
    initial_basename = os.path.basename(initial_ringtone)
    initial_value = initial_basename if initial_basename in mp3s else (mp3s[0] if mp3s else "")
    ringtone_var = tk.StringVar(value=initial_value)

    short_basename = os.path.basename(short_ringtone)
    short_value = short_basename if short_basename in mp3s else (mp3s[0] if mp3s else "")
    short_var = tk.StringVar(value=short_value)

    set_short = getattr(ring, "set_short_ringtone", None)
    if callable(set_short) and short_value:
        set_short(os.path.join(ringtones_dir, short_value))

    callsign = getattr(central, "operator", None) or getattr(getattr(central, "_config", object()), "callsign", "") or ""
    tk.Label(root, text=f"呼号：{callsign}" if callsign else "呼号：—", anchor="w",
             font=("TkDefaultFont", 11, "bold")).pack(fill="x", padx=12, pady=(12, 0))

    notify = tk.LabelFrame(root, text="通知", padx=10, pady=8)
    notify.pack(fill="x", padx=12, pady=(6, 8))
    tk.Label(notify, text="铃声").grid(row=0, column=0, sticky="w", pady=2)
    tk.OptionMenu(notify, ringtone_var, *mp3s).grid(row=0, column=1, sticky="w", padx=(8, 8), pady=2)
    preview_button = tk.Button(notify, text="测试播放")
    preview_button.grid(row=0, column=2, sticky="w", pady=2)
    tk.Label(notify, text="短铃声").grid(row=1, column=0, sticky="w", pady=2)
    tk.OptionMenu(notify, short_var, *mp3s).grid(row=1, column=1, sticky="w", padx=(8, 8), pady=2)
    short_preview_button = tk.Button(notify, text="试听")
    short_preview_button.grid(row=1, column=2, sticky="w", pady=2)

    previews = _PreviewControls(ring, preview_button, short_preview_button)
    preview_button.config(command=previews.toggle_long)
    short_preview_button.config(command=previews.toggle_short)

    def on_ringtone_change(*_args: object) -> None:
        ring.set_ringtone(os.path.join(ringtones_dir, ringtone_var.get()))
        previews.channel_changed("long")
        if short_state_file:
            prefs = load_preferences(short_state_file)
            prefs["long_ringtone_path"] = os.path.join(ringtones_dir, ringtone_var.get())
            save_preferences(short_state_file, prefs)

    def on_short_ringtone_change(*_args: object) -> None:
        if callable(set_short):
            set_short(os.path.join(ringtones_dir, short_var.get()))
        previews.channel_changed("short")
        if short_state_file:
            save_short_ringtone(short_state_file, os.path.join(ringtones_dir, short_var.get()))

    ringtone_var.trace_add("write", on_ringtone_change)
    short_var.trace_add("write", on_short_ringtone_change)

    dnd = central.get_dnd()
    if not isinstance(dnd, Mapping):
        dnd = {}
    enabled = bool(dnd.get("enabled", False))
    message = dnd.get("message") if isinstance(dnd.get("message"), str) else ""
    auto_deny_var = tk.BooleanVar(value=enabled)
    message_var = tk.StringVar(value=message)
    dnd_status = tk.StringVar(value="")

    def sync_dnd() -> None:
        result = central.set_dnd(bool(auto_deny_var.get()), message_entry.get())
        success = isinstance(result, Mapping) and bool(result.get("success"))
        dnd_status.set("已同步" if success else "同步失败，请重试")
        after = getattr(root, "after", None)
        if callable(after):
            after(2000, lambda: dnd_status.set(""))

    def on_auto_deny_change(*_args: object) -> None:
        enabled_now = bool(auto_deny_var.get())
        message_entry.config(state="normal" if enabled_now else "disabled")
        save_button.config(state="normal" if enabled_now else "disabled")
        sync_dnd()

    dnd_frame = tk.LabelFrame(root, text="勿扰模式", padx=10, pady=8)
    dnd_frame.pack(fill="x", padx=12, pady=(0, 12))
    columnconfigure = getattr(dnd_frame, "columnconfigure", None)
    if callable(columnconfigure):
        columnconfigure(1, weight=1)
    tk.Checkbutton(dnd_frame, text="来电自动拒绝", variable=auto_deny_var).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
    tk.Label(dnd_frame, text="自动回复消息").grid(row=1, column=0, sticky="w", pady=2)
    message_entry = tk.Entry(dnd_frame, textvariable=message_var, state="normal" if enabled else "disabled")
    message_entry.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=2)
    save_button = tk.Button(dnd_frame, text="保存", command=sync_dnd, state="normal" if enabled else "disabled")
    save_button.grid(row=2, column=1, sticky="e", pady=(6, 2))
    tk.Label(dnd_frame, textvariable=dnd_status, fg="gray").grid(row=3, column=0, columnspan=2, sticky="w")
    auto_deny_var.trace_add("write", on_auto_deny_change)

    get_quiet = getattr(central, "get_quiet_window", None)
    if callable(get_quiet):
        quiet = get_quiet()
        if not isinstance(quiet, Mapping):
            quiet = {}
        quiet_window_seconds = quiet.get("quiet_window_seconds", 300)
        quiet_mode = quiet.get("quiet_mode", "short")
        if isinstance(quiet_window_seconds, bool) or not isinstance(quiet_window_seconds, int):
            quiet_window_seconds = 300
        if quiet_mode not in {"short", "silent"}:
            quiet_mode = "short"
        minutes = quiet_window_seconds // 60
        quiet_mode_var = tk.StringVar(value=quiet_mode)
        quiet_status = tk.StringVar(value="")
        quiet_frame = tk.LabelFrame(root, text="接听后", padx=10, pady=8)
        quiet_frame.pack(fill="x", padx=12, pady=(0, 12))
        qconfigure = getattr(quiet_frame, "columnconfigure", None)
        if callable(qconfigure):
            qconfigure(1, weight=1)
        quiet_minutes_entry = tk.Entry(quiet_frame)
        quiet_minutes_entry.grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=2)
        setter = getattr(quiet_minutes_entry, "set_text", None)
        if callable(setter):
            setter(str(minutes))
        else:
            insert = getattr(quiet_minutes_entry, "insert", None)
            if callable(insert):
                insert(0, str(minutes))

        def sync_quiet() -> None:
            try:
                minutes_val = int(quiet_minutes_entry.get())
            except (ValueError, TypeError):
                minutes_val = 5
            set_quiet = getattr(central, "set_quiet_window", None)
            if callable(set_quiet):
                set_quiet(minutes_val * 60, quiet_mode_var.get())
            quiet_status.set("已同步")

        tk.Label(quiet_frame, text="接听一次来电后，设定时长内的新来电自动降铃，避免频繁打扰", fg="gray").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        tk.Radiobutton(quiet_frame, text="短铃声", variable=quiet_mode_var, value="short").grid(row=1, column=0, sticky="w", pady=(0, 4))
        tk.Radiobutton(quiet_frame, text="静音", variable=quiet_mode_var, value="silent").grid(row=1, column=1, sticky="w", pady=(0, 4))
        tk.Label(quiet_frame, text="时长(分钟)").grid(row=2, column=0, sticky="w", pady=2)
        quiet_save = tk.Button(quiet_frame, text="保存", command=sync_quiet)
        quiet_save.grid(row=3, column=1, sticky="e", pady=(6, 2))
        tk.Label(quiet_frame, textvariable=quiet_status, fg="gray").grid(row=4, column=0, columnspan=2, sticky="w")


class CentralHttpDesktopClient:
    """Terminal-side HTTP adapter; it contains no call-state decisions."""

    def __init__(self, config: DesktopTerminalConfig) -> None:
        self._config, self._session_id = config, None
        self._states: dict[str, dict[str, object]] = {}
        self.operator: str | None = getattr(self._config, "callsign", None) or None

    def _request(self, method: str, path: str, *, json_body: object | None = None) -> dict[str, object]:
        headers: dict[str, str] = {}
        if self._session_id:
            headers["x-terminal-session"] = self._session_id
        else:
            legacy = getattr(self._config, "terminal_credential", "")
            if legacy:
                headers["authorization"] = f"Terminal {legacy}"
        try:
            with httpx.Client(timeout=5.0, verify=self._config.verify_tls) as client:
                response = client.request(method, self._config.central_url + path, json=json_body, headers=headers)
            data = response.json()
        except (httpx.HTTPError, ValueError, OSError) as error:
            raise ConnectionError("central service is unreachable") from error
        return data if isinstance(data, dict) else {"success": False, "status": "invalid_response"}

    def authenticate_desktop(self, credential: str | None = None) -> dict[str, object]:
        session_file = self._session_file()
        if session_file.exists():
            try:
                recovered = session_file.read_text(encoding="utf-8").strip()
            except OSError:
                recovered = ""
            if recovered:
                self._session_id = recovered
                return {"success": True, "session_id": recovered}
        result = self._request("POST", "/api/terminals/register", json_body={
            "callsign": self._config.callsign,
            "password": self._config.operator_password,
            "device_id": self._config.device_id,
            "terminal_platform": self._config.terminal_platform,
            "endpoint": (self._config.listener_endpoint() if callable(getattr(self._config, "listener_endpoint", None)) else getattr(self._config, "listener_endpoint", "")),
            "gui_ready": True,
        })
        session = result.get("session_id")
        if result.get("success") and isinstance(session, str):
            self._session_id = session
            try:
                session_file.parent.mkdir(parents=True, exist_ok=True)
                session_file.write_text(session, encoding="utf-8")
            except OSError:
                pass
        return result

    def _session_file(self) -> Path:
        if self._config.session_store_path:
            return Path(self._config.session_store_path)
        digest = hashlib.sha256(self._config.central_url.encode("utf-8")).hexdigest()
        return Path(tempfile.gettempdir()) / f"hitl-desktop-{digest}.session"

    def heartbeat(self) -> dict[str, object]:
        return self._request("POST", "/api/terminals/heartbeat", json_body={"gui_ready": True})

    def synchronize(self) -> list[dict[str, object]]:
        result = self._request("GET", "/api/terminals/state")
        states = result.get("states")
        if not result.get("success") or not isinstance(states, list):
            raise ConnectionError("central terminal state is unavailable")
        self._states = {str(item["call_id"]): dict(item) for item in states if isinstance(item, Mapping) and isinstance(item.get("call_id"), str)}
        return list(self._states.values())

    def read_call(self, call_id: str, session_id: str) -> dict[str, object]:
        if session_id != self._session_id:
            return {"success": False, "status": "session_not_found"}
        return self._request("GET", f"/api/calls/{call_id}")

    def act(self, action: str, call_id: str, session_id: str, operation_id: str) -> dict[str, object]:
        if session_id != self._session_id:
            return {"success": False, "status": "session_not_found"}
        return self._request("POST", f"/api/calls/{call_id}/{action}", json_body={"operation_id": operation_id})

    def submit(self, call_id: str, submission_id: str, result: Mapping[str, object],
               attachment_ids: list[str] | None = None) -> dict[str, object]:
        merged = dict(result)
        if attachment_ids:
            merged["attachments"] = list(attachment_ids)
        return self._request("POST", f"/api/calls/{call_id}/submissions", json_body={"submission_id": submission_id, "result": merged})

    def upload_attachment(self, call_id: str, filename: str, media_type: str, content: bytes) -> dict[str, object]:
        return self._request("POST", f"/api/calls/{call_id}/attachments", json_body={
            "filename": filename,
            "media_type": media_type,
            "content": base64.b64encode(content).decode("ascii"),
        })

    def cancel(self, call_id: str, operation_id: str) -> dict[str, object]:
        return self._request("POST", f"/api/calls/{call_id}/cancel", json_body={"operation_id": operation_id})

    def set_dnd(self, enabled: bool, message: str) -> dict[str, object]:
        return self._request("POST", "/api/terminals/dnd", json_body={"dnd": enabled, "message": message})

    def get_dnd(self) -> dict[str, object]:
        return self._request("GET", "/api/terminals/dnd")

    def set_quiet_window(self, quiet_window_seconds: int, quiet_mode: str) -> dict[str, object]:
        return self._request("POST", "/api/terminals/quiet", json_body={
            "quiet_window_seconds": quiet_window_seconds, "quiet_mode": quiet_mode,
        })

    def get_quiet_window(self) -> dict[str, object]:
        return self._request("GET", "/api/terminals/quiet")


class _TerminalListener:
    def acknowledge(self, event: dict[str, object]) -> None:
        del event


class TkPresentation:
    """Small portable Tk presentation adapter, isolated to the terminal executable."""

    def __init__(self, root: Any, client_ref: Callable[[], DesktopTerminalClient]) -> None:
        self._root, self._client_ref, self._windows = root, client_ref, {}
        self._calls: dict[str, dict[str, object]] = {}
        self._buttons: dict[str, tuple[Any, Any]] = {}

    def show(self, state: dict[str, object]) -> None:
        call_id = state.get("call_id")
        if not isinstance(call_id, str) or call_id in self._windows:
            return
        import tkinter as tk
        window = tk.Toplevel(self._root)
        window.title("呼叫")
        window.protocol("WM_DELETE_WINDOW", lambda: self._decline(call_id))
        payload = state.get("payload")
        payload = dict(payload) if isinstance(payload, Mapping) else {}
        prompt = payload.get("prompt") or payload.get("message") or payload.get("task_description") or "Human input requested"
        tk.Label(window, text=str(prompt), wraplength=520, justify="left").pack(padx=16, pady=(16, 12), anchor="w")
        buttons = tk.Frame(window)
        buttons.pack(fill="x", padx=16, pady=(0, 16))
        accept_btn = tk.Button(buttons, text="接听", command=lambda: self._accept(call_id), width=12)
        accept_btn.pack(side="left")
        decline_btn = tk.Button(buttons, text="拒绝", command=lambda: self._decline(call_id), width=12)
        decline_btn.pack(side="left", padx=(8, 0))
        self._buttons[call_id] = (accept_btn, decline_btn)
        self._windows[call_id] = window
        self._calls[call_id] = {**state, "payload": payload}
        window.bell()

    def close(self, call_id: str) -> None:
        window = self._windows.pop(call_id, None)
        if window is not None:
            window.destroy()

    def show_handled_hint(self, call_id: str, hint: str) -> None:
        """Briefly show that the call is now handled elsewhere before closing."""
        window = self._windows.pop(call_id, None)
        if window is None:
            return
        buttons = self._buttons.pop(call_id, None)
        if buttons:
            for btn in buttons:
                btn.config(state="disabled")
        import tkinter as tk
        tk.Label(window, text=str(hint)).pack(padx=16, pady=12)
        after = getattr(window, "after", None)
        if callable(after):
            after(1500, window.destroy)
        else:
            window.destroy()

    def _accept(self, call_id: str) -> None:
        self._stop_ring_now(call_id)
        result = self._client_ref().accept(call_id=call_id, operation_id=uuid.uuid4().hex)
        if result.get("success"):
            self._answer(call_id, self._calls.get(call_id, {}))
        else:
            self._calls.pop(call_id, None)

    def _decline(self, call_id: str) -> None:
        self._stop_ring_now(call_id)
        self._client_ref().decline(call_id=call_id, operation_id=uuid.uuid4().hex)
        self._calls.pop(call_id, None)

    def _stop_ring_now(self, call_id: str) -> None:
        """Stop the ring and close the alert instantly on user action."""
        client = self._client_ref()
        ring = getattr(client, "ring", None)
        if ring is not None:
            ring.stop(call_id)
        self.close(call_id)

    def _answer(self, call_id: str, state: Mapping[str, object]) -> None:
        import tkinter as tk
        dialog = tk.Toplevel(self._root)
        dialog.title("回答")
        payload = state.get("payload")
        payload = dict(payload) if isinstance(payload, Mapping) else {}
        tool = str(payload.get("tool") or payload.get("interaction_type") or "text").lower()
        prompt = payload.get("prompt") or payload.get("message") or payload.get("task_description") or "Please complete this request"
        tk.Label(dialog, text=str(prompt), wraplength=620, justify="left").pack(padx=12, pady=(12, 6), anchor="w")
        error = tk.StringVar(value="")
        build_result = self._response_controls(dialog, tool, payload, tk)
        build_progress = getattr(build_result, "progress", None)

        attachment_ids: list[str] = []
        attachment_box = tk.Listbox(dialog, height=4)
        attachment_box.pack(padx=12, pady=(6, 0), fill="x")

        def choose_files() -> None:
            from tkinter import filedialog
            central = self._client_ref()._central
            for path in filedialog.askopenfilenames():
                selected = Path(path)
                uploaded = central.upload_attachment(
                    call_id=call_id, filename=selected.name,
                    media_type="application/octet-stream", content=selected.read_bytes(),
                )
                attachment_id = uploaded.get("attachment_id")
                if uploaded.get("success") and isinstance(attachment_id, str):
                    attachment_ids.append(attachment_id)
                    attachment_box.insert("end", str(path))
                else:
                    error.set("附件上传失败，请重试。")
        tk.Button(dialog, text="添加附件", command=choose_files).pack(padx=12, anchor="w")

        def submit() -> None:
            result = build_result()
            if result is None:
                error.set("Please complete the required response before submitting.")
                return
            response = self._client_ref()._central.submit(call_id, uuid.uuid4().hex, result, attachment_ids)
            if response.get("success"):
                self._calls.pop(call_id, None)
                dialog.destroy()
            else:
                error.set("The central service did not accept this response. Refresh or retry.")

        def cancel() -> None:
            self._client_ref()._central.cancel(call_id=call_id, operation_id=uuid.uuid4().hex)
            self._calls.pop(call_id, None)
            dialog.destroy()

        def report_progress() -> None:
            response = self._client_ref()._central.submit(call_id, uuid.uuid4().hex, build_progress(), attachment_ids)
            if response.get("success"):
                error.set("进度已上报")
            else:
                error.set("进度上报失败，请重试。")
        tk.Label(dialog, textvariable=error, fg="firebrick", justify="left").pack(padx=12, anchor="w")
        button_bar = tk.Frame(dialog)
        button_bar.pack(fill="x", padx=12, pady=(8, 12))
        if build_progress is not None:
            tk.Button(button_bar, text="报告进度", command=report_progress).pack(side="left")
        tk.Button(button_bar, text="取消", command=cancel, width=10).pack(side="right")
        tk.Button(button_bar, text="提交", command=submit, width=10).pack(side="right", padx=(0, 8))

    @staticmethod
    def _response_controls(dialog: Any, tool: str, payload: Mapping[str, object], tk: Any) -> Callable[[], dict[str, object] | None]:
        """Build labelled controls for every shared interaction family."""
        required = bool(payload.get("required", True))
        if "confirm" in tool:
            value = tk.StringVar(value="")
            tk.Label(dialog, text="Choose a confirmation result").pack(padx=12, pady=(6, 0), anchor="w")
            tk.Radiobutton(dialog, text="Yes, confirm", variable=value, value="true").pack(padx=20, anchor="w")
            tk.Radiobutton(dialog, text="No, do not confirm", variable=value, value="false").pack(padx=20, anchor="w")
            return lambda: {"confirmed": value.get() == "true"} if value.get() else None
        if "info" in tool or "acknowledge" in tool:
            value = tk.BooleanVar(value=False)
            tk.Checkbutton(dialog, text="I have read and acknowledge this information", variable=value).pack(padx=12, pady=8, anchor="w")
            return lambda: {"acknowledged": True} if value.get() else None
        if "feedback" in tool:
            box = TkPresentation._labelled_text(dialog, tk, "Feedback")
            return lambda: TkPresentation._text_result("feedback", box, required)
        if "task" in tool or "assign" in tool:
            status = tk.StringVar(value="completed")
            tk.Label(dialog, text="Task status").pack(padx=12, pady=(6, 0), anchor="w")
            tk.OptionMenu(dialog, status, "completed", "failed").pack(padx=12, anchor="w")
            report = TkPresentation._labelled_text(dialog, tk, "Task report")
            def task_result() -> dict[str, object] | None:
                text = report.get("1.0", "end-1c")
                return {"status": status.get(), "report": text} if text or not required else None
            def task_progress() -> dict[str, object]:
                return {"status": "in_progress", "progress": report.get("1.0", "end-1c")}
            task_result.progress = task_progress  # type: ignore[attr-defined]
            return task_result
        choices = payload.get("choices") or payload.get("options")
        if ("choice" in tool or "select" in tool or isinstance(choices, list)) and isinstance(choices, list):
            multiple = bool(payload.get("multiple") or payload.get("allow_multiple"))
            tk.Label(dialog, text="Choose one or more options" if multiple else "Choose one option").pack(padx=12, pady=(6, 0), anchor="w")
            normalized = [
                (str(item.get("value", item.get("label", ""))), str(item.get("label", item.get("value", ""))))
                if isinstance(item, Mapping) else (str(item), str(item)) for item in choices
            ]
            if multiple:
                values = [(raw, tk.BooleanVar(value=False)) for raw, _label in normalized]
                for (_raw, label), (_value, variable) in zip(normalized, values):
                    tk.Checkbutton(dialog, text=label, variable=variable).pack(padx=20, anchor="w")
                def many() -> dict[str, object] | None:
                    selected = [raw for raw, variable in values if variable.get()]
                    return {"selected_choices": selected} if selected or not required else None
                return many
            selected = tk.StringVar(value="")
            for raw, label in normalized:
                tk.Radiobutton(dialog, text=label, variable=selected, value=raw).pack(padx=20, anchor="w")
            return lambda: {"selected_choices": [selected.get()]} if selected.get() else None
        multiline = "multiline" in tool or "long" in tool or bool(payload.get("multiline"))
        numeric = "number" in tool or str(payload.get("input_type", "")).lower() == "number"
        if multiline:
            box = TkPresentation._labelled_text(dialog, tk, "Enter your complete response")
            return lambda: TkPresentation._text_result("user_input", box, required)
        label = "Enter a number" if numeric else "Enter your response"
        tk.Label(dialog, text=label).pack(padx=12, pady=(6, 0), anchor="w")
        entry = tk.Entry(dialog, width=70)
        entry.pack(padx=12, pady=(0, 8), fill="x")
        return lambda: {"user_input": entry.get()} if entry.get() or not required else None

    @staticmethod
    def _labelled_text(dialog: Any, tk: Any, label: str) -> Any:
        tk.Label(dialog, text=label).pack(padx=12, pady=(6, 0), anchor="w")
        box = tk.Text(dialog, width=70, height=10)
        box.pack(padx=12, pady=(0, 8))
        return box

    @staticmethod
    def _text_result(key: str, box: Any, required: bool) -> dict[str, object] | None:
        value = box.get("1.0", "end-1c")
        return {key: value} if value or not required else None


class SilentRing:
    """The window bell is the baseline cross-platform ring; no OS-specific service."""
    def start(self, call_id: str) -> None: del call_id
    def stop(self, call_id: str) -> None: del call_id


class _PlatformPlayer:
    """Cross-platform default ringtone backend (afplay / winsound / aplay)."""

    def play(self, path: str, loop: bool = True) -> object:
        import subprocess
        import sys as _sys
        if _sys.platform == "win32":
            try:
                import winsound
                flags = winsound.SND_FILENAME | winsound.SND_ASYNC | (winsound.SND_LOOP if loop else 0)
                winsound.PlaySound(path, flags)
                return ("winsound", path)
            except Exception:
                return None
        command = ["afplay"] if _sys.platform == "darwin" else (["aplay", "-q"] if loop else ["paplay"])
        try:
            proc = subprocess.Popen(command + [path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return proc
        except Exception:
            return None

    def stop(self, handle: object) -> None:
        if isinstance(handle, tuple) and handle and handle[0] == "winsound":
            try:
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
            return
        if handle is not None and hasattr(handle, "terminate"):
            try:
                handle.terminate()
            except Exception:
                pass


class RingtonePlayer:
    """Loop a configured ringtone file and fall back to the window bell."""

    def __init__(self, ringtone_path: str, ringtone_alt_path: str | None = None,
                 player: Any = None, bell: Any = None, short_ringtone_path: str = "") -> None:
        self._ringtone_path = ringtone_path
        self._ringtone_alt_path = ringtone_alt_path
        self._short_ringtone_path = short_ringtone_path
        self._player = player if player is not None else _PlatformPlayer()
        self._bell = bell
        self._handles: dict[str, object] = {}
        self._preview_handle: object | None = None
        self._short_preview_handle: object | None = None

    def start(self, call_id: str, mode: str = "long") -> None:
        if mode == "silent":
            return
        if call_id in self._handles:
            return
        if mode == "short":
            short_path = self._short_ringtone_path
            if short_path and Path(short_path).is_file():
                handle = self._player.play(short_path, loop=False)
                if handle is not None:
                    # Register even one-shot plays: accept/decline must be
                    # able to kill the sound mid-file instead of letting a
                    # long "short" ringtone play to its natural end.
                    self._handles[call_id] = handle
                return
            if self._bell is not None:
                self._bell()
            return
        path = self._select_ringtone()
        if path is None:
            if self._bell is not None:
                self._bell()
            return
        handle = self._player.play(path, loop=True)
        if handle is not None:
            self._handles[call_id] = handle

    def set_ringtone(self, path: str) -> None:
        self._ringtone_path = path

    def set_short_ringtone(self, path: str) -> None:
        self._short_ringtone_path = path

    def toggle_preview(self) -> bool:
        self.stop_short_preview()
        if self._preview_handle is not None:
            self.stop_preview()
            return False
        path = self._select_ringtone()
        if path is None:
            if self._bell is not None:
                self._bell()
            return False
        handle = self._player.play(path, loop=True)
        if handle is None:
            if self._bell is not None:
                self._bell()
            return False
        self._preview_handle = handle
        return True

    def stop_preview(self) -> None:
        handle = self._preview_handle
        self._preview_handle = None
        if handle is not None:
            self._player.stop(handle)

    def toggle_short_preview(self) -> bool:
        self.stop_preview()
        if self._short_preview_handle is not None:
            self.stop_short_preview()
            return False
        short_path = self._short_ringtone_path
        if not (short_path and Path(short_path).is_file()):
            return False
        handle = self._player.play(short_path, loop=False)
        if handle is not None:
            self._short_preview_handle = handle
            return True
        return False

    def stop_short_preview(self) -> None:
        handle = self._short_preview_handle
        self._short_preview_handle = None
        if handle is not None:
            self._player.stop(handle)

    @property
    def previewing_long(self) -> bool:
        return self._preview_handle is not None

    @property
    def previewing_short(self) -> bool:
        return self._short_preview_handle is not None

    def stop(self, call_id: str) -> None:
        handle = self._handles.pop(call_id, None)
        if handle is not None:
            self._player.stop(handle)

    def _select_ringtone(self) -> str | None:
        if self._ringtone_path and Path(self._ringtone_path).is_file():
            return self._ringtone_path
        if self._ringtone_alt_path and Path(self._ringtone_alt_path).is_file():
            return self._ringtone_alt_path
        return None


def _callback_app(client: DesktopTerminalClient) -> Callable[..., Any]:
    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("method") != "POST" or scope.get("path") != "/terminal/events":
            status, result = 404, {"success": False, "status": "not_found"}
        else:
            headers = {bytes(k).lower(): bytes(v).decode("latin-1") for k, v in scope.get("headers", [])}
            body = b""
            while True:
                item = await receive(); body += item.get("body", b"")
                if not item.get("more_body"): break
            try:
                event = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                event = None
            result = client.receive_notification(event if isinstance(event, Mapping) else {}, callback_credential=headers.get(b"x-central-callback"))
            status = 200 if result.get("success") else 401
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": json.dumps(result).encode("utf-8")})
    return app


def run_heartbeat_tick(central: Any, client: Any, *, callback_credential: str | None,
                       heartbeat_seconds: float, run_worker: Callable[[Callable[[], None]], None],
                       schedule_ui: Callable[[int, Callable[[], None]], None]) -> None:
    """Run one heartbeat/sync group off the UI thread and re-arm the next tick."""

    def next_tick() -> None:
        run_heartbeat_tick(
            central, client,
            callback_credential=callback_credential,
            heartbeat_seconds=heartbeat_seconds,
            run_worker=run_worker,
            schedule_ui=schedule_ui,
        )

    def network_group() -> None:
        try:
            central.heartbeat()
            status = getattr(client, "status", None)
            if callable(status) and not status().get("online"):
                recover = getattr(client, "recover", None)
                if callable(recover):
                    recover()
            for state in central.synchronize():
                client.receive_notification(state, callback_credential=callback_credential)
            client.sync()
        except ConnectionError:
            schedule_ui(0, client._central_lost)

    run_worker(network_group)
    schedule_ui(int(heartbeat_seconds * 1000), next_tick)


def acquire_instance_lock(lock_path: str | Path) -> bool:
    """Exclusively create a PID lock; steal it when the holder is dead.

    A bare O_EXCL file survives SIGKILL (launchctl kickstart -k, crashes)
    and then blocks every KeepAlive respawn forever.  Writing the owner's
    PID lets a fresh start detect a dead holder and take over.
    """
    path = str(lock_path)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    for _ in range(2):
        try:
            fd = os.open(path, flags)
        except FileExistsError:
            try:
                holder = Path(path).read_text(encoding="utf-8").strip()
            except OSError:
                holder = ""
            if holder.isdigit():
                try:
                    os.kill(int(holder), 0)
                    return False  # holder is alive: genuinely locked
                except OSError:
                    pass  # holder is dead: steal the stale lock
            elif holder == "":
                # Empty legacy lock with no PID: refuse to steal while ANY
                # terminal process is alive (locks are per-central-url, and
                # a legacy holder writes no PID to identify itself).
                import subprocess
                probe = subprocess.run(
                    ["pgrep", "-f", "central_desktop_terminal.py"],
                    capture_output=True, text=True)
                if probe.stdout.strip():
                    return False
                pass  # nobody alive: fall through and steal
            try:
                Path(path).unlink()
            except OSError:
                return False
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        return True
    return False


def main(argv: list[str] | None = None) -> None:
    """Run the independent desktop terminal; it never starts an MCP endpoint."""
    parser = argparse.ArgumentParser(prog="hitl-desktop-terminal")
    parser.add_argument("--config", required=True, help="desktop-terminal JSON configuration")
    args = parser.parse_args(argv)
    try:
        config = DesktopTerminalConfig.from_mapping(json.loads(Path(args.config).read_text(encoding="utf-8")))
        import tkinter as tk
        import uvicorn
        root = tk.Tk(); root.withdraw()
    except Exception as error:
        # Tk raises TclError when there is no graphical session: report this
        # honestly rather than registering a terminal that cannot answer.
        raise SystemExit(f"desktop terminal unavailable: {error}") from error
    lock_path = Path(tempfile.gettempdir()) / f"hitl-desktop-{hashlib.sha256(config.central_url.encode('utf-8')).hexdigest()}.lock"
    if not acquire_instance_lock(lock_path):
        raise SystemExit("desktop terminal unavailable: another instance is already running")
    central = CentralHttpDesktopClient(config)
    holder: dict[str, DesktopTerminalClient] = {}
    presenter = TkPresentation(root, lambda: holder["client"])
    prefs_file = preferences_file(args.config)
    prefs = load_preferences(prefs_file)
    short_state_file = prefs_file
    short_ringtone_path = config.short_ringtone_path or prefs.get("short_ringtone_path", "")
    long_ringtone = prefs.get("long_ringtone_path") or config.ringtone_path
    ring = RingtonePlayer(ringtone_path=long_ringtone, ringtone_alt_path=config.ringtone_alt_path, bell=root.bell, short_ringtone_path=short_ringtone_path)
    client = DesktopTerminalClient(config={"callsign": config.callsign, "operator_password": config.operator_password, "device_id": config.device_id, "terminal_credential": config.terminal_credential, "callback_credential": config.callback_credential}, central=central, listener=_TerminalListener(), gui_capability=lambda: True, ring=ring, presenter=presenter)
    holder["client"] = client
    build_settings_panel(root, ring, central, config.ringtones_dir, scan_ringtones(config.ringtones_dir), config.ringtone_path, tk, short_ringtone_path, short_state_file)
    if config.listener_scheme == "https" and (not config.listener_tls_cert_file or not config.listener_tls_key_file):
        raise SystemExit("desktop terminal unavailable: HTTPS listener requires listener_tls_cert_file and listener_tls_key_file")
    server = uvicorn.Server(uvicorn.Config(
        _callback_app(client), host=config.listener_host, port=config.listener_port, log_level="warning",
        ssl_certfile=config.listener_tls_cert_file, ssl_keyfile=config.listener_tls_key_file,
    ))
    threading.Thread(target=server.run, daemon=True, name="hitl-terminal-listener").start()
    started = client.start()
    if not started.get("success"):
        server.should_exit = True
        raise SystemExit(f"desktop terminal unavailable: {started.get('status')}")

    def run_worker(fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, daemon=True, name="hitl-heartbeat").start()

    run_heartbeat_tick(
        central, client,
        callback_credential=config.callback_credential,
        heartbeat_seconds=config.heartbeat_seconds,
        run_worker=run_worker,
        schedule_ui=root.after,
    )
    root.deiconify(); root.title("HITL 操作台")
    try:
        root.mainloop()
    finally:
        server.should_exit = True


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
