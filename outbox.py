#!/usr/bin/env python3
"""
Outbox viewer for the Human-in-the-Loop MCP Server.

A small, self-contained (stdlib-only) tool to browse and delete the "outbox" —
the local archive of everything the human sent back to the AI through the
`assign_task_to_human` tool: the AI's command + description, the human's written
note, and any file/image attachments.

Deliberately styled after classic Windows 9x (silver 3D widgets, raised/sunken
reliefs, bitmap font) — NOT a modern UI.

Run:  python outbox.py
The archive location matches the server: $HUMAN_LOOP_OUTBOX_DIR, else
~/.human_loop_outbox
"""

import json
import os
import shutil
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox
from datetime import datetime

# --- Must match human_loop_server.py (kept duplicated to stay dependency-free) ---
OUTBOX_ENV_VAR = "HUMAN_LOOP_OUTBOX_DIR"
DEFAULT_OUTBOX_DIR = os.path.join(os.path.expanduser("~"), ".human_loop_outbox")

# --- Classic Win 9x palette / fonts ---
SILVER = "#C0C0C0"
WHITE = "#FFFFFF"
BLACK = "#000000"
NAVY = "#000080"
DISABLED = "#808080"
CLASSIC_FONT = ("MS Sans Serif", 9)      # substituted by Tk if unavailable
CLASSIC_FONT_BOLD = ("MS Sans Serif", 9, "bold")
FIXED_FONT = ("Courier New", 10)


def get_outbox_dir() -> str:
    return os.environ.get(OUTBOX_ENV_VAR) or DEFAULT_OUTBOX_DIR


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
        messagebox.showerror("Open Attachment", f"Could not open:\n{path}\n\n{e}")


