#!/usr/bin/env python3
"""Central Portal — drive ONE HITL Central round from the command line.

An agent harness (opencode, Claude Code, ...) can wait on a shell command for
a very long time but must not burn LLM turns on keepalive traffic.  Central
Portal is the deterministic bridge: the agent states ONE dialog call as argv
(``central-portal call get_user_input --arg prompt='现在方便吗？'``) and blocks
on the process; the portal performs the MCP JSON-RPC handshake, parks the
call server-side, and then — in plain Python, no tokens spent — keeps issuing
the continuation legs (heartbeat → opened → in_progress → …) with the correct
``after_event_seq`` cursor until the round reaches a terminal outcome
(``needs_continuation: false``).  The outcome is printed once, as JSON, on
stdout.  Progress goes to stderr; stdout is always exactly one JSON object.

Exit codes
    0  a definitive outcome arrived (answered/completed/failed/declined/
       cancelled/busy-refused/business error — read ``outcome``)
    1  local usage error (bad argv)
    2  transport/server failure (unreachable, unauthorized, JSON-RPC error);
       round state may be open — recover with ``call list_my_interactions``
    3  --max-wait-seconds exceeded while the round is still open; the payload
       carries ``resume`` to continue the SAME round later
    5  the single line stayed busy for --busy-wait-seconds (no round created)

Configuration resolution (highest wins)
    CLI flags  →  HITL_CENTRAL / HITL_TOKEN / HITL_CAFILE env
    →  ~/.config/hitl-portal/config.json {url, token, ca_file}
    →  built-in default https://10.10.0.1:9443

The token authenticates as an agent caller id configured on the central
(``agent_credentials``); rounds opened here are visible to ``list_my_interactions``
under that caller and can be resumed from any client of the same caller id.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable, Mapping

# ── constants ──────────────────────────────────────────────────────────────

DEFAULT_CENTRAL = "https://10.10.0.1:9443"
DEFAULT_CONFIG = Path("~/.config/hitl-portal/config.json").expanduser()
LEG_SECONDS_DEFAULT = 10         # one parked leg (~7.5s park); heartbeats are
                                 # free in deterministic code, and a dead
                                 # connection is detected within one leg
TIMEOUT_EXTRA_DEFAULT = 90       # HTTP read timeout margin above the leg
BUSY_WAIT_DEFAULT = 180          # how long to keep retrying a busy line

EXIT_OUTCOME = 0
EXIT_USAGE = 1
EXIT_TRANSPORT = 2
EXIT_DEADLINE = 3
EXIT_BUSY = 5

_COMMON_FIELDS = ("callsign", "client_timeout_seconds", "max_result_bytes",
                  "interaction_id", "call_id", "after_event_seq")


class PortalError(Exception):
    """A terminal portal-level failure carrying an exit code + JSON payload."""

    def __init__(self, exit_code: int, payload: Mapping[str, Any], message: str = "") -> None:
        super().__init__(message or payload.get("error", "portal error"))
        self.exit_code = exit_code
        self.payload = dict(payload)


class TransportError(Exception):
    """Network-level failure; ``status`` is the HTTP code when there was one."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# ── embedded tool catalog (offline help; live truth is `central-portal tools`) ──

