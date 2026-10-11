# System Context

Runtime environment notes for the AutoCAD MCP File IPC backend — how this system behaves on a real machine (Windows 11, AutoCAD MEP 2026 Education, Antigravity IDE).

---

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

---

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

---

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

---

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

---

## 5. Security Gates (AutoCAD side)

| Gate | Effect | Resolution |
|------|--------|------------|
| **Secure Load** | `(load ...)` canceled if path not trusted | `setvar "TRUSTEDPATHS"` append before load (done in `initialize()`) |
| Command whitelist | dispatcher has **no eval** — commands map only | extend `mcp-dispatch-command` in `mcp_dispatch.lsp` |
| `SECURELOAD` | blocks `execute-lisp` temp loads | dispatcher sets `SECURELOAD 0` around `load`, restores after |
| **`acad_command` denylist** | passthrough accepts any command *except* three classes | see §6; refused commands return `refused by policy` |

---

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

---

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

**Not yet verified against live AutoCAD (v3.3, 2026-10-11):** the colour and
batch-delete paths have headless coverage only. Before using them on a real
drawing, reload the dispatcher and check four things in order:

1. `entity.set_color` on a known handle → `entity.get` returns the new colour
2. `entity.create_line` with `color: "red"` → the line renders red
3. `entity.delete_by` with a `TEMP` layer → nothing outside that layer moves
4. `entity.delete_all` with no `confirm` → refuses

## 7b. MCP Tool Annotations (declared 2026-10-11)

All 8 tools declare the four protocol hints. Values match handler behaviour,
not aspiration — a client that refuses destructive tools must see the truth:

| Tool | readOnly | destructive | idempotent | Why |
|------|----------|-------------|------------|-----|
| `view` | ✅ | ❌ | ✅ | zoom/screenshot change no drawing state |
| `drawing` | ❌ | ✅ | ❌ | `purge`, `undo`/`redo`, overwrites files |
| `entity` | ❌ | ✅ | ❌ | `erase`, `delete_by`, `delete_all` delete |
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

> [!NOTE]
> v3.3 added `delete_all` but the `entity` tool's `destructiveHint` is **already**
> `true` (it shipped with `erase`), so the annotation remains accurate without
> change. The hint describes the tool, not the operation — `delete_all` is the
> one operation inside `entity` that is not undoable, which is why it carries its
> own `confirm` guard rather than relying on the annotation.

`idempotentHint` is `false` wherever an operation creates (repeating doubles
the entity count) or mutates undo history; only `view` qualifies as idempotent
(zooming twice lands on the same viewport).

**Cleanup discipline:** verification must erase **the entity it created, by
handle**. An earlier test used `ERASE _ALL` and destroyed the user's drawing;
`ERASE` is not on the denylist precisely because it is a normal CAD command, so
nothing stops a careless test from reaching for it.

**Known issue (external):** Antigravity `call_mcp_tool` bridge sends `arguments` as JSON string → `Invalid request parameters` on every server. Workaround: stdio CLI (`mcp_cli.py`). See [Met_Errors.md §7](Met_Errors.md).

---

## 8. Environment

| Item | Value |
|------|-------|
| OS | Windows 11 |
| AutoCAD | MEP 2026 Education (NON COMMERCIAL), drawing: `Hộp giảm tốc 1 cấp bánh răng côn.dwg` |
| Python | `.venv` at `C:\Dev\autocad-mcp\.venv` (native Windows, Python 3.14) |
| IPC dir | `C:/temp` |
| LISP dir | `C:/Dev/autocad-mcp/lisp-code` (trusted at runtime) |
| MCP transport | stdio (`python -m autocad_mcp`) |

---

## 9. Entity Colour (v3.3)

Colour is a **per-entity ACI value** (AutoCAD Color Index). Before v3.3 it existed only at layer level.

| Form | Example | ACI |
|------|---------|-----|
| Name | `red`, `yellow`, `green`, `cyan`, `blue`, `magenta`, `white`, `grey`/`gray` | 1–8 |
| By-block | `byblock` | 0 |
| By-layer | `bylayer` | 256 |
| Raw index | `30` | passthrough |
| Unknown name | `octarine` | 7 (white, no raise) |

### Group codes

| Code | Meaning | Used by |
|------|---------|---------|
| **62** | ACI colour | colour-by-name — reads (`entget`) and writes (`entmod`) |
| 420 | True colour (RGB) | not used; no colour-name maps to an RGB |
| 8 | Layer name | existing entity queries |
| 5 | Handle | existing entity queries |

### BYLAYER inheritance — the behaviour that surprises people

