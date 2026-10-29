"""Cross-platform background-service registration for the HITL MCP server.

The server pops GUI dialogs, so on every OS it must run inside the user's
graphical login session (never a Windows Service / macOS LaunchDaemon, which run
headless in session 0). Each platform therefore uses its per-user, GUI-session
autostart mechanism:

  - macOS   : a LaunchAgent in ~/Library/LaunchAgents (launchd)
  - Linux   : a systemd --user service in ~/.config/systemd/user
  - Windows : a .vbs launcher in the Startup folder that runs pythonw (no console)

All three just run ``<python> human_loop_server.py --service``; the server reads
host/port/HTTPS from the shared config file in that mode, so these definitions
stay trivial and settings live in one place.

Stdlib-only (the Management Console must stay dependency-free). Public API:
    supported() -> bool
    platform_label() -> str
    is_installed() -> bool
    running_pid() -> int | None          # best-effort; None if stopped/unknown
    describe_location() -> str
    install(python_exe, script, workdir)  # raises RuntimeError on failure
    uninstall()                           # raises RuntimeError on failure
"""

import os
import plistlib
import re
import subprocess
import sys

IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")

LABEL = "com.human-loop.hitl"     # macOS launchd label
UNIT = "human-loop-hitl"          # linux systemd unit / windows launcher basename

HOME = os.path.expanduser("~")
SERVER_LOG = os.path.join(HOME, ".human_loop_server.log")


def supported():
    return IS_MACOS or IS_WINDOWS or IS_LINUX


def platform_label():
    if IS_MACOS:
        return "macOS LaunchAgent (launchd)"
    if IS_LINUX:
        return "systemd --user service"
    if IS_WINDOWS:
        return "Windows Startup item"
    return "unsupported platform"


def _run(*args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


# ------------------------------------------------------------------ macOS --- #
_MAC_AGENTS = os.path.join(HOME, "Library", "LaunchAgents")
_MAC_PLIST = os.path.join(_MAC_AGENTS, LABEL + ".plist")


def _mac_domain():
    return f"gui/{os.getuid()}"


def _mac_install(python_exe, script, workdir):
    os.makedirs(_MAC_AGENTS, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [python_exe, script, "--service"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "LimitLoadToSessionType": "Aqua",     # graphical login session only
        "ProcessType": "Interactive",
        "StandardOutPath": SERVER_LOG,
        "StandardErrorPath": SERVER_LOG,
        "WorkingDirectory": workdir,
        "EnvironmentVariables": {
            "PATH": os.environ.get(
                "PATH", "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
        },
    }
    with open(_MAC_PLIST, "wb") as f:
        plistlib.dump(plist, f)
    dom = _mac_domain()
    _run("launchctl", "bootout", f"{dom}/{LABEL}")               # ignore if absent
    r = _run("launchctl", "bootstrap", dom, _MAC_PLIST)
    if r.returncode != 0:
        r = _run("launchctl", "load", "-w", _MAC_PLIST)          # older-macOS fallback
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "launchctl failed").strip())
    _run("launchctl", "kickstart", "-k", f"{dom}/{LABEL}")


def _mac_uninstall():
    _run("launchctl", "bootout", f"{_mac_domain()}/{LABEL}")
    _run("launchctl", "unload", "-w", _MAC_PLIST)
    try:
        os.remove(_MAC_PLIST)
    except OSError:
        pass


def _mac_installed():
    return os.path.isfile(_MAC_PLIST)


def _mac_pid():
    r = _run("launchctl", "list", LABEL)
    if r.returncode != 0:
        return None
    m = re.search(r'"PID"\s*=\s*(\d+)', r.stdout)
    return int(m.group(1)) if m else None


# ------------------------------------------------------------------ Linux --- #
_LINUX_UNIT_DIR = os.path.join(HOME, ".config", "systemd", "user")
_LINUX_UNIT = os.path.join(_LINUX_UNIT_DIR, UNIT + ".service")


def _linux_install(python_exe, script, workdir):
    os.makedirs(_LINUX_UNIT_DIR, exist_ok=True)
    unit = f"""[Unit]
Description=Human-in-the-Loop MCP Server
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart="{python_exe}" "{script}" --service
WorkingDirectory={workdir}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"""
    with open(_LINUX_UNIT, "w", encoding="utf-8") as f:
        f.write(unit)
    r = _run("systemctl", "--user", "daemon-reload")
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "systemctl daemon-reload failed").strip())
    # Make the current graphical session's display available to the user manager
    # so the spawned server can open windows (best-effort; harmless if unset).
    _run("systemctl", "--user", "import-environment",
         "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY")
    r = _run("systemctl", "--user", "enable", "--now", UNIT + ".service")
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "systemctl enable failed").strip())


