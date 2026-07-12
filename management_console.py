#!/usr/bin/env python3
"""
Management Console for the Human-in-the-Loop MCP Server.

A small, self-contained (stdlib-only, + the sibling human_loop_config module)
tabbed control panel for the operator:

  - Logs          : browse / open / delete the recorded interactions (every tool
                    that asks the human for input, plus delegated tasks)
  - User Profile  : who you are (name/role/responsibilities) + how the AI should
                    talk to you; the server injects this into its guidance prompt
  - Server        : bring the MCP HTTP server Online / Offline (the console owns
                    the process; closing the console stops it)
  - Task Options  : default task timeout, default max result size, attachments on/off
  - Notification  : ringtone for incoming tasks + mute

Run:  python management_console.py
Settings are stored in ~/.human_loop_config.json (override $HUMAN_LOOP_CONFIG).
The logs archive matches the server: $HUMAN_LOOP_LOGS_DIR (or legacy
$HUMAN_LOOP_OUTBOX_DIR), else ~/.human_loop_logs (or legacy ~/.human_loop_outbox).
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox, filedialog, ttk
from datetime import datetime

import human_loop_config

# --- Must match human_loop_server.py (kept duplicated to stay dependency-free) ---
LOGS_ENV_VAR = "HUMAN_LOOP_LOGS_DIR"
DEFAULT_LOGS_DIR = os.path.join(os.path.expanduser("~"), ".human_loop_logs")
# Legacy names (older releases called this the "outbox"); still honored.
OUTBOX_ENV_VAR = "HUMAN_LOOP_OUTBOX_DIR"
DEFAULT_OUTBOX_DIR = os.path.join(os.path.expanduser("~"), ".human_loop_outbox")

# The server script this console launches for HTTP mode, plus its PID / log files.
_HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_SCRIPT = os.path.join(_HERE, "human_loop_server.py")
PID_FILE = os.path.join(os.path.expanduser("~"), ".human_loop_server.pid")
SERVER_LOG = os.path.join(os.path.expanduser("~"), ".human_loop_server.log")
BUNDLED_RINGTONE = os.path.join(_HERE, "notify.wav")

# Where auto-generated TLS material lives.
CERT_DIR = os.path.join(os.path.expanduser("~"), ".human_loop_certs")
DEFAULT_CERTFILE = os.path.join(CERT_DIR, "server.crt")
DEFAULT_KEYFILE = os.path.join(CERT_DIR, "server.key")

_server_python_cache = None


def _cert_san_names(host):
    """Loopback names plus the bind host, de-duplicated, order-stable."""
    return list(dict.fromkeys([(host or "127.0.0.1").strip() or "127.0.0.1",
                               "localhost", "127.0.0.1", "::1"]))


def generate_trusted_cert(host, certfile=DEFAULT_CERTFILE, keyfile=DEFAULT_KEYFILE):
    """Create a cert/key PEM pair for `host` (+ loopback names).

    Prefers **mkcert**, which installs a locally-trusted CA into the OS/browser
    trust stores so Electron/Chromium clients (Claude Desktop) accept the cert
    without a security warning. Falls back to a bare **openssl** self-signed cert,
    which the client must be told to trust manually.

    Returns ``(certfile, keyfile, trusted)`` where ``trusted`` is True only for the
    mkcert path. Raises RuntimeError (with a readable message) if neither tool is
    available or generation fails.
    """
    os.makedirs(CERT_DIR, exist_ok=True)
    host = (host or "127.0.0.1").strip() or "127.0.0.1"
    names = _cert_san_names(host)

    mkcert = shutil.which("mkcert")
    if mkcert:
        # Install the local CA into the system + browser trust stores (idempotent;
        # may prompt for your password the first time).
        subprocess.run([mkcert, "-install"], capture_output=True, text=True, timeout=180)
        r = subprocess.run([mkcert, "-cert-file", certfile, "-key-file", keyfile] + names,
                           capture_output=True, text=True, timeout=180)
        if r.returncode == 0:
            try:
                os.chmod(keyfile, 0o600)
            except OSError:
                pass
            return certfile, keyfile, True
        # mkcert present but failed — fall through to self-signed.

    openssl = shutil.which("openssl")
    if not openssl:
        raise RuntimeError(
            "Neither mkcert nor openssl was found on PATH.\n\n"
            "Install mkcert for a trusted certificate (recommended):\n"
            "    brew install mkcert\n\n"
            "…or install openssl, or point the certificate/key fields at an existing "
            "PEM pair.")
    san_str = ",".join(
        (("IP:" + s) if s.replace(".", "").replace(":", "").isdigit() or ":" in s else ("DNS:" + s))
        for s in names)
    cmd = [
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", keyfile, "-out", certfile,
        "-days", "825", "-subj", f"/CN={host}",
        "-addext", f"subjectAltName={san_str}",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "openssl failed").strip())
    try:
        os.chmod(keyfile, 0o600)
    except OSError:
        pass
    return certfile, keyfile, False


def resolve_server_python():
    """Find a Python interpreter that has the server's deps (fastmcp).

    The console itself is stdlib-only and may be launched by a system Python that
    lacks fastmcp/pydantic; the server it spawns needs them. Prefer the current
    interpreter, then a sibling .venv/venv. Returns None if none qualifies.
    """
    global _server_python_cache
    if _server_python_cache:
        return _server_python_cache
    candidates = [sys.executable]
    for venv in (".venv", "venv"):
        candidates.append(os.path.join(_HERE, venv, "bin", "python"))
        candidates.append(os.path.join(_HERE, venv, "Scripts", "python.exe"))
    for py in candidates:
        if py and os.path.exists(py):
            try:
                r = subprocess.run([py, "-c", "import fastmcp"],
                                   capture_output=True, timeout=20)
                if r.returncode == 0:
                    _server_python_cache = py
                    return py
            except Exception:
                continue
    return None


def _read_tail(path, n=2500):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()[-n:]
    except OSError:
        return ""

SILVER = "#C0C0C0"
WHITE = "#FFFFFF"
BLACK = "#000000"
NAVY = "#000080"
GREEN = "#008000"
GRAY = "#808080"
CLASSIC_FONT = ("MS Sans Serif", 9)      # substituted by Tk if unavailable
CLASSIC_FONT_BOLD = ("MS Sans Serif", 9, "bold")
FIXED_FONT = ("Courier New", 10)


def get_logs_dir() -> str:
    """Match human_loop_server.get_logs_dir(): new env/dir, with a legacy fallback
    so a prior outbox archive stays visible."""
    env = os.environ.get(LOGS_ENV_VAR) or os.environ.get(OUTBOX_ENV_VAR)
    if env:
        return env
    if not os.path.isdir(DEFAULT_LOGS_DIR) and os.path.isdir(DEFAULT_OUTBOX_DIR):
        return DEFAULT_OUTBOX_DIR
    return DEFAULT_LOGS_DIR


def open_with_os_default(path: str) -> None:
    """Open a file with the OS default application."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        messagebox.showerror("Open", f"Could not open:\n{path}\n\n{e}")