_CATALOG: list[dict[str, Any]] = [
    {"name": "get_user_input", "kind": "interaction",
     "description": "Ask the operator one short question and WAIT for their answer.",
     "fields": {"prompt": ("string", "The question shown to the operator."),
                "required": ("boolean", "Whether an answer is mandatory.")},
     "examples": ["central-portal call get_user_input --arg 'prompt=现在方便开始吗？'"]},
    {"name": "get_number_input", "kind": "interaction",
     "description": "Ask the operator for a number and WAIT for their answer.",
     "fields": {"prompt": ("string", "The question shown to the operator."),
                "required": ("boolean", "Whether an answer is mandatory.")},
     "examples": ["central-portal call get_number_input --arg 'prompt=需要买几件？' --arg required=true"]},
    {"name": "get_user_choice", "kind": "interaction",
     "description": "Offer the operator a set of options and WAIT for their selection.",
     "fields": {"choices": ("array", "The options to choose from (JSON array of strings)."),
                "prompt": ("string", "The question shown to the operator."),
                "multiple": ("boolean", "Allow selecting more than one option.")},
     "examples": ["central-portal call get_user_choice --arg 'prompt=选哪个时段？'"
                  " --arg 'choices=[\"上午\",\"下午\",\"晚上\"]'"]},
    {"name": "get_multiline_input", "kind": "interaction",
     "description": "Ask the operator for a long free-form answer and WAIT.",
     "fields": {"prompt": ("string", "The question shown to the operator.")},
     "examples": ["central-portal call get_multiline_input --arg 'prompt=请描述墙面的现状'"]},
    {"name": "show_confirmation_dialog", "kind": "interaction",
     "description": "Ask the operator to confirm or deny one proposal and WAIT.",
     "fields": {"prompt": ("string", "The proposal to confirm.")},
     "examples": ["central-portal call show_confirmation_dialog --arg 'prompt=将下单购买清单中的 3 件商品'"]},
    {"name": "show_info_message", "kind": "interaction",
     "description": "Show the operator one message and WAIT for their acknowledgement.",
     "fields": {"message": ("string", "The message to show.")},
     "examples": ["central-portal call show_info_message --arg 'message=任务开始：我先整理清单，需要你时我会叫你'"]},
    {"name": "get_feedback_from_user", "kind": "interaction",
     "description": "Collect free-form feedback from the operator and WAIT.",
     "fields": {"prompt": ("string", "Prompt shown to the operator.")},
     "examples": ["central-portal call get_feedback_from_user --arg 'prompt=还有什么要补充的吗？'"]},
    {"name": "assign_task_to_human", "kind": "interaction",
     "description": "Hand the operator one physical/real-world task and WAIT for their final report.",
     "fields": {"task_description": ("string", "One actionable instruction for the operator.")},
     "examples": ["central-portal call assign_task_to_human --max-wait-seconds 3600"
                  " --arg 'task_description=拍一张北墙的照片发给我' --arg max_result_bytes=1000000"]},
    {"name": "read_interaction_result", "kind": "read",
     "description": "Read the full body of a saved result (pagination via cursor).",
     "fields": {"interaction_id": ("string", "Owning interaction (optional)."),
                "result_id": ("string", "The result_id from a body_omitted_for_size response."),
                "cursor": ("string", "Pagination cursor from the previous page."),
                "max_result_bytes": ("integer", "Page budget.")},
     "examples": ["central-portal call read_interaction_result --arg result_id=R123"]},
    {"name": "read_interaction_attachment", "kind": "read",
     "description": "Read one byte chunk of a saved attachment (pagination via offset_bytes).",
     "fields": {"interaction_id": ("string", "Owning interaction (optional)."),
                "attachment_id": ("string", "The attachment id to read."),
                "offset_bytes": ("integer", "Byte offset of this chunk."),
                "max_result_bytes": ("integer", "Chunk budget.")},
     "examples": ["central-portal call read_interaction_attachment"
                  " --arg attachment_id=A1 --arg offset_bytes=0"]},
    {"name": "list_operators", "kind": "readonly",
     "description": "List the human operators this service can reach, with DND state.",
     "fields": {}, "examples": ["central-portal call list_operators"]},
    {"name": "get_operator_profile", "kind": "readonly",
     "description": "Read one operator's profile: who they are and how to address them.",
     "fields": {"callsign": ("string", "Operator callsign from list_operators (omit for primary).")},
     "examples": ["central-portal call get_operator_profile"]},
    {"name": "health_check", "kind": "readonly",
     "description": "Report HITL service health (storage, desktop online, push channels).",
     "fields": {}, "examples": ["central-portal call health_check"]},
    {"name": "get_human_loop_prompt", "kind": "readonly",
     "description": "Return the HITL operating manual (wait model, cursor, outcomes).",
     "fields": {}, "examples": ["central-portal call get_human_loop_prompt"]},
    {"name": "list_my_interactions", "kind": "readonly",
     "description": "List your own open rounds — the recovery path after context loss.",
     "fields": {}, "examples": ["central-portal call list_my_interactions"]},
    {"name": "cancel_interaction", "kind": "control",
     "description": "End one open round to release the single line.",
     "fields": {"interaction_id": ("string", "The round's interaction_id."),
                "call_id": ("string", "The round's call_id.")},
     "examples": ["central-portal call cancel_interaction --arg interaction_id=I1 --arg call_id=C1"]},
]

