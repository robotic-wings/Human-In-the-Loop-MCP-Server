# Human-In-the-Loop MCP Server

![](https://badge.mcpx.dev?type=server 'MCP Server')
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![PyPI version](https://badge.fury.io/py/hitl-mcp-server.svg)](https://badge.fury.io/py/hitl-mcp-server)

A powerful **Model Context Protocol (MCP) Server** that enables AI assistants like Claude to interact with humans through intuitive GUI dialogs. This server bridges the gap between automated AI processes and human decision-making by providing real-time user input tools, choices, confirmations, and feedback mechanisms.

![demo](demo.gif)

## 🚀 Features

### 💬 Interactive Dialog Tools
- **Text Input**: Get text, numbers, or other data from users with validation
- **Multiple Choice**: Present options for single or multiple selections  
- **Multi-line Input**: Collect longer text content, code, or detailed descriptions
- **Confirmation Dialogs**: Ask for yes/no decisions before proceeding with actions
- **Information Messages**: Display notifications, status updates, and results
- **Delegate a Task to a Human**: Hand off a real-world task and wait (long-running) for the human to report **Completed / Failed / Still-progressing**, with an optional written note and file/image attachments. Survives client tool-call timeouts via a heartbeat protocol, and archives every submission to a local **Outbox**.
- **Health Check**: Monitor server status and GUI availability

### 🎨 Modern Cross-Platform GUI
- **Windows**: Modern Windows 11-style interface with beautiful styling, hover effects, and enhanced visual design
- **macOS**: Native macOS experience with SF Pro Display fonts and proper window management
- **Linux**: Ubuntu-compatible GUI with modern styling and system fonts

### ⚡ Advanced Features
- **Non-blocking Operation**: All dialogs run in separate threads to prevent blocking
- **Timeout Protection**: Configurable 5-minute timeouts prevent hanging operations
- **Platform Detection**: Automatic optimization for each operating system
- **Modern UI Design**: Beautiful interface with smooth animations and hover effects
- **Error Handling**: Comprehensive error reporting and graceful recovery
- **Keyboard Navigation**: Full keyboard shortcuts support (Enter/Escape)

## 📦 Installation & Setup

### Quick Install with uvx (Recommended)

The easiest way to use this MCP server is with `uvx`:

```bash
# Install and run directly
uvx hitl-mcp-server

# Or use the underscore version
uvx hitl_mcp_server
```

### Manual Installation

1. **Install from PyPI**:
   ```bash
   pip install hitl-mcp-server
   ```

2. **Run the server**:
   ```bash
   hitl-mcp-server
   # or
   hitl_mcp_server
   ```

### Development Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/GongRzhe/Human-In-the-Loop-MCP-Server.git
   cd Human-In-the-Loop-MCP-Server
   ```

2. **Install in development mode**:
   ```bash
   pip install -e .
   ```

## 🔧 Claude Desktop Configuration

To use this server with Claude Desktop, add the following configuration to your `claude_desktop_config.json`:

### Using uvx (Recommended)

```json
{
  "mcpServers": {
    "human-in-the-loop": {
      "command": "uvx",
      "args": ["hitl-mcp-server"]
    }
  }
}
```

### Using pip installation

```json
{
  "mcpServers": {
    "human-in-the-loop": {
      "command": "hitl-mcp-server",
      "args": []
    }
  }
}
```

### Configuration File Locations

- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

### Important Note for macOS Users

**Note:** You may need to allow Python to control your computer in **System Preferences > Security & Privacy > Accessibility** for the GUI dialogs to work properly.


After updating the configuration, restart Claude Desktop for the changes to take effect.

## 🛠️ Available Tools

### 1. `get_user_input`
Get single-line text, numbers, or other data from users.

**Parameters:**
- `title` (str): Dialog window title
- `prompt` (str): Question/prompt text  
- `default_value` (str): Pre-filled value (optional)
- `input_type` (str): "text", "integer", or "float" (default: "text")

**Example Usage:**
```python
result = await get_user_input(
    title="Project Setup",
    prompt="Enter your project name:",
    default_value="my-project",
    input_type="text"
)
```

### 2. `get_user_choice`
Present multiple options for user selection.

**Parameters:**
- `title` (str): Dialog window title
- `prompt` (str): Question/prompt text
- `choices` (List[str]): Available options
- `allow_multiple` (bool): Allow multiple selections (default: false)

**Example Usage:**
```python
result = await get_user_choice(
    title="Framework Selection",
    prompt="Choose your preferred framework:",
    choices=["React", "Vue", "Angular", "Svelte"],
    allow_multiple=False
)
```

### 3. `get_multiline_input`
Collect longer text content, code, or detailed descriptions.

**Parameters:**
- `title` (str): Dialog window title
- `prompt` (str): Question/prompt text
- `default_value` (str): Pre-filled text (optional)

**Example Usage:**
```python
result = await get_multiline_input(
    title="Code Review",
    prompt="Please provide your detailed feedback:",
    default_value=""
)
```

### 4. `show_confirmation_dialog`
Ask for yes/no confirmation before proceeding.

**Parameters:**
- `title` (str): Dialog window title
- `message` (str): Confirmation message

**Example Usage:**
```python
result = await show_confirmation_dialog(
    title="Delete Confirmation",
    message="Are you sure you want to delete these 5 files? This action cannot be undone."
)
```

### 5. `show_info_message`
Display information, notifications, or status updates.

**Parameters:**
- `title` (str): Dialog window title
- `message` (str): Information message

**Example Usage:**
```python
result = await show_info_message(
    title="Process Complete",
    message="Successfully processed 1,247 records in 2.3 seconds!"
)
```

### 6. `assign_task_to_human`
Delegate a real-world task to a human and wait — potentially for a long time — for them to report back, optionally with a written note and file/image attachments.

**Parameters:**
- `task_title` (str): Short title of the task.
- `task_description` (str): Detailed instructions for the human.
- `task_id` (str, optional): Leave empty to **start** a new task. Pass back the previous call's `task_id` to **continue** an existing task (after a heartbeat or an interim update) — this revives the same window without losing the human's work.
- `client_timeout_seconds` (int, default `240`): **Your own per-tool-call timeout.** Claude Desktop hard-codes ~240s, so pass `240`. Claude Code has no timeout, so pass a large value like `3600`.
- `context_note` (str, optional): Extra context shown to the human.

**One window = one submission.** Whenever the human clicks a button, the current note + attachments are sent to the AI and **the window closes**. The only difference between the buttons is whether the task is finished:
- **✅ Completed** / **❌ Failed** — terminal; the task ends.
- **⏳ Still progressing** — a formal interim update; the task is *not* finished. The AI decides whether to reopen a fresh window (by calling again with the same `task_id`, optionally replying via `context_note`) or to finish. (An empty interim update is rejected in the UI to avoid accidental blank submissions.)

**Long-running heartbeat protocol (important):**
Claude Desktop enforces a hard ~4-minute per-tool-call timeout that cannot be disabled from the server. To keep the human's dialog alive indefinitely, the server returns a **heartbeat** shortly before your `client_timeout_seconds` elapses:

- `status: "heartbeat"` (`human_action: false`) → the human hasn't submitted anything yet. **Do not respond to the user or reason about the task — immediately call the tool again with the same `task_id`.** The same window (and everything typed in it) stays intact; a countdown warns the human a few seconds before each sync so they can pause.
- `status: "in_progress"` (`human_action: true`) → a genuine interim update; **the window has closed** and the task isn't finished. Process it, then decide: re-call with the same `task_id` to reopen a fresh window, or finish.
- `status: "completed"` / `"failed"` / `"cancelled"` → terminal (`needs_continuation: false`); the `task_id` is gone.

Human submissions with attachments are returned as **content blocks**: images inline (the model can see them) and other files as embedded resources, with an ~25 MB cap (oversized files are referenced by path instead). Every submission is also archived to the **Outbox** (see below).

**Example Usage:**
```python
# First call — starts the task and opens the dialog
res = await assign_task_to_human(
    task_title="Reboot the staging server",
    task_description="Please reboot staging and confirm the app comes back up.",
    client_timeout_seconds=240,   # Claude Desktop; use 3600 for Claude Code
)
# If res["status"] == "heartbeat": call again with res["task_id"] (no user-facing reply)
# When res is a content list with status "completed": you're done.
```

### 7. `health_check`
Check server status and GUI availability.

**Example Usage:**
```python
status = await health_check()
# Returns detailed platform and functionality information
```

## 📤 Outbox (发件箱) & Viewer

Every human submission sent through `assign_task_to_human` (the AI's command + description, the human's note, and copies of all attachments) is archived to disk so nothing is lost when the window is cleared or closed.

- **Location:** `~/.human_loop_outbox/` by default, or set the `HUMAN_LOOP_OUTBOX_DIR` environment variable.
- **Layout:** one directory per submission, each containing an `entry.json` and an `attachments/` folder with copies of the files.

A standalone, dependency-free viewer (`outbox.py`, classic Windows 9x styling) lets you browse, open attachments, and delete entries:

```bash
python outbox.py
# Uses the same location; override with:
HUMAN_LOOP_OUTBOX_DIR=/path/to/outbox python outbox.py
```

## 📋 Response Format

All tools return structured JSON responses:

```json
{
    "success": true,
    "user_input": "User's response text",
    "cancelled": false,
    "platform": "windows",
    "input_type": "text"
}
```

**Common Response Fields:**
- `success` (bool): Whether the operation completed successfully
- `cancelled` (bool): Whether the user cancelled the dialog
- `platform` (str): Operating system platform
- `error` (str): Error message if operation failed

**Tool-Specific Fields:**
- **get_user_input**: `user_input`, `input_type`
- **get_user_choice**: `selected_choice`, `selected_choices`, `allow_multiple`
- **get_multiline_input**: `user_input`, `character_count`, `line_count`
- **show_confirmation_dialog**: `confirmed`, `response`
- **show_info_message**: `acknowledged`
- **assign_task_to_human**: `status` (`heartbeat` / `in_progress` / `completed` / `failed` / `cancelled` / `session_not_found` / `expired`), `human_action`, `needs_continuation`, `task_id`. Human submissions are returned as a content list (summary text + inline images/files) rather than a plain dict.

## 🧠 Best Practices for AI Integration

### When to Use Human-in-the-Loop Tools

1. **Ambiguous Requirements** - When user instructions are unclear
2. **Decision Points** - When you need user preference between valid alternatives
3. **Creative Input** - For subjective choices like design or content style
4. **Sensitive Operations** - Before executing potentially destructive actions
5. **Missing Information** - When you need specific details not provided
6. **Quality Feedback** - To get user validation on intermediate results

### Example Integration Patterns

#### File Operations
```python
# Get target directory
location = await get_user_input(
    title="Backup Location",
    prompt="Enter backup directory path:",
    default_value="~/backups"
)

# Choose backup type
backup_type = await get_user_choice(
    title="Backup Options",
    prompt="Select backup type:",
    choices=["Full Backup", "Incremental", "Differential"]
)

# Confirm before proceeding
confirmed = await show_confirmation_dialog(
    title="Confirm Backup",
    message=f"Create {backup_type['selected_choice']} backup to {location['user_input']}?"
)

if confirmed['confirmed']:
    # Perform backup
    await show_info_message("Success", "Backup completed successfully!")
```

#### Content Creation
```python
# Get content requirements
requirements = await get_multiline_input(
    title="Content Requirements",
    prompt="Describe your content requirements in detail:"
)

# Choose tone and style
tone = await get_user_choice(
    title="Content Style",
    prompt="Select desired tone:",
    choices=["Professional", "Casual", "Friendly", "Technical"]
)

# Generate and show results
# ... content generation logic ...
await show_info_message("Content Ready", "Your content has been generated successfully!")
```

## 🔍 Troubleshooting

### Common Issues

**GUI Not Appearing**
- Verify you're running in a desktop environment (not headless server)
- Check if tkinter is installed: `python -c "import tkinter"`
- Run health check: `health_check()` tool to diagnose issues

**Permission Errors (macOS)**
- Grant accessibility permissions in System Preferences > Security & Privacy > Accessibility
- Allow Python to control your computer
- Restart terminal after granting permissions

**Import Errors**
- Ensure package is installed: `pip install hitl-mcp-server`
- Check Python version compatibility (>=3.8 required)
- Verify virtual environment activation if using one

**Claude Desktop Integration Issues**
- Check configuration file syntax and location
- Restart Claude Desktop after configuration changes
- Verify uvx is installed: `pip install uvx`
- Test server manually: `uvx hitl-mcp-server`

**Dialog Timeout**
- Default timeout is 5 minutes (300 seconds)
- Dialogs will return with cancelled=true if user doesn't respond
- Ensure user is present when dialogs are triggered

### Debug Mode

Enable detailed logging by running the server with environment variable:
```bash
HITL_DEBUG=1 uvx hitl-mcp-server
```

## 🏗️ Development

### Project Structure
```
Human-In-the-Loop-MCP-Server/
├── human_loop_server.py       # Main server implementation
├── outbox.py                 # Standalone Outbox viewer (Win 9x style)
├── pyproject.toml            # Package configuration
├── README.md                 # Documentation
├── LICENSE                   # MIT License
├── .gitignore               # Git ignore rules
└── demo.gif                 # Demo animation
```

### Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature-name`
3. Make your changes with proper testing
4. Follow code style guidelines (Black, Ruff)
5. Add type hints and docstrings
6. Submit a pull request with detailed description

### Code Quality

- **Formatting**: Black (line length: 88)
- **Linting**: Ruff with comprehensive rule set
- **Type Checking**: MyPy with strict configuration
- **Testing**: Pytest for unit and integration tests

## 🌍 Platform Support

### Windows
- Windows 10/11 with modern UI styling
- Enhanced visual design with hover effects
- Segoe UI and Consolas font integration
- Full keyboard navigation support

### macOS
- Native macOS experience
- SF Pro Display system fonts
- Proper window management and focus
- Accessibility permission handling

### Linux
- Ubuntu/Debian compatible
- Modern styling with system fonts
- Cross-distribution GUI support
- Minimal dependency requirements

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🤝 Acknowledgments

- Built with [FastMCP](https://github.com/jlowin/fastmcp) framework
- Uses [Pydantic](https://pydantic-docs.helpmanual.io/) for data validation
- Cross-platform GUI powered by tkinter
- Inspired by the need for human-AI collaboration

## 🔗 Links

- **PyPI Package**: [https://pypi.org/project/hitl-mcp-server/](https://pypi.org/project/hitl-mcp-server/)
- **Repository**: [https://github.com/GongRzhe/Human-In-the-Loop-MCP-Server](https://github.com/GongRzhe/Human-In-the-Loop-MCP-Server)
- **Issues**: [Report bugs or request features](https://github.com/GongRzhe/Human-In-the-Loop-MCP-Server/issues)
- **MCP Protocol**: [Learn about Model Context Protocol](https://modelcontextprotocol.io/)

## 📊 Usage Statistics

- **Cross-Platform**: Windows, macOS, Linux
- **Python Support**: 3.8, 3.9, 3.10, 3.11, 3.12+
- **GUI Framework**: tkinter (built-in with Python)
- **Thread Safety**: Full concurrent operation support
- **Response Time**: < 100ms dialog initialization
- **Memory Usage**: < 50MB typical operation

---

**Made with ❤️ for the AI community - Bridging humans and AI through intuitive interaction**
