#!/usr/bin/env python3
"""
Human-in-the-Loop MCP Server

This server provides tools for getting human input and choices through GUI dialogs.
It enables LLMs to pause and ask for human feedback, input, or decisions.
Now supports both Windows and macOS platforms.
"""

import asyncio
import concurrent.futures
import json
import mimetypes
import platform
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
import tkinter as tk
from tkinter import messagebox, filedialog
from typing import List, Dict, Any, Optional, Literal
from datetime import datetime, timezone
import sys
import os
from pydantic import Field
from typing import Annotated
# Set required environment variable for FastMCP 2.8.1+
os.environ.setdefault('FASTMCP_LOG_LEVEL', 'INFO')
from fastmcp import FastMCP, Context
from fastmcp.utilities.types import Image, File
import human_loop_config

# Platform detection
CURRENT_PLATFORM = platform.system().lower()
IS_WINDOWS = CURRENT_PLATFORM == 'windows'
IS_MACOS = CURRENT_PLATFORM == 'darwin'
IS_LINUX = CURRENT_PLATFORM == 'linux'


def _out(*args):
    """Write diagnostics to STDERR. Under the default stdio transport, STDOUT is
    the JSON-RPC channel — anything printed there would corrupt the protocol."""
    sys.stderr.write(" ".join(str(a) for a in args) + "\n")
    sys.stderr.flush()


def _format_operator_profile():
    """Human-readable block describing who the operator is, from the config file.

    This must reach the model through channels it ACTUALLY reads — MCP prompts are
    user-invoked and most clients never load them, so we surface the profile via
    the server `instructions` (the initialize response) and tool descriptions
    instead. Read at server startup; edit the profile then take the server
    Offline/Online in the Management Console to refresh it.
    """
    p = human_loop_config.get_profile()
    name = (p.get("name") or "").strip()
    role = (p.get("role") or "").strip()
    resp = (p.get("responsibilities") or "").strip()
    comm = (p.get("communication") or "").strip()
    lines = ["ABOUT THE HUMAN OPERATOR YOU ARE ASSISTING:"]
    lines.append(f"- Name: {name}" if name else "- Name: (not provided)")
    lines.append(f"- Role: {role}" if role else "- Role: (unspecified)")
    if resp:
        lines.append(f"- Responsibilities: {resp}")
    if not role and not resp:
        lines.append("- Scope: no role/responsibilities set — they can be assigned ANY task.")
    if comm:
        lines.append(f"- How to communicate with them: {comm}")
    return "\n".join(lines)


def _server_instructions():
    """The MCP `instructions` string (delivered in the initialize response)."""
    return (
        "This server lets you interact with a human operator through GUI dialogs "
        "(ask questions, get choices/confirmations, and delegate real-world tasks) "
        "and to know who that operator is.\n\n"
        + _format_operator_profile()
    )


# Initialize the MCP server. `instructions` rides along in the initialize
# response so clients that surface it give the model the operator's identity.
mcp = FastMCP("Human-in-the-Loop Server", instructions=_server_instructions())

# --------------------------------------------------------------------------- #
# Outbox: local, on-disk archive of everything the human sends to the AI
# --------------------------------------------------------------------------- #
OUTBOX_ENV_VAR = "HUMAN_LOOP_OUTBOX_DIR"
DEFAULT_OUTBOX_DIR = os.path.join(os.path.expanduser("~"), ".human_loop_outbox")
OUTBOX_SCHEMA_VERSION = 1

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# Tool-result size budgeting. Clients cap how big a tool result may be
# attachments are base64-inlined, so the encoded
# size is what counts. The AI passes its own limit as `max_result_bytes`.
DEFAULT_MAX_RESULT_BYTES = 1_000_000   # safe default
RESULT_OVERHEAD_BYTES = 4096           # rough JSON envelope / manifest allowance


