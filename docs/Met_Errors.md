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

**Workaround:** [mcp_cli.py](../tools/mcp_cli.py) (see README → CLI bypass) calls the server directly over stdio, bypassing the bridge.

---

## 8. Denylist silently allows everything (`vl-string-translate` with `""` deletes nothing)

**Symptom:** The `acad_command` policy denylist was implemented, loaded cleanly, and reported `ok: true` — but `_.SHELL`, `-shell` and `_.SCRIPT` all executed. Three separate fixes were tried before the real cause surfaced.

**Cause:** `vl-string-translate` **does not delete characters when the replacement string is empty.** Given `"_.SHELL"` and `"_-"` → `""`, it returns `"_.SHELL"` unchanged. The locale prefix was therefore never stripped, so `(strcase "_.SHELL")` produced `"_.SHELL"`, which never matched the list entry `"SHELL"`.

This is worth stating plainly because the failure is silent and looks like a logic error: the code reads correctly, and the only way to see it is to ask AutoCAD what the function actually returns.

**Diagnosis — one probe per expression, so the error names the real failure:**

```lisp
(vl-string-translate "._-" "" "_.SHELL")   ;=> "_.SHELL"   ← not "SHELL"
(mcp-cmd-denied-p "SHELL")                 ;=> (SHELL STARTAPP ...)  ← matching works
(mcp-cmd-denied-p "_.SHELL")               ;=> nil                     ← stripping does not
```

The third line is the tell: the bare name matched and the prefixed name did not, which localises the fault to prefix stripping rather than to `member` or the list itself.

**Fix:** an explicit `substr` loop in `mcp-cmd-basename`, which has no such special case:

```lisp
(defun mcp-cmd-basename (name)
  (if (not name)
    nil
    (progn
      (while (member (substr name 1 1) '("." "_" "-"))
        (setq name (substr name 2))
      )
      name)))
```

Two related traps found in the same debugging session:

- **`member` compares strings case-sensitively.** `strcase` upper-cases its input, so the denylist literals had to be written upper-case. A lower-case list would silently never match, for the same reason.
- **`vl-string-downcase` does not exist in AutoLISP**, which is what made the case bug look unfixable until the list was upper-cased instead.

> [!TIP]
> When an AutoLISP predicate returns a wrong answer, probe its **inputs** rather than reasoning about the body. `(mcp-cmd-denied-p "SHELL")` returning a match while `(mcp-cmd-denied-p "_.SHELL")` returned nil identified prefix stripping in one step. Every earlier attempt — a paren-balance checker, a control-diff against `git show HEAD`, a multi-branch probe — failed to isolate it.

---

## 9. `(load ...)` reports success even when the file is broken

**Symptom:** After editing `mcp_dispatch.lsp`, a reload call returned `{"ok": true, "payload": ""}` and the new function was absent. AutoCAD had printed a syntax error to the Text Window, but nothing propagated back to the caller.

**Cause:** AutoLISP `load` **prints errors to the command line and returns; it does not raise.** So an MCP `execute_lisp` call wrapping `(load ...)` looks successful no matter how badly the file is broken.

**Fix:** wrap it so the error travels back as a return value:

```lisp
(setq *r* (vl-catch-all-apply 'load (list "C:/Dev/autocad-mcp/lisp-code/mcp_dispatch.lsp")))
(if (vl-catch-all-error-p *r*)
  (princ (strcat "LOADERR: " (vl-catch-all-error-message *r*)))
  (princ "LOADOK"))
```

Assert on the **function**, not just on `LOADOK`, so a stale definition left in memory by an earlier successful load is caught too:

```lisp
(if (vl-catch-all-error-p (vl-catch-all-apply 'mcp-cmd-denied-p (list "zzzz")))
  (princ "LOADOK_BUT_MISSING")   ; old code still loaded
  (princ "LOADOK"))
```

> [!WARNING]
> `load` **succeeds** if the file parses even when a called function does not exist yet — AutoLISP resolves symbols at call time, not load time. A clean load therefore proves the file is syntactically valid, and nothing more.

---

## 10. `'int' object has no attribute 'replace'` on a numeric command token

**Symptom:** `acad_command` with `["_.CIRCLE", "127749.0919,88293.0194", "1", ""]` failed with `'int' object has no attribute 'replace'`.

**Cause:** a JSON schema coerces `"1"` into a Python `int` before it reaches the backend, and `int` has no `.replace`.

**Fix:** in `file_ipc.py::acad_command`, coerce before transforming:

```python
# str() first: a numeric token such as a radius arrives as an int when it
# comes through a JSON schema, and int has no .replace.
encoded = [str(a).replace(" ", "~") for a in args]
```

**Note:** the fix requires an MCP server restart to take effect. LISP-side edits only need a `(load ...)`.

---

## 11. `no function definition: VL-SYMBOL-FUNCTION` / `VL-STRING-DOWNCASE`

**Symptom:** probes failed with `no function definition` for two names that look like plausible Visual LISP utilities.

**Cause:** neither exists. AutoLISP's string utilities are `strcase` (upper-cases; there is no lower-case counterpart), `substr`, `strcat`, `vl-string-translate`, `vl-string-search`. Symbol introspection is done by calling through `vl-catch-all-apply` and testing `vl-catch-all-error-p`, not by asking whether a symbol has a definition.

**Fix:** see §8 and §9 — probe with a caught call:

```lisp
(if (vl-catch-all-error-p (vl-catch-all-apply 'some-fn (list "x")))
  "does not exist or raised"
  "ran successfully")
```

---

## 12. AutoLISP `cond` treats a non-nil value as a function

**Symptom:** a probe reading back a list of results died with `bad function: "SHELL"`.