def _fmt_timestamp(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso_str or "?"


class OutboxViewer:
    def __init__(self, root):
        self.root = root
        self.entries = []  # list of dicts: {dir, record}

        root.title("HITL Outbox Viewer")
        root.configure(bg=SILVER)
        root.geometry("1000x618")
        root.minsize(1000, 618)

        self._build_menu()
        self._build_body()
        self._build_buttons()
        self.refresh()

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #
    def _build_menu(self):
        menubar = tk.Menu(self.root, bg=SILVER, fg=BLACK, font=CLASSIC_FONT,
                          activebackground=NAVY, activeforeground=WHITE, tearoff=0)

        file_menu = tk.Menu(menubar, tearoff=0, bg=SILVER, fg=BLACK, font=CLASSIC_FONT,
                            activebackground=NAVY, activeforeground=WHITE)
        file_menu.add_command(label="Refresh", command=self.refresh)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.destroy)
        menubar.add_cascade(label="File", menu=file_menu)

        edit_menu = tk.Menu(menubar, tearoff=0, bg=SILVER, fg=BLACK, font=CLASSIC_FONT,
                            activebackground=NAVY, activeforeground=WHITE)
        edit_menu.add_command(label="Delete Selected Entry", command=self.delete_selected)
        menubar.add_cascade(label="Edit", menu=edit_menu)

        help_menu = tk.Menu(menubar, tearoff=0, bg=SILVER, fg=BLACK, font=CLASSIC_FONT,
                            activebackground=NAVY, activeforeground=WHITE)
        help_menu.add_command(label="About", command=self._about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menubar)

    def _build_body(self):
        # Directory label (sunken bar)
        top = tk.Frame(self.root, bg=SILVER, bd=1, relief="sunken")
        top.pack(side="top", fill="x", padx=6, pady=(6, 0))
        self.dir_var = tk.StringVar(value=f"Outbox: {get_outbox_dir()}")
        tk.Label(top, textvariable=self.dir_var, bg=SILVER, fg=BLACK,
                 font=CLASSIC_FONT, anchor="w").pack(side="left", padx=4, pady=2)

        body = tk.Frame(self.root, bg=SILVER)
        body.pack(side="top", fill="both", expand=True, padx=6, pady=6)

        # --- Left: entry list ---
        left = tk.Frame(body, bg=SILVER)
        left.pack(side="left", fill="both", expand=False)
        tk.Label(left, text="Entries (newest first):", bg=SILVER, fg=BLACK,
                 font=CLASSIC_FONT_BOLD, anchor="w").pack(side="top", fill="x")
        list_wrap = tk.Frame(left, bg=SILVER, bd=2, relief="sunken")
        list_wrap.pack(side="top", fill="both", expand=True)
        self.listbox = tk.Listbox(list_wrap, width=42, bg=WHITE, fg=BLACK,
                                  font=CLASSIC_FONT, bd=0, relief="flat",
                                  highlightthickness=0, activestyle="none",
                                  selectbackground=NAVY, selectforeground=WHITE,
                                  exportselection=False)
        self.listbox.pack(side="left", fill="both", expand=True)
        lb_scroll = tk.Scrollbar(list_wrap, orient="vertical", command=self.listbox.yview)
        lb_scroll.pack(side="right", fill="y")
        self.listbox.configure(yscrollcommand=lb_scroll.set)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        # --- Right: details + attachments ---
        right = tk.Frame(body, bg=SILVER)
        right.pack(side="left", fill="both", expand=True, padx=(6, 0))

        tk.Label(right, text="Details:", bg=SILVER, fg=BLACK,
                 font=CLASSIC_FONT_BOLD, anchor="w").pack(side="top", fill="x")
        det_wrap = tk.Frame(right, bg=SILVER, bd=2, relief="sunken")
        det_wrap.pack(side="top", fill="both", expand=True)
        self.details = tk.Text(det_wrap, wrap="word", bg=WHITE, fg=BLACK,
                               font=FIXED_FONT, bd=0, relief="flat",
                               highlightthickness=0, state="disabled")
        self.details.pack(side="left", fill="both", expand=True)
        det_scroll = tk.Scrollbar(det_wrap, orient="vertical", command=self.details.yview)
        det_scroll.pack(side="right", fill="y")
        self.details.configure(yscrollcommand=det_scroll.set)

        tk.Label(right, text="Attachments (double-click to open):", bg=SILVER, fg=BLACK,
                 font=CLASSIC_FONT_BOLD, anchor="w").pack(side="top", fill="x", pady=(6, 0))
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

    def _classic_button(self, parent, text, command, width=14):
        return tk.Button(parent, text=text, command=command, width=width,
                         bg=SILVER, fg=BLACK, font=CLASSIC_FONT,
                         relief="raised", bd=2, activebackground=SILVER,
                         highlightthickness=0, padx=4, pady=2)

    def _build_buttons(self):
        bar = tk.Frame(self.root, bg=SILVER, bd=1, relief="raised")
        bar.pack(side="bottom", fill="x")
        inner = tk.Frame(bar, bg=SILVER)
        inner.pack(side="right", padx=6, pady=6)
        self._classic_button(inner, "Refresh", self.refresh).pack(side="left", padx=(0, 4))
        self._classic_button(inner, "Open Attachment", self.open_attachment, width=16).pack(side="left", padx=(0, 4))
        self._classic_button(inner, "Delete", self.delete_selected).pack(side="left", padx=(0, 4))
        self._classic_button(inner, "Close", self.root.destroy).pack(side="left")

    # ------------------------------------------------------------------ #
    # Data
    # ------------------------------------------------------------------ #
    def refresh(self):
        self.entries = []
        outbox = get_outbox_dir()
        self.dir_var.set(f"Outbox: {outbox}")
        if os.path.isdir(outbox):
            for name in os.listdir(outbox):
                if name.startswith("."):
                    continue
                entry_dir = os.path.join(outbox, name)
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

        # newest first (created_at ISO sorts lexically; dir name also timestamp-prefixed)
        self.entries.sort(key=lambda e: e["record"].get("created_at", "") or os.path.basename(e["dir"]),
                          reverse=True)

        self.listbox.delete(0, tk.END)
        for e in self.entries:
            r = e["record"]
            label = f'{_fmt_timestamp(r.get("created_at", ""))}  [{r.get("status", "?")}]  {r.get("task_title", "")}'
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
        lines = [
            f'Task title:   {r.get("task_title", "")}',
            f'Status:       {r.get("status", "")}',
            f'When:         {_fmt_timestamp(r.get("created_at", ""))}',
            f'Task ID:      {r.get("task_id", "")}',
            "",
            "Task description (from the assistant):",
            (r.get("task_description", "") or "(none)"),
        ]
        if r.get("context_note"):
            lines += ["", "Context note:", r["context_note"]]
        lines += [
            "",
            "-" * 60,
            "Human's report:",
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

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #
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
                                   f"Permanently delete this outbox entry?\n\n{title}"):
            return
        try:
            shutil.rmtree(entry["dir"])
        except Exception as e:
            messagebox.showerror("Delete", f"Could not delete:\n{e}")
            return
        self.refresh()

    def _about(self):
        messagebox.showinfo(
            "About",
            "Outbox Viewer\n"
            "Human-in-the-Loop MCP Server\n\n"
            "Browse and delete archived task submissions.\n"
            f"Location: {get_outbox_dir()}")


def main():
    root = tk.Tk()
    # Force the classic (non-themed) look where ttk defaults would otherwise apply.
    try:
        from tkinter import ttk
        ttk.Style().theme_use("classic")
    except Exception:
        pass
    OutboxViewer(root)
    root.mainloop()


if __name__ == "__main__":
    main()