def _fmt_timestamp(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso_str or "?"


# --- Classic-styled widget factories ------------------------------------- #

def classic_button(parent, text, command, width=14):
    return tk.Button(parent, text=text, command=command, width=width,
                     bg=SILVER, fg=BLACK, font=CLASSIC_FONT,
                     relief="raised", bd=2, activebackground=SILVER,
                     highlightthickness=0, padx=4, pady=2)


def classic_entry(parent, width=32):
    return tk.Entry(parent, width=width, bg=WHITE, fg=BLACK, font=CLASSIC_FONT,
                    relief="sunken", bd=2, highlightthickness=0,
                    disabledbackground=SILVER, disabledforeground=GRAY)


def classic_text(parent, height=4, width=40):
    return tk.Text(parent, height=height, width=width, bg=WHITE, fg=BLACK,
                   font=CLASSIC_FONT, relief="sunken", bd=2, wrap="word",
                   highlightthickness=0)


def classic_label(parent, text, bold=False, fg=BLACK):
    return tk.Label(parent, text=text, bg=SILVER, fg=fg,
                    font=CLASSIC_FONT_BOLD if bold else CLASSIC_FONT,
                    anchor="w", justify="left")


# ========================================================================= #
# Logs tab: browse every human-in-the-loop interaction (inputs, choices,
# confirmations, info messages, and delegated tasks).
# ========================================================================= #
class LogsTab:
    def __init__(self, parent):
        self.frame = tk.Frame(parent, bg=SILVER)
        self.entries = []
        self._build_body()
        self._build_buttons()
        self.refresh()

    def _build_body(self):
        top = tk.Frame(self.frame, bg=SILVER, bd=1, relief="sunken")
        top.pack(side="top", fill="x", padx=6, pady=(6, 0))
        self.dir_var = tk.StringVar(value=f"Logs: {get_logs_dir()}")
        tk.Label(top, textvariable=self.dir_var, bg=SILVER, fg=BLACK,
                 font=CLASSIC_FONT, anchor="w").pack(side="left", padx=4, pady=2)

        # Entries (left) and Details (right) live in a horizontal PanedWindow so
        # the divider can be dragged to widen/narrow the list. The right pane's
        # minsize plus a sash clamp keep the list within a sensible width band.
        body = tk.PanedWindow(self.frame, orient="horizontal", bg=SILVER,
                              sashrelief="raised", sashwidth=7, sashpad=0, bd=0,
                              opaqueresize=True)
        body.pack(side="top", fill="both", expand=True, padx=6, pady=6)

        left = tk.Frame(body, bg=SILVER)
        classic_label(left, "Entries (newest first):", bold=True).pack(side="top", fill="x")
        list_wrap = tk.Frame(left, bg=SILVER, bd=2, relief="sunken")
        list_wrap.pack(side="top", fill="both", expand=True)
        self.listbox = tk.Listbox(list_wrap, width=28, bg=WHITE, fg=BLACK,
                                  font=CLASSIC_FONT, bd=0, relief="flat",
                                  highlightthickness=0, activestyle="none",
                                  selectbackground=NAVY, selectforeground=WHITE,
                                  exportselection=False)
        self.listbox.pack(side="left", fill="both", expand=True)
        lb_scroll = tk.Scrollbar(list_wrap, orient="vertical", command=self.listbox.yview)
        lb_scroll.pack(side="right", fill="y")
        self.listbox.configure(yscrollcommand=lb_scroll.set)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        right = tk.Frame(body, bg=SILVER)
        classic_label(right, "Details:", bold=True).pack(side="top", fill="x")
        det_wrap = tk.Frame(right, bg=SILVER, bd=2, relief="sunken")
        det_wrap.pack(side="top", fill="both", expand=True)
        self.details = tk.Text(det_wrap, wrap="word", bg=WHITE, fg=BLACK,
                               font=FIXED_FONT, bd=0, relief="flat",
                               highlightthickness=0, state="disabled")
        self.details.pack(side="left", fill="both", expand=True)
        det_scroll = tk.Scrollbar(det_wrap, orient="vertical", command=self.details.yview)
        det_scroll.pack(side="right", fill="y")
        self.details.configure(yscrollcommand=det_scroll.set)

        classic_label(right, "Attachments (double-click to open):", bold=True).pack(
            side="top", fill="x", pady=(6, 0))
        att_wrap = tk.Frame(right, bg=SILVER, bd=2, relief="sunken")
        att_wrap.pack(side="top", fill="x")
        self.att_listbox = tk.Listbox(att_wrap, height=5, bg=WHITE, fg=BLACK,
                                      font=CLASSIC_FONT, bd=0, relief="flat",
                                      highlightthickness=0, activestyle="none",
                                      selectbackground=NAVY, selectforeground=WHITE,
                                      exportselection=False)
        self.att_listbox.pack(side="left", fill="both", expand=True)
        att_scroll = tk.Scrollbar(att_wrap, orient="vertical", command=self.att_listbox.yview)
        att_scroll.pack(side="right", fill="y")
        self.att_listbox.configure(yscrollcommand=att_scroll.set)
        self.att_listbox.bind("<Double-Button-1>", lambda e: self.open_attachment())

        # Assemble the panes. Both have a minsize so the list can't get too
        # narrow and (because the details pane reserves its own minsize) can't
        # swallow the whole width; a sash clamp gives a firm upper bound too.
        self._paned = body
        body.add(left, minsize=self.LIST_MIN_WIDTH, stretch="never",
                 width=self.LIST_DEFAULT_WIDTH)
        body.add(right, minsize=self.DETAILS_MIN_WIDTH, stretch="always")
        body.bind("<B1-Motion>", self._clamp_sash)
        body.bind("<ButtonRelease-1>", self._clamp_sash)

    # Width band for the entries pane (px).
    LIST_MIN_WIDTH = 220
    LIST_MAX_WIDTH = 520
    LIST_DEFAULT_WIDTH = 320
    DETAILS_MIN_WIDTH = 340

    def _clamp_sash(self, event=None):
        """Keep the draggable divider within [LIST_MIN_WIDTH, LIST_MAX_WIDTH]."""
        try:
            x = self._paned.sash_coord(0)[0]
        except Exception:
            return
        nx = max(self.LIST_MIN_WIDTH, min(self.LIST_MAX_WIDTH, x))
        if nx != x:
            try:
                self._paned.sash_place(0, nx, 1)
            except Exception:
                pass

    def _build_buttons(self):
        bar = tk.Frame(self.frame, bg=SILVER, bd=1, relief="raised")
        bar.pack(side="bottom", fill="x")
        inner = tk.Frame(bar, bg=SILVER)
        inner.pack(side="right", padx=6, pady=6)
        classic_button(inner, "Refresh", self.refresh).pack(side="left", padx=(0, 4))
        classic_button(inner, "Open Attachment", self.open_attachment, width=16).pack(side="left", padx=(0, 4))
        classic_button(inner, "Delete", self.delete_selected).pack(side="left")

    def refresh(self):
        self.entries = []
        logs = get_logs_dir()
        self.dir_var.set(f"Logs: {logs}")
        if os.path.isdir(logs):
            for name in os.listdir(logs):
                if name.startswith("."):
                    continue
                entry_dir = os.path.join(logs, name)
                meta_path = os.path.join(entry_dir, "entry.json")
                if not os.path.isfile(meta_path):
                    continue
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        record = json.load(f)
                except Exception:
                    record = {"task_title": name, "status": "?", "created_at": "",
                              "body": "(could not read entry.json)", "attachments": []}
                self.entries.append({"dir": entry_dir, "record": record})

        self.entries.sort(key=lambda e: e["record"].get("created_at", "") or os.path.basename(e["dir"]),
                          reverse=True)

        self.listbox.delete(0, tk.END)
        for e in self.entries:
            r = e["record"]
            tool = r.get("tool", "assign_task_to_human")
            title = (r.get("task_title", "") or "(untitled)").replace("\n", " ")
            # Title first so entries are distinguishable, then status, tool, time.
            label = (f'{title}    [{r.get("status", "?")}]    {tool}    '
                     f'{_fmt_timestamp(r.get("created_at", ""))}')
            self.listbox.insert(tk.END, label)

        self._set_details("")
        self.att_listbox.delete(0, tk.END)
        if self.entries:
            self.listbox.selection_set(0)
            self._on_select()

    def _current(self):
        sel = self.listbox.curselection()
        if not sel:
            return None
        return self.entries[sel[0]]

    def _on_select(self, event=None):
        entry = self._current()
        if not entry:
            return
        r = entry["record"]
        is_task = r.get("kind", "task") == "task"
        req_label = ("Task description (from the assistant):" if is_task
                     else "Prompt / question shown to you:")
        resp_label = "Human's report:" if is_task else "Your response:"
        lines = [
            f'Tool:         {r.get("tool", "assign_task_to_human")}',
            f'Title:        {r.get("task_title", "")}',
            f'Status:       {r.get("status", "")}',
            f'When:         {_fmt_timestamp(r.get("created_at", ""))}',
            f'ID:           {r.get("task_id", "")}',
            "",
            req_label,
            (r.get("task_description", "") or "(none)"),
        ]
        if r.get("context_note"):
            lines += ["", "Context note:", r["context_note"]]
        lines += [
            "",
            "-" * 60,
            resp_label,
            (r.get("body", "") or "(no text)"),
        ]
        self._set_details("\n".join(lines))

        self.att_listbox.delete(0, tk.END)
        for a in r.get("attachments", []):
            name = a.get("stored_name") or a.get("original_name") or "(unknown)"
            size = a.get("size_bytes")
            suffix = f'  ({size} bytes)' if isinstance(size, int) else (f'  [error: {a["error"]}]' if a.get("error") else "")
            self.att_listbox.insert(tk.END, f"{name}{suffix}")

    def _set_details(self, text):
        self.details.configure(state="normal")
        self.details.delete("1.0", tk.END)
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def open_attachment(self):
        entry = self._current()
        if not entry:
            return
        sel = self.att_listbox.curselection()
        if not sel:
            messagebox.showinfo("Open Attachment", "Select an attachment first.")
            return
        attachments = entry["record"].get("attachments", [])
        if sel[0] >= len(attachments):
            return
        stored = attachments[sel[0]].get("stored_name")
        if not stored:
            messagebox.showwarning("Open Attachment", "This attachment was not stored (see error in list).")
            return
        path = os.path.join(entry["dir"], "attachments", stored)
        if not os.path.isfile(path):
            messagebox.showerror("Open Attachment", f"File missing:\n{path}")
            return
        open_with_os_default(path)

    def delete_selected(self):
        entry = self._current()
        if not entry:
            messagebox.showinfo("Delete", "Select an entry first.")
            return
        title = entry["record"].get("task_title", "")
        if not messagebox.askyesno("Delete Entry",
                                   f"Permanently delete this log entry?\n\n{title}"):
            return
        try:
            shutil.rmtree(entry["dir"])
        except Exception as e:
            messagebox.showerror("Delete", f"Could not delete:\n{e}")
            return
        self.refresh()


# ========================================================================= #
# A base for the simple settings tabs (per-tab Save + transient status)
# ========================================================================= #
class SettingsTab:
    section = None  # config key this tab owns

    def __init__(self, parent):
        self.frame = tk.Frame(parent, bg=SILVER)
        self.body = tk.Frame(self.frame, bg=SILVER)
        self.body.pack(side="top", fill="both", expand=True, padx=16, pady=14)
        self._build()
        self.load()
        bar = tk.Frame(self.frame, bg=SILVER, bd=1, relief="raised")
        bar.pack(side="bottom", fill="x")
        self.status = classic_label(bar, "")
        self.status.pack(side="left", padx=8, pady=6)
        classic_button(bar, "Save", self.save).pack(side="right", padx=8, pady=6)

    def _build(self):
        raise NotImplementedError

    def load(self):
        raise NotImplementedError

    def collect(self):
        """Return the dict for this tab's config section. Raise ValueError to abort."""
        raise NotImplementedError

    def save(self):
        try:
            section_data = self.collect()
        except ValueError as e:
            messagebox.showwarning("Invalid setting", str(e))
            return
        cfg = human_loop_config.load_config()
        cfg[self.section] = {**cfg.get(self.section, {}), **section_data}
        try:
            human_loop_config.save_config(cfg)
        except Exception as e:
            messagebox.showerror("Save", f"Could not save config:\n{e}")
            return
        self._flash("Saved.")

    def _flash(self, msg):
        self.status.config(text=msg)
        self.frame.after(2500, lambda: self.status.config(text=""))


# ---- User Profile ------------------------------------------------------- #
class ProfileTab(SettingsTab):
    section = "profile"

    def _build(self):
        b = self.body
        b.columnconfigure(1, weight=1)
        classic_label(b, "Name:").grid(row=0, column=0, sticky="w", pady=4)
        self.name = classic_entry(b)
        self.name.grid(row=0, column=1, sticky="ew", pady=4)
        classic_label(b, "Role:").grid(row=1, column=0, sticky="w", pady=4)
        self.role = classic_entry(b)
        self.role.grid(row=1, column=1, sticky="ew", pady=4)
        classic_label(b, "Responsibilities:").grid(row=2, column=0, sticky="nw", pady=4)
        self.resp = classic_text(b, height=3)
        self.resp.grid(row=2, column=1, sticky="ew", pady=4)
        classic_label(b, "How to communicate\nwith me:").grid(row=3, column=0, sticky="nw", pady=4)
        self.comm = classic_text(b, height=4)
        self.comm.grid(row=3, column=1, sticky="ew", pady=4)
        classic_label(b, "Tip: leave Role/Responsibilities empty = you can be assigned any task.",
                      fg=GRAY).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def load(self):
        p = human_loop_config.load_config().get("profile", {})
        self.name.delete(0, tk.END); self.name.insert(0, p.get("name", ""))
        self.role.delete(0, tk.END); self.role.insert(0, p.get("role", ""))
        self.resp.delete("1.0", tk.END); self.resp.insert("1.0", p.get("responsibilities", ""))
        self.comm.delete("1.0", tk.END); self.comm.insert("1.0", p.get("communication", ""))

    def collect(self):
        return {
            "name": self.name.get().strip(),
            "role": self.role.get().strip(),
            "responsibilities": self.resp.get("1.0", tk.END).strip(),
            "communication": self.comm.get("1.0", tk.END).strip(),
        }


# ---- Task Options ------------------------------------------------------- #
class TaskOptionsTab(SettingsTab):
    section = "task_defaults"

    def _build(self):
        b = self.body
        b.columnconfigure(1, weight=1)
        classic_label(b, "Default task timeout\n(seconds):").grid(row=0, column=0, sticky="nw", pady=4)
        self.timeout = classic_entry(b, width=12)
        self.timeout.grid(row=0, column=1, sticky="w", pady=4)
        classic_label(b, "Used only when the assistant doesn't specify its own timeout.",
                      fg=GRAY).grid(row=1, column=0, columnspan=2, sticky="w")

        classic_label(b, "Default max result\nsize (bytes):").grid(row=2, column=0, sticky="nw", pady=(12, 4))
        self.maxbytes = classic_entry(b, width=16)
        self.maxbytes.grid(row=2, column=1, sticky="w", pady=(12, 4))
        self.mb_hint = classic_label(b, "", fg=GRAY)
        self.mb_hint.grid(row=3, column=0, columnspan=2, sticky="w")
        self.maxbytes.bind("<KeyRelease>", lambda e: self._update_mb_hint())

        self.attach_var = tk.IntVar(value=1)
        tk.Checkbutton(b, text="Enable file/image attachments", variable=self.attach_var,
                       bg=SILVER, fg=BLACK, font=CLASSIC_FONT, activebackground=SILVER,
                       selectcolor=WHITE, anchor="w").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(16, 0))

    def _update_mb_hint(self):
        try:
            n = int(self.maxbytes.get().strip())
            self.mb_hint.config(text=f"≈ {n / 1_000_000:.2f} MB   (Claude Desktop rejects results over ~1 MB)")
        except ValueError:
            self.mb_hint.config(text="(enter a whole number of bytes)")

    def load(self):
        t = human_loop_config.load_config().get("task_defaults", {})
        self.timeout.delete(0, tk.END); self.timeout.insert(0, str(t.get("timeout_seconds", 240)))
        self.maxbytes.delete(0, tk.END); self.maxbytes.insert(0, str(t.get("max_result_bytes", 1_000_000)))
        self.attach_var.set(1 if t.get("attachments_enabled", True) else 0)
        self._update_mb_hint()

    def collect(self):
        try:
            timeout = int(self.timeout.get().strip())
            maxbytes = int(self.maxbytes.get().strip())
        except ValueError:
            raise ValueError("Timeout and max result size must be whole numbers.")
        if timeout < 2:
            raise ValueError("Timeout must be at least 2 seconds.")
        if maxbytes < 1000:
            raise ValueError("Max result size looks too small (need at least ~1000 bytes).")
        return {
            "timeout_seconds": timeout,
            "max_result_bytes": maxbytes,
            "attachments_enabled": bool(self.attach_var.get()),
        }


