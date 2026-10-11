# Documentation Index

AutoCAD MCP Server — documentation map.

## Documents

| Document | Contents |
|----------|----------|
| [INDEX.MD](INDEX.MD) | This file — documentation map |
| [Met_Errors.md](Met_Errors.md) | Errors encountered during integration and their root causes/fixes. §1–§14 found by reading code; §15–§21 found by driving live AutoCAD |
| [System_Context.md](System_Context.md) | Runtime context: windows, desktops, IPC protocol state, colour/BYLAYER semantics, destructive-operation surface |
| [../README.md](../README.md) | Installation, configuration, tool reference, entity colour and batch delete usage |

## Key Topics

### Architecture
- MCP client → Python server (`autocad_mcp`) → two backends: **File IPC** (drives live AutoCAD) and **ezdxf** (headless).
- File IPC protocol: Python writes `C:/temp/autocad_mcp_cmd_{id}.json` → types `(c:mcp-dispatch)` + Enter into the AutoCAD command line → LISP dispatches via whitelist map → writes `autocad_mcp_result_{id}.json` → Python polls.
- **Generic command passthrough (`system.acad_command`)** — bypasses the whitelist map: any AutoCAD command with a command-line form becomes reachable, which covers most of the ribbon without one wrapper per command. Gated by a policy denylist in `mcp_dispatch.lsp` (`mcp-cmd-denied-p`), not an allowlist — an allowlist would recreate the coverage limit.

### Trust & Compliance
- **Tool annotations** — all 8 tools declare the four MCP hints (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) with values matching actual handler behaviour. `system` is declared read-write/destructive because `execute_lisp` and `acad_command` reach arbitrary commands; `view` is the only read-only tool. Asserted by `tests/test_view_annotation_tools.py::TestToolAnnotations` so a new tool cannot ship without them.
- **M8ven Trust Index** — the repo is listed and Live Monitored (re-scored on every push). Badge is in the README; the scan checks tool hints and per-tool test coverage, both now at 8/8.

### Known Error Classes
See [Met_Errors.md](Met_Errors.md) for full detail. **§1–§14** were found by reading the code; **§15–§21** were found by driving live AutoCAD, and shipped with a fully green 213-test suite.

**Found by reading the code:**

1. **Stale/wrong window handle** — finder picking Help/Text Window; `IsWindow` returning False across desktops.
2. **Click obstruction** — Help window overlaying the WPF CLI Palette, stealing injected clicks.
3. **Unparseable LISP result** — Vietnamese layer names (`\U+XXXX` escapes + cp1252) breaking `json.loads`, surfacing as bogus IPC timeouts.
4. **Secure Load blocked LISP** — empty `TRUSTEDPATHS` → `File load canceled`.
5. **MCP client bridge bug** — `call_mcp_tool` sending `arguments` as string instead of record (client-side, not server).
6. **Silent AutoLISP no-ops** — `vl-string-translate` with `""` deletes nothing; `load` never raises; `member` compares case-sensitively; `vl-string-downcase` / `vl-symbol-function` do not exist. Every one returned a well-formed value instead of an error.
7. **JSON coercion on passthrough tokens** — a numeric token (`"1"` as radius) arrives as Python `int` through the schema; `int` has no `.replace` (§10).
8. **`entget` after `entdel` returns nil** — batch delete read each handle *after* erasing the entity, so `handles` came back empty in every successful response (§13).
9. **Comment-stripping misreads `;` as a comment** — a naive paren counter treated `";"` (the real delimiter passed to `mcp-split-string`) as a comment marker and deleted real closing parentheses, reporting a phantom imbalance of 13 in a file with none (§14).

**Found by live AutoCAD testing — none of these fail a test:**

