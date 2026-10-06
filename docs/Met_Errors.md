# Met Errors

Errors encountered while integrating the AutoCAD MCP server on **AutoCAD MEP 2026 (Education)**, with root causes and fixes. Chronological.

---

## 1. `fork/exec ... .venv\Scripts\python.exe: The system cannot find the path specified`

**Symptom:** MCP server fails to start; Antigravity reports fork/exec error.

**Cause:** Virtualenv path missing/moved — server command in MCP config pointed at a non-existent interpreter.

**Fix:** Recreate/repair the venv so `.venv\Scripts\python.exe` exists at the configured path.

---

## 2. LISP load blocked: `File load canceled: C:/Dev/autocad-mcp/lisp-code/...`

**Symptom:** Typing `(load "...")` into the AutoCAD command line prints `File load canceled`. Subsequent `(c:mcp-dispatch)` fails with `no function definition`.

**Cause:** AutoCAD **Secure Load** — `TRUSTEDPATHS` registry value empty in all profiles. Loads from untrusted directories are canceled.

**Fix (session runtime):** Before loading, type:
```lisp
(setvar "TRUSTEDPATHS" (strcat (getvar "TRUSTEDPATHS")
  (if (= (getvar "TRUSTEDPATHS") "") "" ";") "C:/Dev/autocad-mcp/lisp-code"))
```
Then `(load "C:/Dev/autocad-mcp/lisp-code/mcp_dispatch.lsp")`.

The backend `initialize()` now does this automatically before auto-loading. Persistent fix: add the path via AutoCAD Options → Security → Trusted Paths.

---

## 3. Typed commands never reach the AutoCAD command line (WM_CHAR ignored)

**Symptom:** `PostMessageW(WM_CHAR)` to `MDIClient`/main window produces no effect; F2 Text Window shows nothing typed. A test marker (`ZZTEST999`) typed via real input *does* reach the CLI.

**Cause:** AutoCAD 2026 (full, non-LT) hosts the command line in a **WPF CLI Palette** (`HwndWrapper` child of a "CLI Palette" window). WPF ignores posted messages; only focus + real input works.

**Fix:** In `file_ipc.py`:
1. `_find_command_line_hwnd()` — locate the `CLI Palette` → `HwndWrapper` input surface.
2. `_type_text()` — on `WinSta0\Default`: foreground main window, **click** the CLI input row (moves focus), then inject real keystrokes via `SendInput` (`KEYEVENTF_UNICODE`).

---

## 4. Click lands on the Help window, not the CLI

**Symptom:** Input chain works only when no other AutoCAD window overlaps; intermittently `initialize()` reports dispatcher missing even though typing looks fine.

**Cause:** An open **"AutoCAD MEP 2026 - Help"** window (same process) overlays the main window region where the CLI Palette sits. `SetWindowPos(..., SWP_NOZORDER)` does not raise the main window, so `SetCursorPos` + click hits the Help window.

**Fix:** In `_type_text()` before clicking:
- Enumerate top-level windows and **minimize** visible same-process `*Help*` windows (`SW_MINIMIZE`).
- `SetWindowPos(hwnd, HWND_TOP, ...)` without `SWP_NOZORDER` so the main window raises above siblings.

---

## 5. `AutoCAD LT window not found` / stale handle (`GetWindowRect: Invalid window handle`)

**Symptom:** `find_autocad_window()` returns a handle that later fails `GetWindowRect`, or returns `None` although acad.exe runs.

**Causes (two):**
1. Finder matched the **Help** window first; when Help closed, the cached handle went stale.
2. `_is_window()` / `IsWindowVisible()` called **after** switching back to the private desktop — cross-desktop calls return `False`, so every candidate was rejected.

**Fix:**
- Skip titles containing `help` / `text window`; prefer the `.dwg` document window.
- Validate (`IsWindow` + `IsWindowVisible`) **inside** the predicate callback, i.e. while still on `WinSta0\Default`.
- Guard fallback `EnumWindows` (raises error 18 on foreign desktops) with try/except.

---

## 6. IPC timeout despite LISP consuming the command file

**Symptom:** `ping` OK (returns in ~1s); `drawing-info` / `layer-list` time out (60s+). Diagnostic shows the cmd file is **consumed by LISP at +1.0s** yet no result file is accepted.

**Root cause:** Result file *is* written immediately, but `json.loads` throws forever:
- Vietnamese layer names encoded by AutoCAD as literal `\U+0111`-style escapes → **invalid JSON escape** (`\U` not allowed).
- Layer text in cp1252 (`é` = `0xe9`) → strict UTF-8 decode fails.

The poll loop swallowed `JSONDecodeError` as "file may be partially written" and retried until timeout — a **bogus timeout**.

**Fix:** In `file_ipc.py::_dispatch_unlocked`:
1. `_sanitize_json()` — decode `\U+XXXX` to real Unicode (Vietnamese), then escape any backslash JSON would reject.
2. Encoding fallback UTF-8 → cp1252 (kept).
3. **Fail fast**: result files are written atomically (tmp + rename), so a parse failure never heals — return `Unparseable result ...` after 5 attempts instead of timing out.

**Verified:** `drawing-info` → `{"entity_count": 1669, "layers": ["Nét đậm", "Nét mãnh", "Nét khuất", ...]}`.

---

## 7. MCP `call_mcp_tool` → `Invalid request parameters` (client-side)

**Symptom:** Every Antigravity MCP call fails: `calling "tools/call": Invalid request parameters`. The MongoDB server's stricter validation reveals the cause: `path [params, arguments], expected record, received string`.

**Cause:** The **Antigravity MCP bridge** serializes `arguments` as a JSON *string* instead of an object. Affects all servers; survives app restart → IDE bug, not this project.

**Evidence the server is healthy:** direct stdio self-test passes:
```text
server: autocad-mcp 2025-11-25
tools: drawing entity layer block annotation pid view system
drawing/info → isError: False, entity_count 1669
```

**Workaround:** [mcp_cli.py](../scratch/mcp_cli.py) (see README → CLI bypass) calls the server directly over stdio, bypassing the bridge.

---

## Quick Diagnostic Sequence

When input/IPC fails:
1. **F2** in AutoCAD → Text Window shows command echo + LISP errors (ground truth for "did it type?").
2. Watch `C:/temp/autocad_mcp_*` — cmd file consumed = LISP ran; missing result = parse/write issue (case 6).
3. Check `TRUSTEDPATHS` if load errors (case 2).
4. Check overlapping windows (Help, palettes) if clicks miss (case 4).
5. Validate handles while on `WinSta0\Default` (case 5).