_CATALOG_BY_NAME = {tool["name"]: tool for tool in _CATALOG}


# ── configuration ──────────────────────────────────────────────────────────

def load_config(path: Path) -> dict[str, Any]:
    """Read {url, token, ca_file} from the portal config, tolerating absence."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def resolve_connection(args: argparse.Namespace) -> dict[str, Any]:
    merged: dict[str, Any] = {"url": DEFAULT_CENTRAL, "token": "", "ca_file": None}
    merged.update({k: v for k, v in load_config(DEFAULT_CONFIG).items()
                   if k in ("url", "token", "ca_file") and v})
    merged["url"] = os.environ.get("HITL_CENTRAL", merged["url"]).rstrip("/")
    if os.environ.get("HITL_TOKEN"):
        merged["token"] = os.environ["HITL_TOKEN"]
    if os.environ.get("HITL_CAFILE"):
        merged["ca_file"] = os.environ["HITL_CAFILE"]
    if args.central:
        merged["url"] = args.central.rstrip("/")
    if args.token:
        merged["token"] = args.token
    if args.cafile:
        merged["ca_file"] = args.cafile
    if args.insecure:
        merged["ca_file"] = None
        merged["insecure"] = True
    return merged


# ── HTTP transport (injectable for tests) ──────────────────────────────────

def _ssl_context(ca_file: str | None, insecure: bool) -> ssl.SSLContext:
    if insecure:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if ca_file:
        return ssl.create_default_context(cafile=str(Path(ca_file).expanduser()))
    return ssl.create_default_context()


class HttpTransport:
    """Minimal MCP streamable-HTTP client: POST JSON-RPC to /mcp."""

    def __init__(self, url: str, token: str, ca_file: str | None = None,
                 insecure: bool = False) -> None:
        self.url = url if url.endswith("/mcp") else url + "/mcp"
        self.token = token
        self.context = _ssl_context(ca_file, insecure)

    def send(self, body: Mapping[str, Any], session: str | None,
             read_timeout: float) -> tuple[dict[str, str], Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if session:
            headers["Mcp-Session-Id"] = session
        req = urllib.request.Request(
            self.url, data=json.dumps(body).encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=read_timeout, context=self.context) as resp:
                def _text(item: Any) -> str:
                    return item.decode("latin-1") if isinstance(item, bytes) else str(item)
                raw_headers = {_text(k).lower(): _text(v) for k, v in resp.getheaders()}
                return raw_headers, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = ""
            with suppress(Exception):
                detail = exc.read().decode("utf-8", "replace")[:300]
            raise TransportError(f"HTTP {exc.code}: {detail}", status=exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise TransportError(str(exc) or type(exc).__name__) from None


# ── cursor policy ──────────────────────────────────────────────────────────

def adopt_cursor(current: int | None, structured: Mapping[str, Any]) -> int:
    """The continuation cursor rule, exactly as the central defines it.

    A response carrying an event advances the cursor to that event_seq (the
    server also repeats it as next_after_event_seq); a plain heartbeat keeps
    the cursor the caller already used.  Never fall back to 0 — a reset
    re-drains the oldest event forever and hides every later one.
    """
    for key in ("next_after_event_seq", "event_seq"):
        seq = structured.get(key)
        if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0:
            return seq
    return current if isinstance(current, int) and current >= 0 else 0


# ── the portal ─────────────────────────────────────────────────────────────

Announcer = Callable[[str], None]


def _stderr_log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[{stamp}] {message}", file=sys.stderr, flush=True)


def _summary_text(result: Mapping[str, Any]) -> str:
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                return item["text"]
    return ""


class Portal:
    """One MCP client session; owns request ids and the continuation loop."""

    def __init__(self, transport: Any, announce: Announcer = _stderr_log,
                 sleeper: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.transport = transport
        self.announce = announce
        self.sleep = sleeper
        self.clock = clock
        self._next_id = 1
        self.session: str | None = None

    # -- protocol layer ------------------------------------------------

    def _rpc_once(self, method: str, params: Mapping[str, Any], rid: int,
                  read_timeout: float) -> Mapping[str, Any]:
        body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": dict(params)}
        headers, response = self.transport.send(body, self.session, read_timeout)
        if method == "initialize" and self.session is None:
            sid = headers.get("mcp-session-id")
            self.session = sid if isinstance(sid, str) and sid else None
        if not isinstance(response, Mapping):
            raise TransportError("non-JSON response")
        error = response.get("error")
        if isinstance(error, Mapping):
            raise PortalError(
                EXIT_TRANSPORT,
                {"error": "jsonrpc_error", "detail": dict(error),
                 "hint": "the server rejected the request; check tool name and arguments"},
                f"JSON-RPC error {error.get('code')}: {error.get('message')}")
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise TransportError("response missing result")
        return result

    def _rpc(self, method: str, params: Mapping[str, Any],
             deadline: float | None, leg_timeout: float,
             retry_cap: int | None = None) -> Mapping[str, Any]:
        """Send one logical request, retrying transport failures idempotently.

        Retries reuse the SAME jsonrpc id and payload: the central keys
        idempotency by request id, so a re-sent request replays its recorded
        outcome instead of duplicating the call.  ``deadline`` is an absolute
        monotonic timestamp; past it, a transport failure is fatal instead of
        retried.  ``retry_cap`` bounds retries for requests that own no open
        round (initialize, tools/list, the CREATE leg); once a round is open
        the portal owes it patience and retries without a cap.
        """
        rid = self._next_id
        self._next_id += 1
        backoff = 2.0
        attempts = 0
        while True:
            try:
                result = self._rpc_once(method, params, rid, leg_timeout)
                backoff = 2.0
                return result
            except TransportError as exc:
                if exc.status in (401, 403):
                    raise PortalError(
                        EXIT_TRANSPORT,
                        {"error": "unauthorized", "status": exc.status,
                         "hint": "check the token in ~/.config/hitl-portal/config.json,"
                                 " HITL_TOKEN, or --token"}) from None
                attempts += 1
                give_up = (deadline is not None and self.clock() >= deadline) or \
                          (retry_cap is not None and attempts >= retry_cap)
                if give_up:
                    raise PortalError(
                        EXIT_TRANSPORT,
                        {"error": "unreachable", "detail": str(exc),
                         "hint": "run: central-portal call list_my_interactions"
                                 " once the central is back"}) from None
                self.announce(f"transport error ({exc}); retrying same request (idempotent)")
                self.sleep(backoff)
                backoff = min(30.0, backoff * 1.6)

    def initialize(self, deadline: float | None = None,
                   retry_cap: int | None = 5) -> None:
        self._rpc("initialize", {"protocolVersion": "2025-06-18",
                                 "capabilities": {}, "clientInfo": {"name": "central-portal", "version": "1.0.0"}},
                  deadline, 30.0, retry_cap=retry_cap)

    # -- the continuation loop ------------------------------------------

    def call(self, name: str, arguments: Mapping[str, Any],
             resume: tuple[str, str, int] | None = None,
             leg_seconds: int = LEG_SECONDS_DEFAULT,
             max_wait: float | None = None,
             busy_wait: float = BUSY_WAIT_DEFAULT) -> dict[str, Any]:
        """Run one tool call to its terminal outcome and return the output object.

        ``resume`` = (interaction_id, call_id, after_event_seq) continues an
        existing round instead of creating a new one.
        """
        start = self.clock()
        deadline = None if max_wait is None else start + float(max_wait)
        leg_timeout = leg_seconds + TIMEOUT_EXTRA_DEFAULT
        create_args: dict[str, Any] = dict(arguments)
        create_args["client_timeout_seconds"] = leg_seconds
        if resume is None:
            arguments = create_args
            self.announce(f"calling {name} (leg={leg_seconds}s"
                          + (f", max-wait={float(max_wait):.0f}s" if max_wait is not None else ", max-wait=∞") + ")")
        else:
            arguments = {"interaction_id": resume[0], "call_id": resume[1],
                         "after_event_seq": resume[2],
                         "client_timeout_seconds": leg_seconds}
            self.announce(f"resuming {name} round {resume[0][:12]}… from event {resume[2]}")

        cursor: int | None = None
        last_outcome: Mapping[str, Any] = {}
        busy_since: float | None = None
        leg = 0
        # The CREATE leg owns no round yet: give up after a few transport
        # failures so a dead central fails fast.  A RESUME continues a round
        # that already exists, and every continuation leg owns an open round:
        # those retry without a cap.
        retry_cap = None if resume is not None else 5
        while True:
            if deadline is not None and self.clock() >= deadline:
                payload: dict[str, Any] = {"tool": name, "deadline_exceeded": True,
                                           "max_wait_seconds": max_wait, "outcome": dict(last_outcome)}
                iid, cid = last_outcome.get("interaction_id"), last_outcome.get("call_id")
                if isinstance(iid, str) and isinstance(cid, str):
                    payload["resume"] = {"interaction_id": iid, "call_id": cid,
                                         "after_event_seq": cursor or 0,
                                         "command": f"central-portal call {name} --resume {iid} {cid} {cursor or 0}"}
                    payload["hint"] = "round still open — resume it, or cancel: central-portal call cancel_interaction"
                raise PortalError(EXIT_DEADLINE, payload, "max-wait-seconds exceeded with round open")

            result = self._rpc("tools/call", {"name": name, "arguments": dict(arguments)},
                               deadline, leg_timeout, retry_cap=retry_cap)
            retry_cap = None
            structured = result.get("structuredContent")
            if not isinstance(structured, dict):
                structured = {}
            status = str(structured.get("status", ""))

            if structured.get("needs_continuation") is True:
                iid, cid = structured.get("interaction_id"), structured.get("call_id")
                if not (isinstance(iid, str) and isinstance(cid, str)):
                    # Malformed continuation payload: surface, do not spin.
                    return {"tool": name, "outcome": dict(structured),
                            "summary": _summary_text(result)}
                cursor = adopt_cursor(cursor, structured)
                last_outcome = structured
                leg += 1
                note = {"opened": "human ACCEPTED, working",
                        "in_progress": "human still working (progress report)"}.get(status, "no pickup yet")
                self.announce(f"leg {leg}: {status or 'heartbeat'} — {note} (iid={iid[:12]}…, cursor={cursor})")
                arguments = {"interaction_id": iid, "call_id": cid,
                             "after_event_seq": cursor,
                             "client_timeout_seconds": leg_seconds}
                continue

            if status == "busy":
                last_outcome = structured
                now = self.clock()
                if busy_since is None:
                    busy_since = now
                if now - busy_since >= busy_wait:
                    raise PortalError(
                        EXIT_BUSY,
                        {"tool": name, "busy": True, "outcome": dict(structured),
                         "hint": "single line still held after "
                                 f"{busy_wait:.0f}s; run: central-portal call list_my_interactions,"
                                 " then resume or cancel the round holding it"},
                        "busy line did not clear")
                retry_after = structured.get("retry_after_seconds")
                wait = retry_after if isinstance(retry_after, (int, float)) \
                    and not isinstance(retry_after, bool) and retry_after > 0 else 30.0
                wait = min(float(wait), max(1.0, busy_wait - (now - busy_since)))
                self.announce(f"busy (line held); retrying a fresh create in {wait:.0f}s")
                self.sleep(wait)
                arguments = create_args  # fresh request: new rpc id, so the server re-executes
                continue

            return {"tool": name, "outcome": dict(structured), "summary": _summary_text(result)}


# ── argument parsing ───────────────────────────────────────────────────────

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_value(text: str) -> Any:
    """Parse an --arg value: JSON when it parses, else the literal string.

    'true' → boolean, '3' → int, '[\"a\",\"b\"]' → list — but 'hello world'
    and '0042x' stay strings.  Force a literal string with JSON quotes:
    --arg 'prompt="true"'.
    """
    try:
        return json.loads(text)
    except ValueError:
        return text


def parse_arg(token: str) -> tuple[str, Any]:
    if "=" not in token:
        raise ValueError(f"--arg expects KEY=VALUE, got {token!r}")
    key, _, value = token.partition("=")
    if not _KEY_RE.match(key):
        raise ValueError(f"bad argument key {key!r}")
    return key, parse_value(value)


def build_arguments(tool: str, arg_tokens: list[str], args_json: str | None,
                    announce: Announcer) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    if args_json:
        try:
            parsed = json.loads(args_json)
        except ValueError as exc:
            raise ValueError(f"--args-json is not valid JSON: {exc}") from None
        if not isinstance(parsed, dict):
            raise ValueError("--args-json must be a JSON object")
        arguments.update(parsed)
    for token in arg_tokens:
        key, value = parse_arg(token)
        arguments[key] = value
    catalog = _CATALOG_BY_NAME.get(tool)
    if catalog:
        known = set(catalog["fields"]) | set(_COMMON_FIELDS)
        for key in arguments:
            if key not in known:
                announce(f"warning: {key!r} is not in the known schema of {tool}; sending anyway")
    return arguments


# ── help & listing ─────────────────────────────────────────────────────────

def _print_tool_help(tool: Mapping[str, Any]) -> None:
    print(tool["name"])
    print(f"  {tool['description']}")
    print("  fields:")
    for field, (ftype, fdesc) in tool["fields"].items():
        print(f"    {field}: {ftype} — {fdesc}")
    if tool["kind"] == "interaction":
        print("  common round fields (all interactive tools):")
        print("    callsign: string — which operator to address (omit for primary)")
        print("    client_timeout_seconds: integer — one leg's park budget (the portal sets this)")
        print("    max_result_bytes: integer — result body budget (>=16384)")
        print("    interaction_id/call_id/after_event_seq — continuation (the portal manages these)")
    if tool["examples"]:
        print("  examples:")
        for example in tool["examples"]:
            print(f"    {example}")


def print_general_help() -> None:
    print(__doc__.strip())


def print_tools(tools: list[Mapping[str, Any]], verbose: bool) -> None:
    if verbose:
        print(json.dumps(tools, ensure_ascii=False, indent=2))
        return
    for tool in tools:
        print(f"{tool.get('name', '?'):32} {str(tool.get('description', '')).splitlines()[0]}")


# ── main ───────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="central-portal",
        description="Drive ONE HITL Central round from the command line: the call parks"
                    " server-side, this process issues every continuation leg, and the"
                    " terminal outcome arrives once as JSON on stdout.",
        epilog="See `central-portal help` for exit codes, configuration and quoting rules.")
    parser.add_argument("--central", help="HITL Central base URL (default https://10.10.0.1:9443)")
    parser.add_argument("--token", help="agent bearer token (default from config/env)")
    parser.add_argument("--cafile", help="CA bundle file for TLS verification")
    parser.add_argument("--insecure", action="store_true", help="skip TLS verification")
    sub = parser.add_subparsers(dest="command", required=True)

    call = sub.add_parser("call", help="call TOOL and wait for its terminal outcome")
    call.add_argument("tool", help="tool name (see `central-portal tools`)")
    call.add_argument("--arg", action="append", default=[], metavar="KEY=VALUE",
                      help="one tool argument; VALUE is parsed as JSON when possible"
                           " (repeatable). Force a literal string with 'prompt=\"true\"'.")
    call.add_argument("--args-json", metavar="JSON", help="all arguments as one JSON object")
    call.add_argument("--resume", nargs=3, metavar=("INTERACTION_ID", "CALL_ID", "AFTER_EVENT_SEQ"),
                      help="continue an existing open round instead of creating a new one")
    call.add_argument("--leg-seconds", type=int, default=LEG_SECONDS_DEFAULT,
                      help=f"one leg's server-side park budget (default {LEG_SECONDS_DEFAULT})")
    call.add_argument("--max-wait-seconds", type=float, default=None,
                      help="overall wall-clock cap; on expiry with an open round, exit 3"
                           " with a resume command (default: wait forever)")
    call.add_argument("--busy-wait-seconds", type=float, default=BUSY_WAIT_DEFAULT,
                      help=f"how long to retry a busy single line before exit 5 (default {BUSY_WAIT_DEFAULT})")

    tools = sub.add_parser("tools", help="list the central's tools (live, falls back to embedded)")
    tools.add_argument("-v", "--verbose", action="store_true", help="print full schemas as JSON")

    help_p = sub.add_parser("help", help="show help: general, or for one tool")
    help_p.add_argument("tool", nargs="?", help="tool name")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "help":
        if args.tool:
            tool = _CATALOG_BY_NAME.get(args.tool)
            if tool is None:
                print(f"no embedded help for {args.tool!r}; try: central-portal tools -v",
                      file=sys.stderr)
                return EXIT_USAGE
            _print_tool_help(tool)
        else:
            print_general_help()
        return EXIT_OUTCOME

    conn = resolve_connection(args)

    if args.command == "tools":
        catalog = [{"name": t["name"], "description": t["description"],
                    "inputSchema": {"type": "object",
                                    "properties": {**{field: {"type": ftype} for field, (ftype, _) in t["fields"].items()},
                                                   **{field: {} for field in _COMMON_FIELDS}}}}
                   for t in _CATALOG]
        try:
            transport = HttpTransport(conn["url"], conn["token"],
                                      conn.get("ca_file"), conn.get("insecure", False))
            portal = Portal(transport)
            portal.initialize()
            result = portal._rpc("tools/list", {}, portal.clock() + 45.0, 30.0, retry_cap=3)
            print_tools(list(result.get("tools", [])), args.verbose)
        except (TransportError, PortalError) as exc:
            print(f"offline ({exc}); showing the embedded catalog, which may lag the server",
                  file=sys.stderr)
            print_tools(catalog, args.verbose)  # type: ignore[arg-type]
        return EXIT_OUTCOME

    # -- call --
    try:
        arguments = build_arguments(args.tool, args.arg, args.args_json, _stderr_log)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    resume: tuple[str, str, int] | None = None
    if args.resume:
        iid, cid, seq = args.resume
        try:
            resume = (iid, cid, int(seq))
        except ValueError:
            print("error: AFTER_EVENT_SEQ must be an integer", file=sys.stderr)
            return EXIT_USAGE

    transport = HttpTransport(conn["url"], conn["token"],
                              conn.get("ca_file"), conn.get("insecure", False))
    portal = Portal(transport)
    max_wait = args.max_wait_seconds
    try:
        portal.initialize(None if max_wait is None else portal.clock() + float(max_wait) + 30.0)
        output = portal.call(args.tool, arguments, resume=resume,
                             leg_seconds=args.leg_seconds,
                             max_wait=None if max_wait is None else float(max_wait),
                             busy_wait=args.busy_wait_seconds)
    except PortalError as exc:
        print(json.dumps(exc.payload, ensure_ascii=False, indent=2))
        print(f"central-portal: {exc}", file=sys.stderr)
        return exc.exit_code
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return EXIT_OUTCOME


if __name__ == "__main__":
    sys.exit(main())
