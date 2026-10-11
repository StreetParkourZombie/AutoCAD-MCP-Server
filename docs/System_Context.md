# System Context

Runtime environment notes for the AutoCAD MCP File IPC backend — how this system behaves on a real machine (Windows 11, AutoCAD MEP 2026 Education, Antigravity IDE).

---

## 1. Process & Window Layout

```
acad.exe (AutoCAD MEP 2026 — Education, NON COMMERCIAL)
├── main window        "AutoCAD MEP 2026 - EDUCATION ... - [<drawing>.dwg]"   ← target
├── Help window        "AutoCAD MEP 2026 - Help"     ← overlays main; minimize before clicking
├── MDIClient          (LT-era child; NOT where 2026 full hosts the CLI)
├── "CLI Palette"      ← WPF palette bar
│   └── HwndWrapper    ← actual command-line input surface (click target)
└── Text Window        "F2" log (command echo, LISP errors) — transient
```

- Finder (`find_autocad_window`) must pick the **`.dwg` document window**, skipping `Help` / `Text Window`.
- All handles must be validated with `IsWindow` **while on the Default desktop**.

## 2. Desktop Topology (critical)

| Desktop | Owner | Sees |
|---------|-------|------|
| `WinSta0\Default` | user / AutoCAD | all real windows |
| `agy-*` (private) | Antigravity MCP server process | foreign windows invisible |

Consequences:

- `EnumWindows`, `GetWindowText`, `IsWindow`, `IsWindowVisible`, `SetCursorPos`, `SendInput` behave incorrectly (empty/False) from the private desktop.
- Every Win32 operation runs inside a thread-desktop switch:
  1. `OpenDesktopW("Default")` → `GetThreadDesktop` → `SetThreadDesktop(Default)`
  2. do the work (enumerate / click / type)
  3. `SetThreadDesktop(prev)` + `CloseDesktop` in `finally`
- Helper: `_enum_on_default_desktop(predicate)` captures titles **during** the switch; returned handles are re-validated there, never after switching back.

## 3. Input Chain (proven working)

Posted `WM_CHAR` **never** reaches the WPF CLI. The working path:

```
SetThreadDesktop(Default)
→ minimize *Help* windows (same process)
→ SetWindowPos(main, HWND_TOP)      # must raise z-order (no SWP_NOZORDER)
→ SetForegroundWindow(main)         # + AttachThreadInput fallback
→ SetCursorPos + click CLI input    # moves focus to HwndWrapper
→ ESC ESC                           # cancel stale pending command
→ SendInput chars (KEYEVENTF_UNICODE)
→ SendInput VK_RETURN               # execute
→ restore previous desktop
```

Evidence: marker `ZZTEST999` echoed in Text Window as `command "ZZTEST999" *Cancel*` (unknown command → proves it arrived).

## 4. IPC Protocol State

Files in `C:/temp` (must match `*mcp-ipc-dir*` in LISP):

```
autocad_mcp_cmd_{request_id}.json      # Python → LISP  (atomic: .tmp + rename)
autocad_mcp_result_{request_id}.json   # LISP → Python  (atomic: .tmp + rename)
```

- One trigger = `c:mcp-dispatch` processes **first** cmd file only; leftover cmd files from prior runs get consumed in order.
- Stale files cleaned after `STALE_THRESHOLD` (60s).
- Result parsing chain: read UTF-8 → fallback cp1252 → `_sanitize_json()`:
  1. decode AutoCAD `\U+XXXX` escapes → real Unicode (Vietnamese layer names)
  2. escape lone backslashes JSON rejects (`\U`, `\+`, ...)
  3. `json.loads` → verify `request_id`
  4. parse failures are fatal (atomic write ⇒ never heals) → fail fast, no bogus timeout

## 5. Security Gates (AutoCAD side)

| Gate | Effect | Resolution |
|------|--------|------------|
| **Secure Load** | `(load ...)` canceled if path not trusted | `setvar "TRUSTEDPATHS"` append before load (done in `initialize()`) |
| Command whitelist | dispatcher has **no eval** — commands map only | extend `mcp-dispatch-command` in `mcp_dispatch.lsp` |
| `SECURELOAD` | blocks `execute-lisp` temp loads | dispatcher sets `SECURELOAD 0` around `load`, restores after |
| **`acad_command` denylist** | passthrough accepts any command *except* three classes | see §6; refused commands return `refused by policy` |

## 6. Passthrough Policy (`system.acad_command`)

The whitelist map covers ~60 commands with typed parameters. The passthrough
reaches anything with a command-line form, so the gate is a **denylist** — an
allowlist would reinstate the ceiling the feature removes.

