# AutoCAD MCP Server

[![M8ven Score](https://m8ven.ai/badge/mcp/streetparkourzombie/autocad-mcp-server)](https://m8ven.ai/mcp/streetparkourzombie/autocad-mcp-server?s=readme)

Maintainer: **Street Parkour Zombie** ([@StreetParkourZombie](https://github.com/StreetParkourZombie))

MCP server for AutoCAD LT automation and headless DXF generation.

Two backends, one API:

| Backend | Runtime | Requires AutoCAD? | Screenshot |
|---------|---------|-------------------|------------|
| **File IPC** | Windows Python | Yes — AutoCAD LT 2024+ (Windows) | Win32 PrintWindow |
| **ezdxf** | Any platform | No (headless) | matplotlib render |

The server exposes **8 consolidated tools** (`drawing`, `entity`, `layer`, `block`, `annotation`, `pid`, `view`, `system`) over the MCP stdio transport. An MCP client (Claude Desktop, Claude Code, Antigravity, etc.) connects and drives AutoCAD through natural-language requests.

> **Docs:** [INDEX.MD](docs/INDEX.MD) · [Met_Errors.md](docs/Met_Errors.md) (integration errors & fixes) · [System_Context.md](docs/System_Context.md) (runtime environment)

## Prerequisites (File IPC backend)

- **Windows 10/11** (the File IPC backend uses Win32 APIs: desktop switching, focus click, `SendInput`)
- **AutoCAD LT 2024 or newer** — AutoLISP support was added in LT 2024 for Windows. AutoCAD LT for Mac exists but does **not** support AutoLISP.
- **Python 3.10+** (Windows native — not WSL Python)
- **uv** package manager ([install guide](https://docs.astral.sh/uv/getting-started/installation/))

> The ezdxf headless backend works on any platform (Linux, macOS, WSL) for offline DXF generation without AutoCAD installed.

## Quick Start

### 1. Clone and install

```powershell
git clone https://github.com/StreetParkourZombie/AutoCAD-MCP-Server.git
cd AutoCAD-MCP-Server
uv sync
```

### 2. Load the LISP dispatcher in AutoCAD LT

Open AutoCAD LT and load `mcp_dispatch.lsp` using **APPLOAD**:

1. Type `APPLOAD` in the AutoCAD command line
2. Browse to `<repo>/lisp-code/mcp_dispatch.lsp`
3. Click **Load**
4. You should see: `=== MCP Dispatch v3.1 loaded ===` and `Ready for commands via (c:mcp-dispatch)`

> **Tip:** Add the file to your AutoCAD Startup Suite (in the APPLOAD dialog) so it loads automatically with every drawing.
>
> **Secure Load:** If AutoCAD prints `File load canceled`, the lisp directory is not trusted yet. The backend auto-trusts and auto-loads on first `initialize()` (appends to `TRUSTEDPATHS`, then `(load ...)`). Or add the folder permanently: AutoCAD Options → Security → Trusted Paths. See [docs/Met_Errors.md](docs/Met_Errors.md) §2.

### 3. Configure your MCP client

Add to your MCP client configuration (e.g. Claude Desktop `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "autocad-mcp": {
      "command": "C:\\path\\to\\autocad-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "autocad_mcp"],
      "env": { "AUTOCAD_MCP_BACKEND": "auto" }
    }
  }
}
```

**Key points:**

- The `command` must point to the **Windows Python** inside the project venv (not WSL python).
- `AUTOCAD_MCP_BACKEND` can be `auto` (default — tries File IPC, falls back to ezdxf), `file_ipc` (requires AutoCAD), or `ezdxf` (headless only).

#### Running from WSL

If your MCP client runs in WSL (e.g. Claude Code), launch the server through `cmd.exe` so it runs as a native Windows process:

```json
{
  "mcpServers": {
    "autocad-mcp": {
      "type": "stdio",
      "command": "cmd.exe",
      "args": ["/d", "/s", "/c", "cd /d C:\\path\\to\\autocad-mcp && .venv\\Scripts\\python.exe -m autocad_mcp"],
      "env": { "AUTOCAD_MCP_BACKEND": "auto" }
    }
  }
}
```

### 4. Verify

From your MCP client, call:

```
system(operation="status")
```

You should see `backend: "file_ipc"` if AutoCAD is running, or `backend: "ezdxf"` for headless mode.

#### CLI bypass (no MCP client needed)

Call the server directly over stdio — for scripts, pipelines, or when an MCP client's bridge misbehaves:

```powershell
# list tools
.venv\Scripts\python.exe <path>\mcp_cli.py --list
# drawing info
.venv\Scripts\python.exe <path>\mcp_cli.py drawing info
# with data payload
.venv\Scripts\python.exe <path>\mcp_cli.py layer create --data '{"name":"WALLS","color":"1"}'
```

Exit code `0` = ok, `1` = tool error, `2` = bad usage.

## Tools

### `drawing` — File/drawing management

| Operation | Description | File IPC | ezdxf |
|-----------|-------------|----------|-------|
| `create` | Reset to clean drawing (erase all + purge) | Yes | Yes |
| `open` | Open an existing drawing | Yes | Yes (DXF) |
| `info` | Get entity count and layers | Yes | Yes |
| `save` | Save current drawing (to path if given) | Yes | Yes |
| `save_as_dxf` | Export as DXF | Yes | Yes |
| `plot_pdf` | Plot to PDF | Yes | No |
| `purge` | Purge unused objects | Yes | Yes |
| `get_variables` | Get system variables by name | Yes | Yes |
| `undo` | Undo last operation | Yes | No |
| `redo` | Redo last undone operation | Yes | No |

### `entity` — Entity CRUD + modification

**Create:** `create_line`, `create_circle`, `create_polyline`, `create_rectangle`, `create_arc`, `create_ellipse`, `create_mtext`, `create_hatch`

**Read:** `list`, `count`, `get`

**Modify:** `copy`, `move`, `rotate`, `scale`, `mirror`, `offset`\*, `array`, `fillet`\*, `chamfer`\*, `erase`

> \* `offset`, `fillet`, `chamfer` are File IPC only (not supported in ezdxf headless backend).

### `layer` — Layer management

`list`, `create`, `set_current`, `set_properties`, `freeze`, `thaw`, `lock`, `unlock`

### `block` — Block operations

| Operation | File IPC | ezdxf |
|-----------|----------|-------|
| `list` | Yes | Yes |
| `insert` | Yes | Yes |
| `insert_with_attributes` | Yes | Yes |
| `get_attributes` | Yes | Yes |
| `update_attribute` | Yes | Yes |
| `define` | No | Yes |

### `annotation` — Text, dimensions, leaders

`create_text`, `create_dimension_linear`, `create_dimension_aligned`, `create_dimension_angular`, `create_dimension_radius`, `create_leader`

### `pid` — P&ID operations (CTO symbol library)

`setup_layers`, `insert_symbol`, `list_symbols`, `draw_process_line`, `connect_equipment`, `add_flow_arrow`, `add_equipment_tag`, `add_line_number`, `insert_valve`, `insert_instrument`, `insert_pump`, `insert_tank`

> P&ID symbol insertion requires the [CAD Tools Online](https://www.cadtoolsonline.com/) (CTO) P&ID Symbol Library installed at `C:\PIDv4-CTO\`. The ezdxf backend has built-in CTO library support. For the File IPC backend, some P&ID operations require additional LISP helpers — see the P&ID section in the wiki for setup details.

### `view` — Viewport and screenshot

| Operation | Description |
|-----------|-------------|
| `zoom_extents` | Zoom to show all entities |
| `zoom_window` | Zoom to a specified window |
| `get_screenshot` | Capture current AutoCAD view as PNG |

Screenshots use `PrintWindow` (Win32) for the File IPC backend — works even when AutoCAD is minimized or in the background. The ezdxf backend renders via matplotlib.

### `system` — Server management

`status`, `health`, `get_backend`, `runtime`, `init`, `execute_lisp`, `acad_command`

> `execute_lisp` runs arbitrary AutoLISP code (File IPC only). Pass `data: {code: "(+ 1 2)"}`. This turns the server into an extensible automation platform — any valid AutoLISP expression can be executed.

#### `acad_command` — run any AutoCAD command (File IPC only)

The ~60 commands wrapped with typed parameters elsewhere in this API are only the commands that were convenient to expose. `acad_command` removes that ceiling: **any AutoCAD command that has a command-line (script) form** becomes reachable — which is most of the ribbon. TRIM, EXTEND, JOIN, BLOCK, SPLINE, MIRROR3D, ARRAY, OFFSET, and thousands more.

```json
{
  "operation": "acad_command",
  "data": { "args": ["_.TRIM", "", "_F", "", ""] }
}
```

**Token format:**

- Tokens are passed to AutoLISP's `command` verbatim, in order. An **empty string is a bare Enter**, which is how a prompt is accepted without input (`""` = accept default, `_F` = pick the `F` option of an English command).
- Prefix commands with `_` for the English name regardless of UI language — `_.TRIM`, not `_.CẮT`. Add `.` for the global variant (`_.LINE`).
- A **space inside a token is encoded as `~`** by the Python side and decoded back before dispatch, so a token can carry a value containing spaces (a file path, a text style) without colliding with anything.

**Security policy — a denylist, not an allowlist:**

An allowlist would reintroduce the coverage limit this feature exists to remove, so the gate blocks three specific classes of command and allows everything else:

| Class | Commands refused | Why |
|-------|------------------|-----|
| Arbitrary code injection | `SCRIPT`, `APPLOAD`, `LOAD`, `VL-LOAD`, `VL-LOAD-COM` | Would let a caller run any LISP, which is equivalent to bypassing the whole policy |
| Process escape | `SHELL`, `STARTAPP`, `RUN`, `BROWSER` | Would let a caller launch other programs |
| Security reconfiguration | `SECURITY`, `SECURELOAD`, `TRUSTEDPATHS`, `ACAD`, `R13`, `R14`, `R15` | Would let a caller disable Secure Load or rewrite the profile |

Everything else stays reachable, **including destructive-but-normal CAD commands** such as `ERASE`, `SAVE`, `NEW`, and `SAVEAS`. Those are no more dangerous than a person typing them by hand, and blocking them would make the tool unusable for real work. Callers are expected to be an automated agent driving a human's own drawing, not an untrusted tenant.

> [!IMPORTANT]
> `execute_lisp` has **no** such denylist — it runs arbitrary AutoLISP by design. The `acad_command` denylist is a guardrail against accidental or careless calls, not a security boundary. If an untrusted party can reach this MCP server, they can already reach `execute_lisp`.

A refused command returns an error rather than doing nothing:

```text
acad-command: refused by policy: _.SHELL
```

## Architecture

```
MCP Client (Claude)
    │  stdio (JSON-RPC)
    ▼
Python MCP Server (autocad_mcp)
    │
    ├── File IPC Backend ──► C:/temp/*.json ──► mcp_dispatch.lsp (AutoCAD LT)
    │   PostMessageW(WM_CHAR) to MDIClient — no focus steal
    │
    └── ezdxf Backend ──► in-memory DXF (headless, no AutoCAD needed)
```

**Input chain (AutoCAD 2026 full / WPF CLI):** posted `WM_CHAR` is ignored by the WPF command line, so the backend switches to `WinSta0\Default`, raises the main window above overlapping helper windows (e.g. Help), clicks the **CLI Palette** input row to move focus, then injects real keystrokes via `SendInput`, triggering `(c:mcp-dispatch)` in AutoLISP. Focus returns to your app automatically (no permanent focus steal). On AutoCAD LT the CLI is a classic `MDIClient` edit — the same code path works.

All desktop-sensitive calls (`EnumWindows`, `IsWindow`, `SetCursorPos`, `SendInput`) run while the thread is on the Default desktop; MCP servers launched by Antigravity otherwise run on a private `agy-*` desktop where foreign windows are invisible. See [docs/System_Context.md](docs/System_Context.md).

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTOCAD_MCP_BACKEND` | `auto` | Backend selection: `auto`, `file_ipc`, `ezdxf` |
| `AUTOCAD_MCP_IPC_DIR` | `C:/temp` | Directory for IPC command/result JSON files (must match on both Python and LISP sides) |
| `AUTOCAD_MCP_IPC_TIMEOUT` | `10.0` | IPC command timeout in seconds (1-300) |
| `AUTOCAD_MCP_ONLY_TEXT` | `false` | Disable screenshot capture (text feedback only) |

> **Note:** If you change `AUTOCAD_MCP_IPC_DIR`, you must also update the `*mcp-ipc-dir*` variable in `mcp_dispatch.lsp` to match.

## Troubleshooting

| Symptom | Likely cause | See |
|---------|--------------|-----|
| `File load canceled` on `(load ...)` | Secure Load — path not in `TRUSTEDPATHS` | [Met_Errors §2](docs/Met_Errors.md) |
| Typed commands do nothing | WPF CLI ignores posted messages; wrong hwnd | [Met_Errors §3](docs/Met_Errors.md) |
| Clicks hit the wrong window | Help window overlaying the CLI Palette | [Met_Errors §4](docs/Met_Errors.md) |
| `window not found` / `Invalid window handle` | Stale handle or cross-desktop `IsWindow` | [Met_Errors §5](docs/Met_Errors.md) |
| IPC timeout although cmd file was consumed | Unparseable result (Vietnamese `\U+XXXX` escapes) | [Met_Errors §6](docs/Met_Errors.md) |
| MCP client: `Invalid request parameters` | Client bridge sends `arguments` as string (not this server) | [Met_Errors §7](docs/Met_Errors.md) |
| `acad-command: refused by policy: ...` | Command is on the denylist (by design) | [README §acad_command](#acad_command--run-any-autocad-command-file-ipc-only) |
| A `denylist` check silently allows a blocked command | `vl-string-translate` with `""` deletes nothing — the locale prefix was never stripped | [Met_Errors §8](docs/Met_Errors.md) |
| `(load ...)` reported success but the change did not take effect | AutoLISP `load` prints errors instead of raising; wrap it in `vl-catch-all-apply` | [Met_Errors §9](docs/Met_Errors.md) |

Ground truth for "did AutoCAD get it?": press **F2** and read the Text Window (command echo + LISP errors).

## Development

```powershell
uv sync
uv run pytest tests/ -v
```

## AutoCAD LT AutoLISP Compatibility

AutoLISP was added to AutoCAD LT in the **2024 release (Windows only)**. AutoCAD LT for Mac does not support AutoLISP.

| Supported (LT 2024+ Windows) | Not Supported |
|-------------------------------|---------------|
| `.lsp` / `.fas` / `.vlx` / `.dcl` | VLIDE (Visual LISP IDE) |
| All `vl-*` utility functions | `vlax-*` (ActiveX/COM) |
| File I/O (`open`, `read-line`, etc.) | Express Tools |
| Entity access (`entget`, `entmod`, etc.) | 3D operations |
| Selection sets | AutoLISP on Mac |

The `mcp_dispatch.lsp` dispatcher is fully compatible with LT 2024+.

## What's New in v3.2

- **`acad_command` passthrough** — Run *any* AutoCAD command that has a command-line form, not just the ~60 wrapped with typed parameters. This removes the coverage ceiling: TRIM, EXTEND, JOIN, MIRROR3D, ARRAY and the rest of the ribbon become reachable without adding a wrapper per command. Gated by a policy denylist (see [`system.acad_command`](#acad_command--run-any-autocad-command-file-ipc-only)).
- **Numeric command tokens** — `acad_command` accepts `["_.CIRCLE", "x,y", "1"]`; tokens are coerced to string, so a radius that arrives as a JSON number no longer crashes with `'int' object has no attribute 'replace'`.

## What's New in v3.1

- **`execute_lisp`** — Run arbitrary AutoLISP code via temp file pattern. Turns the server from a fixed command set into an extensible automation platform.
- **Undo / Redo** — Single-step undo and redo via `drawing` tool.
- **Drawing open** — Open existing `.dwg` files programmatically (FILEDIA suppressed).
- **Drawing create** — Now resets current drawing (erase all + purge) instead of `_.NEW`, preserving the LISP dispatcher namespace.
- **Drawing save with path** — `save` with a `path` parameter uses SAVEAS; without path uses QSAVE.
- **`get_variables` fix** — Respects the `names` parameter; returns requested variables with proper type handling.
- **Polyline/leader fix** — Point arrays properly encoded via semicolon-delimited format.
- **ESC prefix** — Sends 2x ESC before each dispatch to cancel stale pending commands from prior timeouts.
- **UTF-8/cp1252 fallback** — Handles non-ASCII characters in LISP result files (AutoCAD writes Windows-1252).
- **Configurable IPC timeout** — `AUTOCAD_MCP_IPC_TIMEOUT` env var (1–300 seconds, default 10).
- **Thread-safe backend init** — `asyncio.Lock` prevents parallel initialization races.

## License

MIT