# ---- Notification ------------------------------------------------------- #
class NotificationTab(SettingsTab):
    section = "notification"

    def _build(self):
        b = self.body
        b.columnconfigure(1, weight=1)
        classic_label(b, "Ringtone (.wav):").grid(row=0, column=0, sticky="w", pady=4)
        self.ring = classic_entry(b)
        self.ring.grid(row=0, column=1, sticky="ew", pady=4)
        btns = tk.Frame(b, bg=SILVER)
        btns.grid(row=1, column=1, sticky="w", pady=(0, 6))
        classic_button(btns, "Browse…", self._browse, width=10).pack(side="left")
        classic_button(btns, "Use default", self._use_default, width=12).pack(side="left", padx=(6, 0))
        classic_button(btns, "Test", self._test, width=8).pack(side="left", padx=(6, 0))
        classic_button(btns, "Stop", self._stop_test, width=8).pack(side="left", padx=(6, 0))
        classic_label(b, "Empty = the bundled notify.wav.", fg=GRAY).grid(
            row=2, column=1, sticky="w")

        self.mute_var = tk.IntVar(value=0)
        tk.Checkbutton(b, text="Mute (no sound on incoming tasks)", variable=self.mute_var,
                       bg=SILVER, fg=BLACK, font=CLASSIC_FONT, activebackground=SILVER,
                       selectcolor=WHITE, anchor="w").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(16, 0))
        self._test_proc = None

    def _browse(self):
        path = filedialog.askopenfilename(title="Choose a ringtone (.wav)",
                                          filetypes=[("WAV audio", "*.wav"), ("All files", "*.*")])
        if path:
            self.ring.delete(0, tk.END); self.ring.insert(0, path)

    def _use_default(self):
        self.ring.delete(0, tk.END)

    def _test(self):
        self._stop_test()
        path = self.ring.get().strip() or BUNDLED_RINGTONE
        if not os.path.isfile(path):
            messagebox.showwarning("Test", f"File not found:\n{path}")
            return
        try:
            if sys.platform.startswith("win"):
                import winsound
                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            elif sys.platform == "darwin":
                self._test_proc = subprocess.Popen(["afplay", path])
            else:
                player = shutil.which("paplay") or shutil.which("aplay")
                if player:
                    self._test_proc = subprocess.Popen([player, path])
        except Exception as e:
            messagebox.showwarning("Test", f"Could not play sound:\n{e}")

    def _stop_test(self):
        if sys.platform.startswith("win"):
            try:
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
        if self._test_proc is not None:
            try:
                self._test_proc.terminate()
            except Exception:
                pass
            self._test_proc = None

    def load(self):
        n = human_loop_config.load_config().get("notification", {})
        self.ring.delete(0, tk.END); self.ring.insert(0, n.get("ringtone_path", ""))
        self.mute_var.set(1 if n.get("muted", False) else 0)

    def collect(self):
        return {
            "ringtone_path": self.ring.get().strip(),
            "muted": bool(self.mute_var.get()),
        }


