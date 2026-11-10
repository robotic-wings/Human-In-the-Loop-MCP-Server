"""Playwright E2E: drives the mobile web pages against the real central service.

Opens a real browser (chromium), logs in as the operator, and interacts with
the mobile web UI the same way the iPhone does via Bark → Safari.
"""
import json
import ssl
import sys
import time
import uuid
import urllib.request
import tempfile
import subprocess
from pathlib import Path

CENTRAL = "https://10.10.0.61:9443"
CTX = ssl._create_unverified_context()
CREDENTIAL = "owner-iphone-login-2026"
_PASS = 0
_FAIL = 0
_FAILURES = []


def mcp(tool, args):
    body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "tools/call",
            "params": {"name": tool, "arguments": dict(args)}}
    req = urllib.request.Request(
        CENTRAL + "/mcp", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer agent-secret", "Content-Type": "application/json"},
        method="POST")
    resp = urllib.request.urlopen(req, timeout=15, context=CTX)
    return json.loads(resp.read()).get("result", {}).get("structuredContent", {})


def check(name, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ✓ {name}")
    else:
        _FAIL += 1
        _FAILURES.append(f"{name}  {detail}")
        print(f"  ✗ {name}  {detail}")


def sweep():
    """Clear all in-flight calls via central DB."""
    import subprocess, tempfile
    script = ("import sqlite3\n"
              "c=sqlite3.connect(r'C:\\Users\\Administrator\\hitl-physical-e2e\\data\\lifecycle.sqlite3')\n"
              "c.execute(\"UPDATE lifecycle_calls SET call_state='not_accepted',reason='ring_timeout' "
              "WHERE call_state IN ('ringing','accepted','in_progress')\")\n"
              "c.execute(\"UPDATE lifecycle_quiet_window SET quiet_window_seconds=0\")\n"
              "c.commit()\n")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(script); p = f.name
    try:
        subprocess.run(["scp","-q",p,"10.10.0.61:C:/Users/Administrator/_sweep.py"],timeout=10,capture_output=True)
        subprocess.run(["ssh","10.10.0.61","python C:\\Users\\Administrator\\_sweep.py"],timeout=10,capture_output=True)
    except Exception: pass
    finally: Path(p).unlink(missing_ok=True)
    time.sleep(0.5)


def login(page, url):
    """Navigate to a call page, logging in if needed."""
    page.goto(url, wait_until="domcontentloaded")
    # Check if we got the login form
    cred_input = page.query_selector("#operator-credential")
    if cred_input:
        cred_input.fill(CREDENTIAL)
        page.click("button[type=submit]")
        page.wait_for_load_state("domcontentloaded")
        time.sleep(0.5)


def run():
    from playwright.sync_api import sync_playwright

    sweep()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 390, "height": 844},  # iPhone-ish
            ignore_https_errors=True)

        # ── M1: Login flow ──
        print("── M1: Login page renders with Bootstrap card")
        page.goto(CENTRAL + "/calls/nonexistent-id", wait_until="domcontentloaded")
        cred = page.query_selector("#operator-credential")
        check("M1.1 login form shown", cred is not None)
        card = page.query_selector(".card")
        check("M1.2 Bootstrap card present", card is not None)

        # ── M2: Accept/Decline page ──
        print("── M2: Accept flow")
        sc = mcp("get_user_input", {"prompt": "M2 mobile accept test", "required": True,
                                     "client_timeout_seconds": 30, "request_id": "m2-" + uuid.uuid4().hex[:6]})
        cid = sc.get("call_id")
        check("M2.1 call created", cid is not None)
        if cid:
            login(page, f"{CENTRAL}/calls/{cid}")
            # Should show the accept/decline buttons
            accept_btn = page.query_selector("button:has-text('接听')")
            check("M2.2 accept button shown", accept_btn is not None)
            decline_btn = page.query_selector("button:has-text('拒绝')")
            check("M2.3 decline button shown", decline_btn is not None)
            # Check Bootstrap classes
            card = page.query_selector(".card")
            check("M2.4 card layout", card is not None)
            if accept_btn:
                accept_btn.click()
                page.wait_for_load_state("domcontentloaded")
                time.sleep(0.8)
                # Should show the response form
                text_input = page.query_selector("[data-result-key='user_input']")
                check("M2.5 response form shown", text_input is not None)
                if text_input:
                    text_input.fill("mobile answer")
                    submit = page.query_selector("button:has-text('提交回答')")
                    check("M2.6 submit button shown", submit is not None)
                    if submit:
                        submit.click()
                        page.wait_for_load_state("domcontentloaded")
                        time.sleep(0.8)
            # Verify on central
            st = mcp("get_user_input", {"interaction_id": sc.get("interaction_id"),
                      "call_id": cid, "after_event_seq": 1, "client_timeout_seconds": 5})
            check("M2.7 answered", st.get("call_state") == "answered", f"got {st.get('call_state')}")
            check("M2.8 result", st.get("result", {}).get("user_input") == "mobile answer",
                  f"got {st.get('result', {})}")

        # ── M3: Decline flow ──
        print("── M3: Decline flow")
        sc = mcp("get_user_input", {"prompt": "M3 decline test", "required": True,
                                     "client_timeout_seconds": 30, "request_id": "m3-" + uuid.uuid4().hex[:6]})
        cid = sc.get("call_id")
        if cid:
            login(page, f"{CENTRAL}/calls/{cid}")
            decline_btn = page.query_selector("button:has-text('拒绝')")
            if decline_btn:
                decline_btn.click()
                page.wait_for_load_state("domcontentloaded")
                time.sleep(0.8)
            st = mcp("get_user_input", {"interaction_id": sc.get("interaction_id"),
                      "call_id": cid, "after_event_seq": 0, "client_timeout_seconds": 5})
            check("M3.1 declined", st.get("call_state") == "declined", f"got {st.get('call_state')}")

        # ── M4: Choice tool ──
        print("── M4: Choice (single)")
        sc = mcp("get_user_choice", {"prompt": "M4 pick one", "choices": ["甲", "乙", "丙"],
                                      "multiple": False, "client_timeout_seconds": 30,
                                      "request_id": "m4-" + uuid.uuid4().hex[:6]})
        cid = sc.get("call_id")
        if cid:
            login(page, f"{CENTRAL}/calls/{cid}")
            accept_btn = page.query_selector("button:has-text('接听')")
            if accept_btn:
                accept_btn.click()
                page.wait_for_load_state("domcontentloaded")
                time.sleep(0.8)
            radios = page.query_selector_all("[data-result-key='selected_choices']")
            check("M4.1 radio options shown", len(radios) == 3, f"got {len(radios)}")
            if len(radios) >= 2:
                radios[1].check()  # pick 乙
                submit = page.query_selector("button:has-text('提交回答')")
                if submit:
                    submit.click()
                    page.wait_for_load_state("domcontentloaded")
                    time.sleep(0.8)
            st = mcp("get_user_choice", {"interaction_id": sc.get("interaction_id"),
                      "call_id": cid, "after_event_seq": 1, "client_timeout_seconds": 5})
            check("M4.2 answered", st.get("call_state") == "answered")
            check("M4.3 selected", st.get("result", {}).get("selected_choices") == ["乙"],
                  f"got {st.get('result', {})}")

        # ── M5: Confirm tool ──
        print("── M5: Confirm dialog")
        sc = mcp("show_confirmation_dialog", {"prompt": "M5 confirm?",
                                              "client_timeout_seconds": 30,
                                              "request_id": "m5-" + uuid.uuid4().hex[:6]})
        cid = sc.get("call_id")
        if cid:
            login(page, f"{CENTRAL}/calls/{cid}")
            accept_btn = page.query_selector("button:has-text('接听')")
            if accept_btn:
                accept_btn.click()
                page.wait_for_load_state("domcontentloaded")
                time.sleep(0.8)
            yes = page.query_selector("[data-result-key='confirmed'][value='true']")
            check("M5.1 yes radio shown", yes is not None)
            if yes:
                yes.check()
                submit = page.query_selector("button:has-text('提交回答')")
                if submit:
                    submit.click()
                    page.wait_for_load_state("domcontentloaded")
                    time.sleep(0.8)
            st = mcp("show_confirmation_dialog", {"interaction_id": sc.get("interaction_id"),
                      "call_id": cid, "after_event_seq": 1, "client_timeout_seconds": 5})
            check("M5.2 answered", st.get("call_state") == "answered")
            check("M5.3 confirmed true", st.get("result", {}).get("confirmed") is True,
                  f"got {st.get('result', {})}")

        # ── M6: Page refresh preserves state ──
        print("── M6: Page refresh")
        sc = mcp("get_user_input", {"prompt": "M6 refresh test", "required": True,
                                     "client_timeout_seconds": 30, "request_id": "m6-" + uuid.uuid4().hex[:6]})
        cid = sc.get("call_id")
        if cid:
            login(page, f"{CENTRAL}/calls/{cid}")
            accept_btn = page.query_selector("button:has-text('接听')")
            if accept_btn:
                accept_btn.click()
                page.wait_for_load_state("domcontentloaded")
                time.sleep(0.5)
            # Refresh — should still show the form (accepted state)
            page.reload(wait_until="domcontentloaded")
            time.sleep(0.5)
            form = page.query_selector("#response-form")
            check("M6.1 form survives refresh", form is not None)
            # Clean up
            mcp("cancel_interaction", {"interaction_id": sc.get("interaction_id"),
                "call_id": cid, "client_timeout_seconds": 5,
                "request_id": "m6c-" + uuid.uuid4().hex[:6]})

        # ── M7: DND ──
        print("── M7: DND auto-decline")
        # Set DND via the API (simulating the desktop terminal)
        import subprocess
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("import urllib.request,ssl,json,sqlite3\n"
                    "c=sqlite3.connect(r'C:\\Users\\Administrator\\hitl-physical-e2e\\data\\lifecycle.sqlite3')\n"
                    "c.execute(\"UPDATE lifecycle_dnd SET enabled=1, message='mobile dnd test' WHERE operator_id='only-operator'\")\n"
                    "c.commit()\n")
            sp = f.name
        try:
            subprocess.run(["scp","-q",sp,"10.10.0.61:C:/Users/Administrator/_dnd.py"],timeout=10,capture_output=True)
            subprocess.run(["ssh","10.10.0.61","python C:\\Users\\Administrator\\_dnd.py"],timeout=10,capture_output=True)
        except Exception: pass
        finally: Path(sp).unlink(missing_ok=True)
        time.sleep(0.3)
        sc = mcp("get_user_input", {"prompt": "M7 dnd test", "required": True,
                                     "client_timeout_seconds": 10, "request_id": "m7-" + uuid.uuid4().hex[:6]})
        check("M7.1 auto-declined", sc.get("status") == "declined")
        check("M7.2 dnd message", sc.get("message") == "mobile dnd test")
        # Reset DND
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("import sqlite3\n"
                    "c=sqlite3.connect(r'C:\\Users\\Administrator\\hitl-physical-e2e\\data\\lifecycle.sqlite3')\n"
                    "c.execute(\"UPDATE lifecycle_dnd SET enabled=0, message='' WHERE operator_id='only-operator'\")\n"
                    "c.commit()\n")
            sp = f.name
        try:
            subprocess.run(["scp","-q",sp,"10.10.0.61:C:/Users/Administrator/_dnd.py"],timeout=10,capture_output=True)
            subprocess.run(["ssh","10.10.0.61","python C:\\Users\\Administrator\\_dnd.py"],timeout=10,capture_output=True)
        except Exception: pass
        finally: Path(sp).unlink(missing_ok=True)

        browser.close()

    print(f"\n{'='*40}\nRESULTS: {_PASS} passed, {_FAIL} failed")
    if _FAILURES:
        print("\nFailures:")
        for f in _FAILURES:
            print(f"  ✗ {f}")
    return _FAIL


if __name__ == "__main__":
    sys.exit(run())
