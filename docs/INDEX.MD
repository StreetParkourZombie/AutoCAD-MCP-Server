# Documentation Index

AutoCAD MCP Server — documentation map.

## Documents

| Document | Contents |
|----------|----------|
| [INDEX.MD](INDEX.MD) | This file — documentation map |
| [Met_Errors.md](Met_Errors.md) | Errors encountered during integration and their root causes/fixes |
| [System_Context.md](System_Context.md) | Runtime context: windows, desktops, IPC protocol state |
| [../README.md](../README.md) | Installation, configuration, tool reference |

## Key Topics

### Architecture
- MCP client → Python server (`autocad_mcp`) → two backends: **File IPC** (drives live AutoCAD) and **ezdxf** (headless).
- File IPC protocol: Python writes `C:/temp/autocad_mcp_cmd_{id}.json` → types `(c:mcp-dispatch)` + Enter into the AutoCAD command line → LISP dispatches via whitelist map → writes `autocad_mcp_result_{id}.json` → Python polls.
- **Generic command passthrough (`system.acad_command`)** — bypasses the whitelist map: any AutoCAD command with a command-line form becomes reachable, which covers most of the ribbon without one wrapper per command. Gated by a policy denylist in `mcp_dispatch.lsp` (`mcp-cmd-denied-p`), not an allowlist — an allowlist would recreate the coverage limit.

### Known Error Classes
See [Met_Errors.md](Met_Errors.md) for full detail:
1. **Stale/wrong window handle** — finder picking Help/Text Window; `IsWindow` returning False across desktops.
2. **Click obstruction** — Help window overlaying the WPF CLI Palette, stealing injected clicks.
3. **Unparseable LISP result** — Vietnamese layer names (`\U+XXXX` escapes + cp1252) breaking `json.loads`, surfacing as bogus IPC timeouts.
4. **Secure Load blocked LISP** — empty `TRUSTEDPATHS` → `File load canceled`.
5. **MCP client bridge bug** — `call_mcp_tool` sending `arguments` as string instead of record (client-side, not server).
6. **Silent AutoLISP no-ops** — `vl-string-translate` with `""` deletes nothing; `load` never raises; `member` compares case-sensitively; `vl-string-downcase` / `vl-symbol-function` do not exist. Every one returned a well-formed value instead of an error.

> **Diagnostic rule:** these do not appear in the AutoCAD Text Window and do not time out. To find them, probe one expression at a time and print what it **returns** — a predicate reading correctly in the source can still return `nil`.

### Runtime Context
See [System_Context.md](System_Context.md):
- Antigravity MCP server runs on a private desktop (`agy-*`); all Win32 enumeration/typing must switch to `WinSta0\Default`.
- AutoCAD 2026 full (MEP) hosts the command line in a WPF **CLI Palette**, not `MDIClient` — posted `WM_CHAR` is ignored; real `SendInput` after click-focus is required.

## Conventions
- Docs use `snake_case` tool operation names matching the MCP tool schemas.
- Code paths are Windows-style (`C:\Dev\autocad-mcp\...`); LISP paths use forward slashes.
