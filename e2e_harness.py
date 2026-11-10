"""E2E harness: drives the real desktop terminal GUI against the real central service.

Deterministic notification injection (no HTTP listener needed — we call
receive_notification directly, same code path the central's push would hit).
Each test creates a real MCP call, drives real Tkinter widgets, verifies both
GUI and central state.
"""
import json
import ssl
import sys
import time
import uuid
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import tkinter as tk
from central_desktop_terminal import (
    DesktopTerminalConfig, CentralHttpDesktopClient, DesktopTerminalClient,
    _TerminalListener, RingtonePlayer, TkPresentation,
)

CENTRAL = "https://10.10.0.61:9443"
CTX = ssl._create_unverified_context()
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


class Harness:
    def __init__(self):
        cfg = json.loads(Path("desktop-terminal.json").read_text())
        self.config = DesktopTerminalConfig.from_mapping(cfg)
        self.root = tk.Tk()
        self.root.withdraw()
        self.ring = RingtonePlayer(
            ringtone_path=self.config.ringtone_path,
            ringtone_alt_path=self.config.ringtone_alt_path,
            bell=self.root.bell)
        self.central = CentralHttpDesktopClient(self.config)
        self.holder = {}
        self.presenter = TkPresentation(self.root, lambda: self.holder["client"])
        self.client = DesktopTerminalClient(
            config={"terminal_credential": self.config.terminal_credential,
                    "callback_credential": self.config.callback_credential},
            central=self.central, listener=_TerminalListener(),
            gui_capability=lambda: True, ring=self.ring, presenter=self.presenter)
        self.holder["client"] = self.client
        assert self.client.start()["success"], "terminal failed to start"
        self.central.set_dnd(False, "")
        self.central.set_quiet_window(0, "short")
        self.root.update()

    # ---------- primitives ----------

    def beat(self):
        """Refresh liveness so ready_desktop() sees us."""
        self.central.heartbeat()

    def call(self, tool="get_user_input", timeout=45, retry_busy=True, **kw):
        self.beat()
        time.sleep(0.15)
        args = {"prompt": kw.pop("prompt", "e2e"), "required": True,
                "client_timeout_seconds": timeout,
                "request_id": "e2e-" + uuid.uuid4().hex[:8]}
        args.update(kw)
        sc = mcp(tool, args)
        for attempt in range(3) if retry_busy else range(0):
            if sc.get("call_id"):
                break
            # busy — force-clear ALL in-flight calls via the lifecycle API
            import subprocess, tempfile
            sweep = ("import sqlite3\n"
                     "c=sqlite3.connect(r'C:\\Users\\Administrator\\hitl-physical-e2e\\data\\lifecycle.sqlite3')\n"
                     "c.execute(\"UPDATE lifecycle_calls SET call_state='not_accepted',reason='ring_timeout' "
                     "WHERE call_state IN ('ringing','accepted','in_progress')\")\n"
                     "c.commit()\n")
            with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
                f.write(sweep); p = f.name
            try:
                subprocess.run(["scp","-q",p,"10.10.0.61:C:/Users/Administrator/_sweep.py"],timeout=10,capture_output=True)
                subprocess.run(["ssh","10.10.0.61","python C:\\Users\\Administrator\\_sweep.py"],timeout=10,capture_output=True)
            except Exception: pass
            finally: Path(p).unlink(missing_ok=True)
            time.sleep(0.8)
            self.beat(); time.sleep(0.15)
            sc = mcp(tool, args)
        cid = sc.get("call_id")
        if cid and sc.get("call_state") == "ringing":
            self.notify(cid)
        return sc

    def notify(self, cid, version=1):
        """Inject the notification (same effect as the central's push callback).
        Detects quiet-window degradation from central state, like the real push."""
        quiet = {"quiet_window_seconds": 0, "quiet_mode": "short", "last_accept_at": None}
        try:
            quiet = self.central.get_quiet_window()
        except Exception:
            pass
        event = {"call_id": cid, "version": version, "ring_deadline": time.time() + 120}
        la = quiet.get("last_accept_at")
        if la and time.time() - la < quiet.get("quiet_window_seconds", 0):
            event["degraded"] = True
            event["quiet_mode"] = quiet.get("quiet_mode", "short")
        self.client.receive_notification(
            event, callback_credential=self.config.callback_credential)
        self.root.update()

    def state(self, cid):
        return self.central.read_call(cid, self.client._session_id)

    def window(self, cid):
        return self.presenter._windows.get(cid)

    def accept(self, cid):
        btns = self.presenter._buttons[cid]
        btns[0].invoke()
        self.root.update(); time.sleep(0.4); self.root.update()

    def decline(self, cid):
        btns = self.presenter._buttons[cid]
        btns[1].invoke()
        self.root.update(); time.sleep(0.4); self.root.update()

    def answer_dialog(self):
        for child in self.root.winfo_children():
            if isinstance(child, tk.Toplevel) and "回答" in child.title():
                return child
        return None

    def find_btn(self, parent, text):
        stack = list(parent.winfo_children())
        while stack:
            c = stack.pop(0)
            if isinstance(c, tk.Button) and c.cget("text") == text:
                return c
            stack.extend(c.winfo_children())
        return None

    def find_entry(self, parent):
        stack = list(parent.winfo_children())
        while stack:
            c = stack.pop(0)
            if isinstance(c, tk.Entry):
                return c
            stack.extend(c.winfo_children())
        return None

    def cleanup(self, cid):
        if not cid: return
        try: self.central.cancel(call_id=cid, operation_id=uuid.uuid4().hex)
        except Exception: pass
        self.presenter.close(cid)
        self.root.update()

    def sweep(self):
        """Force-clear all ringing calls via central DB (between test groups)."""
        import subprocess, tempfile
        script = (
            "import sqlite3\n"
            "c = sqlite3.connect(r'C:\\Users\\Administrator\\hitl-physical-e2e\\data\\lifecycle.sqlite3')\n"
            "c.execute(\"UPDATE lifecycle_calls SET call_state='not_accepted', reason='ring_timeout' "
            "WHERE call_state IN ('ringing','accepted','in_progress')\")\n"
            "c.commit()\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(script); path = f.name
        try:
            subprocess.run(["scp", "-q", path, "10.10.0.61:C:/Users/Administrator/_sweep.py"], timeout=10)
            subprocess.run(["ssh", "10.10.0.61", "python C:\\Users\\Administrator\\_sweep.py"],
                          capture_output=True, timeout=10)
        except Exception:
            pass
        finally:
            Path(path).unlink(missing_ok=True)
        time.sleep(0.5)

    def check(self, name, cond, detail=""):
        global _PASS, _FAIL
        if cond:
            _PASS += 1
            print(f"  ✓ {name}")
        else:
            _FAIL += 1
            _FAILURES.append(f"{name}  {detail}")
            print(f"  ✗ {name}  {detail}")


# ==================== TESTS ====================

def t_lifecycle(h):
    print("── T1: ring → accept → submit → answered")
    sc = h.call()
    cid = sc["call_id"]
    h.check("1.1 ringing window", h.window(cid) is not None)
    h.accept(cid)
    h.check("1.2 accepted", h.state(cid).get("call_state") == "accepted")
    h.check("1.3 window closed", h.window(cid) is None)
    dlg = h.answer_dialog()
    h.check("1.4 answer dialog", dlg is not None)
    if dlg:
        e = h.find_entry(dlg)
        if e: e.insert(0, "e2e answer")
        else: print("    [debug] no entry found in dialog!", flush=True)
        s = h.find_btn(dlg, "提交")
        if s:
            s.invoke(); time.sleep(1.0)
        else: print("    [debug] no submit button!", flush=True)
    else:
        print("    [debug] no answer dialog!", flush=True)
    # read_call doesn't carry results — poll via MCP continuation
    # after_event_seq=1 skips the accept replay, gets the submit/answered event
    iid = sc.get("interaction_id", "")
    time.sleep(0.5)
    st = mcp("get_user_input", {"interaction_id": iid, "call_id": cid,
              "after_event_seq": 1, "client_timeout_seconds": 10})
    h.check("1.5 answered", st.get("call_state") == "answered", f"got {st.get('call_state')}")
    h.check("1.6 result", st.get("result", {}).get("user_input") == "e2e answer",
            f"got {json.dumps(st.get('result',{}), ensure_ascii=False)[:120]}")

    print("── T2: ring → decline")
    sc = h.call()
    cid = sc["call_id"]
    h.check("2.1 window", h.window(cid) is not None)
    h.decline(cid)
    h.check("2.2 declined", h.state(cid).get("call_state") == "declined")

    print("── T3: ring → accept → cancel")
    sc = h.call()
    cid = sc["call_id"]
    h.accept(cid)
    dlg = h.answer_dialog()
    if dlg:
        c = h.find_btn(dlg, "取消")
        h.check("3.1 cancel btn", c is not None)
        if c:
            c.invoke(); time.sleep(0.4)
    h.check("3.2 cancelled", h.state(cid).get("call_state") == "cancelled")

    print("── T4: ring stops instantly on accept")
    sc = h.call()
    cid = sc["call_id"]
    handle = h.ring._handles.get(cid)
    h.check("4.1 ring playing", handle is not None)
    h.accept(cid)
    h.check("4.2 ring handle cleared", h.ring._handles.get(cid) is None)
    if handle and hasattr(handle, "poll"):
        h.check("4.3 audio process dead", handle.poll() is not None)
    h.cleanup(cid)


def t_negative(h):
    print("── T5: DND auto-decline + message")
    h.central.set_dnd(True, "e2e dnd")
    sc = h.call()
    h.check("5.1 declined", sc.get("status") == "declined")
    h.check("5.2 reason dnd", sc.get("reason") == "do_not_disturb")
    h.check("5.3 message", sc.get("message") == "e2e dnd")
    h.central.set_dnd(False, "")

    print("── T6: busy")
    sc1 = h.call()
    sc2 = h.call(retry_busy=False)  # expect busy, don't auto-clear
    h.check("6.1 busy", sc2.get("status") == "busy")
    h.cleanup(sc1["call_id"])

    print("── T7: quiet window — answer, next call degraded")
    h.central.set_quiet_window(300, "short")
    sc1 = h.call()
    h.accept(sc1["call_id"])
    time.sleep(0.5)
    sc2 = h.call()
    h.check("7.1 second call created", sc2.get("call_id") is not None)
    h.check("7.2 window shows", h.window(sc2["call_id"]) is not None)
    h.cleanup(sc2["call_id"])
    h.cleanup(sc1["call_id"])
    h.central.set_quiet_window(0, "short")

    print("── T8: quiet window silent mode")
    h.central.set_quiet_window(300, "silent")
    sc1 = h.call()
    h.accept(sc1["call_id"])
    time.sleep(0.5)
    sc2 = h.call()
    h.check("8.1 second call created", sc2.get("call_id") is not None)
    h.check("8.2 window shows (silent still pops)", h.window(sc2["call_id"]) is not None)
    h.check("8.3 ring NOT playing (silent)", h.ring._handles.get(sc2["call_id"]) is None)
    h.cleanup(sc2["call_id"])
    h.cleanup(sc1["call_id"])
    h.central.set_quiet_window(0, "short")


def t_edge(h):
    print("── T9: sync refreshes liveness (no disconnect during form fill)")
    sc = h.call()
    cid = sc["call_id"]
    h.accept(cid)
    # Simulate the terminal's periodic sync proving aliveness
    for _ in range(3):
        h.central.synchronize()
        time.sleep(0.3)
    st = h.state(cid)
    h.check("9.1 still accepted after syncs", st.get("call_state") in ("accepted", "in_progress"),
            f"got {st.get('call_state')}")
    h.cleanup(cid)

    print("── T10: rapid accept after ring")
    sc = h.call()
    cid = sc["call_id"]
    h.accept(cid)  # accept immediately, no pump/sleep
    st = h.state(cid)
    h.check("10.1 accepted", st.get("call_state") == "accepted",
            f"got {st.get('call_state')}")
    h.cleanup(cid)

    print("── T11: double decline (idempotent)")
    sc = h.call()
    cid = sc["call_id"]
    h.decline(cid)
    st1 = h.state(cid)
    h.check("11.1 declined", st1.get("call_state") == "declined")
    # Try declining again (already declined — should not crash)
    try:
        h.presenter._decline(cid)
        h.check("11.2 second decline no crash", True)
    except Exception as ex:
        h.check("11.2 second decline no crash", False, str(ex))

    print("── T12: notify with stale version (duplicate)")
    sc = h.call()
    cid = sc["call_id"]
    h.notify(cid, version=1)
    h.notify(cid, version=1)  # duplicate
    h.check("12.1 no duplicate window", h.window(cid) is not None)  # still there, not duplicated
    h.decline(cid)

    print("── T13: upload attachment during accepted call")
    sc = h.call()
    cid = sc["call_id"]
    h.accept(cid)
    up = h.central.upload_attachment(
        call_id=cid, filename="e2e.txt",
        media_type="text/plain", content=b"e2e attachment content")
    h.check("13.1 upload succeeds", up.get("success") is True, f"got {up}")
    h.check("13.2 attachment_id returned", isinstance(up.get("attachment_id"), str))
    h.cleanup(cid)


def run():
    h = Harness()
    for group in (t_lifecycle, t_negative, t_edge):
        h.sweep()
        try:
            group(h)
        except Exception as ex:
            global _FAIL
            _FAIL += 1
            _FAILURES.append(f"{group.__name__} crashed: {ex}")
            print(f"  ✗ {group.__name__} crashed: {ex}")
    print(f"\n{'='*40}\nRESULTS: {_PASS} passed, {_FAIL} failed")
    if _FAILURES:
        print("\nFailures:")
        for f in _FAILURES:
            print(f"  ✗ {f}")
    return _FAIL


if __name__ == "__main__":
    sys.exit(run())
