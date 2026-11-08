"""GUI-free desktop registration and authoritative state synchronization."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from typing import Any

from central_notification_router import TerminalRegistry


class DesktopRegistrationStateService:
    """Central-facing authentication and state-read boundary for one desktop."""

    def __init__(
        self,
        *,
        registry: TerminalRegistry,
        clock: Callable[[], float],
        registered_credentials: Mapping[str, Mapping[str, str]],
        device_state: Callable[[str], list[dict[str, object]]],
        audit: list[str] | None = None,
    ) -> None:
        self._registry = registry
        self._clock = clock
        self._credentials = dict(registered_credentials)
        self._device_state = device_state
        self._audit = audit if audit is not None else []
        self._sessions: dict[str, str] = {}
        self.actions: list[dict[str, object]] = []

    def register(self, *, credential: str, endpoint: str, gui_ready: bool) -> dict[str, object]:
        metadata = self._credentials.get(credential)
        if metadata is None:
            return {"success": False, "status": "terminal_not_authenticated"}
        device_id = str(metadata["device_id"])
        managed = self._registry.desktop_state(device_id)
        if managed is None or endpoint != managed["endpoint"]:
            return {"success": False, "status": "endpoint_not_preconfigured"}
        callback_credential = str(managed["callback_credential"])
        self._registry.register_desktop(
            actor_role="management",
            device_id=device_id,
            endpoint=endpoint,
            callback_credential=callback_credential,
            gui_ready=gui_ready,
        )
        session_id = str(uuid.uuid4())
        self._sessions[session_id] = device_id
        self._audit.append(f"register:{device_id}")
        return {
            "success": True,
            "status": "registered",
            "session_id": session_id,
        }

    def heartbeat(self, *, session_id: str, gui_ready: bool) -> dict[str, object]:
        device_id = self._sessions.get(session_id)
        if device_id is None:
            return {"success": False, "status": "session_not_found"}
        state = self._registry.heartbeat(device_id, gui_ready)
        self._audit.append(f"heartbeat:{device_id}")
        return {"success": True, **(state or {})}

    def states(self, *, session_id: str) -> dict[str, object]:
        device_id = self._sessions.get(session_id)
        if device_id is None:
            return {"success": False, "status": "session_not_found"}
        return {"success": True, "states": self._device_state(device_id)}


class DesktopRegistrationSyncClient:
    """Local presentation client that only follows central authoritative state."""

    def __init__(self, *, config: Mapping[str, object], central: Any, presenter: Any, ring: Any) -> None:
        self._config = dict(config)
        self._central = central
        self._presenter = presenter
        self._ring = ring
        self._session_id: str | None = None
        self._visible: dict[str, int] = {}
        self._versions: dict[str, int] = {}

    def start(self) -> dict[str, object]:
        credential = self._config.get("terminal_credential")
        endpoint = self._config.get("listener_endpoint")
        if not isinstance(credential, str) or not isinstance(endpoint, str):
            return {"success": False, "status": "invalid_config"}
        registered = self._central.register(credential=credential, endpoint=endpoint, gui_ready=True)
        if not registered.get("success"):
            return registered
        self._session_id = str(registered["session_id"])
        return self._synchronize()

    def wake(self) -> dict[str, object]:
        return self._synchronize()

    def receive_callback(
        self, event: Mapping[str, object], *, callback_credential: str | None = None
    ) -> dict[str, object]:
        expected_credential = self._config.get("callback_credential")
        if not isinstance(expected_credential, str) or not expected_credential or callback_credential != expected_credential:
            return {"success": False, "status": "callback_not_authenticated"}
        call_id, version = event.get("call_id"), event.get("version")
        if not isinstance(call_id, str) or not isinstance(version, int):
            return {"success": False, "status": "session_not_found"}
        seen = self._versions.get(call_id, -1)
        if version <= seen:
            return {"success": True, "status": "duplicate" if version == seen else "stale", "call_id": call_id}
        self._apply(dict(event))
        return {"success": True, "status": "presented", "call_id": call_id}

    def accept(self, *, call_id: str, operation_id: str) -> dict[str, object]:
        del operation_id
        if call_id not in self._visible:
            return {"success": False, "status": "session_not_found"}
        return {"success": False, "status": "session_not_found"}

    def _synchronize(self) -> dict[str, object]:
        if self._session_id is None:
            return {"success": False, "status": "session_not_found"}
        heartbeat = self._central.heartbeat(session_id=self._session_id, gui_ready=True)
        if not heartbeat.get("success"):
            return heartbeat
        current = self._central.states(session_id=self._session_id)
        if not current.get("success"):
            return current
        states = current["states"]
        active: list[str] = []
        for state in states:
            call_id = state.get("call_id")
            if isinstance(call_id, str):
                active.append(call_id)
                self._apply(state)
        for call_id in list(self._visible):
            if call_id not in active:
                self._close(call_id)
        return {"success": True, "status": "synchronized", "call_ids": sorted(self._visible)}

    def _apply(self, state: Mapping[str, object]) -> None:
        call_id, version = state.get("call_id"), state.get("version")
        if not isinstance(call_id, str) or not isinstance(version, int):
            return
        if version <= self._versions.get(call_id, -1):
            return
        self._versions[call_id] = version
        if state.get("call_state") == "ringing":
            self._visible[call_id] = version
            self._presenter.show(dict(state))
            self._ring.start(call_id)
        else:
            self._close(call_id)

    def _close(self, call_id: str) -> None:
        if call_id in self._visible:
            del self._visible[call_id]
            self._ring.stop(call_id)
            self._presenter.close(call_id)
