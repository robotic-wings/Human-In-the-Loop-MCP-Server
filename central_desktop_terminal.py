"""Portable, GUI-free desktop terminal client boundary.

The central service remains authoritative.  This class deliberately owns only
local presentation lifecycle and an authenticated, injected notification
listener seam; it neither starts nor proxies an MCP server.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
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
        self._auto_deny = bool(self._config.get("auto_deny", False))
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
            authenticated = self._central.authenticate_desktop(
                str(self._config.get("terminal_credential", ""))
            )
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
        if self._auto_deny:
            try:
                declined = self._central.act("decline", call_id, self._session_id, uuid.uuid4().hex)
            except (ConnectionError, OSError):
                return self._central_lost()
            if not declined.get("success"):
                return {"success": False, "status": "session_not_found"}
            self.listener.acknowledge(dict(event))
            return {"success": True, "status": "declined", "call_id": call_id}
        try:
            state = self._central.read_call(call_id, self._session_id)
        except (ConnectionError, OSError):
            return self._central_lost()
        if not state.get("success"):
            return {"success": False, "status": "session_not_found"}
        self.listener.acknowledge(dict(event))
        if state.get("call_state") != "ringing":
            self._close(call_id)
            return {"success": True, "status": "synchronized", "call_id": call_id}
        self._shown[call_id] = version
        self.presenter.show(state)
        self.ring.start(call_id)
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
    terminal_credential: str
    callback_credential: str
    listener_host: str
    listener_port: int
    listener_scheme: str = "http"
    listener_tls_cert_file: str | None = None
    listener_tls_key_file: str | None = None
    verify_tls: bool | str = True
    heartbeat_seconds: float = 10.0
    ringtone_path: str = "ringtone.mp3"
    ringtone_alt_path: str | None = None
    auto_deny: bool = False

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
        required = ("central_url", "terminal_credential", "callback_credential", "listener_host", "listener_port")
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
        if normalized["terminal_credential"] == normalized["callback_credential"]:
            raise ValueError("callback_credential must differ from terminal_credential")
        ringtone_path = normalized.get("ringtone_path", "ringtone.mp3")
        if not isinstance(ringtone_path, str) or not ringtone_path:
            ringtone_path = "ringtone.mp3"
        ringtone_alt = normalized.get("ringtone_alt_path")
        ringtone_alt_path = ringtone_alt if isinstance(ringtone_alt, str) and ringtone_alt else None
        auto_deny = normalized.get("auto_deny", False)
        if not isinstance(auto_deny, bool):
            auto_deny = False
        return cls(
            central_url=central_url.rstrip("/"), terminal_credential=str(normalized["terminal_credential"]),
            callback_credential=str(normalized["callback_credential"]), listener_host=str(normalized["listener_host"]),
            listener_port=port, listener_scheme=str(scheme), verify_tls=verify_tls,
            listener_tls_cert_file=(str(normalized["listener_tls_cert_file"]) if normalized.get("listener_tls_cert_file") else None),
            listener_tls_key_file=(str(normalized["listener_tls_key_file"]) if normalized.get("listener_tls_key_file") else None),
            heartbeat_seconds=float(interval),
            ringtone_path=ringtone_path, ringtone_alt_path=ringtone_alt_path, auto_deny=auto_deny,
        )


class CentralHttpDesktopClient:
    """Terminal-side HTTP adapter; it contains no call-state decisions."""

    def __init__(self, config: DesktopTerminalConfig) -> None:
        self._config, self._session_id = config, None
        self._states: dict[str, dict[str, object]] = {}

    def _request(self, method: str, path: str, *, json_body: object | None = None) -> dict[str, object]:
        headers: dict[str, str] = {}
        if self._session_id:
            headers["x-terminal-session"] = self._session_id
        else:
            headers["authorization"] = f"Terminal {self._config.terminal_credential}"
        try:
            with httpx.Client(timeout=5.0, verify=self._config.verify_tls) as client:
                response = client.request(method, self._config.central_url + path, json=json_body, headers=headers)
            data = response.json()
        except (httpx.HTTPError, ValueError, OSError) as error:
            raise ConnectionError("central service is unreachable") from error
        return data if isinstance(data, dict) else {"success": False, "status": "invalid_response"}

    def authenticate_desktop(self, credential: str) -> dict[str, object]:
        if credential != self._config.terminal_credential:
            return {"success": False, "status": "terminal_not_authenticated"}
        result = self._request("POST", "/api/terminals/register", json_body={
            "endpoint": self._config.listener_endpoint, "gui_ready": True,
        })
        session = result.get("session_id")
        if result.get("success") and isinstance(session, str):
            self._session_id = session
        return result

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


class _TerminalListener:
    def acknowledge(self, event: dict[str, object]) -> None:
        del event


class TkPresentation:
    """Small portable Tk presentation adapter, isolated to the terminal executable."""

    def __init__(self, root: Any, client_ref: Callable[[], DesktopTerminalClient]) -> None:
        self._root, self._client_ref, self._windows = root, client_ref, {}
        self._calls: dict[str, dict[str, object]] = {}

    def show(self, state: dict[str, object]) -> None:
        call_id = state.get("call_id")
        if not isinstance(call_id, str) or call_id in self._windows:
            return
        import tkinter as tk
        window = tk.Toplevel(self._root)
        window.title("Human-in-the-Loop call")
        window.protocol("WM_DELETE_WINDOW", lambda: self._decline(call_id))
        payload = state.get("payload")
        payload = dict(payload) if isinstance(payload, Mapping) else {}
        prompt = payload.get("prompt") or payload.get("message") or payload.get("task_description") or "Human input requested"
        tk.Label(window, text=str(prompt), wraplength=520, justify="left").pack(padx=16, pady=12)
        tk.Button(window, text="Accept", command=lambda: self._accept(call_id)).pack(side="left", padx=16, pady=12)
        tk.Button(window, text="Decline", command=lambda: self._decline(call_id)).pack(side="right", padx=16, pady=12)
        self._windows[call_id] = window
        self._calls[call_id] = {**state, "payload": payload}
        window.bell()

    def close(self, call_id: str) -> None:
        window = self._windows.pop(call_id, None)
        if window is not None:
            window.destroy()

    def _accept(self, call_id: str) -> None:
        result = self._client_ref().accept(call_id=call_id, operation_id=uuid.uuid4().hex)
        if result.get("success"):
            self._answer(call_id, self._calls.get(call_id, {}))

    def _decline(self, call_id: str) -> None:
        self._client_ref().decline(call_id=call_id, operation_id=uuid.uuid4().hex)
        self._calls.pop(call_id, None)

    def _answer(self, call_id: str, state: Mapping[str, object]) -> None:
        import tkinter as tk
        dialog = tk.Toplevel(self._root)
        dialog.title("Respond to call")
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
        tk.Button(dialog, text="Attach files", command=choose_files).pack(padx=12, anchor="w")

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
            if not response.get("success"):
                error.set("The central service did not accept this progress report. Retry.")
        tk.Label(dialog, textvariable=error, fg="firebrick", justify="left").pack(padx=12, anchor="w")
        if build_progress is not None:
            tk.Button(dialog, text="Report progress", command=report_progress).pack(pady=(0, 12))
        tk.Button(dialog, text="Submit", command=submit).pack(pady=(0, 12))
        tk.Button(dialog, text="Cancel", command=cancel).pack(pady=(0, 12))

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
                 player: Any = None, bell: Any = None) -> None:
        self._ringtone_path = ringtone_path
        self._ringtone_alt_path = ringtone_alt_path
        self._player = player if player is not None else _PlatformPlayer()
        self._bell = bell
        self._handles: dict[str, object] = {}

    def start(self, call_id: str) -> None:
        if call_id in self._handles:
            return
        path = self._select_ringtone()
        if path is None:
            if self._bell is not None:
                self._bell()
            return
        handle = self._player.play(path, loop=True)
        if handle is not None:
            self._handles[call_id] = handle

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
    central = CentralHttpDesktopClient(config)
    holder: dict[str, DesktopTerminalClient] = {}
    presenter = TkPresentation(root, lambda: holder["client"])
    ring = RingtonePlayer(ringtone_path=config.ringtone_path, ringtone_alt_path=config.ringtone_alt_path, bell=root.bell)
    client = DesktopTerminalClient(config={"terminal_credential": config.terminal_credential, "callback_credential": config.callback_credential, "auto_deny": config.auto_deny}, central=central, listener=_TerminalListener(), gui_capability=lambda: True, ring=ring, presenter=presenter)
    holder["client"] = client
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
    def tick() -> None:
        try:
            central.heartbeat()
            for state in central.synchronize():
                client.receive_notification(state, callback_credential=config.callback_credential)
            client.sync()
        except ConnectionError:
            client._central_lost()
        root.after(int(config.heartbeat_seconds * 1000), tick)
    root.deiconify(); root.title("HITL Desktop Terminal"); root.after(0, tick)
    try:
        root.mainloop()
    finally:
        server.should_exit = True


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
