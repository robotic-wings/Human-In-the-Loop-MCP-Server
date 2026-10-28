# Human-In-the-Loop MCP Server

This artifact provides the GUI-free central Human-in-the-Loop service. It
accepts authenticated MCP requests over HTTPS at `/mcp`, persists calls and
results in SQLite, serves the phone web interface from the same HTTPS listener,
and coordinates configured notification routes.

## What is shipped

The packaged wheel contains:

- `hitl-central-service`, the only console entry point;
- `central_service.py`, the HTTPS/MCP and web entry point; and
- `call_coordinator.py`, the SQLite-backed call coordinator.

The independent desktop terminal is not shipped by this artifact. This artifact
does not provide a local GUI, a management console, a local MCP server, stdio,
or a fallback to local interaction. Legacy source files are not supported
runtime entry points.

## Start the central HTTPS service

Install the distributed wheel in an isolated Python environment, supply trusted
TLS material and protected credential files, then start the service with an
explicit JSON configuration file:

```bash
hitl-central-service --config <CENTRAL_SERVICE_CONFIG.json>
```

The service has no default listener. The command requires `--config`; a
no-argument launch and the legacy `--stdio`, `--local`, and `--service`
modes are rejected with a non-zero exit.

## Configuration

`<CENTRAL_SERVICE_CONFIG.json>` must be a JSON object. Replace every marked
placeholder with environment-specific values. Do not put credential values in
the configuration file, command line, source control, or notifications.

```json
{
  "listen_host": "<LISTEN_ADDRESS>",
  "listen_port": 8443,
  "web_base_url": "https://<WEB_HOST>:8443",
  "data_dir": "<CENTRAL_DATA_DIRECTORY>",
  "https_certfile": "<PROTECTED_TLS_CERTIFICATE_PATH>",
  "https_keyfile": "<PROTECTED_TLS_PRIVATE_KEY_PATH>",
  "agent_credentials_file": "<PROTECTED_AGENT_CREDENTIALS_JSON_PATH>",
  "terminal_credentials_file": "<PROTECTED_TERMINAL_CREDENTIALS_JSON_PATH>",
  "operator_credentials_file": "<PROTECTED_OPERATOR_CREDENTIALS_JSON_PATH>",
  "eligible_desktop_device_ids": ["<CONFIGURED_DESKTOP_DEVICE_ID>"],
  "bark_enabled": false
}
```

`listen_host`, `listen_port`, `web_base_url`, `data_dir`,
`https_certfile`, and `https_keyfile` are required. `web_base_url` must be
an HTTPS URL and is the authoritative source for phone-notification links; the
service never infers it from request headers or the bound listener.

The service creates its SQLite database and attachment archive below
`data_dir`. Keep that directory and all credential files outside the repository
and readable only by the service identity. The credential-file settings refer to
separate protected JSON files for Agent, terminal, and operator credentials.
An MCP deployment must provide Agent credentials, because calls to `/mcp` use
Bearer authentication.

## TLS and MCP clients

The central service always listens with HTTPS. Supply a certificate and private
key whose identity matches `<WEB_HOST>` (or the configured IP address), and
configure every MCP, terminal, and phone client to trust the issuing authority.
Do not disable certificate validation. Missing or invalid TLS material, or a
non-HTTPS `web_base_url`, is a startup error; the service never falls back to
plaintext HTTP.

Point an HTTP(S)-capable MCP client to the central endpoint and send its
protected Agent credential:

```text
https://<WEB_HOST>:8443/mcp
Authorization: Bearer <AGENT_TOKEN_FROM_PROTECTED_CREDENTIAL_FILE>
```

The MCP client's exact configuration syntax is client-specific. It must issue
HTTPS requests to `/mcp`; it must not launch this project as a
`command`/`args` subprocess.

## Data and operations

Call records, events, drafts, immutable results, and uploaded attachments are
central-service archives under `data_dir`. They survive a central-service
restart and are distinct from runtime logs. The service never treats a
desktop-terminal local path as a stored attachment.

This artifact does not install an OS background service or include an in-app
management console. Process supervision, certificate issuance, credential
provisioning, deployment, migration, and archive-retention decisions are
outside this repository's startup instructions.

## Troubleshooting

- A missing `--config` argument, invalid JSON, invalid TLS paths, or a
  plaintext `web_base_url` causes an explicit startup error before a listener
  is opened.
- Verify that the configured HTTPS endpoint has a certificate trusted by the
  MCP client and that the endpoint address matches the certificate identity.
- Verify that the service identity can read the configured credential files and
  write `data_dir`.
- Do not attempt `python human_loop_server.py`, `management_console.py`, or
  the former `hitl-management-console` entry point; they are not shipped
  runtime entry points for this artifact.

## Development

```
Human-In-the-Loop-MCP-Server/
├── central_service.py          # HTTPS central-service entry point
├── call_coordinator.py         # SQLite-backed call coordination
├── pyproject.toml              # Package configuration and console script
├── README.md                   # Documentation
├── LICENSE                     # MIT License
└── tests/                      # Automated tests
```

Run the central-service startup and installed-artifact tests before changing
the documented launch contract.

## License

This project is licensed under the MIT License; see [LICENSE](LICENSE).
