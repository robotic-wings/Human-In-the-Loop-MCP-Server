"""Compatibility facade: the HITL modules split, the contracts did not.

The durable core lives in hitl_core, the MCP surface in hitl_mcp, and the
HTTP transport in hitl_transport.  This module re-exports the historical
names so every existing import keeps working.
"""

from hitl_core import (  # noqa: F401
    ACTIVE_STATES,
    CallEventBus,
    CallMachine,
    Clock,
    InvalidTransition,
    Notifier,
    RouteAdmission,
    Waiter,
    WaitingLifecycle,
)
from hitl_mcp import (  # noqa: F401
    _HUMAN_LOOP_MANUAL,
    _MCP_TOOLS,
    _RpcError,
    McpRpcMixin,
    _interactive_schema,
)
from hitl_transport import Application, create_app  # noqa: F401