**Cause:** `(cond (r "DENY") (T "allow"))` looks like "if `r` is true" but a bare symbol in a `cond` clause is a **call**, so `r` was invoked with `"DENY"`.

**Fix:** use `if` when testing a value rather than enumerating cases.

---

## 13. `entget` after `entdel` returns nil — batch delete reports empty handles

**Symptom:** `entity_delete_by` returned `{"ok": true, "matched": 3, "deleted": 3, "handles": []}`. The entities were genuinely erased; the handle list was simply empty. No error, no timeout, no Text Window output.

**Cause:** the loop read each handle *after* erasing it:

```lisp
(foreach target targets
  (if (entdel target)
    (progn
      (setq deleted (1+ deleted))
      (setq handles-out (strcat handles-out "\"" (cdr (assoc 5 (entget target))) "\""))
    )
  )
)
```

`entdel` invalidates the entity, so the `entget` that follows yields `nil` and `cdr` of that yields `nil`. The JSON stayed well-formed, so nothing downstream could tell the difference between "deleted three entities, here are their handles" and "deleted three entities, handles unknown". This is the §8/§12 family — a plausible-looking value rather than an error.

**Fix:** capture the handle **before** erasing:

```lisp
(foreach target targets
  (setq h (cdr (assoc 5 (entget target))))
  (if (entdel target)
    (progn
      (setq deleted (1+ deleted))
      (setq handles-out (strcat handles-out "\"" h "\""))
    )
  )
)
```

The same applies anywhere an operation both reads and destroys — capture the identifier first, always.

**Regression guard:** `tests/test_lisp_dispatcher.py::test_handles_captured_before_entdel` asserts the `entget` call appears *before* `entdel` in the source. The ordering is invisible to pytest otherwise, since the LISP is never executed.

---

## 14. A paren checker that treated `;` as a comment invented an imbalance

**Symptom:** a scratch script checking `mcp_dispatch.lsp` reported 13 unbalanced parentheses. The baseline (`git show HEAD:...`) reported 0, and the working file differed by three new defuns. Every new defun balanced on inspection.

**Cause:** the checker stripped comments with `re.sub(r";.*", "", text)` — the naive AutoLISP rule that everything after `;` is a comment. But `";"` is a real string literal in this file: `mcp-split-string` is called with it as a delimiter, and the batch-delete handlers pass `"handles_str"` values delimited by semicolons. The strip deleted everything after the first `";"` on those lines, taking valid closing parentheses with it.

Worse, the "13 unbalanced" was treated as evidence about the *code*. The actual defect was in the checker.

**Fix:** string-aware scan — skip over literals entirely, and treat `;` as a comment only outside a string:

```python
def _skip_literal(src, i):
    if src[i] == "\\":          # AutoLISP char literal, e.g. \U+0041
        return i + 2
    quote = src[i]
    i += 1
    while i < len(src):
        if src[i] == "\\":
            i += 2
            continue
        if src[i] == quote:
            return i + 1
        i += 1
    return i
```

A second draft kept an `in_string` flag *and* skipped literals inline. The double handling consumed the closing quote twice and desynchronised the walk, reporting 19 phantom parentheses. One mechanism, done once. That check is now `tests/test_lisp_dispatcher.py` so the class of mistake cannot recur silently.

> [!TIP]
> When a static check disagrees with code you have read and believe is correct, **suspect the check first**. Reproduce its claim manually on the smallest input that should trip it — here, `(setq s (mcp-split-string handles-str ";"))` — and the false positive is obvious. The generalisation: a finding is evidence, not a verdict. This is §7's diagnostic rule applied to tooling instead of AutoLISP.

---

## Quick Diagnostic Sequence

When input/IPC fails:
1. **F2** in AutoCAD → Text Window shows command echo + LISP errors (ground truth for "did it type?").
2. Watch `C:/temp/autocad_mcp_*` — cmd file consumed = LISP ran; missing result = parse/write issue (case 6).
3. Check `TRUSTEDPATHS` if load errors (case 2).
4. Check overlapping windows (Help, palettes) if clicks miss (case 4).
5. Validate handles while on `WinSta0\Default` (case 5).

When **LISP returns a plausible but wrong value** (no error, no timeout):

6. `load` succeeding proves nothing on its own — it does not raise (case 9). Assert on the function via a caught call.
7. Probe one expression at a time and **print the return value**. A predicate that looks right can still return `nil` because an earlier step silently no-ops (case 8).
8. Watch for AutoLISP functions that do not exist: `vl-string-downcase`, `vl-symbol-function` (case 11).
9. In any read-then-destroy loop, capture the identifier **before** the destructive call (case 13).

When a **static check** reports a defect in code you have read and believe correct:

10. Suspect the checker before rewriting the code. Reproduce its claim on the smallest input that should trip it (case 14).
11. Check whether the rule the tool applies is actually true for this language and file — here, "everything after `;` is a comment" is false inside a string literal.

When verifying a **destructive operation** against a live drawing:

12. Erase by **handle**, created in the same step. `ERASE _ALL` is not on the denylist — nothing stops a careless test from reaching for it (see [System_Context §7b](System_Context.md)).
13. Treat an empty selector set as a bug in the *caller*, not a no-op. `delete_by` refuses it; `delete_all` refuses without `confirm: true`.

> [!NOTE]
> Every passthrough bug in §8–§14 returned `ok: true` or a well-formed value. The Text Window was empty. Only a probe that printed what a function **returns** — or a manual reproduction of a checker's claim — revealed the fault. Reading the source repeatedly did not.

> [!CAUTION]
> v3.3 adds batch delete. Before running any of it against a real drawing: reload `mcp_dispatch.lsp` (§9), then test on a throwaway `TEMP` layer, never on `0`.