# --- PID-file helpers ---------------------------------------------------- #
def _write_pid_file(pid):
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(pid))
    except OSError:
        pass


def _read_pid_file():
    try:
        with open(PID_FILE) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _remove_pid_file():
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---- Server (Online / Offline) ----------------------------------------- #
class ServerTab:
    section = "server"

    def __init__(self, parent, app):
        self.app = app
        self.frame = tk.Frame(parent, bg=SILVER)
        self.proc = None       # Popen we started this session
        self.pid = None        # adopted PID (from a prior session), if any
        self._logf = None      # server's stdout+stderr log file handle
        body = tk.Frame(self.frame, bg=SILVER)
        body.pack(side="top", fill="both", expand=True, padx=16, pady=14)
        body.columnconfigure(1, weight=1)

        classic_label(body, "HTTP port:").grid(row=0, column=0, sticky="w", pady=4)
        self.port = classic_entry(body, width=10)
        self.port.grid(row=0, column=1, sticky="w", pady=4)
        classic_label(body, "Bind host:").grid(row=1, column=0, sticky="w", pady=4)
        self.host = classic_entry(body, width=18)
        self.host.grid(row=1, column=1, sticky="w", pady=4)

        # --- HTTPS / TLS ---
        self.https_var = tk.BooleanVar(value=False)
        self.https_chk = tk.Checkbutton(
            body, text="Enable HTTPS  (required by Claude Desktop and other URL connectors)",
            variable=self.https_var, command=self._toggle_https_fields,
            bg=SILVER, activebackground=SILVER, font=CLASSIC_FONT, anchor="w")
        self.https_chk.grid(row=2, column=0, columnspan=2, sticky="w", pady=(12, 2))

        self.cert_lbl = classic_label(body, "Certificate (.pem):")
        self.cert_lbl.grid(row=3, column=0, sticky="w", pady=4)
        cert_row = tk.Frame(body, bg=SILVER)
        cert_row.grid(row=3, column=1, sticky="ew", pady=4)
        self.certfile = classic_entry(cert_row, width=30)
        self.certfile.pack(side="left", fill="x", expand=True)
        self.cert_browse = classic_button(cert_row, "Browse…", self._browse_cert, width=9)
        self.cert_browse.pack(side="left", padx=(6, 0))

        self.key_lbl = classic_label(body, "Private key (.pem):")
        self.key_lbl.grid(row=4, column=0, sticky="w", pady=4)
        key_row = tk.Frame(body, bg=SILVER)
        key_row.grid(row=4, column=1, sticky="ew", pady=4)
        self.keyfile = classic_entry(key_row, width=30)
        self.keyfile.pack(side="left", fill="x", expand=True)
        self.key_browse = classic_button(key_row, "Browse…", self._browse_key, width=9)
        self.key_browse.pack(side="left", padx=(6, 0))

        self.gen_btn = classic_button(body, "Generate certificate…", self._generate_cert, width=22)
        self.gen_btn.grid(row=5, column=1, sticky="w", pady=(0, 2))
        self.https_hint = classic_label(
            body, "Claude Desktop only trusts CA-signed certs. Install mkcert "
                  "(brew install mkcert) and click Generate for a\ncertificate it accepts; "
                  "otherwise Generate makes a self-signed one you must trust manually.", fg=GRAY)
        self.https_hint.grid(row=6, column=0, columnspan=2, sticky="w")

        self.status_var = tk.StringVar(value="● Offline")
        self.status_lbl = tk.Label(body, textvariable=self.status_var, bg=SILVER, fg=GRAY,
                                   font=CLASSIC_FONT_BOLD, anchor="w")
        self.status_lbl.grid(row=7, column=0, columnspan=2, sticky="w", pady=(16, 4))
        self.endpoint_var = tk.StringVar(value="")
        self.endpoint_lbl = tk.Label(body, textvariable=self.endpoint_var, bg=SILVER, fg=BLACK,
                                     font=CLASSIC_FONT, anchor="w")
        self.endpoint_lbl.grid(row=8, column=0, columnspan=2, sticky="w")
        classic_label(body, "Going Online launches the MCP server. Point a URL-based MCP client at\n"
                            "the endpoint above. Dialogs pop on THIS machine's desktop.\n"
                            "Closing this console takes the server Offline.", fg=GRAY).grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(12, 0))

        bar = tk.Frame(self.frame, bg=SILVER, bd=1, relief="raised")
        bar.pack(side="bottom", fill="x")
        self.toggle_btn = classic_button(bar, "Go Online", self.toggle, width=14)
        self.toggle_btn.pack(side="right", padx=8, pady=6)

        self._load()
        self._adopt_existing()
        self._poll()

    def _load(self):
        s = human_loop_config.load_config().get("server", {})
        self.port.delete(0, tk.END); self.port.insert(0, str(s.get("http_port", 8000)))
        self.host.delete(0, tk.END); self.host.insert(0, str(s.get("http_host", "0.0.0.0")))
        self.https_var.set(bool(s.get("https_enabled", False)))
        self.certfile.delete(0, tk.END); self.certfile.insert(0, str(s.get("https_certfile", "")))
        self.keyfile.delete(0, tk.END); self.keyfile.insert(0, str(s.get("https_keyfile", "")))
        self._toggle_https_fields()

    def _toggle_https_fields(self):
        """Enable the TLS inputs only when HTTPS is checked (and we're offline)."""
        on = self.https_var.get() and not self.is_online()
        state = "normal" if on else "disabled"
        for w in (self.certfile, self.keyfile, self.cert_browse, self.key_browse, self.gen_btn):
            try:
                w.config(state=state)
            except Exception:
                pass

    def _browse_cert(self):
        p = filedialog.askopenfilename(
            title="Select TLS certificate (PEM)",
            filetypes=[("PEM certificate", "*.pem *.crt *.cert"), ("All files", "*.*")])
        if p:
            self.certfile.delete(0, tk.END); self.certfile.insert(0, p)

    def _browse_key(self):
        p = filedialog.askopenfilename(
            title="Select TLS private key (PEM)",
            filetypes=[("PEM key", "*.pem *.key"), ("All files", "*.*")])
        if p:
            self.keyfile.delete(0, tk.END); self.keyfile.insert(0, p)

    def _generate_cert(self):
        host = self.host.get().strip() or "127.0.0.1"
        try:
            cert, key, trusted = generate_trusted_cert(host)
        except Exception as e:
            messagebox.showerror("Generate certificate", str(e))
            return
        self.certfile.delete(0, tk.END); self.certfile.insert(0, cert)
        self.keyfile.delete(0, tk.END); self.keyfile.insert(0, key)
        if trusted:
            messagebox.showinfo(
                "Generate certificate",
                f"Created a locally-trusted certificate (via mkcert) for '{host}':\n\n"
                f"{cert}\n{key}\n\n"
                "mkcert also installed its local CA into your system trust store, so "
                "Claude Desktop and browsers will accept it.\n\n"
                "If the client is already running, restart it once so it reloads the "
                "trust store.")
        else:
            messagebox.showwarning(
                "Generate certificate",
                f"Created a SELF-SIGNED certificate for '{host}':\n\n"
                f"{cert}\n{key}\n\n"
                "WARNING: self-signed certs are NOT trusted by Claude Desktop "
                "(you'll see 'connection is not private' / ERR_CERT_AUTHORITY_INVALID).\n\n"
                "For a certificate that just works, install mkcert and generate again:\n"
                "    brew install mkcert\n\n"
                "Otherwise you must manually trust this certificate in your OS keychain.")

    def is_online(self):
        if self.proc is not None and self.proc.poll() is None:
            return True
        if self.pid is not None and _pid_alive(self.pid):
            return True
        return False

    def _adopt_existing(self):
        """If a prior console left a server running, adopt it so we can stop it."""
        pid = _read_pid_file()
        if pid and _pid_alive(pid):
            self.pid = pid
        else:
            _remove_pid_file()
        self._render()

    def _endpoint(self):
        scheme = "https" if self.https_var.get() else "http"
        return f"{scheme}://{self.host.get().strip() or '127.0.0.1'}:{self.port.get().strip()}/mcp"

    def toggle(self):
        if self.is_online():
            self.stop()
        else:
            self.start()

    def start(self):
        port = self.port.get().strip()
        host = self.host.get().strip() or "127.0.0.1"
        if not port.isdigit():
            messagebox.showwarning("Server", "Port must be a number.")
            return
        py = resolve_server_python()
        if py is None:
            messagebox.showerror(
                "Server",
                "Couldn't find a Python with the server's dependencies (fastmcp).\n\n"
                "Install them and launch this console from the same environment, e.g.:\n"
                "    uv pip install -e .\n"
                "    uv run python management_console.py\n"
                "(or create a .venv next to these files with fastmcp installed).")
            return
        https_on = self.https_var.get()
        cert = self.certfile.get().strip()
        key = self.keyfile.get().strip()
        if https_on:
            # Offer to generate a self-signed pair if none is configured yet.
            if not cert and not key:
                if messagebox.askyesno(
                        "HTTPS",
                        "HTTPS is enabled but no certificate is set.\n\n"
                        "Generate one now? (mkcert is used for a trusted certificate "
                        "if installed; otherwise a self-signed one you must trust.)"):
                    try:
                        cert, key, _trusted = generate_trusted_cert(host)
                        self.certfile.delete(0, tk.END); self.certfile.insert(0, cert)
                        self.keyfile.delete(0, tk.END); self.keyfile.insert(0, key)
                    except Exception as e:
                        messagebox.showerror("Server", f"Could not generate a certificate:\n{e}")
                        return
                else:
                    return
            missing = [name for name, p in (("certificate", cert), ("private key", key))
                       if not p or not os.path.isfile(p)]
            if missing:
                messagebox.showwarning(
                    "Server", f"The TLS {' and '.join(missing)} file is missing.\n\n"
                              "Pick an existing PEM file or generate a self-signed pair.")
                return
        cfg = human_loop_config.load_config()
        cfg["server"] = {
            "http_port": int(port), "http_host": host,
            "https_enabled": https_on, "https_certfile": cert, "https_keyfile": key,
        }
        try:
            human_loop_config.save_config(cfg)
        except Exception:
            pass
        env = dict(os.environ, HUMAN_LOOP_HTTP_PORT=port, HUMAN_LOOP_HTTP_HOST=host)
        if https_on:
            env.update(HUMAN_LOOP_HTTPS="1", HUMAN_LOOP_HTTPS_CERT=cert, HUMAN_LOOP_HTTPS_KEY=key)
        try:
            self._logf = open(SERVER_LOG, "w")
            self.proc = subprocess.Popen([py, SERVER_SCRIPT], env=env,
                                         stdout=self._logf, stderr=subprocess.STDOUT)
        except Exception as e:
            self._close_log()
            messagebox.showerror("Server", f"Could not start server:\n{e}")
            self.proc = None
            return
        self.pid = None
        _write_pid_file(self.proc.pid)
        self._render()

    def _close_log(self):
        if self._logf is not None:
            try:
                self._logf.close()
            except Exception:
                pass
            self._logf = None

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:
                pass
        elif self.pid is not None:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except Exception:
                pass
        self._close_log()
        self.proc = None
        self.pid = None
        _remove_pid_file()
        self._render()

    def _handle_crash(self):
        """The server we launched exited on its own — surface why."""
        self._close_log()
        log = _read_tail(SERVER_LOG)
        self.proc = None
        _remove_pid_file()
        self._render()
        messagebox.showerror(
            "Server stopped",
            "The MCP server process exited unexpectedly.\n\n" + (log or "(no output captured)"))

    def _poll(self):
        # Detect a server that died on its own; keep the button label correct.
        if self.proc is not None and self.proc.poll() is not None:
            self._handle_crash()
        elif self.proc is None and self.pid is not None and not _pid_alive(self.pid):
            self.pid = None
            _remove_pid_file()
            self._render()
        self.frame.after(1000, self._poll)

    def _render(self):
        if self.is_online():
            self.status_var.set("● Online")
            self.status_lbl.config(fg=GREEN)
            self.endpoint_var.set(self._endpoint())
            self.toggle_btn.config(text="Go Offline")
            self.port.config(state="disabled")
            self.host.config(state="disabled")
            self.https_chk.config(state="disabled")
        else:
            self.status_var.set("● Offline")
            self.status_lbl.config(fg=GRAY)
            self.endpoint_var.set("")
            self.toggle_btn.config(text="Go Online")
            self.port.config(state="normal")
            self.host.config(state="normal")
            self.https_chk.config(state="normal")
        self._toggle_https_fields()

    def shutdown(self):
        """Called when the console is closing: stop a server we own."""
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
            _remove_pid_file()
        self._close_log()