10. **Common Lisp leaking into AutoLISP** — calling a function held in a variable requires `apply`, not `(fn params)` (§15); `(last lst n)` raises `too many arguments` because `last` is unary; `(nth k lst)` returns the *element*, not the *tail* (§17).
11. **Bare `entmod` is a silent no-op** — `(entmod (list (cons 62 aci)))` returns success and changes nothing. Colour must be appended onto `(entget ent)` (§16).
12. **AutoLISP truthiness** — only `nil` is false, so `(atoi "")` = `0` passes an `if` test and then trims the match list to empty; `delete_by` reports `matched: 0` against a layer full of entities (§18).
13. **An empty JSON string decoding as `"`** — a stray quote is truthy, so every `(if <field>)` guard downstream passed and the handle filter rejected the whole drawing (§19).
14. **Parallel live tests fabricating failures** — two scripts sharing `C:/temp` consume each other's command files. Run live tests one at a time (§21).

> **Diagnostic rule:** these do not appear in the AutoCAD Text Window and do not time out. To find them, probe one expression at a time and print what it **returns** — a predicate reading correctly in the source can still return `nil`. The same rule applies to static analysis: a checker that reports a problem is making claims about the code, so verify the checker before rewriting the code.

### Entity Colour & Batch Delete (v3.3)

- **Colour is a per-entity ACI value** (AutoCAD Color Index 0–256), previously only reachable at layer level. Accepted forms: name (`red`, `cyan`, ...), raw index, or `bylayer` / `byblock`. Unknown names degrade to 7 (white) rather than raising.
- **BYLAYER inheritance is the sharp edge.** Omitting `color` on a create leaves the entity at 256, inheriting its layer's colour. Writing an explicit colour **breaks that link** — `entmod` group 62 overwrites the inherited value. `bylayer` restores it. Documented on the tool docstring and in the README because the failure is invisible until someone recolours a layer.
- **DXF group 62 is the ACI colour; 420 is true colour.** The colour-by-name path uses 62. `test_lisp_dispatcher.py` asserts both backends map the same names to the same indices, so `red` cannot mean one thing in ezdxf and another in AutoCAD.
- **Batch delete reports `matched` and `deleted` separately** — a partial delete is visible in the response rather than swallowed. Selectors are ANDed; **an empty selector set is refused**, never treated as "delete everything".
- **`entity_delete_all` requires `confirm: true`** and is the only irreversible operation in the tool. `drawing` → `undo` is the way back, and only while the undo stack holds it.

### Dispatcher Test Coverage

`mcp_dispatch.lsp` is plain text loaded at runtime — nothing in pytest executes it, so a paren typo surfaced as an opaque `dispatcher not loaded` error minutes later (§9). `tests/test_lisp_dispatcher.py` now walks the source with a string-aware scanner and asserts paren balance, unique defun names, a handler behind every `cond` branch, agreement between the LISP colour map and `_color_to_int`, and the specific AutoLISP shapes behind §15–§19 (an `apply` call, an `entget` append, no two-argument `last`, no `nth` used as a tail, a `> 0` guard, an empty-string branch returning `nil`).

> [!NOTE]
> Pytest can prove the dispatcher is **well-formed**. It cannot prove AutoCAD has it **loaded**, nor that it behaves correctly once loaded — the suite was green while five of six colour/delete paths were broken. After any LISP edit, reload and exercise the path against a throwaway layer. See [System_Context §11](System_Context.md).

### Runtime Context
See [System_Context.md](System_Context.md):
- Antigravity MCP server runs on a private desktop (`agy-*`); all Win32 enumeration/typing must switch to `WinSta0\Default`.
- AutoCAD 2026 full (MEP) hosts the command line in a WPF **CLI Palette**, not `MDIClient` — posted `WM_CHAR` is ignored; real `SendInput` after click-focus is required.

## Conventions
- Docs use `snake_case` tool operation names matching the MCP tool schemas.
- Code paths are Windows-style (`C:\Dev\autocad-mcp\...`); LISP paths use forward slashes.
- New backend operations land in **five** places or they fail silently on one backend: `base.py` (declaration), `ezdxf_backend.py`, `file_ipc.py` (dispatcher), `mcp_dispatch.lsp` (cond branch + defun), `server.py` (routing). `tests/test_ipc_protocol.py::EXPECTED_COMMANDS` must gain the kebab command name too.