def _linux_uninstall():
    _run("systemctl", "--user", "disable", "--now", UNIT + ".service")
    try:
        os.remove(_LINUX_UNIT)
    except OSError:
        pass
    _run("systemctl", "--user", "daemon-reload")


def _linux_installed():
    return os.path.isfile(_LINUX_UNIT)


def _linux_pid():
    r = _run("systemctl", "--user", "show", "-p", "MainPID", "--value", UNIT + ".service")
    if r.returncode != 0:
        return None
    val = (r.stdout or "").strip()
    return int(val) if val.isdigit() and int(val) > 0 else None


# ---------------------------------------------------------------- Windows --- #
def _win_startup_dir():
    return os.path.join(os.environ.get("APPDATA", os.path.join(HOME, "AppData", "Roaming")),
                        "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def _win_vbs_path():
    return os.path.join(_win_startup_dir(), UNIT + ".vbs")


def _pythonw(python_exe):
    """Prefer pythonw.exe (no console window) next to the given interpreter."""
    d = os.path.dirname(python_exe)
    cand = os.path.join(d, "pythonw.exe")
    return cand if os.path.isfile(cand) else python_exe


def _vbs_quote(s):
    # Inside a VBS double-quoted string, a literal " is written as "".
    return s.replace('"', '""')


def _win_install(python_exe, script, workdir):
    os.makedirs(_win_startup_dir(), exist_ok=True)
    pyw = _pythonw(python_exe)
    # WshShell.Run with window style 0 launches hidden; pythonw has no console.
    vbs = (
        'Set s = CreateObject("WScript.Shell")\r\n'
        f's.CurrentDirectory = "{_vbs_quote(workdir)}"\r\n'
        f's.Run """{_vbs_quote(pyw)}"" ""{_vbs_quote(script)}"" --service", 0, False\r\n'
    )
    with open(_win_vbs_path(), "w", encoding="utf-8") as f:
        f.write(vbs)
    # Start it now too, so it runs without waiting for the next login.
    try:
        DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([pyw, script, "--service"], cwd=workdir, close_fds=True,
                         creationflags=DETACHED)
    except Exception as e:
        raise RuntimeError(f"installed the startup item but could not start it now: {e}")


def _win_uninstall():
    try:
        os.remove(_win_vbs_path())
    except OSError:
        pass
    # Best-effort stop of a running instance (match our script in the command line).
    try:
        subprocess.run(
            ["wmic", "process", "where",
             "name='pythonw.exe' and CommandLine like '%human_loop_server.py%'",
             "call", "terminate"],
            capture_output=True, text=True)
    except Exception:
        pass


def _win_installed():
    return os.path.isfile(_win_vbs_path())


def _win_pid():
    # Best-effort: find a pythonw running our script. None if we can't tell.
    try:
        r = subprocess.run(
            ["wmic", "process", "where",
             "name='pythonw.exe' and CommandLine like '%human_loop_server.py%'",
             "get", "ProcessId"],
            capture_output=True, text=True)
        m = re.search(r"(\d+)", r.stdout or "")
        return int(m.group(1)) if m else None
    except Exception:
        return None


# ------------------------------------------------------------- dispatch ---- #
def _backend():
    if IS_MACOS:
        return _mac_install, _mac_uninstall, _mac_installed, _mac_pid, _MAC_PLIST
    if IS_LINUX:
        return _linux_install, _linux_uninstall, _linux_installed, _linux_pid, _LINUX_UNIT
    if IS_WINDOWS:
        return _win_install, _win_uninstall, _win_installed, _win_pid, _win_vbs_path()
    raise RuntimeError(f"Background service is not supported on this platform ({sys.platform}).")


def install(python_exe, script, workdir):
    _backend()[0](python_exe, script, workdir)


def uninstall():
    _backend()[1]()


def is_installed():
    return _backend()[2]()


def running_pid():
    return _backend()[3]()


def describe_location():
    return _backend()[4]