| Class | Refused | Rationale |
|-------|---------|-----------|
| Code injection | `SCRIPT` `APPLOAD` `LOAD` `VL-LOAD` `VL-LOAD-COM` | arbitrary LISP == bypassing the policy itself |
| Process escape | `SHELL` `STARTAPP` `RUN` `BROWSER` | launching other programs |
| Security reconfiguration | `SECURITY` `SECURELOAD` `TRUSTEDPATHS` `ACAD` `R13` `R14` `R15` | disabling Secure Load, rewriting the profile |

**Normal CAD commands stay reachable**, including destructive ones (`ERASE`,
`SAVE`, `NEW`, `SAVEAS`). Blocking those would make the tool useless, and they
are no more dangerous than a user typing them by hand.

> [!CAUTION]
> `system.execute_lisp` has **no** denylist — it executes arbitrary AutoLISP by
> design. The denylist is a guardrail against careless calls, not a security
> boundary. Treat MCP access to this server as equivalent to a shell on the box.

**Two AutoLISP behaviours the policy depends on** (both silent when wrong — see [Met_Errors §8](Met_Errors.md)):

- `mcp-cmd-denied-p` strips the locale prefix with a `substr` loop. `vl-string-translate` with an empty replacement deletes **nothing**, which would leave `_.SHELL` unstripped and unmatched.
- The denylist literals are **upper-case**, because `member` compares strings exactly and `strcase` upper-cases its input.

Verified refusals:

```text
_.SHELL   -> acad-command: refused by policy: _.SHELL
-shell     -> acad-command: refused by policy: -shell
_.SCRIPT   -> acad-command: refused by policy: _.SCRIPT
_.REGEN    -> {"command": "_.REGEN"}          (passes)
_.LINE     -> entity count 33 -> 34          (passes)
```

## 7. Verified State (2026-10-07, passthrough branch)

```text
backend          file_ipc
main hwnd        788512 (valid on Default)
CLI hwnd         198774 (HwndWrapper)
initialize       True
dispatcher load  LOADOK   (wrapped in vl-catch-all-apply — bare load never raises)
entity count     34
denylist         _.SHELL / -shell / _.SCRIPT refused
passthrough      _.REGEN ok; _.LINE 33 -> 34; cleanup erases by handle
version          3.2.0
```

Entity counts are drawing-specific and were 1669 in an earlier session; treat
them as environment state, not a regression signal.

## 7b. MCP Tool Annotations (declared 2026-10-11)

All 8 tools declare the four protocol hints. Values match handler behaviour,
not aspiration — a client that refuses destructive tools must see the truth:

| Tool | readOnly | destructive | idempotent | Why |
|------|----------|-------------|------------|-----|
| `view` | ✅ | ❌ | ✅ | zoom/screenshot change no drawing state |
| `drawing` | ❌ | ✅ | ❌ | `purge`, `undo`/`redo`, overwrites files |
| `entity` | ❌ | ✅ | ❌ | `erase` deletes |
| `layer` | ❌ | ❌ | ❌ | creates/updates properties only |
| `block` | ❌ | ❌ | ❌ | insert/update attributes |
| `annotation` | ❌ | ❌ | ❌ | text/dimension creation |
| `pid` | ❌ | ❌ | ❌ | symbol insertion |
| `system` | ❌ | ✅ | ❌ | `execute_lisp`/`acad_command` reach anything |

`openWorldHint` is `true` on all 8 — every tool talks to a live AutoCAD
process (external state). Before these were declared, the M8ven scan reported
"8/8 tools missing one or more hints" and OpenAI's directory rejects such
tools outright. `TestToolAnnotations` in `tests/test_view_annotation_tools.py`
asserts all four hints exist on every registered tool, so a 9th tool cannot be
added without them.

`idempotentHint` is `false` wherever an operation creates (repeating doubles
the entity count) or mutates undo history; only `view` qualifies as idempotent
(zooming twice lands on the same viewport).

**Cleanup discipline:** verification must erase **the entity it created, by
handle**. An earlier test used `ERASE _ALL` and destroyed the user's drawing;
`ERASE` is not on the denylist precisely because it is a normal CAD command, so
nothing stops a careless test from reaching for it.

**Known issue (external):** Antigravity `call_mcp_tool` bridge sends `arguments` as JSON string → `Invalid request parameters` on every server. Workaround: stdio CLI (`mcp_cli.py`). See [Met_Errors.md §7](Met_Errors.md).

## 8. Environment

| Item | Value |
|------|-------|
| OS | Windows 11 |
| AutoCAD | MEP 2026 Education (NON COMMERCIAL), drawing: `Hộp giảm tốc 1 cấp bánh răng côn.dwg` |
| Python | `.venv` at `C:\Dev\autocad-mcp\.venv` (native Windows, Python 3.14) |
| IPC dir | `C:/temp` |
| LISP dir | `C:/Dev/autocad-mcp/lisp-code` (trusted at runtime) |
| MCP transport | stdio (`python -m autocad_mcp`) |
