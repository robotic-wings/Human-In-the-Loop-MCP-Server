# Human-in-the-Loop MCP Server

This project provides a central, GUI-free HTTP(S) MCP service and a separate desktop-terminal implementation. The central service owns call admission, durable state, timing, notification routing, iPhone response pages, and results. The desktop terminal is not an MCP server and never falls back to a local server.

## Supported architecture

Agents connect only to the central service at `POST /mcp`. The central service sends bounded notifications to the configured desktop terminal and/or Bark, and the iPhone response page is served by the central service. A computer running the desktop terminal may sleep or be offline without preventing the central service from accepting an Agent request or notifying the iPhone.

The project deliberately has no stdio transport, local integrated MCP server, GUI management console, or local-service fallback. Passing `--stdio` or `--local` to the central entry point is rejected.

## Install

```bash
pip install .
```

This installs the HTTP central-service command:

```bash
hitl-central-service --config /path/to/central-config.json
```

`--config` is mandatory. The configuration must define at least:

```json
{
  "listen_host": "10.10.0.10",
  "listen_port": 8443,
  "web_base_url": "https://10.10.0.10:8443",
  "data_dir": "/var/lib/hitl-central",
  "agent_credentials": {"example-agent": "credential-from-secret-store"},
  "tls_cert_file": "/secure/path/server.crt",
  "tls_key_file": "/secure/path/server.key",
  "route_mode": "iphone",
  "bark_endpoint": "https://bark.example.test/push",
  "bark_credential": "credential-from-secret-store",
  "operator_sessions": {
    "opaque-browser-session": {
      "operator_id": "only-operator",
      "browser_session_id": "browser-session-id",
      "csrf_token": "csrf-token"
    }
  }
}
```

Use protected configuration or platform secret storage for real credentials. Do not commit credentials, notification links, TLS private keys, or business content. `web_base_url` is the configured HTTPS address used in Bark call links; it is not derived from the listener address or request headers.

For a desktop or dual route, provide management-owned `desktop_credentials` and `desktop_endpoints` entries. The endpoint is the configured terminal notification listener, not an Agent-accessible MCP interface.

## Desktop terminal

`central_desktop_terminal.py` is an independently runnable terminal program. It starts only its authenticated `/terminal/events` callback listener (never an MCP endpoint), registers/synchronizes with the central service, reports GUI availability, and presents accept/decline plus a response dialog. Install it with the same package, then run:

```bash
hitl-desktop-terminal --config /path/to/desktop-terminal.json
```

The terminal configuration is separate from central configuration and uses only placeholders here:

```json
{
  "central_url": "https://10.10.0.10:8443",
  "terminal_credential": "credential-from-secret-store",
  "callback_credential": "different-central-to-terminal-secret",
  "listener_host": "10.10.0.61",
  "listener_port": 9100,
  "listener_scheme": "http",
  "verify_tls": true,
  "heartbeat_seconds": 10
}
```

The configured listener address must match the management-preconfigured desktop endpoint. `callback_credential` authenticates central-to-terminal callbacks and is deliberately distinct from `terminal_credential`, which authenticates terminal-to-central registration and actions. On macOS, Windows, and Linux the Tk desktop runtime needs a graphical session; when unavailable, the process exits before registering as GUI-ready. `central_terminal_http_service.py` remains a testable central-side HTTP boundary; it is not embedded by the desktop program.

The desktop packaging and platform UI adapter remain separate from the central-service process. The terminal must report `gui_unavailable` rather than claim it can accept calls when no graphical session is available.

## Development checks

```bash
pytest -q
```

The contract tests cover the central coordinator, HTTP ingress, iPhone web flow, terminal registration and synchronization, notification delivery, waiting/reconnection lifecycle, and the prohibition on importing Tk or the removed integrated server from central modules.
