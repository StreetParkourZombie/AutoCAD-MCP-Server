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

## 6. Verified State (2026-10-06)

```text
backend          file_ipc
main hwnd        329562 (valid on Default)
CLI hwnd         198528 (HwndWrapper)
initialize       True
ping             pong (~1.0s)
drawing-info     {"entity_count": 1669, "layers": ["0","Nét đậm","Nét mãnh","Nét khuất", ...]}
stdio self-test  8/8 tools, drawing/info isError=False
```

**Known issue (external):** Antigravity `call_mcp_tool` bridge sends `arguments` as JSON string → `Invalid request parameters` on every server. Workaround: stdio CLI (`mcp_cli.py`). See [Met_Errors.md §7](Met_Errors.md).

## 7. Environment

| Item | Value |
|------|-------|
| OS | Windows 11 |
| AutoCAD | MEP 2026 Education (NON COMMERCIAL), drawing: `Hộp giảm tốc 1 cấp bánh răng côn.dwg` |
| Python | `.venv` at `C:\Dev\autocad-mcp\.venv` (native Windows, Python 3.14) |
| IPC dir | `C:/temp` |
| LISP dir | `C:/Dev/autocad-mcp/lisp-code` (trusted at runtime) |
| MCP transport | stdio (`python -m autocad_mcp`) |