An entity with no explicit colour stores 256 and **follows its layer's colour**. Writing an explicit ACI breaks that link:

```text
layer "WALLS" is red
  line A  color 256  →  renders red; recolour WALLS to blue → A turns blue
  line B  color 1    →  renders red; recolour WALLS to blue → B stays red
  entity.set_color(B, "bylayer")  →  B inherits again
```

This is standard AutoCAD semantics, not a bug, but it is invisible until someone recolours a layer and one set of entities changes and another does not. It is documented on the `entity` tool docstring and in the README for that reason.

### Both backends must agree

`EzdxfBackend._color_to_int` and the LISP `mcp-color-to-aci` are two implementations of the same table. Drift between them would make `red` mean one thing headless and another in AutoCAD, so `test_lisp_dispatcher.py::test_aci_mapping_matches_python` asserts key-for-key agreement by parsing the LISP `cond` clauses and calling the Python helper.

> [!NOTE]
> Colour reaches LISP as a **string** deliberately. `file_ipc.py::entity_set_color` coerces with `str(color)` — a JSON number would arrive via `mcp-json-get-number` as an `atof`-truncated float (see [Met_Errors §10](Met_Errors.md)).

---

## 10. Destructive Operation Surface (v3.3)

| Operation | Guard | Reversible |
|----------|-------|-----------|
| `entity.erase` | needs `entity_id` | yes, `drawing` → `undo` |
| `entity.delete_by` | **refuses an empty selector set** | yes |
| `entity.delete_all` | **refuses without `confirm: true`** | only via `undo`, while the stack holds it |

`delete_by` selectors are **ANDed** — passing two narrows the result, it does not widen it:

```text
delete_by(layer="TEMP")                    → every entity on TEMP
delete_by(layer="TEMP", color="red")       → red entities on TEMP only
delete_by()                                → error, nothing deleted
```

Both delete operations report `matched` and `deleted` separately. A partial delete is therefore visible in the response rather than swallowed — which matters because a delete that partially fails still returns `ok: true`.

### Handle capture ordering

AutoLISP `entdel` invalidates the entity; a following `entget` returns `nil`. Any loop that both reads and destroys must capture the identifier **first**. The original implementation did not, and returned `handles: []` on every success — see [Met_Errors §13](Met_Errors.md).

### Verification discipline

> [!CAUTION]
> Test batch delete on a throwaway `TEMP` layer, never on `0` or a named layer. Prefer `delete_by(layer="TEMP")` over `delete_all` — the blast radius is bounded by something you created in the same session.

`ERASE _ALL` is reachable through the `acad_command` passthrough and is deliberately **not** on the denylist (§6): it is a normal CAD command, and blocking it would make the tool useless. That makes cleanup discipline the user's responsibility, which is why `delete_all` needs an explicit `confirm`.

---

## 11. Dispatcher Test Coverage (v3.3)

`mcp_dispatch.lsp` is plain text that AutoCAD loads at runtime — **nothing in pytest executes it**. A paren typo surfaces to the user as an opaque `dispatcher not loaded` error, minutes after the edit, with an empty Text Window.

`tests/test_lisp_dispatcher.py` closes that gap with a string-aware scanner:

| Test | Asserts |
|------|---------|
| `test_parentheses_balance` | whole-file balance |
| `test_no_unterminated_string` | no unclosed string literal |
| `test_every_defun_closes_on_its_own` | each `defun` balances independently |
| `test_defun_names_unique` | no shadowing duplicates |
| `test_every_branch_has_a_handler` | every `cond` branch names a real defun |
| `test_expected_commands_all_routed` | Python `EXPECTED_COMMANDS` ↔ LISP `cond` branches agree |
| `test_create_commands_go_through_color_wrapper` | no create branch silently drops `color` |
| `test_aci_mapping_matches_python` | LISP and Python colour tables agree |
| `test_handles_captured_before_entdel` | the §13 ordering |

> [!IMPORTANT]
> The scanner **must** be string-aware. Treating every `;` as a comment marker reads the literal `";"` delimiter — passed to `mcp-split-string` — as a comment and deletes real closing parentheses. That produced a phantom imbalance of 13 in a file with none ([Met_Errors §14](Met_Errors.md)).

> [!CAUTION]
> These tests prove the dispatcher is **well-formed**. They cannot prove AutoCAD has it **loaded**. Reload `(load "C:/Dev/autocad-mcp/lisp-code/mcp_dispatch.lsp")` after every LISP edit — `LOADOK` alone proves nothing (§9 of Met_Errors).