def _b64_len(nbytes):
    """Length of base64-encoding `nbytes` raw bytes."""
    return ((nbytes + 2) // 3) * 4


def estimate_result_bytes(text, attachment_paths):
    """Estimate the encoded size of the tool result for the given submission —
    text (UTF-8) plus base64-inlined attachments plus a fixed overhead. Slightly
    conservative on purpose so the UI warns before the real client limit."""
    total = len((text or "").encode("utf-8")) + RESULT_OVERHEAD_BYTES
    for p in (attachment_paths or []):
        try:
            total += _b64_len(os.path.getsize(p)) + 256
        except OSError:
            pass
    return total


def _human_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# Non-blocking heuristics that flag a task_description likely misusing the tool
# (a composite/multi-step order, a list of questions, or a chat-message dump).
COMPOSITE_TASK_CHAR_LIMIT = 400


def composite_task_advisory(task_description):
    """Return a short advisory string if the description looks like it is NOT a
    single atomic action, else None. Purely advisory — never blocks."""
    text = task_description or ""
    reasons = []
    if len(re.findall(r"(?m)^\s*(?:\d+[.)]|[-*•])\s+\S", text)) >= 2:
        reasons.append("it reads as a numbered/bulleted list of several steps")
    if (text.count("?") + text.count("？")) >= 2:
        reasons.append("it contains multiple questions")
    if len(text) > COMPOSITE_TASK_CHAR_LIMIT:
        reasons.append("it is long for a single instruction")
    if not reasons:
        return None
    return (
        "Heads up for the asssistant: the task_description you have just provided may be misusing assign_task_to_human (" +
        "; ".join(reasons) + "). Please note that this tool is for ONE indivisible action you cannot "
        "do itself, whose report is a completion status. If you are asking a question, use get_user_input / "
        "get_multiline_input / get_user_choice instead. If this is several steps, assign "
        "them as separate sequential tasks, adjusting each from the previous report."
    )

# Heartbeat / continuation tuning (seconds).
HEARTBEAT_SAFETY_MARGIN = 30       # max seconds shaved off the client's timeout
DISCONNECT_GRACE_SECONDS = 20      # past a leg deadline before we warn "may be disconnected"
AUTO_CLOSE_AFTER_SECONDS = 180     # further silence before auto-saving the draft & closing

# Pre-task notification / ringtone.
RINGTONE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notify.wav")
FOLLOW_UP_NOTIFY_AFTER_SECONDS = 180  # re-ring on continuation only after this much idle

# The task window's connection/countdown line is debug info now that timeouts no
# longer affect the human's experience - hidden unless this is set.
SHOW_CONNECTION_STATUS = os.environ.get("HUMAN_LOOP_SHOW_STATUS", "").lower() in ("1", "true", "yes")


def compute_leg_seconds(client_timeout_seconds: int) -> int:
    """A single wait 'leg', always strictly shorter than the client's timeout.

    The heartbeat MUST come back before the client kills the call, otherwise the
    dialog is orphaned. So we shave a proportional margin (capped) and never
    return a value >= the client's timeout.
    """
    ct = max(2, int(client_timeout_seconds))
    margin = min(HEARTBEAT_SAFETY_MARGIN, max(1, ct // 4))
    return max(1, ct - margin)


def get_outbox_dir() -> str:
    """Resolve the outbox directory (env override, else default)."""
    return os.environ.get(OUTBOX_ENV_VAR) or DEFAULT_OUTBOX_DIR


def _sanitize_for_path(text: str, max_len: int = 40) -> str:
    """Make a string safe to embed in a directory name."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in (text or ""))
    return safe[:max_len].strip("_") or "untitled"


def _dedupe_name(existing: set, name: str) -> str:
    """Return a filename not already in `existing`, adding ' (n)' if needed."""
    if name not in existing:
        existing.add(name)
        return name
    base, ext = os.path.splitext(name)
    i = 1
    while f"{base} ({i}){ext}" in existing:
        i += 1
    result = f"{base} ({i}){ext}"
    existing.add(result)
    return result


def archive_to_outbox(entry: Dict[str, Any], attachment_paths: List[str]) -> Optional[str]:
    """Persist one submission (command + human reply + attachments) to the outbox.

    Writes to a temporary directory first, then atomically renames it into place
    so a viewer never observes a half-written entry. Returns the final entry
    directory path, or None on failure (failures are swallowed - archiving must
    never crash the tool).
    """
    try:
        outbox = get_outbox_dir()
        os.makedirs(outbox, exist_ok=True)

        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y%m%dT%H%M%S_%f")
        entry_id = uuid.uuid4().hex
        status = entry.get("status", "unknown")
        dir_name = f"{ts}__{_sanitize_for_path(status, 16)}__{_sanitize_for_path(entry.get('task_title', ''))}"

        final_dir = os.path.join(outbox, dir_name)
        tmp_dir = os.path.join(outbox, f".tmp_{entry_id}")
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
        attach_dir = os.path.join(tmp_dir, "attachments")
        os.makedirs(attach_dir, exist_ok=True)

        # Copy attachments into the entry and build the manifest.
        manifest = []
        used_names = set()
        for path in attachment_paths or []:
            try:
                original_name = os.path.basename(path)
                stored_name = _dedupe_name(used_names, original_name)
                dest = os.path.join(attach_dir, stored_name)
                shutil.copy2(path, dest)
                size = os.path.getsize(dest)
                mime = mimetypes.guess_type(dest)[0] or "application/octet-stream"
                manifest.append({
                    "stored_name": stored_name,
                    "original_name": original_name,
                    "mime": mime,
                    "size_bytes": size,
                })
            except Exception as e:
                manifest.append({
                    "stored_name": None,
                    "original_name": os.path.basename(path),
                    "error": str(e),
                })

        record = {
            "schema_version": OUTBOX_SCHEMA_VERSION,
            "entry_id": entry_id,
            "task_id": entry.get("task_id"),
            "created_at": now.isoformat(),
            "status": status,
            "human_action": entry.get("human_action", True),
            "task_title": entry.get("task_title", ""),
            "task_description": entry.get("task_description", ""),
            "context_note": entry.get("context_note", ""),
            "client_timeout_seconds": entry.get("client_timeout_seconds"),
            "body": entry.get("body", ""),
            "attachments": manifest,
        }
        with open(os.path.join(tmp_dir, "entry.json"), "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)

        # Atomic-ish publish. If the (timestamped) name somehow exists, suffix it.
        if os.path.exists(final_dir):
            final_dir = f"{final_dir}__{entry_id[:8]}"
        os.replace(tmp_dir, final_dir)
        return final_dir
    except Exception as e:
        _out(f"Warning: failed to archive submission to outbox: {e}")
        try:
            if 'tmp_dir' in locals() and os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
        return None


class RingPlayer:
    """Cross-OS looping ringtone. Idempotent start()/stop(); never raises.

    Windows uses winsound's native SND_LOOP; macOS/Linux run a daemon thread
    that re-spawns a CLI player (afplay / paplay / aplay) until stopped, since
    those players don't loop on their own.
    """

    def __init__(self, path=RINGTONE_PATH):
        self.path = path
        self._stop = threading.Event()
        self._thread = None
        self._proc = None
        self._started = False

    def _posix_player_cmd(self):
        for name in (("afplay",) if IS_MACOS else ("paplay", "aplay")):
            exe = shutil.which(name)
            if exe:
                return [exe, self.path]
        return None

    def _loop_posix(self, cmd):
        while not self._stop.is_set():
            start = time.monotonic()
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._proc.wait()
            except Exception:
                break
            # If the player exits almost instantly (missing device/file), back
            # off so we don't spin; the stop event also breaks the wait.
            if time.monotonic() - start < 0.2:
                if self._stop.wait(1.0):
                    break

    def start(self):
        if self._started or not self.path or not os.path.isfile(self.path):
            return
        self._started = True
        try:
            if IS_WINDOWS:
                import winsound
                winsound.PlaySound(
                    self.path,
                    winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
            else:
                cmd = self._posix_player_cmd()
                if cmd is None:
                    return
                self._thread = threading.Thread(
                    target=self._loop_posix, args=(cmd,), daemon=True)
                self._thread.start()
        except Exception as e:
            _out(f"Warning: could not start ringtone: {e}")

    def stop(self):
        if not self._started:
            return
        self._started = False
        try:
            if IS_WINDOWS:
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            else:
                self._stop.set()
                if self._proc is not None:
                    try:
                        self._proc.terminate()
                    except Exception:
                        pass
        except Exception:
            pass


class DialogRunner:
    """Owns the tkinter main loop and runs every dialog on the main thread.

    macOS/AppKit (and tkinter in general) require all windows to be created and
    driven from the process's main thread. The MCP server therefore runs on a
    background thread and submits dialog-building callables to this runner; the
    main thread's Tk event loop picks them up, builds the dialog, and delivers
    the result back through a ``concurrent.futures.Future``.
    """

    def __init__(self):
        self.root = None
        self._queue = queue.Queue()
        self._shutdown = threading.Event()
        self._started = threading.Event()

    # ------------------------------------------------------------------ #
    # Main-thread side
    # ------------------------------------------------------------------ #
    def run(self):
        """Create the shared root window and run the Tk main loop (blocks).

        MUST be called on the main thread.
        """
        self.root = tk.Tk()
        self.root.withdraw()
        try:
            if IS_MACOS:
                self.root.call('wm', 'attributes', '.', '-topmost', '1')
                configure_macos_app()
            elif IS_WINDOWS:
                self.root.attributes('-topmost', True)
        except Exception as e:
            _out(f"Warning: GUI initialization failed: {e}")

        self._started.set()
        self.root.after(50, self._pump)
        self.root.mainloop()

    def _pump(self):
        """Drain queued dialog requests; re-armed every 50ms via ``after``."""
        if self._shutdown.is_set():
            try:
                self.root.quit()
            except Exception:
                pass
            return

        while True:
            try:
                fn, future = self._queue.get_nowait()
            except queue.Empty:
                break
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn(self.root))
            except Exception as e:  # noqa: BLE001 - propagate to caller thread
                future.set_exception(e)

        self.root.after(50, self._pump)

    # ------------------------------------------------------------------ #
    # Background-thread side
    # ------------------------------------------------------------------ #
    def submit(self, fn):
        """Schedule ``fn(root)`` on the main thread; returns a Future."""
        future = concurrent.futures.Future()
        self._queue.put((fn, future))
        return future

    async def run_dialog(self, fn, timeout=300):
        """Await a dialog builder that runs on the main thread."""
        future = self.submit(fn)
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)

    def is_ready(self):
        return self.root is not None and self._started.is_set()

    def request_shutdown(self):
        self._shutdown.set()


# Single shared runner; its main loop is started in main().
_dialog_runner = DialogRunner()

def get_system_font():
    """Get appropriate system font for the current platform"""
    if IS_MACOS:
        return ("SF Pro Display", 13)  # macOS system font
    elif IS_WINDOWS:
        return ("Segoe UI", 10)  # Windows system font
    else:
        return ("Ubuntu", 10)  # Linux/other systems

def get_title_font():
    """Get title font for dialogs"""
    if IS_MACOS:
        return ("SF Pro Display", 16, "bold")
    elif IS_WINDOWS:
        return ("Segoe UI", 14, "bold")
    else:
        return ("Ubuntu", 14, "bold")

def get_text_font():
    """Get text font for text widgets"""
    if IS_MACOS:
        return ("Monaco", 12)  # macOS monospace font
    elif IS_WINDOWS:
        return ("Consolas", 11)  # Windows monospace font
    else:
        return ("Ubuntu Mono", 10)  # Linux monospace font

def get_theme_colors():
    """Get modern theme colors based on platform"""
    if IS_WINDOWS:
        return {
            "bg_primary": "#FFFFFF",           # Pure white background
            "bg_secondary": "#F8F9FA",         # Light gray background
            "bg_accent": "#F1F3F4",            # Accent background
            "fg_primary": "#202124",           # Dark text
            "fg_secondary": "#5F6368",         # Secondary text
            "accent_color": "#0078D4",         # Windows blue
            "accent_hover": "#106EBE",         # Darker blue for hover
            "border_color": "#E8EAED",         # Light border
            "success_color": "#137333",        # Green for success
            "error_color": "#D93025",          # Red for errors
            "selection_bg": "#E3F2FD",         # Light blue selection
            "selection_fg": "#1565C0"          # Dark blue selection text
        }
    elif IS_MACOS:
        return {
            "bg_primary": "#FFFFFF",
            "bg_secondary": "#F5F5F7",
            "bg_accent": "#F2F2F7",
            "fg_primary": "#1D1D1F",
            "fg_secondary": "#86868B",
            "accent_color": "#007AFF",
            "accent_hover": "#0056CC",
            "border_color": "#D2D2D7",
            "success_color": "#30D158",
            "error_color": "#FF3B30",
            "selection_bg": "#E3F2FD",
            "selection_fg": "#1565C0"
        }
    else:  # Linux
        return {
            "bg_primary": "#FFFFFF",
            "bg_secondary": "#F8F9FA",
            "bg_accent": "#F1F3F4",
            "fg_primary": "#202124",
            "fg_secondary": "#5F6368",
            "accent_color": "#1976D2",
            "accent_hover": "#1565C0",
            "border_color": "#E8EAED",
            "success_color": "#388E3C",
            "error_color": "#D32F2F",
            "selection_bg": "#E3F2FD",
            "selection_fg": "#1565C0"
        }

def apply_modern_style(widget, widget_type="default", theme_colors=None):
    """Apply modern styling to tkinter widgets"""
    if theme_colors is None:
        theme_colors = get_theme_colors()
    
    try:
        if widget_type == "frame":
            widget.configure(
                bg=theme_colors["bg_primary"],
                relief="flat",
                borderwidth=0
            )
        elif widget_type == "label":
            widget.configure(
                bg=theme_colors["bg_primary"],
                fg=theme_colors["fg_primary"],
                font=get_system_font(),
                anchor="w"
            )
        elif widget_type == "title_label":
            widget.configure(
                bg=theme_colors["bg_primary"],
                fg=theme_colors["fg_primary"],
                font=get_title_font(),
                anchor="w"
            )
        elif widget_type == "listbox":
            widget.configure(
                bg=theme_colors["bg_primary"],
                fg=theme_colors["fg_primary"],
                selectbackground=theme_colors["selection_bg"],
                selectforeground=theme_colors["selection_fg"],
                relief="solid",
                borderwidth=1,
                highlightthickness=1,
                highlightcolor=theme_colors["accent_color"],
                highlightbackground=theme_colors["border_color"],
                font=get_system_font(),
                activestyle="none"
            )
        elif widget_type == "text":
            widget.configure(
                bg=theme_colors["bg_primary"],
                fg=theme_colors["fg_primary"],
                selectbackground=theme_colors["selection_bg"],
                selectforeground=theme_colors["selection_fg"],
                relief="solid",
                borderwidth=1,
                highlightthickness=1,
                highlightcolor=theme_colors["accent_color"],
                highlightbackground=theme_colors["border_color"],
                font=get_text_font(),
                wrap="word",
                padx=12,
                pady=8
            )
        elif widget_type == "scrollbar":
            widget.configure(
                bg=theme_colors["bg_secondary"],
                troughcolor=theme_colors["bg_accent"],
                activebackground=theme_colors["accent_hover"],
                relief="flat",
                borderwidth=0,
                highlightthickness=0
            )
    except Exception:
        pass  # Ignore styling errors on different platforms

def create_modern_button(parent, text, command, button_type="primary", theme_colors=None):
    """Create a modern styled button"""
    if theme_colors is None:
        theme_colors = get_theme_colors()
    
    if button_type == "primary":
        bg_color = theme_colors["accent_color"]
        fg_color = "#FFFFFF"
        hover_color = theme_colors["accent_hover"]
    else:  # secondary
        bg_color = theme_colors["bg_secondary"]
        fg_color = theme_colors["fg_primary"]
        hover_color = theme_colors["bg_accent"]

    # macOS aqua ignores a tk.Button's `bg`, so the intended colored fill never
    # shows and white primary text ends up unreadable on the default light
    # button. Use readable dark/accent text there instead, and tint the button
    # region via highlightbackground.
    if IS_MACOS:
        fg_color = theme_colors["accent_color"] if button_type == "primary" else theme_colors["fg_primary"]

    button = tk.Button(
        parent,
        text=text,
        command=command,
        bg=bg_color,
        fg=fg_color,
        highlightbackground=bg_color,
        font=get_system_font(),
        relief="flat",
        borderwidth=0,
        padx=20,
        pady=8,
        cursor="hand2" if IS_WINDOWS else "pointinghand"
    )
    
    # Add hover effects
    def on_enter(e):
        button.configure(bg=hover_color)
    
    def on_leave(e):
        button.configure(bg=bg_color)
    
    button.bind("<Enter>", on_enter)
    button.bind("<Leave>", on_leave)
    
    return button

def configure_modern_window(window):
    """Apply modern window styling"""
    theme_colors = get_theme_colors()
    
    try:
        window.configure(bg=theme_colors["bg_primary"])
        
        if IS_WINDOWS:
            # Windows-specific modern styling
            try:
                # Try to remove window decorations for modern look (Windows 10/11)
                window.overrideredirect(False)  # Keep decorations for better UX
                window.attributes('-alpha', 0.98)  # Slight transparency
            except:
                pass
        
        # Apply platform-specific configurations
        configure_window_for_platform(window)
        
    except Exception:
        pass  # Fallback to basic styling

def configure_macos_app():
    """Configure macOS-specific application settings"""
    if IS_MACOS:
        try:
            # Try to bring Python to front on macOS
            subprocess.run([
                'osascript', '-e', 
                'tell application "System Events" to set frontmost of first process whose unix id is {} to true'.format(os.getpid())
            ], check=False, capture_output=True)
        except Exception:
            pass  # Ignore if osascript is not available

def ensure_gui_initialized():
    """Report whether the shared GUI main loop is up.

    The GUI is initialized exactly once, on the main thread, by
    ``DialogRunner.run()``. Tool handlers run on the MCP background thread and
    must never create a ``tk.Tk()`` themselves (doing so off the main thread
    crashes on macOS), so this simply checks the shared runner's state.
    """
    return _dialog_runner.is_ready()

def configure_window_for_platform(window):
    """Raise and focus a dialog window. Uses window.attributes (works on a
    Toplevel; window.call does NOT — only the Tk root has .call)."""
    try:
        if IS_MACOS or IS_WINDOWS:
            window.attributes('-topmost', True)
            window.lift()
            window.focus_force()
            if IS_MACOS:
                configure_macos_app()  # bring the Python app to the front
    except Exception as e:
        _out(f"Warning: Platform-specific window configuration failed: {e}")


def keep_notification_in_front(window):
    """Make a ringing HITL notification visible above every normal window.

    Setting ``-topmost`` before a Toplevel has been mapped is not sufficient on
    every window manager (notably macOS), so callers also re-apply this once the
    window becomes visible.
    """
    try:
        window.attributes("-topmost", True)
        window.lift()
        if IS_MACOS:
            configure_macos_app()
        window.focus_force()
    except Exception as e:
        _out(f"Warning: could not bring HITL notification to front: {e}")

def content_text_height(text, minh=2, cap=8):
    """Line count to request for a bounded prompt/message Text: fits short content,
    caps tall content (which then scrolls)."""
    return min(max(minh, len((text or "").splitlines()) + 1), cap)


def build_readonly_text(parent, content, theme_colors, height=6, width=None):
    """A bounded, scrollable Text that is read-only but still selectable/copyable.
    Used for any potentially long body/message so it scrolls instead of clipping
    (a Label grows unbounded and pushes controls off-screen). Returns (frame, text)."""
    c = theme_colors
    wrap = tk.Frame(parent, bg=c["bg_primary"])
    wrap.columnconfigure(0, weight=1)
    wrap.rowconfigure(0, weight=1)
    txt = tk.Text(wrap, height=height, wrap="word", **({"width": width} if width else {}))
    apply_modern_style(txt, "text", theme_colors)
    txt.configure(fg=c["fg_secondary"])
    txt.grid(row=0, column=0, sticky="nsew", padx=(0, 2))
    sb = tk.Scrollbar(wrap, orient="vertical", command=txt.yview)
    apply_modern_style(sb, "scrollbar", theme_colors)
    sb.grid(row=0, column=1, sticky="ns")
    txt.configure(yscrollcommand=sb.set)
    txt.insert("1.0", content or "")

    def _copy(_e):
        try:
            txt.clipboard_clear()
            txt.clipboard_append(txt.get("sel.first", "sel.last"))
        except tk.TclError:
            pass
        return "break"

    def _select_all(_e):
        txt.tag_add("sel", "1.0", "end-1c")
        return "break"

    txt.bind("<Key>", lambda e: "break")      # read-only (block typing/deletion)
    txt.bind("<Control-c>", _copy)
    txt.bind("<Command-c>", _copy)            # macOS
    txt.bind("<Control-a>", _select_all)
    txt.bind("<Command-a>", _select_all)      # macOS
    return wrap, txt


class _LegAwareWindow:
    """Shared countdown + disconnect-watchdog + close for any window that must live
    across heartbeats (the notification toast and every dialog).

    A subclass must set ``self._top`` (its Toplevel) and ``self.session`` and call
    ``self._init_leg()``. It may override ``_render_status`` (to show countdown/
    disconnect state), ``_on_autoclose`` (what to do when the assistant goes
    silent), and ``_ring_stop`` (windows that ring)."""

    WARN_SECONDS = 10

    def _init_leg(self):
        self._alive = True
        self._leg_seconds = None
        self._last_contact = None
        self._ui_after = None

    def begin_countdown(self, leg_seconds):
        """Called each time the assistant checks in (starts a new wait leg)."""
        self._leg_seconds = max(1, leg_seconds)
        self._last_contact = time.monotonic()
        if self._ui_after is None:
            self._ui_tick()

    @staticmethod
    def _fmt(seconds):
        seconds = max(0, int(seconds))
        return f"{seconds // 60:d}:{seconds % 60:02d}"

    def _ui_tick(self):
        if not self._alive:
            self._ui_after = None
            return
        if self._last_contact is None:
            self._ui_after = self._top.after(500, self._ui_tick)
            return
        idle = time.monotonic() - self._last_contact
        leg = self._leg_seconds or 1
        over = idle - leg
        if over >= AUTO_CLOSE_AFTER_SECONDS:
            self._on_autoclose()
            return
        try:
            self._render_status(leg - idle, over, idle)
        except Exception:
            pass
        self._ui_after = self._top.after(1000, self._ui_tick)

    def _render_status(self, remaining, over, idle):
        """Default: no visible status. Override to update a label."""

    def _on_autoclose(self):
        """Default: tell the async side the assistant went silent, then close."""
        try:
            self.session.submit_from_ui({"kind": "decline", "reason": "assistant_disconnected"})
        except Exception:
            pass
        self.close()

    def _ring_stop(self):
        """Hook for windows that play a ringtone."""

    def close(self):
        self._alive = False
        try:
            self._ring_stop()
        except Exception:
            pass
        if self._ui_after:
            try:
                self._top.after_cancel(self._ui_after)
            except Exception:
                pass
            self._ui_after = None
        try:
            self._top.destroy()
        except Exception:
            pass
        try:
            if getattr(self.session, "dialog", None) is self:
                self.session.dialog = None
        except Exception:
            pass

    def _center_window(self):
        w = self._top
        w.update_idletasks()
        width = w.winfo_width()
        height = w.winfo_height()
        sw = w.winfo_screenwidth()
        sh = w.winfo_screenheight()
        x = (sw // 2) - (width // 2)
        y = (sh // 2) - (height // 2)
        if IS_MACOS:
            y = max(50, y - 50)
        elif IS_WINDOWS:
            y = max(30, y - 30)
        w.geometry(f"{width}x{height}+{x}+{y}")


class InteractionDialog(_LegAwareWindow):
    """Non-modal dialog skeleton shared by the simple input/choice/confirm/etc.
    dialogs. Replaces the old modal ``wait_window()`` classes: results flow to the
    async side via ``session.submit_from_ui`` instead of blocking.

    Layout (pack): title (top), bounded scrollable prompt, [body — subclass fills,
    expands], hidden status/countdown label, buttons pinned to the bottom.
    Subclasses override ``_build_body``/``_build_buttons`` and call ``self._submit``.
    """

    HAS_BODY = True   # False for message-only dialogs (confirm/info)

    def __init__(self, parent, session):
        self.parent = parent
        self.session = session
        self.theme_colors = get_theme_colors()
        self._init_leg()
        p = session.params or {}
        c = self.theme_colors

        self._top = tk.Toplevel(parent)
        self._top.title(p.get("title") or "")
        self._top.resizable(True, True)
        self._top.minsize(*self._minsize())
        configure_modern_window(self._top)   # raise/focus — fine, the human clicked View
        gw, gh = self._geometry()
        self._top.geometry(f"{gw}x{gh}")

        main = tk.Frame(self._top, bg=c["bg_primary"])
        main.pack(fill="both", expand=True, padx=24, pady=20)

        tk.Label(main, text=p.get("title") or "", bg=c["bg_primary"], fg=c["fg_primary"],
                 font=get_title_font(), anchor="w", justify="left",
                 wraplength=520).pack(side="top", fill="x", pady=(0, 8))

        # Buttons pinned to the bottom so a long prompt can never push them off.
        self._button_bar = tk.Frame(main, bg=c["bg_primary"])
        self._button_bar.pack(side="bottom", fill="x", pady=(12, 0))

        # Hidden-by-default countdown/disconnect status line.
        self.status_label = tk.Label(main, text="", bg=c["bg_primary"], fg=c["fg_secondary"],
                                     font=get_system_font(), anchor="w", justify="left")
        if SHOW_CONNECTION_STATUS:
            self.status_label.pack(side="bottom", fill="x", pady=(6, 0))

        prompt = p.get("prompt")
        if prompt:
            cap = 8 if self.HAS_BODY else 16
            pf, _ = build_readonly_text(main, prompt, c, height=content_text_height(prompt, cap=cap))
            pf.pack(side="top", fill="x" if self.HAS_BODY else "both",
                    expand=not self.HAS_BODY, pady=(0, 12))

        if self.HAS_BODY:
            self._body = tk.Frame(main, bg=c["bg_primary"])
            self._body.pack(side="top", fill="both", expand=True)
            self._build_body(self._body)

        self._build_buttons(self._button_bar)

        self._top.protocol("WM_DELETE_WINDOW", self._on_close)
        self._top.update_idletasks()
        self._center_window()

    # ---- overridable geometry / body / buttons ---- #
    def _minsize(self):
        return (380, 300)

    def _geometry(self):
        return (460, 360) if IS_WINDOWS else (440, 345)

    def _build_body(self, parent):
        pass

    def _build_buttons(self, parent):
        pass

    # ---- status rendering (disconnect warning) ---- #
    def _render_status(self, remaining, over, idle):
        if not self._alive:
            return
        c = self.theme_colors
        if over >= DISCONNECT_GRACE_SECONDS:
            self.status_label.config(
                text="The assistant may have disconnected — this dialog will close soon.",
                fg=c.get("error_color", c["fg_secondary"]))
        elif remaining > self.WARN_SECONDS:
            self.status_label.config(
                text=f"Assistant connected · next check-in in {self._fmt(remaining)}",
                fg=c["fg_secondary"])
        else:
            self.status_label.config(
                text="Syncing with assistant… (your work is safe)", fg=c["fg_secondary"])

    # ---- result plumbing ---- #
    def _submit(self, payload):
        if not self._alive:
            return
        self.session.submit_from_ui(payload)
        self.close()

    def _on_close(self):
        # Window closed via the window manager == cancel.
        if not self._alive:
            return
        self.session.submit_from_ui({"cancelled": True})
        self.close()


class InputDialog(InteractionDialog):
    """Single-line text / number input (was ModernInputDialog)."""

    def _minsize(self):
        return (360, 300)

    def _geometry(self):
        return (440, 360) if IS_WINDOWS else (420, 345)

    def _build_body(self, parent):
        p = self.session.params
        self.input_type = p.get("input_type", "text")
        c = self.theme_colors
        self.entry = tk.Entry(
            parent, font=get_system_font(), bg=c["bg_primary"], fg=c["fg_primary"],
            relief="solid", borderwidth=1, highlightthickness=1,
            highlightcolor=c["accent_color"], highlightbackground=c["border_color"],
            insertbackground=c["accent_color"])
        self.entry.pack(fill="x", ipady=8, ipadx=12, pady=(0, 8))
        dv = p.get("default_value") or ""
        if dv:
            self.entry.insert(0, dv)
            self.entry.select_range(0, tk.END)
        self.entry.bind("<KeyRelease>", self._validate)
        self.hint_label = tk.Label(
            parent, text="", bg=c["bg_primary"],
            fg=c.get("error_color", c["fg_secondary"]),
            font=get_system_font(), anchor="w", justify="left")
        self.hint_label.pack(fill="x")
        self.entry.focus_set()

    def _build_buttons(self, parent):
        c = self.theme_colors
        self.ok_button = create_modern_button(parent, "OK", self._ok, "primary", c)
        self.ok_button.pack(side=tk.RIGHT, padx=(8, 0))
        create_modern_button(parent, "Cancel", self._cancel, "secondary", c).pack(side=tk.RIGHT)
        self._top.bind('<Return>', lambda e: self._ok())
        self._top.bind('<Escape>', lambda e: self._cancel())
        self._validate()

    def _parse_current(self):
        raw = self.entry.get()
        value = raw.strip()
        if self.input_type == "integer":
            if not value:
                return (False, None)
            try:
                return (True, int(value))
            except ValueError:
                return (False, None)
        elif self.input_type == "float":
            if not value:
                return (False, None)
            try:
                return (True, float(value))
            except ValueError:
                return (False, None)
        return (True, raw if raw else None)

    def _validate(self, *_):
        valid, _v = self._parse_current()
        try:
            self.ok_button.configure(state="normal" if valid else "disabled")
        except Exception:
            pass
        if self.input_type in ("integer", "float") and not valid:
            kind = "whole number" if self.input_type == "integer" else "number"
            self.hint_label.config(text=f"Please enter a valid {kind} to continue.")
        else:
            self.hint_label.config(text="")

    def _ok(self):
        valid, parsed = self._parse_current()
        if not valid:
            self._validate()
            return
        self._submit({"value": parsed})

    def _cancel(self):
        self._submit({"cancelled": True})


class ChoiceBody(InteractionDialog):
    """Pick one/many from a list (was ChoiceDialog)."""

    def _minsize(self):
        return (400, 340)

    def _geometry(self):
        if IS_MACOS:
            return (480, 460)
        if IS_WINDOWS:
            return (500, 480)
        return (450, 430)

    def _build_body(self, parent):
        p = self.session.params
        allow_multiple = p.get("allow_multiple", False)
        choices = p.get("choices") or []
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(
            parent, selectmode=tk.MULTIPLE if allow_multiple else tk.SINGLE, height=8)
        apply_modern_style(self.listbox, "listbox", self.theme_colors)
        for ch in choices:
            self.listbox.insert(tk.END, ch)
        self.listbox.grid(row=0, column=0, sticky="nsew", padx=(0, 2))
        sb = tk.Scrollbar(parent, orient="vertical", command=self.listbox.yview)
        apply_modern_style(sb, "scrollbar", self.theme_colors)
        sb.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=sb.set)
        if choices:
            self.listbox.selection_set(0)
        self.listbox.focus_set()

    def _build_buttons(self, parent):
        c = self.theme_colors
        create_modern_button(parent, "OK", self._ok, "primary", c).pack(side=tk.RIGHT, padx=(8, 0))
        create_modern_button(parent, "Cancel", self._cancel, "secondary", c).pack(side=tk.RIGHT)
        self._top.bind('<Return>', lambda e: self._ok())
        self._top.bind('<Escape>', lambda e: self._cancel())

    def _ok(self):
        sel = self.listbox.curselection()
        if not sel:
            self._submit({"cancelled": True})
            return
        items = [self.listbox.get(i) for i in sel]
        self._submit({"value": items if len(items) > 1 else items[0]})

    def _cancel(self):
        self._submit({"cancelled": True})


class MultilineBody(InteractionDialog):
    """Long-form text input (was MultilineInputDialog)."""

    def _minsize(self):
        return (460, 380)

    def _geometry(self):
        if IS_MACOS:
            return (580, 480)
        if IS_WINDOWS:
            return (600, 500)
        return (550, 450)

    def _build_body(self, parent):
        p = self.session.params
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        self.text_widget = tk.Text(parent, height=12)
        apply_modern_style(self.text_widget, "text", self.theme_colors)
        self.text_widget.grid(row=0, column=0, sticky="nsew", padx=(0, 2))
        sb = tk.Scrollbar(parent, orient="vertical", command=self.text_widget.yview)
        apply_modern_style(sb, "scrollbar", self.theme_colors)
        sb.grid(row=0, column=1, sticky="ns")
        self.text_widget.configure(yscrollcommand=sb.set)
        dv = p.get("default_value") or ""
        if dv:
            self.text_widget.insert("1.0", dv)
        self.text_widget.focus_set()

    def _build_buttons(self, parent):
        c = self.theme_colors
        create_modern_button(parent, "OK", self._ok, "primary", c).pack(side=tk.RIGHT, padx=(8, 0))
        create_modern_button(parent, "Cancel", self._cancel, "secondary", c).pack(side=tk.RIGHT)
        self._top.bind('<Control-Return>', lambda e: self._ok())
        self._top.bind('<Escape>', lambda e: self._cancel())

    def _ok(self):
        self._submit({"value": self.text_widget.get("1.0", tk.END).strip()})

    def _cancel(self):
        self._submit({"cancelled": True})


class ConfirmBody(InteractionDialog):
    """Yes/No confirmation (was ModernConfirmationDialog). The message is shown as
    the base 'prompt'."""

    HAS_BODY = False

    def _minsize(self):
        return (380, 210)

    def _geometry(self):
        return (440, 240) if IS_WINDOWS else (420, 230)

    def _build_buttons(self, parent):
        c = self.theme_colors
        create_modern_button(parent, "Yes", lambda: self._submit({"value": True}),
                             "primary", c).pack(side=tk.RIGHT, padx=(8, 0))
        create_modern_button(parent, "No", lambda: self._submit({"value": False}),
                             "secondary", c).pack(side=tk.RIGHT)
        self._top.bind('<Return>', lambda e: self._submit({"value": True}))
        self._top.bind('<Escape>', lambda e: self._submit({"value": False}))

    def _on_close(self):
        # Closing a confirmation == "No".
        if not self._alive:
            return
        self.session.submit_from_ui({"value": False})
        self.close()


class InfoBody(InteractionDialog):
    """Information message with an OK acknowledgement (was ModernInfoDialog)."""

    HAS_BODY = False

    def _minsize(self):
        return (360, 200)

    def _geometry(self):
        return (420, 300) if IS_WINDOWS else (400, 285)

    def _build_buttons(self, parent):
        c = self.theme_colors
        create_modern_button(parent, "OK", lambda: self._submit({"value": True}),
                             "primary", c).pack(side=tk.RIGHT)
        self._top.bind('<Return>', lambda e: self._submit({"value": True}))
        self._top.bind('<Escape>', lambda e: self._submit({"value": True}))

    def _on_close(self):
        # Closing an info dialog == acknowledged.
        if not self._alive:
            return
        self.session.submit_from_ui({"value": True})
        self.close()


# --------------------------------------------------------------------------- #
# Long-running "assign a task to a human" dialog + session
# --------------------------------------------------------------------------- #

# Registry of live task sessions, keyed by task_id. Lives on the server process
# and survives across multiple tool invocations (heartbeat continuations).
_sessions: "Dict[str, InteractionSession]" = {}


class InteractionSession:
    """Server-side state for one delegated human task.

    Persists across heartbeat continuations. The async tool (MCP background
    thread) consumes human submissions from ``updates``; the Tk dialog (main
    thread) produces them via :meth:`submit_from_ui`, bridged with
    ``loop.call_soon_threadsafe`` so the queue is only ever touched on the loop
    thread.
    """

    MAX_LIFETIME_SECONDS = 30 * 60  # orphan safety net

    def __init__(self, session_id, kind, params, client_timeout_seconds, loop, make_body,
                 max_result_bytes=DEFAULT_MAX_RESULT_BYTES, attachments_enabled=True):
        self.id = session_id
        self.task_id = session_id            # alias used by the tools/notification
        self.kind = kind                     # "task" | "input" | "choice" | ...
        self.params = dict(params or {})     # dialog-specific args (title/prompt/choices/...)
        self.make_body = make_body           # factory: (root, session) -> the body dialog
        # Task-facing fields (the task dialog reads these directly; simple dialogs
        # read from params instead). Seeded from params.
        self.task_title = self.params.get("task_title") or self.params.get("title", "")
        self.task_description = self.params.get("task_description", "")
        self.context_note = self.params.get("context_note", "")
        self.client_timeout_seconds = client_timeout_seconds
        self.max_result_bytes = max_result_bytes
        self.attachments_enabled = attachments_enabled
        self.loop = loop
        self.updates = asyncio.Queue()
        self.dialog = None
        self.created_at = time.monotonic()
        self.created_wall = datetime.now()
        self.last_seen = self.created_at
        self.last_human_action_at = self.created_at  # for follow-up re-notify timing
        self.advisory = None  # non-blocking hint if the description looks misused
        self.closed = False

    def attach_dialog(self, dialog):
        self.dialog = dialog
        return self

    # --- called from the Tk main thread (dialog button handlers) --- #
    def submit_from_ui(self, payload):
        """Hand a human submission to the async consumer, thread-safely."""
        try:
            self.loop.call_soon_threadsafe(self.updates.put_nowait, payload)
        except RuntimeError:
            pass  # loop already closed

    # --- called on the main thread via _dialog_runner.submit --- #
    def begin_leg(self, leg_seconds):
        self.last_seen = time.monotonic()
        if self.dialog:
            self.dialog.begin_countdown(leg_seconds)

    def close(self):
        self.closed = True
        if self.dialog:
            self.dialog.close()
            self.dialog = None

    def is_expired(self):
        # Based on last activity so an actively-continued task never expires,
        # but an abandoned dormant session (window closed, AI never re-called)
        # is eventually reaped.
        return (time.monotonic() - self.last_seen) > self.MAX_LIFETIME_SECONDS


class NotificationWindow(_LegAwareWindow):
    """Small, always-on-top top-right toast that rings before the real dialog
    opens. View opens the dialog (via ``session.make_body``); Cancel declines. Uses
    the shared leg/disconnect machinery so a ringing orphan can't outlive the
    assistant going away."""

    def __init__(self, parent, session):
        self.parent = parent
        self.session = session
        self.theme_colors = get_theme_colors()
        self._init_leg()
        # Operator-configured ringtone / mute (falls back to the bundled sound).
        _notif = human_loop_config.get_notification()
        self._muted = bool(_notif.get("muted", False))
        self.ring = RingPlayer(path=_notif.get("ringtone_path") or RINGTONE_PATH)

        c = self.theme_colors
        self.win = tk.Toplevel(parent)
        self._top = self.win
        self.win.title("Request from the assistant")
        self.win.resizable(False, False)
        self.win.configure(bg=c["bg_primary"])
        w, h = 360, 175
        try:
            sw = self.win.winfo_screenwidth()
        except Exception:
            sw = 1440
        self.win.geometry(f"{w}x{h}+{max(0, sw - w - 24)}+24")
        # A ringing HITL request must not disappear behind the currently active
        # application. Re-assert after mapping because some window managers
        # ignore topmost/focus requests made while a Toplevel is still hidden.
        keep_notification_in_front(self.win)
        self.win.after_idle(lambda: keep_notification_in_front(self.win))
        self.win.after(150, lambda: keep_notification_in_front(self.win))

        # Button bar is pinned to the WINDOW bottom FIRST, so View/Cancel are
        # ALWAYS visible no matter how tall the preview/heading get.
        btns = tk.Frame(self.win, bg=c["bg_primary"])
        btns.pack(side="bottom", fill="x", padx=16, pady=(0, 14))
        create_modern_button(btns, "View", self._on_view, "primary", c).pack(side=tk.RIGHT, padx=(8, 0))
        create_modern_button(btns, "Cancel", self._on_cancel, "secondary", c).pack(side=tk.RIGHT)

        main = tk.Frame(self.win, bg=c["bg_primary"])
        main.pack(side="top", fill="both", expand=True, padx=16, pady=(14, 6))

        heading = session.params.get("notify_heading") or "The assistant needs you"
        # wraplength so a long heading wraps to a second line instead of clipping.
        tk.Label(main, text=heading, bg=c["bg_primary"], fg=c["fg_primary"],
                 font=get_title_font(), anchor="w", justify="left",
                 wraplength=324).pack(fill="x", pady=(0, 6))

        # The toast is a short teaser, not the content: collapse whitespace/newlines
        # to a single wrapped block and truncate, so it can't grow tall enough to
        # crowd out the buttons. The full content is in the dialog opened by View.
        preview = (session.params.get("notify_preview") or session.task_title
                   or session.params.get("title") or "(request)")
        preview = " ".join(preview.split())
        if len(preview) > 100:
            preview = preview[:99] + "…"
        tk.Label(main, text=preview, bg=c["bg_primary"], fg=c["fg_secondary"],
                 font=get_system_font(), anchor="w", justify="left",
                 wraplength=324).pack(fill="x", pady=(0, 4))

        self.status_label = tk.Label(
            main, text="Ringing… choose View or Cancel.",
            bg=c["bg_primary"], fg=c["fg_secondary"], font=get_system_font(),
            anchor="w", justify="left", wraplength=324)
        self.status_label.pack(fill="x", pady=(0, 4))

        self.win.protocol("WM_DELETE_WINDOW", self._on_cancel)
        # Size the toast to its content (capped) so a long/CJK preview can't clip.
        self.win.update_idletasks()
        h = min(max(160, self.win.winfo_reqheight()), 340)
        try:
            sw = self.win.winfo_screenwidth()
        except Exception:
            sw = 1440
        self.win.geometry(f"360x{h}+{max(0, sw - 360 - 24)}+24")
        if not self._muted:
            self.ring.start()

    def _ring_stop(self):
        self.ring.stop()

    def _render_status(self, remaining, over, idle):
        if over >= DISCONNECT_GRACE_SECONDS and self._alive:
            self.status_label.config(
                text="The assistant may have disconnected - this notification will close soon.",
                fg=self.theme_colors.get("error_color", self.theme_colors["fg_secondary"]))

    def _on_view(self):
        if not self._alive:
            return
        self.ring.stop()
        # Open the real dialog immediately (main thread) via the session factory,
        # so it appears the instant the human clicks - no AI round-trip needed.
        try:
            win = self.session.make_body(self.parent, self.session)
            self.session.attach_dialog(win)
            win.begin_countdown(compute_leg_seconds(self.session.client_timeout_seconds))
        except Exception as e:
            _out(f"Warning: failed to open dialog: {e}")
        self.close()  # closes the toast; session.dialog now points at the real dialog
        self.session.submit_from_ui({"kind": "view"})

    def _on_cancel(self):
        if not self._alive:
            return
        self.ring.stop()
        self.session.submit_from_ui({"kind": "decline"})
        self.close()


class HumanTaskDialog(_LegAwareWindow):
    """Persistent (non-modal, no ``wait_window``) task dialog with a live
    countdown. Built on the main thread; button handlers push submissions to the
    owning :class:`InteractionSession`. Uses the shared leg/disconnect machinery;
    overrides ``_render_status`` (rich countdown) and ``_on_autoclose`` (archive)."""

    def __init__(self, parent, session):
        self.session = session
        self.theme_colors = get_theme_colors()
        self.attachments = []          # list[str] of chosen file paths
        self._init_leg()

        c = self.theme_colors
        self._tid8 = session.task_id[:8]
        self.dialog = tk.Toplevel(parent)
        self._top = self.dialog
        self.dialog.title(f"Task from assistant - {session.task_title}  [{self._tid8}]")
        self.dialog.resizable(True, True)
        configure_modern_window(self.dialog)
        if IS_MACOS:
            self.dialog.geometry("580x700")
        elif IS_WINDOWS:
            self.dialog.geometry("600x720")
        else:
            self.dialog.geometry("560x680")
        self.dialog.minsize(460, 520)
        self._center_window()

        main = tk.Frame(self.dialog, bg=c["bg_primary"])
        main.pack(fill="both", expand=True, padx=24, pady=20)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(5, weight=1)   # body text expands

        # Title
        tk.Label(main, text=session.task_title, bg=c["bg_primary"],
                 fg=c["fg_primary"], font=get_title_font(), anchor="w",
                 justify="left", wraplength=520).grid(row=0, column=0, sticky="ew", pady=(0, 8))

        # Task description (what the AI is asking the human to do). A fixed-height,
        # scrollable, read-only-but-SELECTABLE text box — so a long description
        # can't push the controls off-screen, and the human can select/copy it.
        desc = session.task_description or ""
        if session.context_note:
            desc = f"{desc}\n\n--- context ---\n{session.context_note}"
        self._make_readonly_text(main, desc, height=6).grid(
            row=1, column=0, sticky="ew", pady=(0, 8))

        # Connection/countdown line. Kept as a widget so the liveness/disconnect
        # watchdog can update it, but hidden by default (debug-only info now).
        self.countdown_label = tk.Label(
            main, text="", bg=c["bg_primary"], fg=c["fg_secondary"],
            font=get_system_font(), anchor="w", justify="left")
        if SHOW_CONNECTION_STATUS:
            self.countdown_label.grid(row=3, column=0, sticky="ew", pady=(0, 10))

        # Body ("email body")
        tk.Label(main, text="Your report to the assistant:", bg=c["bg_primary"],
                 fg=c["fg_primary"], font=get_system_font(), anchor="w").grid(
            row=4, column=0, sticky="ew", pady=(0, 4))

        body_container = tk.Frame(main, bg=c["bg_primary"])
        body_container.grid(row=5, column=0, sticky="nsew", pady=(0, 10))
        body_container.columnconfigure(0, weight=1)
        body_container.rowconfigure(0, weight=1)
        self.body_text = tk.Text(body_container, height=8)
        apply_modern_style(self.body_text, "text", self.theme_colors)
        self.body_text.grid(row=0, column=0, sticky="nsew", padx=(0, 2))
        body_scroll = tk.Scrollbar(body_container, orient="vertical", command=self.body_text.yview)
        apply_modern_style(body_scroll, "scrollbar", self.theme_colors)
        body_scroll.grid(row=0, column=1, sticky="ns")
        self.body_text.configure(yscrollcommand=body_scroll.set)

        # Attachments (only when the operator has enabled the feature)
        self.attach_listbox = None
        if getattr(session, "attachments_enabled", True):
            attach_header = tk.Frame(main, bg=c["bg_primary"])
            attach_header.grid(row=6, column=0, sticky="ew", pady=(0, 4))
            tk.Label(attach_header, text="Attachments:", bg=c["bg_primary"],
                     fg=c["fg_primary"], font=get_system_font(), anchor="w").pack(side=tk.LEFT)
            create_modern_button(attach_header, "Attach files/images…", self._add_attachments,
                                 "secondary", self.theme_colors).pack(side=tk.RIGHT)
            create_modern_button(attach_header, "Remove selected", self._remove_attachment,
                                 "secondary", self.theme_colors).pack(side=tk.RIGHT, padx=(0, 8))

            attach_container = tk.Frame(main, bg=c["bg_primary"])
            attach_container.grid(row=7, column=0, sticky="ew", pady=(0, 14))
            attach_container.columnconfigure(0, weight=1)
            self.attach_listbox = tk.Listbox(attach_container, selectmode=tk.EXTENDED, height=4)
            apply_modern_style(self.attach_listbox, "listbox", self.theme_colors)
            self.attach_listbox.grid(row=0, column=0, sticky="ew", padx=(0, 2))
            attach_scroll = tk.Scrollbar(attach_container, orient="vertical", command=self.attach_listbox.yview)
            apply_modern_style(attach_scroll, "scrollbar", self.theme_colors)
            attach_scroll.grid(row=0, column=1, sticky="ns")
            self.attach_listbox.configure(yscrollcommand=attach_scroll.set)

        # Live submission-size counter (text + attachments vs the client's limit).
        self.size_label = tk.Label(
            main, text="", bg=c["bg_primary"], fg=c["fg_secondary"],
            font=get_system_font(), anchor="w", justify="left")
        self.size_label.grid(row=8, column=0, sticky="ew", pady=(0, 6))

        # Action buttons: Completed / Failed / Still progressing
        button_frame = tk.Frame(main, bg=c["bg_primary"])
        button_frame.grid(row=9, column=0, sticky="ew")
        self.btn_completed = create_modern_button(
            button_frame, "Completed", lambda: self._submit("completed", terminal=True),
            "primary", self.theme_colors)
        self.btn_completed.pack(side=tk.LEFT)
        self.btn_failed = create_modern_button(
            button_frame, "Failed", lambda: self._submit("failed", terminal=True),
            "secondary", self.theme_colors)
        self.btn_failed.pack(side=tk.LEFT, padx=(8, 0))
        self.btn_in_progress = create_modern_button(
            button_frame, "Still progressing", lambda: self._submit("in_progress", terminal=False),
            "secondary", self.theme_colors)
        self.btn_in_progress.pack(side=tk.RIGHT)
        self._submit_buttons = (self.btn_completed, self.btn_failed, self.btn_in_progress)

        self.dialog.protocol("WM_DELETE_WINDOW", self._on_close)
        self.body_text.bind("<KeyRelease>", lambda e: self._update_size_counter())
        self._update_size_counter()
        self.body_text.focus_set()

    def _update_size_counter(self):
        """Refresh the size counter and enable/disable the submit buttons based on
        whether the estimated result fits the client's max_result_bytes budget."""
        if not self._alive:
            return
        limit = self.session.max_result_bytes or DEFAULT_MAX_RESULT_BYTES
        body = self.body_text.get("1.0", tk.END).strip()
        est = estimate_result_bytes(body, self.attachments)
        over = est > limit
        c = self.theme_colors
        if over:
            self.size_label.config(
                text=(f"Submission size: {_human_bytes(est)} / {_human_bytes(limit)} limit — "
                      f"too large. Remove attachments or text to submit."),
                fg=c.get("error_color", c["fg_secondary"]))
        else:
            self.size_label.config(
                text=f"Submission size: {_human_bytes(est)} / {_human_bytes(limit)} limit",
                fg=c["fg_secondary"])
        state = "disabled" if over else "normal"
        for b in self._submit_buttons:
            try:
                b.configure(state=state)
            except Exception:
                pass

    def _make_readonly_text(self, parent, content, height):
        wrap, _ = build_readonly_text(parent, content, self.theme_colors, height=height)
        return wrap

    # ---------------- attachment handling ---------------- #
    def _add_attachments(self):
        try:
            paths = filedialog.askopenfilenames(parent=self.dialog, title="Attach files or images")
        except Exception:
            paths = ()
        for p in paths:
            if p and p not in self.attachments:
                self.attachments.append(p)
        self._refresh_attachments()

    def _remove_attachment(self):
        for idx in sorted(self.attach_listbox.curselection(), reverse=True):
            if 0 <= idx < len(self.attachments):
                del self.attachments[idx]
        self._refresh_attachments()

    def _refresh_attachments(self):
        if self.attach_listbox is not None:
            self.attach_listbox.delete(0, tk.END)
            for p in self.attachments:
                try:
                    sz = _human_bytes(os.path.getsize(p))
                except OSError:
                    sz = "?"
                self.attach_listbox.insert(tk.END, f"{os.path.basename(p)}  ({sz})")
        self._update_size_counter()

    # ---------------- liveness / countdown / watchdog ---------------- #
    def _render_status(self, remaining, over, idle):
        """Rich countdown/disconnect rendering into the (usually hidden) label."""
        if not self._alive:
            return
        c = self.theme_colors
        warn_c = c.get("error_color", c["fg_secondary"])
        opened = self.session.created_wall.strftime("%H:%M:%S")
        if remaining > self.WARN_SECONDS:
            self.countdown_label.config(
                text=f"Assistant connected · next check-in in {self._fmt(remaining)}   "
                     f"(task {self._tid8} · opened {opened})",
                fg=c["fg_secondary"])
        elif remaining > 0:
            self.countdown_label.config(
                text=f"Syncing shortly - you may pause a moment; your work is saved.   "
                     f"(task {self._tid8})",
                fg=c["fg_secondary"])
        elif over < DISCONNECT_GRACE_SECONDS:
            self.countdown_label.config(
                text=f"Waiting for the assistant to check in…   (task {self._tid8})",
                fg=c["fg_secondary"])
        else:
            left = AUTO_CLOSE_AFTER_SECONDS - over
            self.countdown_label.config(
                text=(f"The assistant may have disconnected (silent {int(idle)}s). "
                      f"Your work is safe - this window will auto-save & close in {self._fmt(left)} "
                      f"if it stays silent."),
                fg=warn_c)

    def _on_autoclose(self):
        """Assistant went silent for too long: archive whatever the human drafted
        (so it's never lost) and close the orphaned window."""
        if not self._alive:
            return
        body = self.body_text.get("1.0", tk.END).strip()
        paths = list(self.attachments)
        archived = None
        try:
            if body or paths:
                archived = archive_to_outbox(
                    {
                        "task_id": self.session.task_id,
                        "status": "disconnected_autosave",
                        "human_action": False,
                        "task_title": self.session.task_title,
                        "task_description": self.session.task_description,
                        "context_note": self.session.context_note,
                        "client_timeout_seconds": self.session.client_timeout_seconds,
                        "body": body,
                    },
                    paths,
                )
        except Exception:
            pass
        # Deliver a terminal result so any awaiting (or future) tool call cleans up.
        self.session.submit_from_ui({
            "status": "cancelled",
            "human_action": False,
            "reason": "assistant_disconnected",
            "body": body,
            "attachment_paths": paths,
            "terminal": True,
            "already_archived": True,
            "archived_dir": archived,
        })
        self.close()

    # ---------------- submit / close ---------------- #
    def _submit(self, status, terminal):
        """Any submit action (Completed/Failed/Still-progressing) is one complete
        'email'. It always CLOSES the window - the window's lifecycle is exactly
        one submission. The only difference between statuses is whether the task
        is terminated (`terminal`), not whether the window stays open."""
        if not self._alive:
            return
        body = self.body_text.get("1.0", tk.END).strip()
        paths = list(self.attachments)
        # Guard against accidental empty interim updates (the repeat-click trap).
        # Completed/Failed may legitimately carry no note; an interim update must not.
        if status == "in_progress" and not body and not paths:
            try:
                messagebox.showwarning(
                    "Nothing to send",
                    "Type a note or attach a file before sending an interim update.\n\n"
                    "Use Completed or Failed to finish the task instead.",
                    parent=self.dialog)
            except Exception:
                pass
            return
        # Defensive: buttons are disabled when over budget, but never send an
        # over-limit submission (the client would reject it).
        limit = self.session.max_result_bytes or DEFAULT_MAX_RESULT_BYTES
        if estimate_result_bytes(body, paths) > limit:
            try:
                messagebox.showwarning(
                    "Submission too large",
                    f"This submission is larger than the {_human_bytes(limit)} limit.\n\n"
                    "Remove attachments or shorten your note, then submit.",
                    parent=self.dialog)
            except Exception:
                pass
            return
        payload = {
            "status": status,
            "human_action": True,
            "body": body,
            "attachment_paths": paths,
            "terminal": terminal,
        }
        self.session.submit_from_ui(payload)
        self.close()  # instant, unambiguous feedback; prevents duplicate clicks

    def _on_close(self):
        if not self._alive:
            return
        payload = {
            "status": "cancelled",
            "human_action": True,
            "body": self.body_text.get("1.0", tk.END).strip(),
            "attachment_paths": list(self.attachments),
            "terminal": True,
        }
        self.session.submit_from_ui(payload)
        self.close()

    # close() and _center_window() come from _LegAwareWindow (self._top == self.dialog).

# MCP Tools

def _build_submission_result(session, payload, needs_continuation, archived_dir):
    """Turn a human submission into a mixed content-block list for the LLM.

    Returns ``[summary_text, *Image(...), *File(...)]``. Images the model can see;
    other files come back as embedded resources. Oversized attachments are not
    inlined - they are referenced by path (and are safe in the outbox archive).
    """
    status = payload.get("status", "in_progress")
    body = payload.get("body", "")
    paths = payload.get("attachment_paths", []) or []

    # Hard cap the encoded result to the client's budget so it can NEVER be
    # rejected as "too large" (which would lose the deliverable). Anything that
    # doesn't fit is referenced by path instead of inlined; it is still safe in
    # the outbox archive, and the AI can request a smaller re-submission.
    limit = getattr(session, "max_result_bytes", DEFAULT_MAX_RESULT_BYTES) or DEFAULT_MAX_RESULT_BYTES
    encoded_budget = max(0, limit - RESULT_OVERHEAD_BYTES - len((body or "").encode("utf-8")))

    images, files, manifest, encoded_used, any_omitted = [], [], [], 0, False
    for p in paths:
        name = os.path.basename(p)
        try:
            size = os.path.getsize(p)
            enc = _b64_len(size) + 256
            mime = mimetypes.guess_type(p)[0] or "application/octet-stream"
            ext = os.path.splitext(p)[1].lower()
            is_image = ext in IMAGE_EXTENSIONS or mime.startswith("image/")
            entry = {"name": name, "size_bytes": size, "mime": mime, "inlined": False}
            if (encoded_used + enc) <= encoded_budget:
                if is_image:
                    images.append(Image(path=p))
                else:
                    files.append(File(path=p, name=name))
                entry["inlined"] = True
                encoded_used += enc
            else:
                any_omitted = True
                entry["path_reference"] = p
                entry["note"] = ("omitted from the result to stay under max_result_bytes; "
                                 "the full file is saved in the outbox archive")
            manifest.append(entry)
        except Exception as e:
            manifest.append({"name": name, "error": str(e)})

    disconnected = payload.get("reason") == "assistant_disconnected"
    resubmittable = status in ("completed", "failed")
    summary = {
        "status": status,
        "task_id": session.task_id,
        "human_action": payload.get("human_action", True),
        "needs_continuation": needs_continuation,
        "resubmittable": resubmittable,
        "task_title": session.task_title,
        "body": body,
        "attachments": manifest,
        "attachments_omitted_for_size": any_omitted,
        "outbox_entry": archived_dir,
        "platform": CURRENT_PLATFORM,
    }
    if payload.get("reason"):
        summary["reason"] = payload["reason"]
    if getattr(session, "advisory", None):
        summary["advisory"] = session.advisory

    review = (" Review the deliverable. The session is kept open for review: if it is insufficient "
              "(or an attachment was omitted for size — see attachments_omitted_for_size), you MAY call "
              "assign_task_to_human again with the SAME task_id and an updated task_description (e.g. "
              "asking for a smaller attachment) to request a re-submission. If satisfied, you are done.")
    headers = {
        "completed": "The human reports the task is COMPLETED." + review,
        "failed": "The human reports the task FAILED." + review,
        "in_progress": ("The human sent an INTERIM update and the task window has CLOSED; the task is "
                        "NOT finished. Process this update (they may be asking or discussing something). "
                        "YOU decide the next step: if the human should keep working, call "
                        "assign_task_to_human again with the SAME task_id to reopen a fresh window "
                        "(optionally put your reply to them in context_note); otherwise finish normally."),
        "cancelled": "The human DISMISSED / cancelled the task dialog.",
    }
    header = headers.get(status, "Human submission.")
    if disconnected:
        header = ("The task window auto-closed because it detected the assistant had stopped checking in. "
                  "Any draft the human had typed was auto-saved to the outbox and is included below.")
    summary_text = header + "\n\n" + json.dumps(summary, ensure_ascii=False, indent=2)
    return [summary_text, *images, *files]


def _keepalive_resp(status, session, message):
    """A heartbeat/opened keepalive return (the AI should re-call, not reply)."""
    resp = {
        "success": True,
        "status": status,
        "human_action": False,
        "needs_continuation": True,
        "interaction_id": session.id,
        "task_id": session.id,
        "platform": CURRENT_PLATFORM,
        "message": message,
    }
    if session.advisory:
        resp["advisory"] = session.advisory
    return resp


def _simple_terminal(session, payload, result_builder):
    """Terminal handling shared by the simple (one-shot) dialogs: drop the session
    and build the tool-specific result dict from the payload."""
    session.dialog = None
    _sessions.pop(session.id, None)
    return result_builder(session, payload)


def _task_on_submit(session, payload):
    """assign_task_to_human's rich submission handling (archive + in_progress /
    completed-review / terminal)."""
    status = payload.get("status", "in_progress")
    terminal = payload.get("terminal", status in ("completed", "failed", "cancelled"))
    if payload.get("already_archived"):
        archived_dir = payload.get("archived_dir")
    elif payload.get("body") or payload.get("attachment_paths"):
        archived_dir = archive_to_outbox(
            {
                "task_id": session.task_id,
                "status": status,
                "human_action": payload.get("human_action", True),
                "task_title": session.task_title,
                "task_description": session.task_description,
                "context_note": session.context_note,
                "client_timeout_seconds": session.client_timeout_seconds,
                "body": payload.get("body", ""),
            },
            payload.get("attachment_paths", []),
        )
    else:
        archived_dir = None

    session.dialog = None
    session.last_seen = time.monotonic()
    if payload.get("human_action", True):
        session.last_human_action_at = session.last_seen

    if status in ("completed", "failed"):
        # Keep the session dormant for AI review / re-submission.
        return _build_submission_result(session, payload, needs_continuation=False, archived_dir=archived_dir)
    if terminal:
        _sessions.pop(session.id, None)
        return _build_submission_result(session, payload, needs_continuation=False, archived_dir=archived_dir)
    # in_progress: dormant, may reopen with the same task_id.
    return _build_submission_result(session, payload, needs_continuation=True, archived_dir=archived_dir)


def _task_on_decline(session, payload):
    """assign_task_to_human: Cancel on the notification (or disconnect) == FAILED."""
    session.dialog = None
    _sessions.pop(session.id, None)
    disc = payload.get("reason") == "assistant_disconnected"
    return {
        "success": True,
        "status": "failed",
        "human_action": not disc,
        "needs_continuation": False,
        "task_id": session.id,
        "interaction_id": session.id,
        "reason": payload.get("reason", "declined_via_notification"),
        "message": ("The human declined the task from the notification popup; treat the task as FAILED."
                    if not disc else
                    "The notification auto-closed because you had gone silent; the task was not accepted."),
        "platform": CURRENT_PLATFORM,
    }


async def _run_interaction(*, kind, params, client_timeout_seconds, session_id, ctx,
                           make_body, on_submit, on_decline,
                           max_result_bytes=DEFAULT_MAX_RESULT_BYTES, attachments_enabled=True,
                           advisory=None, tool_name="tool"):
    """Shared notification + ring + heartbeat + disconnect-watchdog orchestration for
    every interactive tool. New interactions ring an always-on-top notification
    toast; the human clicks View to open the actual dialog; the wait is kept alive
    across the client's timeout via heartbeats. `on_submit`/`on_decline` are the
    tool-specific terminal handlers."""
    if not ensure_gui_initialized():
        return {"success": False, "error": "GUI system not available",
                "needs_continuation": False, "platform": CURRENT_PLATFORM}

    loop = asyncio.get_running_loop()
    leg_seconds = compute_leg_seconds(client_timeout_seconds)

    # Reap abandoned/dormant sessions (window closed, AI never re-called).
    for sid, sess in list(_sessions.items()):
        if sess.is_expired():
            _dialog_runner.submit(lambda root, s=sess: s.close())
            _sessions.pop(sid, None)

    session = _sessions.get(session_id) if session_id else None
    if session_id and session is None:
        return {"success": False, "status": "session_not_found",
                "interaction_id": session_id, "task_id": session_id, "needs_continuation": False,
                "message": ("No such id (it was answered/cancelled, or the session expired after "
                            "inactivity). Start again by calling without an id."),
                "platform": CURRENT_PLATFORM}

    if session is None:
        # Brand-new: create the session and RING the non-focus-stealing toast.
        new_id = uuid.uuid4().hex[:12]
        session = InteractionSession(new_id, kind, params, client_timeout_seconds, loop, make_body,
                                     max_result_bytes=max_result_bytes,
                                     attachments_enabled=attachments_enabled)
        session.advisory = advisory
        await _dialog_runner.run_dialog(
            lambda root: session.attach_dialog(NotificationWindow(root, session)), timeout=30)
        _sessions[new_id] = session
        if ctx:
            await ctx.info(f"Notified human ({kind}, id={new_id})")
    elif session.dialog is None and session.updates.empty():
        # Continuation whose window closed (task in_progress dormancy). Refresh
        # params; re-notify after a long gap, else reopen the dialog directly.
        session.params = dict(params or {})
        session.task_title = session.params.get("task_title") or session.params.get("title", "")
        session.task_description = session.params.get("task_description", "")
        session.context_note = session.params.get("context_note", "")
        session.client_timeout_seconds = client_timeout_seconds
        session.max_result_bytes = max_result_bytes
        session.attachments_enabled = attachments_enabled
        session.make_body = make_body
        session.advisory = advisory
        idle = time.monotonic() - session.last_human_action_at
        if idle > FOLLOW_UP_NOTIFY_AFTER_SECONDS:
            await _dialog_runner.run_dialog(
                lambda root: session.attach_dialog(NotificationWindow(root, session)), timeout=30)
        else:
            await _dialog_runner.run_dialog(
                lambda root: session.attach_dialog(session.make_body(root, session)), timeout=30)
    # else: a payload is already buffered (window auto-closed) — deliver it below.

    # Start / refresh this leg's countdown (only if a window is open).
    if session.dialog is not None:
        _dialog_runner.submit(lambda root: session.begin_leg(leg_seconds))

    # Await a human response OR the heartbeat deadline.
    try:
        payload = await asyncio.wait_for(session.updates.get(), timeout=leg_seconds)
    except asyncio.TimeoutError:
        if session.is_expired():
            _dialog_runner.submit(lambda root: session.close())
            _sessions.pop(session.id, None)
            return {"success": False, "status": "expired", "interaction_id": session.id,
                    "task_id": session.id, "needs_continuation": False,
                    "message": "The dialog exceeded its max lifetime and was closed.",
                    "platform": CURRENT_PLATFORM}
        return _keepalive_resp(
            "heartbeat", session,
            "Keepalive only - the human hasn't responded yet. Do NOT respond to the user or reason about "
            f"content; immediately call {tool_name} again with the same id to keep the dialog alive.")

    kind_ev = payload.get("kind")
    if kind_ev == "view":
        return _keepalive_resp(
            "opened", session,
            f"The human opened the dialog. Keep waiting - call {tool_name} again with the same id and do "
            "NOT respond to the user in the meantime.")
    if kind_ev == "decline":
        return on_decline(session, payload)
    return on_submit(session, payload)


@mcp.tool()
async def assign_task_to_human(
    task_title: Annotated[str, Field(description="Short imperative title of the ONE action (e.g. 'Photograph the north wall'). Not a question.")],
    task_description: Annotated[str, Field(description="A SINGLE, atomic, actionable instruction for one action the assistant cannot do itself and must delegate to a human (a physical-world action is the common case). This is a work order, not a chat message: NOT a question, NOT a list of multiple steps, NOT background+questions+a to-do list. If the work has several steps, assign them as separate sequential tasks instead.")],
    task_id: Annotated[Optional[str], Field(description="Leave empty to START a new task. To CONTINUE an existing task (after a 'heartbeat' response or an 'in_progress' update), pass back the task_id from the previous call - this revives the same open window without losing the human's work.")] = None,
    client_timeout_seconds: Annotated[Optional[int], Field(description="YOUR OWN per-tool-call timeout, in seconds. The server returns a 'heartbeat' response safely before this deadline so you can immediately re-call and keep the human's dialog alive. If omitted, the server's configured default is used.")] = None,
    max_result_bytes: Annotated[Optional[int], Field(description="YOUR CLIENT'S max tool-result size, in BYTES. The human sees a live size counter and cannot submit text+attachments whose encoded size would exceed this, so a large deliverable is never rejected/lost. If omitted, the server's configured default is used.")] = None,
    context_note: Annotated[str, Field(description="Optional extra context to show the human in the dialog")] = "",
    ctx: Context = None,
) -> Any:
    """
    Delegate to a human ONE atomic action that the assistant CANNOT do itself, and wait for
    their report of whether it was done (with an optional note and file/image attachments). A
    physical-world action is the common case, but the defining trait is simply that YOU can't
    do it — anything that requires a person (their hands, presence, judgment, credentials, or
    access you lack) qualifies.

    SCOPE — use this tool ONLY for a single action you cannot do yourself, and read these
    three rules; they are the most common ways this tool is misused:
      1. NOT for questions / information requests. Do NOT use assign_task_to_human to ask
         the human anything (a date, a status, a preference, a fact). Its report describes
         TASK EXECUTION, not answers to your questions. To ask, use `get_user_input`,
         `get_multiline_input`, or `get_user_choice` instead.
      2. ONE atomic, indivisible action per task. Do NOT bundle multiple steps (e.g.
         "survey the site AND take photos AND check the soil AND count inventory") into one
         call. Assign steps as SEPARATE sequential tasks, adjusting each based on the
         previous report — step-by-step direction is the whole point of this tool.
      3. A work order, not a chat message. Keep `task_description` to a single actionable
         instruction. Dumping background + several questions + a checklist defeats the
         structured task→execution→report loop and is equivalent to sending a chat message.

    IMPORTANT - window lifecycle & heartbeat protocol (read carefully):
    - The flow starts with a small, non-focus-stealing notification toast that RINGS in the
      corner (it does not interrupt the human's current work). They click View to open the
      task window, or Cancel to decline.
    - `status: "opened"` (human_action=false): the human clicked View and the task window is
      now open. Like a heartbeat - do NOT respond to the user; just call again with the same
      `task_id` to keep waiting for their report.
    - One window == one submission. Whenever the human clicks a button
      (Completed / Failed / Still-progressing), the window CLOSES and you receive their
      note + attachments. The ONLY difference between the buttons is whether the task is
      finished, not whether the window stays open.
    - Always pass `client_timeout_seconds` (your own per-call timeout). The server returns
      BEFORE that deadline so your client doesn't kill the call.
    - `status: "heartbeat"` (human_action=false): the human has NOT submitted anything yet.
      Do NOT reply to the user and do NOT reason about the task - just call this tool AGAIN
      immediately with the SAME `task_id`. Pure keepalive; the SAME window stays open with
      their work intact (a countdown warns them a few seconds before each sync).
    - `status: "in_progress"` (human_action=true): the human sent an interim update and the
      window has CLOSED; the task is not finished. Process the update, then DECIDE: to let
      them keep working, call again with the SAME `task_id` to reopen a fresh window
      (optionally reply via `context_note`); otherwise just finish.
    - `status: "completed"` / `"failed"` WITH `resubmittable: true` (human_action=true): the
      human's report from the task window. The session is kept open for your REVIEW — NOT
      destroyed. If the deliverable is insufficient, or `attachments_omitted_for_size` is true
      (an attachment was too big to inline under `max_result_bytes` and was referenced by
      path / saved to the outbox), you MAY call again with the SAME `task_id` and an updated
      `task_description` (e.g. "please attach a smaller photo") to request a re-submission.
      If satisfied, simply stop — the session is reaped automatically.
    - Terminal outcomes (needs_continuation=false, task_id is gone, NOT resubmittable) — check
      `status` + `reason`:
        * `status: "failed"`, `reason: "declined_via_notification"` — the human clicked Cancel
          on the notification (declined the task; distinct from a task-window "Failed" report,
          which is resubmittable per above).
        * `status: "cancelled"` (no reason) — the human closed the task window without reporting.
        * `reason: "assistant_disconnected"` (status "failed" from the notification or
          "cancelled" from the task window) — the dialog auto-closed after you went silent; any
          draft the human had typed is auto-saved to the outbox and included.
    - Pass `max_result_bytes` = your client's max tool-result size. The human sees a live size
      counter and cannot submit anything whose encoded size would exceed it, and the server
      hard-caps the result to that budget — so a deliverable is never rejected as "too large".
    - Every human submission (note + attachments + your command) is also archived to a
      local "outbox" that can be browsed later in the Management Console (management_console.py).
    """
    try:
        if ctx:
            await ctx.info(f"assign_task_to_human: '{task_title}' (task_id={task_id or 'new'})")
        _defaults = human_loop_config.get_task_defaults()
        if client_timeout_seconds is None:
            client_timeout_seconds = int(_defaults.get("timeout_seconds", 240))
        if max_result_bytes is None:
            max_result_bytes = int(_defaults.get("max_result_bytes", DEFAULT_MAX_RESULT_BYTES))
        attachments_enabled = bool(_defaults.get("attachments_enabled", True))
        params = {
            "task_title": task_title,
            "task_description": task_description,
            "context_note": context_note,
            "notify_heading": "You have a new task",
            "notify_preview": task_title,
        }
        return await _run_interaction(
            kind="task", params=params, client_timeout_seconds=client_timeout_seconds,
            session_id=task_id, ctx=ctx,
            make_body=lambda root, s: HumanTaskDialog(root, s),
            on_submit=_task_on_submit, on_decline=_task_on_decline,
            max_result_bytes=max_result_bytes, attachments_enabled=attachments_enabled,
            advisory=composite_task_advisory(task_description),
            tool_name="assign_task_to_human")
    except Exception as e:
        if ctx:
            await ctx.error(f"Error in assign_task_to_human: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "needs_continuation": False,
            "platform": CURRENT_PLATFORM,
        }


# --- Heartbeat protocol shared by the simple interactive tools below ---------
# All five tools ring an always-on-top notification, wait across the client's
# timeout via heartbeats, and reuse _run_interaction. Each supplies a result_builder
# that maps the dialog's {"value"}/{"cancelled"} payload to its own legacy result dict.
_HEARTBEAT_TIMEOUT_FIELD = Annotated[Optional[int], Field(description=(
    "YOUR OWN per-tool-call timeout, in seconds. The server returns a 'heartbeat'/'opened' "
    "response safely before this deadline so you can immediately re-call and keep the dialog "
    "alive. If omitted, the server's configured default is used."))]
_INTERACTION_ID_FIELD = Annotated[Optional[str], Field(description=(
    "Leave empty to START. On a 'heartbeat' or 'opened' response, re-call with the "
    "interaction_id from the previous call to keep the SAME dialog alive (do NOT respond to "
    "the user in between)."))]


def _simple_timeout_default(client_timeout_seconds):
    if client_timeout_seconds is None:
        return int(human_loop_config.get_task_defaults().get("timeout_seconds", 240))
    return client_timeout_seconds


@mcp.tool()
async def get_user_input(
    title: Annotated[str, Field(description="Title of the input dialog window")],
    prompt: Annotated[str, Field(description="The single focused question to show the user")],
    default_value: Annotated[str, Field(description="Default value to pre-fill in the input field")] = "",
    input_type: Annotated[Literal["text", "integer", "float"], Field(description="Type of input expected")] = "text",
    client_timeout_seconds: _HEARTBEAT_TIMEOUT_FIELD = None,
    interaction_id: _INTERACTION_ID_FIELD = None,
    ctx: Context = None,
) -> Dict[str, Any]:
    """
    Ask the user for ONE piece of single-line text/number input.

    Ask a SINGLE focused question, then use the answer to decide what to ask next -
    do NOT pack several questions into one `prompt`. If you need multiple pieces of
    information, call this tool once per question in sequence, adapting as you go.
    (`get_user_choice` for picking from options; `get_multiline_input` for long text.)

    Window lifecycle & heartbeat: this rings an always-on-top notification toast; the
    human clicks View to open the input dialog. Pass `client_timeout_seconds`; on a
    'heartbeat' or 'opened' status re-call with the returned `interaction_id` and do NOT
    reply to the user until they submit or cancel.
    """
    try:
        if ctx:
            await ctx.info(f"get_user_input: {prompt}")
        client_timeout_seconds = _simple_timeout_default(client_timeout_seconds)
        params = {"title": title, "prompt": prompt, "default_value": default_value,
                  "input_type": input_type,
                  "notify_heading": "The assistant needs some input", "notify_preview": prompt or title}

        def rb(session, payload):
            if payload.get("cancelled") or payload.get("kind") == "decline" or payload.get("value") is None:
                return {"success": False, "user_input": None, "input_type": input_type,
                        "cancelled": True, "interaction_id": session.id,
                        "needs_continuation": False, "platform": CURRENT_PLATFORM}
            return {"success": True, "user_input": payload.get("value"), "input_type": input_type,
                    "cancelled": False, "interaction_id": session.id,
                    "needs_continuation": False, "platform": CURRENT_PLATFORM}

        return await _run_interaction(
            kind="input", params=params, client_timeout_seconds=client_timeout_seconds,
            session_id=interaction_id, ctx=ctx,
            make_body=lambda root, s: InputDialog(root, s),
            on_submit=lambda s, p: _simple_terminal(s, p, rb),
            on_decline=lambda s, p: _simple_terminal(s, p, rb),
            tool_name="get_user_input")
    except Exception as e:
        if ctx:
            await ctx.error(f"Error in get_user_input: {str(e)}")
        return {"success": False, "error": str(e), "cancelled": False,
                "needs_continuation": False, "platform": CURRENT_PLATFORM}


@mcp.tool()
async def get_user_choice(
    title: Annotated[str, Field(description="Title of the choice dialog window")],
    prompt: Annotated[str, Field(description="The prompt/question to show to the user")],
    choices: Annotated[List[str], Field(description="List of choices to present to the user")],
    allow_multiple: Annotated[bool, Field(description="Whether user can select multiple choices")] = False,
    client_timeout_seconds: _HEARTBEAT_TIMEOUT_FIELD = None,
    interaction_id: _INTERACTION_ID_FIELD = None,
    ctx: Context = None,
) -> Dict[str, Any]:
    """
    Ask the user to pick from options for ONE decision.

    Present a SINGLE choice, then adapt based on what they pick - do NOT chain several
    unrelated questions into one prompt. For a sequence of decisions, call this once per
    decision, using each answer to shape the next.

    Window lifecycle & heartbeat: this rings an always-on-top notification toast; the
    human clicks View to open the dialog. Pass `client_timeout_seconds`; on a 'heartbeat' or
    'opened' status re-call with the returned `interaction_id` and do NOT reply to the user
    until they submit or cancel.
    """
    try:
        if ctx:
            await ctx.info(f"get_user_choice: {prompt}")
        client_timeout_seconds = _simple_timeout_default(client_timeout_seconds)
        params = {"title": title, "prompt": prompt, "choices": choices,
                  "allow_multiple": allow_multiple,
                  "notify_heading": "The assistant needs you to choose", "notify_preview": prompt or title}

        def rb(session, payload):
            if payload.get("cancelled") or payload.get("kind") == "decline" or payload.get("value") is None:
                return {"success": False, "selected_choice": None, "selected_choices": [],
                        "allow_multiple": allow_multiple, "cancelled": True,
                        "interaction_id": session.id, "needs_continuation": False,
                        "platform": CURRENT_PLATFORM}
            val = payload.get("value")
            return {"success": True, "selected_choice": val,
                    "selected_choices": val if isinstance(val, list) else [val],
                    "allow_multiple": allow_multiple, "cancelled": False,
                    "interaction_id": session.id, "needs_continuation": False,
                    "platform": CURRENT_PLATFORM}

        return await _run_interaction(
            kind="choice", params=params, client_timeout_seconds=client_timeout_seconds,
            session_id=interaction_id, ctx=ctx,
            make_body=lambda root, s: ChoiceBody(root, s),
            on_submit=lambda s, p: _simple_terminal(s, p, rb),
            on_decline=lambda s, p: _simple_terminal(s, p, rb),
            tool_name="get_user_choice")
    except Exception as e:
        if ctx:
            await ctx.error(f"Error in get_user_choice: {str(e)}")
        return {"success": False, "error": str(e), "cancelled": False,
                "needs_continuation": False, "platform": CURRENT_PLATFORM}


@mcp.tool()
async def get_multiline_input(
    title: Annotated[str, Field(description="Title of the input dialog window")],
    prompt: Annotated[str, Field(description="The prompt/question to show to the user")],
    default_value: Annotated[str, Field(description="Default text to pre-fill in the text area")] = "",
    client_timeout_seconds: _HEARTBEAT_TIMEOUT_FIELD = None,
    interaction_id: _INTERACTION_ID_FIELD = None,
    ctx: Context = None,
) -> Dict[str, Any]:
    """
    Ask the user for ONE piece of long-form text (a description, a document, code, etc.).

    This is for a single open-ended answer that needs room to type - NOT a questionnaire.
    Do NOT list several numbered questions in one `prompt` expecting them all answered in
    one box; ask one focused question, read the answer, then ask the next and adapt.

    Window lifecycle & heartbeat: this rings an always-on-top notification toast; the
    human clicks View to open the dialog. Pass `client_timeout_seconds`; on a 'heartbeat' or
    'opened' status re-call with the returned `interaction_id` and do NOT reply to the user
    until they submit or cancel.
    """
    try:
        if ctx:
            await ctx.info(f"get_multiline_input: {prompt}")
        client_timeout_seconds = _simple_timeout_default(client_timeout_seconds)
        params = {"title": title, "prompt": prompt, "default_value": default_value,
                  "notify_heading": "The assistant needs some input", "notify_preview": prompt or title}

        def rb(session, payload):
            if payload.get("cancelled") or payload.get("kind") == "decline" or payload.get("value") is None:
                return {"success": False, "user_input": None, "cancelled": True,
                        "interaction_id": session.id, "needs_continuation": False,
                        "platform": CURRENT_PLATFORM}
            v = payload.get("value") or ""
            return {"success": True, "user_input": v, "character_count": len(v),
                    "line_count": len(v.split("\n")), "cancelled": False,
                    "interaction_id": session.id, "needs_continuation": False,
                    "platform": CURRENT_PLATFORM}

        return await _run_interaction(
            kind="multiline", params=params, client_timeout_seconds=client_timeout_seconds,
            session_id=interaction_id, ctx=ctx,
            make_body=lambda root, s: MultilineBody(root, s),
            on_submit=lambda s, p: _simple_terminal(s, p, rb),
            on_decline=lambda s, p: _simple_terminal(s, p, rb),
            tool_name="get_multiline_input")
    except Exception as e:
        if ctx:
            await ctx.error(f"Error in get_multiline_input: {str(e)}")
        return {"success": False, "error": str(e), "cancelled": False,
                "needs_continuation": False, "platform": CURRENT_PLATFORM}


@mcp.tool()
async def show_confirmation_dialog(
    title: Annotated[str, Field(description="Title of the confirmation dialog")],
    message: Annotated[str, Field(description="The message to show to the user")],
    client_timeout_seconds: _HEARTBEAT_TIMEOUT_FIELD = None,
    interaction_id: _INTERACTION_ID_FIELD = None,
    ctx: Context = None,
) -> Dict[str, Any]:
    """
    Show a confirmation dialog with Yes/No buttons.

    Displays a message and asks for confirmation - perfect for getting approval before
    proceeding with an action.

    Window lifecycle & heartbeat: this rings an always-on-top notification toast; the
    human clicks View to open the dialog. Pass `client_timeout_seconds`; on a 'heartbeat' or
    'opened' status re-call with the returned `interaction_id` and do NOT reply to the user
    until they answer.
    """
    try:
        if ctx:
            await ctx.info(f"show_confirmation_dialog: {message}")
        client_timeout_seconds = _simple_timeout_default(client_timeout_seconds)
        params = {"title": title, "prompt": message,
                  "notify_heading": "The assistant needs your confirmation",
                  "notify_preview": message or title}

        def rb(session, payload):
            if payload.get("kind") == "decline":
                return {"success": True, "confirmed": False, "response": "no", "cancelled": True,
                        "interaction_id": session.id, "needs_continuation": False,
                        "platform": CURRENT_PLATFORM}
            val = bool(payload.get("value"))
            return {"success": True, "confirmed": val, "response": "yes" if val else "no",
                    "interaction_id": session.id, "needs_continuation": False,
                    "platform": CURRENT_PLATFORM}

        return await _run_interaction(
            kind="confirm", params=params, client_timeout_seconds=client_timeout_seconds,
            session_id=interaction_id, ctx=ctx,
            make_body=lambda root, s: ConfirmBody(root, s),
            on_submit=lambda s, p: _simple_terminal(s, p, rb),
            on_decline=lambda s, p: _simple_terminal(s, p, rb),
            tool_name="show_confirmation_dialog")
    except Exception as e:
        if ctx:
            await ctx.error(f"Error in show_confirmation_dialog: {str(e)}")
        return {"success": False, "error": str(e), "confirmed": False,
                "needs_continuation": False, "platform": CURRENT_PLATFORM}


@mcp.tool()
async def show_info_message(
    title: Annotated[str, Field(description="Title of the information dialog")],
    message: Annotated[str, Field(description="The information message to show to the user")],
    client_timeout_seconds: _HEARTBEAT_TIMEOUT_FIELD = None,
    interaction_id: _INTERACTION_ID_FIELD = None,
    ctx: Context = None,
) -> Dict[str, Any]:
    """
    Show an information message to the user.

    Displays an informational message dialog to notify the user about something; the user
    just clicks OK to acknowledge.

    Window lifecycle & heartbeat: this rings an always-on-top notification toast; the
    human clicks View to open the message. Pass `client_timeout_seconds`; on a 'heartbeat' or
    'opened' status re-call with the returned `interaction_id` and do NOT reply to the user
    until they acknowledge (or cancel the notification).
    """
    try:
        if ctx:
            await ctx.info(f"show_info_message: {message}")
        client_timeout_seconds = _simple_timeout_default(client_timeout_seconds)
        params = {"title": title, "prompt": message,
                  "notify_heading": "The assistant has an update",
                  "notify_preview": message or title}

        def rb(session, payload):
            acknowledged = payload.get("kind") != "decline"
            return {"success": True, "acknowledged": acknowledged,
                    "interaction_id": session.id, "needs_continuation": False,
                    "platform": CURRENT_PLATFORM}

        return await _run_interaction(
            kind="info", params=params, client_timeout_seconds=client_timeout_seconds,
            session_id=interaction_id, ctx=ctx,
            make_body=lambda root, s: InfoBody(root, s),
            on_submit=lambda s, p: _simple_terminal(s, p, rb),
            on_decline=lambda s, p: _simple_terminal(s, p, rb),
            tool_name="show_info_message")
    except Exception as e:
        if ctx:
            await ctx.error(f"Error in show_info_message: {str(e)}")
        return {"success": False, "error": str(e),
                "needs_continuation": False, "platform": CURRENT_PLATFORM}

@mcp.tool(description=(
    "Returns the human operator's profile (name, role, responsibilities, and how they "
    "want you to communicate) so you know WHO you are assisting. "
    + _format_operator_profile()
))
async def get_operator_profile() -> Dict[str, Any]:
    """Read the current operator profile from the server's config."""
    p = human_loop_config.get_profile()
    return {
        "name": (p.get("name") or "").strip(),
        "role": (p.get("role") or "").strip(),
        "responsibilities": (p.get("responsibilities") or "").strip(),
        "communication": (p.get("communication") or "").strip(),
        "summary": _format_operator_profile(),
        "platform": CURRENT_PLATFORM,
    }


# Add a prompt to get prompting guidance for LLMs
@mcp.prompt()
async def get_human_loop_prompt() -> Dict[str, str]:
    """
    Get prompting guidance for LLMs on when and how to use human-in-the-loop tools.
    
    This tool returns comprehensive guidance that helps LLMs understand when to pause
    and ask for human input, decisions, or feedback during task execution.
    """
    guidance = {
        "main_prompt": """
You have access to Human-in-the-Loop tools that allow you to interact directly with users through GUI dialogs. Use these tools strategically to enhance task completion and user experience.

**CORE PRINCIPLE — ASK ONE THING AT A TIME.** Do NOT front-load a big questionnaire (e.g. one dialog with 5 numbered questions and parenthetical explanations). Ask a SINGLE focused question, use the answer to decide the next one, and continue interactively. One dialog = one question. This applies to every input/choice/confirmation dialog, not just tasks: cramming multiple questions into one prompt is the information-gathering version of bundling multiple steps into one task — it removes your ability to adapt and reads like a chat message. Only combine fields when they are genuinely one inseparable unit (e.g. a start date AND end date of one range).

**WHEN TO USE HUMAN-IN-THE-LOOP TOOLS:**

1. **Ambiguous Requirements** - When user instructions are unclear or could have multiple interpretations
2. **Decision Points** - When you need user preference between valid alternatives
3. **Creative Input** - For subjective choices like design, content style, or personal preferences
4. **Sensitive Operations** - Before executing potentially destructive or irreversible actions
5. **Missing Information** - When you need specific details not provided in the original request
6. **Quality Feedback** - To get user validation on intermediate results before proceeding
7. **Error Handling** - When encountering issues that require user guidance to resolve

**AVAILABLE TOOLS:**
- `get_user_input` - Single-line text/number input for ONE focused question (names, values, paths, etc.)
- `get_user_choice` - Multiple choice selection for ONE decision (pick from options)
- `get_multiline_input` - Long-form text for ONE open-ended answer (a description, code, a document) — not a multi-question form
- `show_confirmation_dialog` - Yes/No decisions (confirmations, approvals)
- `show_info_message` - Status updates and notifications
- `get_operator_profile` - Read WHO you are assisting (the human operator's name, role, and communication preferences)

**ALL INTERACTIVE TOOLS SHARE ONE HEARTBEAT PROTOCOL:** Every tool above (and `assign_task_to_human`) first rings a small, always-on-top notification toast in the corner (View / Cancel), then waits with a heartbeat so a slow human never trips your tool-call timeout. Always pass your own `client_timeout_seconds`. When a call returns `status: "heartbeat"` or `status: "opened"`, do NOT respond to the user or reason about content — immediately re-call the SAME tool with the returned `interaction_id` (for `assign_task_to_human`, `task_id`) to keep the same dialog alive. The human's final answer comes back with `needs_continuation: false` and the tool's usual fields (`user_input` / `selected_choice` / `confirmed` / `acknowledged`). Cancel on the notification returns the tool's cancelled/declined result.

- `assign_task_to_human` - Delegate ONE atomic action the assistant CANNOT do itself (must be handed to a human — a physical-world action is the common case) and wait (long-running) for the human to report Completed/Failed/Still-progressing with an optional note and file/image attachments. Its report describes task execution, so: do NOT use it to ask questions or request information (use the input/choice tools for that), do NOT bundle multiple steps into one task (assign them as separate sequential tasks, adjusting each from the previous report), and keep `task_description` to a single actionable instruction (it's a work order, not a chat message). It first shows an always-on-top ringing notification (View/Cancel). Uses a heartbeat protocol: always pass your own `client_timeout_seconds` and `max_result_bytes` (your client's tool-result size limit); when you get `status: "heartbeat"` or `status: "opened"` re-call with the same `task_id` (do not respond to the user in between). Completed/failed keep the session open for your review — re-call with the same `task_id` and updated `task_description` to request a re-submission (e.g. a smaller attachment). Cancel on the notification returns `status: "failed"`. Submissions are archived to a local outbox.

**BEST PRACTICES:**
- Ask ONE focused question per dialog; let the answer shape the next question (do NOT front-load a multi-question form)
- Ask specific, clear questions with context
- Provide helpful default values when possible
- Use confirmation dialogs before destructive actions
- Give status updates for long-running processes
- Offer meaningful choices rather than overwhelming options
- Be concise but informative in dialog prompts""",
        
        "usage_examples": """
**EXAMPLE SCENARIOS:**

1. **File Operations:**
   - "I'm about to delete 15 files. Should I proceed?" (confirmation)
   - "Enter the target directory path:" (input)
   - "Choose backup format: Full, Incremental, Differential" (choice)

2. **Content Creation:**
   - "What tone should I use: Professional, Casual, Friendly?" (choice)
   - "Please provide any specific requirements:" (multiline input)
   - "Content generated successfully!" (info message)

3. **Code Development:**
   - "Enter the API endpoint URL:" (input)
   - "Select framework: React, Vue, Angular, Vanilla JS" (choice)
   - "Review the generated code and provide feedback:" (multiline input)

4. **Data Processing:**
   - "Found 3 data formats. Which should I use?" (choice)
   - "Enter the date range (YYYY-MM-DD to YYYY-MM-DD):" (input)
   - "Processing complete. 1,250 records updated." (info message)""",
        
        "decision_framework": """
**DECISION FRAMEWORK FOR HUMAN-IN-THE-LOOP:**

ASK YOURSELF:
1. Is this decision subjective or preference-based? → USE CHOICE DIALOG
2. Do I need specific information not provided? → USE INPUT DIALOG
3. Could this action cause problems if wrong? → USE CONFIRMATION DIALOG
4. Is this a long process the user should know about? → USE INFO MESSAGE
5. Do I need detailed explanation or content? → USE MULTILINE INPUT
6. Is there ONE action you CANNOT do yourself that a human must carry out (a physical-world action, or anything needing a person)? → USE assign_task_to_human
   - Asking a question is NOT a task — use the input/choice tools.
   - Several steps are NOT one task — assign them one at a time, sequentially.

AVOID OVERUSE:
- Don't ask for information already provided
- Don't seek confirmation for obviously safe operations
- Don't interrupt flow for trivial decisions
- Don't front-load a questionnaire — ask one focused question at a time and adapt from each answer

OPTIMIZE FOR USER EXPERIENCE:
- Ask incrementally: one question → use the answer → the next question. This beats one giant multi-question dialog and lets you adapt.
- Provide context for why you need the information
- Offer sensible defaults and suggestions
- Make dialogs self-explanatory and actionable""",
        
        "integration_tips": """
**INTEGRATION TIPS:**

1. **Workflow Integration:**
   ```
   Step 1: Analyze user request
   Step 2: Identify decision points and missing info
   Step 3: Use appropriate human-in-the-loop tools
   Step 4: Process user responses
   Step 5: Continue with enhanced information
   ```

2. **Error Recovery:**
   - If user cancels, gracefully explain and offer alternatives
   - Handle timeouts by providing default behavior
   - Always validate user input before proceeding

3. **Progressive Enhancement:**
   - Start with automated solutions
   - Add human input only where it adds clear value
   - Learn from user patterns to improve future automation

4. **Communication:**
   - Explain why you need user input
   - Show progress and intermediate results
   - Confirm successful completion of user-guided actions"""
    }

    # Prepend who the operator is so the assistant knows who it's serving.
    profile_block = _format_operator_profile()
    guidance["operator_profile"] = profile_block
    guidance["main_prompt"] = profile_block + "\n" + guidance["main_prompt"]

    return guidance

# Add a health check tool
@mcp.tool()
async def health_check() -> Dict[str, Any]:
    """Check if the Human-in-the-Loop server is running and GUI is available."""
    try:
        gui_available = ensure_gui_initialized()
        
        return {
            "status": "healthy" if gui_available else "degraded",
            "gui_available": gui_available,
            "server_name": "Human-in-the-Loop Server",
            "platform": CURRENT_PLATFORM,
            "platform_details": {
                "system": platform.system(),
                "release": platform.release(),
                "version": platform.version(),
                "machine": platform.machine(),
                "processor": platform.processor()
            },
            "python_version": sys.version.split()[0],
            "is_windows": IS_WINDOWS,
            "is_macos": IS_MACOS,
            "is_linux": IS_LINUX,
            "tools_available": [
                "get_user_input",
                "get_user_choice",
                "get_multiline_input",
                "show_confirmation_dialog",
                "show_info_message",
                "assign_task_to_human",
                "get_operator_profile",
                "get_human_loop_prompt"
            ],
            "operator": {
                "name": (human_loop_config.get_profile().get("name") or "").strip() or None,
                "profile_configured": bool((human_loop_config.get_profile().get("name") or "").strip()),
            },
            "config_path": human_loop_config.get_config_path(),
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "gui_available": False,
            "error": str(e),
            "platform": CURRENT_PLATFORM
        }

# Main execution

def main():
    _out("Starting Human-in-the-Loop MCP Server...")
    _out("This server provides tools for LLMs to interact with humans through GUI dialogs.")
    _out(f"Platform: {CURRENT_PLATFORM} ({platform.system()} {platform.release()})")
    _out("")
    _out("Available tools:")
    _out("get_user_input - Get text/number input from user")
    _out("get_user_choice - Let user choose from options")
    _out("get_multiline_input - Get multi-line text from user")
    _out("show_confirmation_dialog - Ask user for yes/no confirmation")
    _out("show_info_message - Display information to user")
    _out("get_human_loop_prompt - Get guidance on when to use human-in-the-loop tools")
    _out("health_check - Check server status")
    _out("")
    
    # Platform-specific startup messages
    if IS_MACOS:
        _out("macOS detected - Using native system fonts and window management")
        _out("Note: You may need to allow Python to control your computer in System Preferences > Security & Privacy > Accessibility")
    elif IS_WINDOWS:
        _out("Windows detected - Using modern Windows 11-style GUI with enhanced styling")
        _out("Features: Modern colors, improved fonts, hover effects, and sleek design")
    elif IS_LINUX:
        _out("Linux detected - Using Linux-compatible GUI settings with modern styling")
    
    # Transport: default stdio (client launches us as a subprocess). Set
    # HUMAN_LOOP_HTTP_PORT to instead serve over HTTP on that port so a client
    # can connect by URL. The GUI is still local to THIS machine's desktop.
    # Set HUMAN_LOOP_HTTPS=1 (with HUMAN_LOOP_HTTPS_CERT / _KEY) to serve TLS,
    # which newer MCP clients (e.g. Claude Desktop) now require for URL connectors.
    http_port = os.environ.get("HUMAN_LOOP_HTTP_PORT")
    http_host = os.environ.get("HUMAN_LOOP_HTTP_HOST", "127.0.0.1")
    https_enabled = os.environ.get("HUMAN_LOOP_HTTPS", "").strip().lower() in ("1", "true", "yes", "on")
    https_cert = os.environ.get("HUMAN_LOOP_HTTPS_CERT", "").strip()
    https_key = os.environ.get("HUMAN_LOOP_HTTPS_KEY", "").strip()
    uvicorn_config = None
    scheme = "http"
    if http_port and https_enabled:
        # Validate the cert/key up front so we fail with a clear message rather
        # than an opaque uvicorn traceback deep in the server thread.
        missing = [label for label, p in (("certificate", https_cert), ("private key", https_key))
                   if not p or not os.path.isfile(p)]
        if missing:
            _out(f"ERROR: HTTPS is enabled but the {' and '.join(missing)} file is missing or unset.")
            _out("Set HUMAN_LOOP_HTTPS_CERT and HUMAN_LOOP_HTTPS_KEY to existing PEM files "
                 "(or generate a self-signed pair from the Management Console's Server tab).")
            sys.exit(2)
        uvicorn_config = {"ssl_certfile": https_cert, "ssl_keyfile": https_key}
        scheme = "https"
    if http_port:
        _out(f"Starting MCP server on {scheme}://{http_host}:{http_port} ({scheme.upper()} transport)...")
        _out("Note: HTTP mode is long-running; stop it with Ctrl+C. The GUI dialogs "
              "appear on this machine's desktop only.")
    else:
        _out("")
        _out("Starting MCP server (stdio transport)...")

    # The MCP server runs on a background thread while tkinter owns the main
    # thread. tkinter/macOS AppKit require all windows to be created on the
    # process's main (first) thread, so the server never touches Tk directly -
    # it submits dialog requests to the DialogRunner, which builds them here.
    def _serve():
        try:
            if http_port:
                kwargs = {"transport": "http", "host": http_host, "port": int(http_port)}
                if uvicorn_config:
                    kwargs["uvicorn_config"] = uvicorn_config
                mcp.run(**kwargs)
            else:
                mcp.run()
        finally:
            # Transport closed (client disconnected / stdin EOF / server stopped):
            # tell the main loop to stop so the process can exit cleanly.
            _dialog_runner.request_shutdown()

    server_thread = threading.Thread(target=_serve, name="mcp-server", daemon=True)
    server_thread.start()

    # Blocks on the main thread until shutdown is requested.
    _dialog_runner.run()

if __name__ == "__main__":
    main()
