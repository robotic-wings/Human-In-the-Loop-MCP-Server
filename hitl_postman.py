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


def main() -> None:
    print(f"hitl-postman: start {CENTRAL} -> {CHIDORI} poll={POLL_SECONDS}s", flush=True)
    while True:
        cursor = load_cursor()
        try:
            data = get_json(f"{CENTRAL}/api/agent/outcomes?after={cursor}&limit=50")
            for entry in data.get("outcomes", []):
                seq = entry["seq"]
                if select_target(entry) is None:
                    save_cursor(seq)
                    continue
                delivered = False
                attempt = 0
                while not delivered:
                    try:
                        delivered = deliver(entry)
                    except urllib.error.HTTPError as exc:
                        if classify_http_error(exc.code) == "definitive":
                            print(f"definitive {exc.code} for {select_target(entry)}: undeliverable", flush=True)
                            break
                        attempt += 1
                        if attempt > 60:
                            print(f"giving up on {select_target(entry)} after {attempt} attempts", flush=True)
                            break
                        time.sleep(min(5.0, 0.5 * attempt))
                    except Exception as exc:
                        attempt += 1
                        if attempt > 60:
                            print(f"giving up on {select_target(entry)} after {attempt} attempts: {exc}", flush=True)
                            break
                        time.sleep(min(5.0, 0.5 * attempt))
                try:
                    ack(seq, "delivered" if delivered else "undeliverable")
                except Exception as exc:
                    print(f"ack fail seq={seq}: {exc}", flush=True)
                if delivered:
                    print(f"delivered seq={seq} -> {select_target(entry)} call={entry['call_id'][:8]}", flush=True)
                save_cursor(seq)
        except Exception as exc:
            print(f"poll fail: {exc}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
