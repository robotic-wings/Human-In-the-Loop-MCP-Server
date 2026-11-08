"""Portable, GUI-free desktop terminal client boundary.

The central service remains authoritative.  This class deliberately owns only
local presentation lifecycle and an authenticated, injected notification
listener seam; it neither starts nor proxies an MCP server.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


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