# ========================================================================= #
# The application: menubar + notebook of tabs
# ========================================================================= #
class ManagementConsole:
    def __init__(self, root):
        self.root = root
        root.title("HITL Management Console")
        root.configure(bg=SILVER)
        root.geometry("1000x640")
        root.minsize(860, 560)

        self._style_classic()
        self._build_menu()

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(side="top", fill="both", expand=True, padx=6, pady=6)

        self.logs_tab = LogsTab(self.notebook)
        self.profile_tab = ProfileTab(self.notebook)
        self.server_tab = ServerTab(self.notebook, self)
        self.task_tab = TaskOptionsTab(self.notebook)
        self.notif_tab = NotificationTab(self.notebook)

        self.notebook.add(self.logs_tab.frame, text="  Logs  ")
        self.notebook.add(self.profile_tab.frame, text="  User Profile  ")
        self.notebook.add(self.server_tab.frame, text="  Server  ")
        self.notebook.add(self.task_tab.frame, text="  Task Options  ")
        self.notebook.add(self.notif_tab.frame, text="  Notification  ")

        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _style_classic(self):
        style = ttk.Style()
        try:
            style.theme_use("classic")
        except tk.TclError:
            pass
        style.configure("TNotebook", background=SILVER, borderwidth=1)
        style.configure("TNotebook.Tab", background=SILVER, foreground=BLACK,
                        font=CLASSIC_FONT, padding=[10, 4])
        style.map("TNotebook.Tab",
                  background=[("selected", WHITE)],
                  foreground=[("selected", BLACK)])

    def _build_menu(self):
        menubar = tk.Menu(self.root, bg=SILVER, fg=BLACK, font=CLASSIC_FONT,
                          activebackground=NAVY, activeforeground=WHITE, tearoff=0)
        file_menu = tk.Menu(menubar, tearoff=0, bg=SILVER, fg=BLACK, font=CLASSIC_FONT,
                            activebackground=NAVY, activeforeground=WHITE)
        file_menu.add_command(label="Save All Settings", command=self._save_all)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)
        help_menu = tk.Menu(menubar, tearoff=0, bg=SILVER, fg=BLACK, font=CLASSIC_FONT,
                            activebackground=NAVY, activeforeground=WHITE)
        help_menu.add_command(label="About", command=self._about)
        menubar.add_cascade(label="Help", menu=help_menu)
        self.root.config(menu=menubar)

    def _save_all(self):
        for tab in (self.profile_tab, self.task_tab, self.notif_tab):
            tab.save()

    def _on_tab_changed(self, event=None):
        try:
            if self.notebook.tab(self.notebook.select(), "text").strip() == "Logs":
                self.logs_tab.refresh()
        except tk.TclError:
            pass

    def _about(self):
        messagebox.showinfo(
            "About",
            "HITL Management Console\n"
            "Human-in-the-Loop MCP Server\n\n"
            f"Config: {human_loop_config.get_config_path()}\n"
            f"Logs: {get_logs_dir()}")

    def _on_close(self):
        self.server_tab.shutdown()
        self.root.destroy()


def main():
    root = tk.Tk()
    ManagementConsole(root)
    root.mainloop()


if __name__ == "__main__":
    main()
