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
import shutil
import subprocess
import threading
import time
import uuid
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk, filedialog
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

# Platform detection
CURRENT_PLATFORM = platform.system().lower()
IS_WINDOWS = CURRENT_PLATFORM == 'windows'
IS_MACOS = CURRENT_PLATFORM == 'darwin'
IS_LINUX = CURRENT_PLATFORM == 'linux'

# Initialize the MCP server
mcp = FastMCP("Human-in-the-Loop Server")

# --------------------------------------------------------------------------- #
# Outbox (发件箱): local, on-disk archive of everything the human sends to the AI
# --------------------------------------------------------------------------- #
OUTBOX_ENV_VAR = "HUMAN_LOOP_OUTBOX_DIR"
DEFAULT_OUTBOX_DIR = os.path.join(os.path.expanduser("~"), ".human_loop_outbox")
OUTBOX_SCHEMA_VERSION = 1

# Attachment inlining limits for the tool return value (bytes).
MAX_INLINE_TOTAL_BYTES = 25 * 1024 * 1024   # ~25 MB across all attachments
MAX_INLINE_FILE_BYTES = 10 * 1024 * 1024    # per-file cap
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# Heartbeat / continuation tuning (seconds).
HEARTBEAT_SAFETY_MARGIN = 30   # subtracted from the client's own timeout
MIN_LEG_SECONDS = 30           # floor for a single wait "leg"


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
    directory path, or None on failure (failures are swallowed — archiving must
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
        print(f"Warning: failed to archive submission to outbox: {e}")
        try:
            if 'tmp_dir' in locals() and os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
        return None


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
            print(f"Warning: GUI initialization failed: {e}")

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
    
    button = tk.Button(
        parent,
        text=text,
        command=command,
        bg=bg_color,
        fg=fg_color,
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
    """Apply platform-specific window configurations"""
    try:
        if IS_MACOS:
            # macOS-specific window configuration
            window.call('wm', 'attributes', '.', '-topmost', '1')
            window.lift()
            window.focus_force()
            # Try to activate the app on macOS
            configure_macos_app()
        elif IS_WINDOWS:
            # Windows-specific configuration (existing behavior)
            window.attributes('-topmost', True)
            window.lift()
            window.focus_force()
    except Exception as e:
        print(f"Warning: Platform-specific window configuration failed: {e}")

def create_input_dialog(root, title: str, prompt: str, default_value: str = "", input_type: str = "text"):
    """Build a modern input dialog as a child of the shared root (main thread)."""
    try:
        return ModernInputDialog(root, title, prompt, default_value, input_type).result
    except Exception as e:
        print(f"Error in input dialog: {e}")
        return None

def show_confirmation(root, title: str, message: str):
    """Build a modern confirmation dialog on the shared root (main thread)."""
    try:
        return ModernConfirmationDialog(root, title, message).result
    except Exception as e:
        print(f"Error in confirmation dialog: {e}")
        return False

def show_info(root, title: str, message: str):
    """Build a modern info dialog on the shared root (main thread)."""
    try:
        return ModernInfoDialog(root, title, message).result
    except Exception as e:
        print(f"Error in info dialog: {e}")
        return False

class ModernInputDialog:
    def __init__(self, parent, title, prompt, default_value="", input_type="text"):
        self.result = None
        self.input_type = input_type
        
        # Get theme colors
        self.theme_colors = get_theme_colors()
        
        # Create the dialog window
        self.dialog = tk.Toplevel(parent)
        self.dialog.title(title)
        self.dialog.grab_set()
        self.dialog.resizable(False, False)
        
        # Apply modern window styling
        configure_modern_window(self.dialog)
        
        # Set size based on platform
        if IS_WINDOWS:
            self.dialog.geometry("420x280")
        else:
            self.dialog.geometry("400x260")
        
        self.center_window()
        
        # Create the main frame
        main_frame = tk.Frame(self.dialog, bg=self.theme_colors["bg_primary"])
        main_frame.pack(fill="both", expand=True, padx=24, pady=20)
        
        # Title label
        title_label = tk.Label(
            main_frame,
            text=title,
            bg=self.theme_colors["bg_primary"],
            fg=self.theme_colors["fg_primary"],
            font=get_title_font(),
            anchor="w"
        )
        title_label.pack(fill="x", pady=(0, 8))
        
        # Prompt label
        prompt_label = tk.Label(
            main_frame,
            text=prompt,
            bg=self.theme_colors["bg_primary"],
            fg=self.theme_colors["fg_secondary"],
            font=get_system_font(),
            wraplength=350,
            justify="left",
            anchor="w"
        )
        prompt_label.pack(fill="x", pady=(0, 20))
        
        # Input field
        input_frame = tk.Frame(main_frame, bg=self.theme_colors["bg_primary"])
        input_frame.pack(fill="x", pady=(0, 24))
        
        self.entry = tk.Entry(
            input_frame,
            font=get_system_font(),
            bg=self.theme_colors["bg_primary"],
            fg=self.theme_colors["fg_primary"],
            relief="solid",
            borderwidth=1,
            highlightthickness=1,
            highlightcolor=self.theme_colors["accent_color"],
            highlightbackground=self.theme_colors["border_color"],
            insertbackground=self.theme_colors["accent_color"]
        )
        self.entry.pack(fill="x", ipady=8, ipadx=12)
        
        if default_value:
            self.entry.insert(0, default_value)
            self.entry.select_range(0, tk.END)
        
        # Button frame
        button_frame = tk.Frame(main_frame, bg=self.theme_colors["bg_primary"])
        button_frame.pack(fill="x")
        
        # Create modern buttons
        self.ok_button = create_modern_button(
            button_frame, "OK", self.ok_clicked, "primary", self.theme_colors
        )
        self.ok_button.pack(side=tk.RIGHT, padx=(8, 0))
        
        self.cancel_button = create_modern_button(
            button_frame, "Cancel", self.cancel_clicked, "secondary", self.theme_colors
        )
        self.cancel_button.pack(side=tk.RIGHT)
        
        # Handle window close and keyboard shortcuts
        self.dialog.protocol("WM_DELETE_WINDOW", self.cancel_clicked)
        self.dialog.bind('<Return>', lambda e: self.ok_clicked())
        self.dialog.bind('<Escape>', lambda e: self.cancel_clicked())
        
        # Focus on entry
        self.entry.focus_set()
        
        # Wait for dialog completion
        self.dialog.wait_window()
    
    def center_window(self):
        """Center the dialog window on screen"""
        self.dialog.update_idletasks()
        width = self.dialog.winfo_width()
        height = self.dialog.winfo_height()
        screen_width = self.dialog.winfo_screenwidth()
        screen_height = self.dialog.winfo_screenheight()
        x = (screen_width // 2) - (width // 2)
        y = (screen_height // 2) - (height // 2)
        
        if IS_MACOS:
            y = max(50, y - 50)
        elif IS_WINDOWS:
            y = max(30, y - 30)
            
        self.dialog.geometry(f"{width}x{height}+{x}+{y}")
    
    def ok_clicked(self):
        value = self.entry.get()
        if self.input_type == "integer":
            try:
                self.result = int(value) if value else None
            except ValueError:
                self.result = None
        elif self.input_type == "float":
            try:
                self.result = float(value) if value else None
            except ValueError:
                self.result = None
        else:
            self.result = value if value else None
        self.dialog.destroy()
    
    def cancel_clicked(self):
        self.result = None
        self.dialog.destroy()

class ModernConfirmationDialog:
    def __init__(self, parent, title, message):
        self.result = False
        
        # Get theme colors
        self.theme_colors = get_theme_colors()
        
        # Create the dialog window
        self.dialog = tk.Toplevel(parent)
        self.dialog.title(title)
        self.dialog.grab_set()
        self.dialog.resizable(False, False)
        
        # Apply modern window styling
        configure_modern_window(self.dialog)
        
        # Set size based on content
        if IS_WINDOWS:
            self.dialog.geometry("440x220")
        else:
            self.dialog.geometry("420x200")
        
        self.center_window()
        
        # Create the main frame
        main_frame = tk.Frame(self.dialog, bg=self.theme_colors["bg_primary"])
        main_frame.pack(fill="both", expand=True, padx=24, pady=20)
        
        # Title label
        title_label = tk.Label(
            main_frame,
            text=title,
            bg=self.theme_colors["bg_primary"],
            fg=self.theme_colors["fg_primary"],
            font=get_title_font(),
            anchor="w"
        )
        title_label.pack(fill="x", pady=(0, 12))
        
        # Message label
        message_label = tk.Label(
            main_frame,
            text=message,
            bg=self.theme_colors["bg_primary"],
            fg=self.theme_colors["fg_secondary"],
            font=get_system_font(),
            wraplength=370,
            justify="left",
            anchor="w"
        )
        message_label.pack(fill="x", pady=(0, 24))
        
        # Button frame
        button_frame = tk.Frame(main_frame, bg=self.theme_colors["bg_primary"])
        button_frame.pack(fill="x")
        
        # Create modern buttons
        self.yes_button = create_modern_button(
            button_frame, "Yes", self.yes_clicked, "primary", self.theme_colors
        )
        self.yes_button.pack(side=tk.RIGHT, padx=(8, 0))
        
        self.no_button = create_modern_button(
            button_frame, "No", self.no_clicked, "secondary", self.theme_colors
        )
        self.no_button.pack(side=tk.RIGHT)
        
        # Handle window close and keyboard shortcuts
        self.dialog.protocol("WM_DELETE_WINDOW", self.no_clicked)
        self.dialog.bind('<Return>', lambda e: self.yes_clicked())
        self.dialog.bind('<Escape>', lambda e: self.no_clicked())
        
        # Focus on No button by default (safer)
        self.no_button.focus_set()
        
        # Wait for dialog completion
        self.dialog.wait_window()
    
    def center_window(self):
        """Center the dialog window on screen"""
        self.dialog.update_idletasks()
        width = self.dialog.winfo_width()
        height = self.dialog.winfo_height()
        screen_width = self.dialog.winfo_screenwidth()
        screen_height = self.dialog.winfo_screenheight()
        x = (screen_width // 2) - (width // 2)
        y = (screen_height // 2) - (height // 2)
        
        if IS_MACOS:
            y = max(50, y - 50)
        elif IS_WINDOWS:
            y = max(30, y - 30)
            
        self.dialog.geometry(f"{width}x{height}+{x}+{y}")
    
    def yes_clicked(self):
        self.result = True
        self.dialog.destroy()
    
    def no_clicked(self):
        self.result = False
        self.dialog.destroy()

class ModernInfoDialog:
    def __init__(self, parent, title, message):
        self.result = True
        
        # Get theme colors
        self.theme_colors = get_theme_colors()
        
        # Create the dialog window
        self.dialog = tk.Toplevel(parent)
        self.dialog.title(title)
        self.dialog.grab_set()
        self.dialog.resizable(False, False)
        
        # Apply modern window styling
        configure_modern_window(self.dialog)
        
        # Set size based on content
        if IS_WINDOWS:
            self.dialog.geometry("420x200")
        else:
            self.dialog.geometry("400x180")
        
        self.center_window()
        
        # Create the main frame
        main_frame = tk.Frame(self.dialog, bg=self.theme_colors["bg_primary"])
        main_frame.pack(fill="both", expand=True, padx=24, pady=20)
        
        # Title label
        title_label = tk.Label(
            main_frame,
            text=title,
            bg=self.theme_colors["bg_primary"],
            fg=self.theme_colors["fg_primary"],
            font=get_title_font(),
            anchor="w"
        )
        title_label.pack(fill="x", pady=(0, 12))
        
        # Message label
        message_label = tk.Label(
            main_frame,
            text=message,
            bg=self.theme_colors["bg_primary"],
            fg=self.theme_colors["fg_secondary"],
            font=get_system_font(),
            wraplength=350,
            justify="left",
            anchor="w"
        )
        message_label.pack(fill="x", pady=(0, 24))
        
        # Button frame
        button_frame = tk.Frame(main_frame, bg=self.theme_colors["bg_primary"])
        button_frame.pack(fill="x")
        
        # Create modern OK button
        self.ok_button = create_modern_button(
            button_frame, "OK", self.ok_clicked, "primary", self.theme_colors
        )
        self.ok_button.pack(side=tk.RIGHT)
        
        # Handle window close and keyboard shortcuts
        self.dialog.protocol("WM_DELETE_WINDOW", self.ok_clicked)
        self.dialog.bind('<Return>', lambda e: self.ok_clicked())
        self.dialog.bind('<Escape>', lambda e: self.ok_clicked())
        
        # Focus on OK button
        self.ok_button.focus_set()
        
        # Wait for dialog completion
        self.dialog.wait_window()
    
    def center_window(self):
        """Center the dialog window on screen"""
        self.dialog.update_idletasks()
        width = self.dialog.winfo_width()
        height = self.dialog.winfo_height()
        screen_width = self.dialog.winfo_screenwidth()
        screen_height = self.dialog.winfo_screenheight()
        x = (screen_width // 2) - (width // 2)
        y = (screen_height // 2) - (height // 2)
        
        if IS_MACOS:
            y = max(50, y - 50)
        elif IS_WINDOWS:
            y = max(30, y - 30)
            
        self.dialog.geometry(f"{width}x{height}+{x}+{y}")
    
    def ok_clicked(self):
        self.result = True
        self.dialog.destroy()

def create_choice_dialog(root, title: str, prompt: str, choices: List[str], allow_multiple: bool = False):
    """Build a choice dialog as a child of the shared root (main thread)."""
    try:
        return ChoiceDialog(root, title, prompt, choices, allow_multiple).result
    except Exception as e:
        print(f"Error in choice dialog: {e}")
        return None

def create_multiline_input_dialog(root, title: str, prompt: str, default_value: str = ""):
    """Build a multi-line text input dialog on the shared root (main thread)."""
    try:
        return MultilineInputDialog(root, title, prompt, default_value).result
    except Exception as e:
        print(f"Error in multiline dialog: {e}")
        return None

class ChoiceDialog:
    def __init__(self, parent, title, prompt, choices, allow_multiple=False):
        self.result = None
        
        # Get theme colors
        self.theme_colors = get_theme_colors()
        
        # Create the dialog window
        self.dialog = tk.Toplevel(parent)
        self.dialog.title(title)
        self.dialog.grab_set()
        self.dialog.resizable(True, True)
        
        # Apply modern window styling
        configure_modern_window(self.dialog)
        
        # Set size based on platform
        if IS_MACOS:
            self.dialog.geometry("480x400")
        elif IS_WINDOWS:
            self.dialog.geometry("500x420")
        else:
            self.dialog.geometry("450x350")
        
        self.center_window()
        
        # Create the main frame with modern styling
        main_frame = tk.Frame(self.dialog, bg=self.theme_colors["bg_primary"])
        main_frame.pack(fill="both", expand=True, padx=24, pady=20)
        
        # Configure grid weights
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(1, weight=1)
        
        # Add modern title label
        title_label = tk.Label(
            main_frame, 
            text=title,
            bg=self.theme_colors["bg_primary"],
            fg=self.theme_colors["fg_primary"],
            font=get_title_font(),
            anchor="w"
        )
        title_label.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        
        # Add prompt label with modern styling
        prompt_label = tk.Label(
            main_frame,
            text=prompt,
            bg=self.theme_colors["bg_primary"],
            fg=self.theme_colors["fg_secondary"],
            font=get_system_font(),
            wraplength=450,
            justify="left",
            anchor="w"
        )
        prompt_label.grid(row=1, column=0, sticky="ew", pady=(0, 20))
        
        # Create choice selection widget with modern container
        list_container = tk.Frame(main_frame, bg=self.theme_colors["bg_primary"])
        list_container.grid(row=2, column=0, sticky="nsew", pady=(0, 24))
        list_container.columnconfigure(0, weight=1)
        list_container.rowconfigure(0, weight=1)
        
        # Modern listbox with styling
        if allow_multiple:
            self.listbox = tk.Listbox(list_container, selectmode=tk.MULTIPLE, height=8)
        else:
            self.listbox = tk.Listbox(list_container, selectmode=tk.SINGLE, height=8)
        
        apply_modern_style(self.listbox, "listbox", self.theme_colors)
        
        for choice in choices:
            self.listbox.insert(tk.END, choice)
        self.listbox.grid(row=0, column=0, sticky="nsew", padx=(0, 2))
        
        # Modern scrollbar
        scrollbar = tk.Scrollbar(list_container, orient="vertical", command=self.listbox.yview)
        apply_modern_style(scrollbar, "scrollbar", self.theme_colors)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=scrollbar.set)
        
        # Modern button frame
        button_frame = tk.Frame(main_frame, bg=self.theme_colors["bg_primary"])
        button_frame.grid(row=3, column=0, sticky="ew")
        
        # Create modern buttons
        self.ok_button = create_modern_button(
            button_frame, "OK", self.ok_clicked, "primary", self.theme_colors
        )
        self.ok_button.pack(side=tk.RIGHT, padx=(8, 0))
        
        self.cancel_button = create_modern_button(
            button_frame, "Cancel", self.cancel_clicked, "secondary", self.theme_colors
        )
        self.cancel_button.pack(side=tk.RIGHT)
        
        # Handle window close
        self.dialog.protocol("WM_DELETE_WINDOW", self.cancel_clicked)
        
        # Focus on listbox
        self.listbox.focus_set()
        if choices:
            self.listbox.selection_set(0)  # Select first item by default
        
        # Platform-specific final setup
        if IS_MACOS:
            self.dialog.after(100, lambda: self.listbox.focus_set())
        
        # Add keyboard shortcuts
        self.dialog.bind('<Return>', lambda e: self.ok_clicked())
        self.dialog.bind('<Escape>', lambda e: self.cancel_clicked())
        
        # Wait for the dialog to complete
        self.dialog.wait_window()
    
    def center_window(self):
        """Center the dialog window on screen"""
        self.dialog.update_idletasks()
        width = self.dialog.winfo_width()
        height = self.dialog.winfo_height()
        
        # Get screen dimensions
        screen_width = self.dialog.winfo_screenwidth()
        screen_height = self.dialog.winfo_screenheight()
        
        # Calculate center position
        x = (screen_width // 2) - (width // 2)
        y = (screen_height // 2) - (height // 2)
        
        # Platform-specific adjustments
        if IS_MACOS:
            y = max(50, y - 50)
        elif IS_WINDOWS:
            y = max(30, y - 30)
        
        self.dialog.geometry(f"{width}x{height}+{x}+{y}")
    
    def ok_clicked(self):
        selection = self.listbox.curselection()
        if selection:
            selected_items = [self.listbox.get(i) for i in selection]
            self.result = selected_items if len(selected_items) > 1 else selected_items[0]
        self.dialog.destroy()
    
    def cancel_clicked(self):
        self.result = None
        self.dialog.destroy()

class MultilineInputDialog:
    def __init__(self, parent, title, prompt, default_value=""):
        self.result = None
        
        # Get theme colors
        self.theme_colors = get_theme_colors()
        
        # Create the dialog window
        self.dialog = tk.Toplevel(parent)
        self.dialog.title(title)
        self.dialog.grab_set()
        self.dialog.resizable(True, True)
        
        # Apply modern window styling
        configure_modern_window(self.dialog)
        
        # Set size based on platform
        if IS_MACOS:
            self.dialog.geometry("580x480")
        elif IS_WINDOWS:
            self.dialog.geometry("600x500")
        else:
            self.dialog.geometry("550x450")
        
        self.center_window()
        
        # Create the main frame with modern styling
        main_frame = tk.Frame(self.dialog, bg=self.theme_colors["bg_primary"])
        main_frame.pack(fill="both", expand=True, padx=24, pady=20)
        
        # Configure grid weights
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(2, weight=1)
        
        # Add modern title label
        title_label = tk.Label(
            main_frame,
            text=title,
            bg=self.theme_colors["bg_primary"],
            fg=self.theme_colors["fg_primary"],
            font=get_title_font(),
            anchor="w"
        )
        title_label.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        
        # Add prompt label with modern styling
        prompt_label = tk.Label(
            main_frame,
            text=prompt,
            bg=self.theme_colors["bg_primary"],
            fg=self.theme_colors["fg_secondary"],
            font=get_system_font(),
            wraplength=520,
            justify="left",
            anchor="w"
        )
        prompt_label.grid(row=1, column=0, sticky="ew", pady=(0, 20))
        
        # Create text widget container with modern styling
        text_container = tk.Frame(main_frame, bg=self.theme_colors["bg_primary"])
        text_container.grid(row=2, column=0, sticky="nsew", pady=(0, 24))
        text_container.columnconfigure(0, weight=1)
        text_container.rowconfigure(0, weight=1)
        
        # Modern text widget
        self.text_widget = tk.Text(text_container, height=12)
        apply_modern_style(self.text_widget, "text", self.theme_colors)
        self.text_widget.grid(row=0, column=0, sticky="nsew", padx=(0, 2))
        
        # Modern scrollbar for text widget
        text_scrollbar = tk.Scrollbar(text_container, orient="vertical", command=self.text_widget.yview)
        apply_modern_style(text_scrollbar, "scrollbar", self.theme_colors)
        text_scrollbar.grid(row=0, column=1, sticky="ns")
        self.text_widget.configure(yscrollcommand=text_scrollbar.set)
        
        # Set default value with better formatting
        if default_value:
            self.text_widget.insert("1.0", default_value)
        
        # Modern button frame
        button_frame = tk.Frame(main_frame, bg=self.theme_colors["bg_primary"])
        button_frame.grid(row=3, column=0, sticky="ew")
        
        # Create modern buttons
        self.ok_button = create_modern_button(
            button_frame, "OK", self.ok_clicked, "primary", self.theme_colors
        )
        self.ok_button.pack(side=tk.RIGHT, padx=(8, 0))
        
        self.cancel_button = create_modern_button(
            button_frame, "Cancel", self.cancel_clicked, "secondary", self.theme_colors
        )
        self.cancel_button.pack(side=tk.RIGHT)
        
        # Handle window close
        self.dialog.protocol("WM_DELETE_WINDOW", self.cancel_clicked)
        
        # Focus on text widget
        self.text_widget.focus_set()
        
        # Platform-specific final setup
        if IS_MACOS:
            self.dialog.after(100, lambda: self.text_widget.focus_set())
        
        # Add keyboard shortcuts
        self.dialog.bind('<Control-Return>', lambda e: self.ok_clicked())
        self.dialog.bind('<Escape>', lambda e: self.cancel_clicked())
        
        # Wait for the dialog to complete
        self.dialog.wait_window()
    
    def center_window(self):
        """Center the dialog window on screen"""
        self.dialog.update_idletasks()
        width = self.dialog.winfo_width()
        height = self.dialog.winfo_height()
        
        # Get screen dimensions
        screen_width = self.dialog.winfo_screenwidth()
        screen_height = self.dialog.winfo_screenheight()
        
        # Calculate center position
        x = (screen_width // 2) - (width // 2)
        y = (screen_height // 2) - (height // 2)
        
        # Platform-specific adjustments
        if IS_MACOS:
            y = max(50, y - 50)
        elif IS_WINDOWS:
            y = max(30, y - 30)
        
        self.dialog.geometry(f"{width}x{height}+{x}+{y}")
    
    def ok_clicked(self):
        self.result = self.text_widget.get("1.0", tk.END).strip()
        self.dialog.destroy()
    
    def cancel_clicked(self):
        self.result = None
        self.dialog.destroy()

# --------------------------------------------------------------------------- #
# Long-running "assign a task to a human" dialog + session
# --------------------------------------------------------------------------- #

# Registry of live task sessions, keyed by task_id. Lives on the server process
# and survives across multiple tool invocations (heartbeat continuations).
_task_sessions: "Dict[str, HumanTaskSession]" = {}


class HumanTaskSession:
    """Server-side state for one delegated human task.

    Persists across heartbeat continuations. The async tool (MCP background
    thread) consumes human submissions from ``updates``; the Tk dialog (main
    thread) produces them via :meth:`submit_from_ui`, bridged with
    ``loop.call_soon_threadsafe`` so the queue is only ever touched on the loop
    thread.
    """

    MAX_LIFETIME_SECONDS = 30 * 60  # orphan safety net

    def __init__(self, task_id, task_title, task_description, context_note,
                 client_timeout_seconds, loop):
        self.task_id = task_id
        self.task_title = task_title
        self.task_description = task_description
        self.context_note = context_note
        self.client_timeout_seconds = client_timeout_seconds
        self.loop = loop
        self.updates = asyncio.Queue()
        self.dialog = None
        self.created_at = time.monotonic()
        self.last_seen = self.created_at
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

    def enter_waiting_state(self):
        if self.dialog:
            self.dialog.enter_waiting_state()

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


class HumanTaskDialog:
    """Persistent (non-modal, no ``wait_window``) task dialog with a live
    countdown. Built on the main thread; button handlers push submissions to the
    owning :class:`HumanTaskSession`."""

    WARN_SECONDS = 10  # last-N-seconds "please pause" warning

    def __init__(self, parent, session):
        self.session = session
        self.theme_colors = get_theme_colors()
        self.attachments = []          # list[str] of chosen file paths
        self._alive = True
        self._waiting = False
        self._after_id = None
        self._deadline = None

        c = self.theme_colors
        self.dialog = tk.Toplevel(parent)
        self.dialog.title(f"Task from assistant — {session.task_title}")
        self.dialog.resizable(True, True)
        configure_modern_window(self.dialog)
        if IS_MACOS:
            self.dialog.geometry("580x640")
        elif IS_WINDOWS:
            self.dialog.geometry("600x660")
        else:
            self.dialog.geometry("560x620")
        self._center_window()

        main = tk.Frame(self.dialog, bg=c["bg_primary"])
        main.pack(fill="both", expand=True, padx=24, pady=20)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(5, weight=1)   # body text expands

        # Title
        tk.Label(main, text=session.task_title, bg=c["bg_primary"],
                 fg=c["fg_primary"], font=get_title_font(), anchor="w",
                 justify="left", wraplength=520).grid(row=0, column=0, sticky="ew", pady=(0, 8))

        # Task description (what the AI is asking the human to do)
        tk.Label(main, text=session.task_description, bg=c["bg_primary"],
                 fg=c["fg_secondary"], font=get_system_font(), anchor="w",
                 justify="left", wraplength=520).grid(row=1, column=0, sticky="ew", pady=(0, 6))

        if session.context_note:
            tk.Label(main, text=session.context_note, bg=c["bg_primary"],
                     fg=c["fg_secondary"], font=get_system_font(), anchor="w",
                     justify="left", wraplength=520).grid(row=2, column=0, sticky="ew", pady=(0, 6))

        # Countdown / status banner
        self.countdown_label = tk.Label(
            main, text="", bg=c["bg_primary"], fg=c["fg_secondary"],
            font=get_system_font(), anchor="w", justify="left")
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

        # Attachments
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

        # Action buttons: Completed / Failed / Still progressing
        button_frame = tk.Frame(main, bg=c["bg_primary"])
        button_frame.grid(row=8, column=0, sticky="ew")
        create_modern_button(button_frame, "✅ Completed",
                             lambda: self._submit("completed", terminal=True),
                             "primary", self.theme_colors).pack(side=tk.LEFT)
        create_modern_button(button_frame, "❌ Failed",
                             lambda: self._submit("failed", terminal=True),
                             "secondary", self.theme_colors).pack(side=tk.LEFT, padx=(8, 0))
        create_modern_button(button_frame, "⏳ Still progressing",
                             lambda: self._submit("in_progress", terminal=False),
                             "secondary", self.theme_colors).pack(side=tk.RIGHT)

        self.dialog.protocol("WM_DELETE_WINDOW", self._on_close)
        self.body_text.focus_set()

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
        self.attach_listbox.delete(0, tk.END)
        for p in self.attachments:
            self.attach_listbox.insert(tk.END, os.path.basename(p))

    # ---------------- countdown / banner ---------------- #
    def begin_countdown(self, leg_seconds):
        self._waiting = False
        self._deadline = time.monotonic() + max(1, leg_seconds)
        if self._after_id:
            try:
                self.dialog.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        self._tick()

    def _tick(self):
        if not self._alive or self._waiting or self._deadline is None:
            return
        remaining = int(round(self._deadline - time.monotonic()))
        if remaining < 0:
            remaining = 0
        mm, ss = divmod(remaining, 60)
        if remaining <= self.WARN_SECONDS:
            self.countdown_label.config(
                text=f"⏳ Syncing with assistant in {mm:d}:{ss:02d} — you can pause for a moment (your work is saved).",
                fg=self.theme_colors.get("error_color", self.theme_colors["fg_secondary"]))
        else:
            self.countdown_label.config(
                text=f"⏳ {mm:d}:{ss:02d} until the assistant checks in (this window stays open).",
                fg=self.theme_colors["fg_secondary"])
        if remaining <= 0:
            return  # tool will flip us into the waiting state
        self._after_id = self.dialog.after(250, self._tick)

    def enter_waiting_state(self):
        self._waiting = True
        if self._after_id:
            try:
                self.dialog.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        if self._alive:
            self.countdown_label.config(
                text="🔄 Reconnecting to assistant… (keep working — nothing is lost)",
                fg=self.theme_colors["fg_secondary"])

    # ---------------- submit / close ---------------- #
    def _submit(self, status, terminal):
        """Any submit action (Completed/Failed/Still-progressing) is one complete
        'email'. It always CLOSES the window — the window's lifecycle is exactly
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
                    "Use ✅ Completed or ❌ Failed to finish the task instead.",
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

    def close(self):
        self._alive = False
        if self._after_id:
            try:
                self.dialog.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        try:
            self.dialog.destroy()
        except Exception:
            pass
        # Detach from the session so a re-call knows the window is gone and
        # reopens a fresh one instead of driving a destroyed widget.
        try:
            if self.session.dialog is self:
                self.session.dialog = None
        except Exception:
            pass

    def _center_window(self):
        self.dialog.update_idletasks()
        width = self.dialog.winfo_width()
        height = self.dialog.winfo_height()
        screen_width = self.dialog.winfo_screenwidth()
        screen_height = self.dialog.winfo_screenheight()
        x = (screen_width // 2) - (width // 2)
        y = (screen_height // 2) - (height // 2)
        if IS_MACOS:
            y = max(50, y - 50)
        elif IS_WINDOWS:
            y = max(30, y - 30)
        self.dialog.geometry(f"{width}x{height}+{x}+{y}")

# MCP Tools

def _build_submission_result(session, payload, needs_continuation, archived_dir):
    """Turn a human submission into a mixed content-block list for the LLM.

    Returns ``[summary_text, *Image(...), *File(...)]``. Images the model can see;
    other files come back as embedded resources. Oversized attachments are not
    inlined — they are referenced by path (and are safe in the outbox archive).
    """
    status = payload.get("status", "in_progress")
    body = payload.get("body", "")
    paths = payload.get("attachment_paths", []) or []

    images, files, manifest, inlined_total = [], [], [], 0
    for p in paths:
        name = os.path.basename(p)
        try:
            size = os.path.getsize(p)
            mime = mimetypes.guess_type(p)[0] or "application/octet-stream"
            ext = os.path.splitext(p)[1].lower()
            is_image = ext in IMAGE_EXTENSIONS or mime.startswith("image/")
            entry = {"name": name, "size_bytes": size, "mime": mime, "inlined": False}
            if size <= MAX_INLINE_FILE_BYTES and (inlined_total + size) <= MAX_INLINE_TOTAL_BYTES:
                if is_image:
                    images.append(Image(path=p))
                else:
                    files.append(File(path=p, name=name))
                entry["inlined"] = True
                inlined_total += size
            else:
                entry["path_reference"] = p
                entry["note"] = "too large to inline; saved in the outbox archive"
            manifest.append(entry)
        except Exception as e:
            manifest.append({"name": name, "error": str(e)})

    summary = {
        "status": status,
        "task_id": session.task_id,
        "human_action": True,
        "needs_continuation": needs_continuation,
        "task_title": session.task_title,
        "body": body,
        "attachments": manifest,
        "outbox_entry": archived_dir,
        "platform": CURRENT_PLATFORM,
    }
    headers = {
        "completed": "The human reports the task is COMPLETED.",
        "failed": "The human reports the task FAILED.",
        "in_progress": ("The human sent an INTERIM update and the task window has CLOSED; the task is "
                        "NOT finished. Process this update (they may be asking or discussing something). "
                        "YOU decide the next step: if the human should keep working, call "
                        "assign_task_to_human again with the SAME task_id to reopen a fresh window "
                        "(optionally put your reply to them in context_note); otherwise finish normally."),
        "cancelled": "The human DISMISSED / cancelled the task dialog.",
    }
    summary_text = headers.get(status, "Human submission.") + "\n\n" + json.dumps(
        summary, ensure_ascii=False, indent=2)
    return [summary_text, *images, *files]


@mcp.tool()
async def assign_task_to_human(
    task_title: Annotated[str, Field(description="Short title of the task you are asking the human to perform")],
    task_description: Annotated[str, Field(description="Detailed instructions/description of the task for the human")],
    task_id: Annotated[Optional[str], Field(description="Leave empty to START a new task. To CONTINUE an existing task (after a 'heartbeat' response or an 'in_progress' update), pass back the task_id from the previous call — this revives the same open window without losing the human's work.")] = None,
    client_timeout_seconds: Annotated[int, Field(description="YOUR OWN per-tool-call timeout, in seconds. Claude Desktop hardcodes ~240s, so pass 240. Claude Code has no timeout, so pass a large value like 3600. The server returns a 'heartbeat' response safely before this deadline so you can immediately re-call and keep the human's dialog alive.")] = 240,
    context_note: Annotated[str, Field(description="Optional extra context to show the human in the dialog")] = "",
    ctx: Context = None,
) -> Any:
    """
    Delegate a real-world task to a human and wait (possibly a long time) for them to
    report back Completed / Failed / Still-progressing, optionally with a written note
    and file/image attachments.

    IMPORTANT — window lifecycle & heartbeat protocol (read carefully):
    - One window == one submission. Whenever the human clicks a button
      (Completed / Failed / Still-progressing), the window CLOSES and you receive their
      note + attachments. The ONLY difference between the buttons is whether the task is
      finished, not whether the window stays open.
    - Always pass `client_timeout_seconds` (your own per-call timeout). The server returns
      BEFORE that deadline so your client doesn't kill the call.
    - `status: "heartbeat"` (human_action=false): the human has NOT submitted anything yet.
      Do NOT reply to the user and do NOT reason about the task — just call this tool AGAIN
      immediately with the SAME `task_id`. Pure keepalive; the SAME window stays open with
      their work intact (a countdown warns them a few seconds before each sync).
    - `status: "in_progress"` (human_action=true): the human sent an interim update and the
      window has CLOSED; the task is not finished. Process the update, then DECIDE: to let
      them keep working, call again with the SAME `task_id` to reopen a fresh window
      (optionally reply via `context_note`); otherwise just finish.
    - `status: "completed"` / `"failed"` / `"cancelled"`: terminal (needs_continuation
      false); the task_id is gone.
    - Every human submission (note + attachments + your command) is also archived to a
      local "outbox" that can be browsed later with the standalone outbox.py viewer.
    """
    try:
        if ctx:
            await ctx.info(f"assign_task_to_human: '{task_title}' (task_id={task_id or 'new'})")

        if not ensure_gui_initialized():
            return {
                "success": False,
                "error": "GUI system not available",
                "needs_continuation": False,
                "platform": CURRENT_PLATFORM,
            }

        loop = asyncio.get_running_loop()
        leg_seconds = max(MIN_LEG_SECONDS, int(client_timeout_seconds) - HEARTBEAT_SAFETY_MARGIN)

        # Reap abandoned/dormant sessions (window closed, AI never re-called).
        for sid, sess in list(_task_sessions.items()):
            if sess.is_expired():
                _dialog_runner.submit(lambda root, s=sess: s.close())
                _task_sessions.pop(sid, None)

        session = _task_sessions.get(task_id) if task_id else None
        if task_id and session is None:
            return {
                "success": False,
                "status": "session_not_found",
                "task_id": task_id,
                "needs_continuation": False,
                "message": ("No such task_id (it was completed, failed, cancelled, or expired). "
                            "Start a new task by calling without a task_id."),
                "platform": CURRENT_PLATFORM,
            }

        if session is None:
            # Brand-new task: create the session and open a fresh window.
            new_id = uuid.uuid4().hex[:12]
            session = HumanTaskSession(
                new_id, task_title, task_description, context_note, client_timeout_seconds, loop)
            await _dialog_runner.run_dialog(
                lambda root: session.attach_dialog(HumanTaskDialog(root, session)), timeout=30)
            _task_sessions[new_id] = session
            if ctx:
                await ctx.info(f"Opened human task dialog (task_id={new_id})")
        elif session.dialog is None:
            # Continuation of an existing task whose window closed after a prior
            # interim ("in_progress") submission — reopen a fresh window. Carry
            # over the latest command/description/context so the human sees the
            # assistant's follow-up.
            session.task_title = task_title
            session.task_description = task_description
            session.context_note = context_note
            session.client_timeout_seconds = client_timeout_seconds
            await _dialog_runner.run_dialog(
                lambda root: session.attach_dialog(HumanTaskDialog(root, session)), timeout=30)
            if ctx:
                await ctx.info(f"Reopened human task dialog (task_id={session.task_id})")

        # Start / refresh this leg's countdown on the main thread.
        _dialog_runner.submit(lambda root: session.begin_leg(leg_seconds))

        # Await a human submission OR the heartbeat deadline.
        try:
            payload = await asyncio.wait_for(session.updates.get(), timeout=leg_seconds)
        except asyncio.TimeoutError:
            if session.is_expired():
                _dialog_runner.submit(lambda root: session.close())
                _task_sessions.pop(session.task_id, None)
                return {
                    "success": False,
                    "status": "expired",
                    "task_id": session.task_id,
                    "needs_continuation": False,
                    "message": "The human task dialog exceeded its max lifetime and was closed.",
                    "platform": CURRENT_PLATFORM,
                }
            _dialog_runner.submit(lambda root: session.enter_waiting_state())
            if ctx:
                await ctx.debug(f"Heartbeat (no human action yet) for task_id={session.task_id}")
            return {
                "success": True,
                "status": "heartbeat",
                "human_action": False,
                "needs_continuation": True,
                "task_id": session.task_id,
                "message": ("Keepalive only — the human has not submitted anything yet. Do NOT respond to "
                            "the user or reason about task content. Immediately call assign_task_to_human "
                            "again with the same task_id to keep the dialog alive."),
                "platform": CURRENT_PLATFORM,
            }

        # A human submission arrived.
        status = payload.get("status", "in_progress")
        terminal = payload.get("terminal", status in ("completed", "failed", "cancelled"))

        archived_dir = None
        if payload.get("body") or payload.get("attachment_paths"):
            archived_dir = archive_to_outbox(
                {
                    "task_id": session.task_id,
                    "status": status,
                    "human_action": True,
                    "task_title": session.task_title,
                    "task_description": session.task_description,
                    "context_note": session.context_note,
                    "client_timeout_seconds": session.client_timeout_seconds,
                    "body": payload.get("body", ""),
                },
                payload.get("attachment_paths", []),
            )

        if ctx:
            await ctx.info(f"Human submission: status={status}, task_id={session.task_id}, "
                           f"archived={'yes' if archived_dir else 'no'}")

        # The dialog closed itself on submit (one window == one submission).
        session.dialog = None
        session.last_seen = time.monotonic()

        if terminal:
            _task_sessions.pop(session.task_id, None)
            return _build_submission_result(session, payload, needs_continuation=False, archived_dir=archived_dir)

        # in_progress: the window is closed and the task is NOT finished. Keep the
        # session dormant so the assistant MAY reopen a fresh window by calling
        # again with the same task_id (e.g. with a follow-up in context_note).
        return _build_submission_result(session, payload, needs_continuation=True, archived_dir=archived_dir)

    except Exception as e:
        if ctx:
            await ctx.error(f"Error in assign_task_to_human: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "needs_continuation": False,
            "platform": CURRENT_PLATFORM,
        }


@mcp.tool()
async def get_user_input(
    title: Annotated[str, Field(description="Title of the input dialog window")],
    prompt: Annotated[str, Field(description="The prompt/question to show to the user")],
    default_value: Annotated[str, Field(description="Default value to pre-fill in the input field")] = "",
    input_type: Annotated[Literal["text", "integer", "float"], Field(description="Type of input expected")] = "text",
    ctx: Context = None
) -> Dict[str, Any]:
    """
    Create an input dialog window for the user to enter text, numbers, or other data.
    
    This tool opens a GUI dialog box where the user can input information that the LLM needs.
    Perfect for getting specific details, clarifications, or data from the user.
    """
    try:
        if ctx:
            await ctx.info(f"Requesting user input: {prompt}")
        
        # Ensure GUI is initialized
        if not ensure_gui_initialized():
            return {
                "success": False,
                "error": "GUI system not available",
                "cancelled": False,
                "platform": CURRENT_PLATFORM
            }
        
        # Build the dialog on the main thread (required by tkinter/macOS)
        result = await _dialog_runner.run_dialog(
            lambda root: create_input_dialog(root, title, prompt, default_value, input_type),
            timeout=300,  # 5 minute timeout
        )

        if result is not None:
            if ctx:
                await ctx.info(f"User provided input: {result}")
            return {
                "success": True,
                "user_input": result,
                "input_type": input_type,
                "cancelled": False,
                "platform": CURRENT_PLATFORM
            }
        else:
            if ctx:
                await ctx.warning("User cancelled the input dialog")
            return {
                "success": False,
                "user_input": None,
                "input_type": input_type,
                "cancelled": True,
                "platform": CURRENT_PLATFORM
            }
    
    except Exception as e:
        if ctx:
            await ctx.error(f"Error creating input dialog: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "cancelled": False,
            "platform": CURRENT_PLATFORM
        }

@mcp.tool()
async def get_user_choice(
    title: Annotated[str, Field(description="Title of the choice dialog window")],
    prompt: Annotated[str, Field(description="The prompt/question to show to the user")],
    choices: Annotated[List[str], Field(description="List of choices to present to the user")],
    allow_multiple: Annotated[bool, Field(description="Whether user can select multiple choices")] = False,
    ctx: Context = None
) -> Dict[str, Any]:
    """
    Create a choice dialog window for the user to select from multiple options.
    
    This tool opens a GUI dialog box with a list of choices where the user can select
    one or multiple options. Perfect for getting decisions, preferences, or selections from the user.
    """
    try:
        if ctx:
            await ctx.info(f"Requesting user choice: {prompt}")
            await ctx.debug(f"Available choices: {choices}")
        
        # Ensure GUI is initialized
        if not ensure_gui_initialized():
            return {
                "success": False,
                "error": "GUI system not available",
                "cancelled": False,
                "platform": CURRENT_PLATFORM
            }
        
        # Build the dialog on the main thread (required by tkinter/macOS)
        result = await _dialog_runner.run_dialog(
            lambda root: create_choice_dialog(root, title, prompt, choices, allow_multiple),
            timeout=300,  # 5 minute timeout
        )

        if result is not None:
            if ctx:
                await ctx.info(f"User selected: {result}")
            return {
                "success": True,
                "selected_choice": result,
                "selected_choices": result if isinstance(result, list) else [result],
                "allow_multiple": allow_multiple,
                "cancelled": False,
                "platform": CURRENT_PLATFORM
            }
        else:
            if ctx:
                await ctx.warning("User cancelled the choice dialog")
            return {
                "success": False,
                "selected_choice": None,
                "selected_choices": [],
                "allow_multiple": allow_multiple,
                "cancelled": True,
                "platform": CURRENT_PLATFORM
            }
    
    except Exception as e:
        if ctx:
            await ctx.error(f"Error creating choice dialog: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "cancelled": False,
            "platform": CURRENT_PLATFORM
        }

@mcp.tool()
async def get_multiline_input(
    title: Annotated[str, Field(description="Title of the input dialog window")],
    prompt: Annotated[str, Field(description="The prompt/question to show to the user")],
    default_value: Annotated[str, Field(description="Default text to pre-fill in the text area")] = "",
    ctx: Context = None
) -> Dict[str, Any]:
    """
    Create a multi-line text input dialog for the user to enter longer text content.
    
    This tool opens a GUI dialog box with a large text area where the user can input
    multiple lines of text. Perfect for getting detailed descriptions, code, or long-form content.
    """
    try:
        if ctx:
            await ctx.info(f"Requesting multiline user input: {prompt}")
        
        # Ensure GUI is initialized
        if not ensure_gui_initialized():
            return {
                "success": False,
                "error": "GUI system not available",
                "cancelled": False,
                "platform": CURRENT_PLATFORM
            }
        
        # Build the dialog on the main thread (required by tkinter/macOS)
        result = await _dialog_runner.run_dialog(
            lambda root: create_multiline_input_dialog(root, title, prompt, default_value),
            timeout=300,  # 5 minute timeout
        )

        if result is not None:
            if ctx:
                await ctx.info(f"User provided multiline input ({len(result)} characters)")
            return {
                "success": True,
                "user_input": result,
                "character_count": len(result),
                "line_count": len(result.split('\n')),
                "cancelled": False,
                "platform": CURRENT_PLATFORM
            }
        else:
            if ctx:
                await ctx.warning("User cancelled the multiline input dialog")
            return {
                "success": False,
                "user_input": None,
                "cancelled": True,
                "platform": CURRENT_PLATFORM
            }
    
    except Exception as e:
        if ctx:
            await ctx.error(f"Error creating multiline input dialog: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "cancelled": False,
            "platform": CURRENT_PLATFORM
        }

@mcp.tool()
async def show_confirmation_dialog(
    title: Annotated[str, Field(description="Title of the confirmation dialog")],
    message: Annotated[str, Field(description="The message to show to the user")],
    ctx: Context = None
) -> Dict[str, Any]:
    """
    Show a confirmation dialog with Yes/No buttons.
    
    This tool displays a message to the user and asks for confirmation.
    Perfect for getting approval before proceeding with an action.
    """
    try:
        if ctx:
            await ctx.info(f"Requesting user confirmation: {message}")
        
        # Ensure GUI is initialized
        if not ensure_gui_initialized():
            return {
                "success": False,
                "error": "GUI system not available",
                "confirmed": False,
                "platform": CURRENT_PLATFORM
            }
        
        # Build the dialog on the main thread (required by tkinter/macOS)
        result = await _dialog_runner.run_dialog(
            lambda root: show_confirmation(root, title, message),
            timeout=300,  # 5 minute timeout
        )

        if ctx:
            await ctx.info(f"User confirmation result: {'Yes' if result else 'No'}")
        
        return {
            "success": True,
            "confirmed": result,
            "response": "yes" if result else "no",
            "platform": CURRENT_PLATFORM
        }
    
    except Exception as e:
        if ctx:
            await ctx.error(f"Error showing confirmation dialog: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "confirmed": False,
            "platform": CURRENT_PLATFORM
        }

@mcp.tool()
async def show_info_message(
    title: Annotated[str, Field(description="Title of the information dialog")],
    message: Annotated[str, Field(description="The information message to show to the user")],
    ctx: Context = None
) -> Dict[str, Any]:
    """
    Show an information message to the user.
    
    This tool displays an informational message dialog to notify the user about something.
    The user just needs to click OK to acknowledge the message.
    """
    try:
        if ctx:
            await ctx.info(f"Showing info message to user: {message}")
        
        # Ensure GUI is initialized
        if not ensure_gui_initialized():
            return {
                "success": False,
                "error": "GUI system not available",
                "platform": CURRENT_PLATFORM
            }
        
        # Build the dialog on the main thread (required by tkinter/macOS)
        result = await _dialog_runner.run_dialog(
            lambda root: show_info(root, title, message),
            timeout=300,  # 5 minute timeout
        )

        if ctx:
            await ctx.info("Info message acknowledged by user")
        
        return {
            "success": True,
            "acknowledged": result,
            "platform": CURRENT_PLATFORM
        }
    
    except Exception as e:
        if ctx:
            await ctx.error(f"Error showing info message: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "platform": CURRENT_PLATFORM
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

**WHEN TO USE HUMAN-IN-THE-LOOP TOOLS:**

1. **Ambiguous Requirements** - When user instructions are unclear or could have multiple interpretations
2. **Decision Points** - When you need user preference between valid alternatives
3. **Creative Input** - For subjective choices like design, content style, or personal preferences
4. **Sensitive Operations** - Before executing potentially destructive or irreversible actions
5. **Missing Information** - When you need specific details not provided in the original request
6. **Quality Feedback** - To get user validation on intermediate results before proceeding
7. **Error Handling** - When encountering issues that require user guidance to resolve

**AVAILABLE TOOLS:**
- `get_user_input` - Single-line text/number input (names, values, paths, etc.)
- `get_user_choice` - Multiple choice selection (pick from options)
- `get_multiline_input` - Long-form text (descriptions, code, documents)
- `show_confirmation_dialog` - Yes/No decisions (confirmations, approvals)
- `show_info_message` - Status updates and notifications
- `assign_task_to_human` - Delegate a real-world task and wait (long-running) for the human to report Completed/Failed/Still-progressing with an optional note and file/image attachments. Uses a heartbeat protocol: always pass your own `client_timeout_seconds`, and when you get `status: "heartbeat"` re-call with the same `task_id` (do not respond to the user in between). Submissions are archived to a local outbox.

**BEST PRACTICES:**
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

AVOID OVERUSE:
- Don't ask for information already provided
- Don't seek confirmation for obviously safe operations
- Don't interrupt flow for trivial decisions
- Don't ask multiple questions when one comprehensive dialog would suffice

OPTIMIZE FOR USER EXPERIENCE:
- Batch related questions together when possible
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
                "get_human_loop_prompt"
            ]
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
    print("Starting Human-in-the-Loop MCP Server...")
    print("This server provides tools for LLMs to interact with humans through GUI dialogs.")
    print(f"Platform: {CURRENT_PLATFORM} ({platform.system()} {platform.release()})")
    print("")
    print("Available tools:")
    print("get_user_input - Get text/number input from user")
    print("get_user_choice - Let user choose from options")
    print("get_multiline_input - Get multi-line text from user")
    print("show_confirmation_dialog - Ask user for yes/no confirmation")
    print("show_info_message - Display information to user")
    print("get_human_loop_prompt - Get guidance on when to use human-in-the-loop tools")
    print("health_check - Check server status")
    print("")
    
    # Platform-specific startup messages
    if IS_MACOS:
        print("macOS detected - Using native system fonts and window management")
        print("Note: You may need to allow Python to control your computer in System Preferences > Security & Privacy > Accessibility")
    elif IS_WINDOWS:
        print("Windows detected - Using modern Windows 11-style GUI with enhanced styling")
        print("Features: Modern colors, improved fonts, hover effects, and sleek design")
    elif IS_LINUX:
        print("Linux detected - Using Linux-compatible GUI settings with modern styling")
    
    print("")
    print("Starting MCP server...")

    # The MCP server runs on a background thread while tkinter owns the main
    # thread. tkinter/macOS AppKit require all windows to be created on the
    # process's main (first) thread, so the server never touches Tk directly —
    # it submits dialog requests to the DialogRunner, which builds them here.
    def _serve():
        try:
            mcp.run()
        finally:
            # Transport closed (client disconnected / stdin EOF): tell the main
            # loop to stop so the process can exit cleanly.
            _dialog_runner.request_shutdown()

    server_thread = threading.Thread(target=_serve, name="mcp-server", daemon=True)
    server_thread.start()

    # Blocks on the main thread until shutdown is requested.
    _dialog_runner.run()

if __name__ == "__main__":
    main()