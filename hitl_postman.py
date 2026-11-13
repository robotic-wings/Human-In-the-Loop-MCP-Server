"""HITL postman bridge: deliver central outcomes to detached Chidori agents.

Runs as a systemd service beside the central. It polls the central outcome
feed, and for each chidori-detached entry delivers the outcome to the target
agent's signal mailbox (``POST /agents/detached/{agent}/send``), acks the
delivery, and advances a durable cursor so a restart resumes without loss.

Delivery is at-least-once: the cursor is only advanced after a successful send
or a definitive failure, so a crash mid-delivery re-reads the entry. The
classification between "definitive" (agent gone / run terminal → mark
undeliverable) and "retryable" (Chidori restarting → back off) is the one
decision that decides whether a human answer is preserved or dropped.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

CENTRAL = os.environ.get("HITL_CENTRAL", "https://10.10.0.1:9443").rstrip("/")
TOKEN = os.environ.get("HITL_TOKEN", "")
CHIDORI = os.environ.get("CHIDORI_URL", "http://127.0.0.1:8080")
CHIDORI_TOKEN = os.environ.get("CHIDORI_API_KEY", "")
POLL_SECONDS = float(os.environ.get("HITL_POLL_SECONDS", "5"))
CURSOR_FILE = os.environ.get("HITL_CURSOR_FILE", "/var/lib/hitl-central/postman.cursor")

DEFINITIVE_HTTP_CODES = frozenset({404, 409})


def classify_http_error(status: int) -> str:
    """'definitive' (agent truly gone / run terminal) vs 'retryable' (restart).

    A definitive status means the target agent can never receive this outcome
    again, so the entry is marked undeliverable and the cursor advances. A
    retryable status (or a transport error, which has no status) means Chidori
    is briefly unavailable, so the send is retried with capped backoff.
    """
    return "definitive" if status in DEFINITIVE_HTTP_CODES else "retryable"


def select_target(entry: dict) -> str | None:
    """The target agent name for a chidori-detached delivery, else None."""
    delivery = entry.get("delivery")
    if not isinstance(delivery, dict) or delivery.get("kind") != "chidori-detached":
        return None
    target = delivery.get("agent")
    return target if isinstance(target, str) and target else None


def load_cursor() -> int:
    try:
        return int(open(CURSOR_FILE).read().strip() or "0")
    except (OSError, ValueError):
        return 0


def save_cursor(seq: int) -> None:
    tmp = CURSOR_FILE + ".tmp"
    open(tmp, "w").write(str(seq))
    os.replace(tmp, CURSOR_FILE)


def get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def post_json(url: str, body: dict, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def deliver(entry: dict) -> bool:
    """Deliver one outcome to its target agent's signal mailbox.

    Returns True when the agent accepted the delivery. Raises urllib errors for
    the caller to classify; the caller owns the retry/backoff policy.
    """
    target = select_target(entry)
    if target is None:
        return False
    out = post_json(
        f"{CHIDORI}/agents/detached/{target}/send",
        {"name": f"hitl:{entry['call_id']}", "payload": entry["outcome"],
         "from": {"kind": "human", "id": entry.get("operator") or "operator"}},
        token=CHIDORI_TOKEN,
    )
    return bool(out.get("delivered") or out.get("status"))


def ack(seq: int, status: str) -> None:
    post_json(f"{CENTRAL}/api/agent/outcomes/ack", {"seq": seq, "status": status}, token=TOKEN)


def process_entry(entry: dict, *, deliver_fn, ack_fn, save_fn, sleep_fn,
                  max_attempts: int = 60) -> str:
    """Deliver one entry and advance the cursor; returns the terminal status.

    At-least-once by construction: the cursor is only advanced after a send
    succeeds or a definitive failure. A crash before ``save_fn`` re-reads the
    entry on restart, so a human answer is never silently dropped. A definitive
    HTTP status (agent gone / run terminal) short-circuits to undeliverable
    without a retry; anything else backs off for at most ``max_attempts``.
    """
    seq = entry["seq"]
    if select_target(entry) is None:
        save_fn(seq)
        return "skipped"
    delivered = False
    attempt = 0
    while not delivered:
        try:
            delivered = deliver_fn(entry)
        except urllib.error.HTTPError as exc:
            if classify_http_error(exc.code) == "definitive":
                break
            attempt += 1
            if attempt > max_attempts:
                break
            sleep_fn(min(5.0, 0.5 * attempt))
        except Exception:
            attempt += 1
            if attempt > max_attempts:
                break
            sleep_fn(min(5.0, 0.5 * attempt))
    try:
        ack_fn(seq, "delivered" if delivered else "undeliverable")
    except Exception:
        pass
    save_fn(seq)
    return "delivered" if delivered else "undeliverable"


def main() -> None:
    print(f"hitl-postman: start {CENTRAL} -> {CHIDORI} poll={POLL_SECONDS}s", flush=True)
    while True:
        cursor = load_cursor()
        try:
            data = get_json(f"{CENTRAL}/api/agent/outcomes?after={cursor}&limit=50")
            for entry in data.get("outcomes", []):
                try:
                    status = process_entry(
                        entry,
                        deliver_fn=deliver,
                        ack_fn=ack,
                        save_fn=save_cursor,
                        sleep_fn=time.sleep,
                    )
                except Exception as exc:
                    print(f"entry {entry.get('seq')} failed: {exc}", flush=True)
                    continue
                if status == "delivered":
                    print(f"delivered seq={entry['seq']} -> {select_target(entry)} call={entry['call_id'][:8]}", flush=True)
                elif status == "undeliverable":
                    print(f"undeliverable seq={entry['seq']}", flush=True)
        except Exception as exc:
            print(f"poll fail: {exc}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
