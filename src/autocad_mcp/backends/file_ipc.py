"""File-based IPC backend for AutoCAD LT.

Protocol:
1. Python writes JSON command to C:/temp/autocad_mcp_cmd_{request_id}.json
2. Python types the fixed string "(c:mcp-dispatch)" + Enter
3. LISP reads cmd, dispatches via command map, writes result to
   C:/temp/autocad_mcp_result_{request_id}.json
4. Python polls for result file (100ms intervals, 10s timeout)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

import structlog

from autocad_mcp.backends.base import AutoCADBackend, BackendCapabilities, CommandResult
from autocad_mcp.config import IPC_DIR, IPC_TIMEOUT, LISP_DIR

log = structlog.get_logger()

# IPC settings
POLL_INTERVAL = 0.1  # seconds
TIMEOUT = IPC_TIMEOUT  # seconds (configurable via AUTOCAD_MCP_IPC_TIMEOUT)
STALE_THRESHOLD = 60.0  # clean up files older than this

# Characters that may legally follow a backslash inside a JSON string.
_JSON_VALID_ESCAPES = frozenset('"\\/bfnrtu')


def _sanitize_json(text: str) -> str:
    r"""Escape backslashes that JSON would reject.

    AutoCAD LISP payloads may contain layer names with literal text like
    ``\U+0111`` (how AutoCAD stores characters the ANSI codepage cannot
    represent). mcp_dispatch.lsp concatenates names raw, so json.loads
    fails on ``\U`` — which previously surfaced as a bogus IPC timeout.

    Scans escape pairs left-to-right (``\\`` is a valid escaped
    backslash and is kept), escaping only lone backslashes followed by
    a character JSON does not accept.
    """

    def fix(m: re.Match[str]) -> str:
        esc = m.group(0)
        return esc if m.group(1) in _JSON_VALID_ESCAPES else "\\" + esc

    # AutoCAD encodes characters outside the ANSI codepage as \U+XXXX
    # inside layer names — decode them to real Unicode first (Vietnamese).
    text = re.sub(
        r"\\U\+([0-9A-Fa-f]{4})",
        lambda m: chr(int(m.group(1), 16)),
        text,
    )
    return re.sub(r"\\(.)", fix, text, flags=re.S)


def _enum_on_default_desktop(predicate) -> list[tuple[int, str]]:
    r"""Run EnumWindows + predicate on the WinSta0\Default desktop.

    Returns [(hwnd, title), ...] — titles are captured while on Default
    because after switching back to Antigravity's private desktop
    (agy-*), GetWindowText on foreign hwnds yields empty strings.

    Returns [] if the desktop switch is not possible; caller then
    falls back to enumerating on the current desktop.
    """
    import ctypes

    import win32gui

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    def obj_name(handle) -> bytes:
        buf = ctypes.create_string_buffer(256)
        user32.GetUserObjectInformationA(handle, 2, buf, 256, None)  # UOI_NAME
        return buf.value

    def grab() -> list[tuple[int, str]]:
        """Enumerate + filter while on Default desktop."""
        hits: list[tuple[int, str]] = []

        def cb(hwnd, _acc):
            try:
                if predicate(hwnd):
                    hits.append((hwnd, win32gui.GetWindowText(hwnd)))
            except Exception:
                pass
            return True

        win32gui.EnumWindows(cb, None)
        return hits

    hdef = 0
    hprev = 0
    switched = False
    try:
        hdef = user32.OpenDesktopW("Default", 0, False, 0x1FF)  # DESKTOP_ALL
        if not hdef:
            return []
        tid = kernel32.GetCurrentThreadId()
        hprev = user32.GetThreadDesktop(tid)
        if obj_name(hdef) != obj_name(hprev):
            if not user32.SetThreadDesktop(hdef):
                return []
            switched = True
        return grab()
    except Exception:
        return []
    finally:
        if switched and hprev:
            user32.SetThreadDesktop(hprev)
        if hdef:
            user32.CloseDesktop(hdef)


def _is_window(hwnd: int) -> bool:
    """True if hwnd is still a valid window (guards stale handles)."""
    if not hwnd:
        return False
    try:
        import ctypes

        return bool(ctypes.windll.user32.IsWindow(hwnd))
    except Exception:
        return False


def find_autocad_window() -> int | None:
    r"""Find the AutoCAD window handle (main window or Text Window).

    Runs the enumeration on WinSta0\Default because Antigravity launches
    the MCP server on its own private desktop (agy-*), where EnumWindows
    sees no foreign windows and GetWindowText yields empty titles.
    """
    if sys.platform != "win32":
        return None
    try:
        import win32gui

        def match(hwnd: int) -> bool:
            # NOTE: runs while on WinSta0\Default — IsWindow/IsWindowVisible
            # must be called here; from our private desktop they return False.
            if not _is_window(hwnd) or not win32gui.IsWindowVisible(hwnd):
                return False
            text = win32gui.GetWindowText(hwnd).lower()
            # Skip auxiliary windows (Help/Text Window) — they close
            # independently and leave us with a stale handle.
            if "help" in text or "text window" in text:
                return False
            if text.startswith("autocad"):
                return True
            # Fallback: original heuristic (contains "autocad" + drawing name)
            return "autocad" in text and ("drawing" in text or ".dwg" in text)

        matched = _enum_on_default_desktop(match)
        if matched:
            # Prefer the drawing document window over anything else
            for h, title in matched:
                if ".dwg" in title.lower():
                    return h
            return matched[0][0]

        # Fallback: enumerate on current desktop (same-desktop case).
        # On a foreign desktop this raises (error 18) — ignore.
        try:
            fallback: list[int] = []
            win32gui.EnumWindows(lambda hwnd, acc: acc.append(hwnd) or True, fallback)
            hits = [h for h in fallback if match(h)]
            if hits:
                return hits[0]
        except Exception:
            pass
        return None
    except ImportError:
        return None


class FileIPCBackend(AutoCADBackend):
    """File-based IPC with AutoCAD LT via mcp_dispatch.lsp."""

    def __init__(self):
        self._hwnd: int | None = None
        self._command_hwnd: int | None = None
        self._ipc_dir = Path(IPC_DIR)
        self._screenshot_provider = None
        self._lock = asyncio.Lock()  # Single in-flight command

    @property
    def name(self) -> str:
        return "file_ipc"

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            can_read_drawing=True,
            can_modify_entities=True,
            can_create_entities=True,
            can_screenshot=True,
            can_save=True,
            can_plot_pdf=True,
            can_zoom=True,
            can_query_entities=True,
            can_file_operations=True,
            can_undo=True,
        )

    async def initialize(self) -> CommandResult:
        """Find AutoCAD window and verify dispatcher is loaded."""
        self._hwnd = find_autocad_window()
        if not self._hwnd:
            return CommandResult(ok=False, error="AutoCAD LT window not found")

        # Set up screenshot provider
        try:
            from autocad_mcp.screenshot import Win32ScreenshotProvider

            self._screenshot_provider = Win32ScreenshotProvider(self._hwnd)
        except Exception:
            pass

        # Find command-line child edit control for focus-free dispatch
        self._command_hwnd = self._find_command_line_hwnd()
        log.info("command_line_hwnd", hwnd=self._command_hwnd)

        # Ensure IPC directory exists
        self._ipc_dir.mkdir(parents=True, exist_ok=True)

        # Clean up stale IPC files
        self._cleanup_stale_files()

        # Ping the dispatcher; if not loaded, trust the lisp dir then auto-load
        lisp_path = str(LISP_DIR / "mcp_dispatch.lsp").replace("\\", "/")
        trusted_dir = str(LISP_DIR).replace("\\", "/")
        result = await self._dispatch("ping", {})
        if not result.ok:
            log.info("dispatcher_missing_trying_autoload", lisp=lisp_path)
            # Secure Load cancels the file otherwise ("File load canceled").
            # Append the lisp dir to TRUSTEDPATHS without dropping existing entries.
            self._type_text(
                '(setvar "TRUSTEDPATHS" (strcat (getvar "TRUSTEDPATHS")'
                f' (if (= (getvar "TRUSTEDPATHS") "") "" ";") "{trusted_dir}"))'
            )
            await asyncio.sleep(0.5)
            self._type_text(f'(load "{lisp_path}")')
            await asyncio.sleep(2.0)  # give AutoCAD time to load
            result = await self._dispatch("ping", {})
        if not result.ok:
            return CommandResult(
                ok=False,
                error=(
                    "AutoCAD LT detected but mcp_dispatch.lsp not loaded.\n"
                    f'In AutoCAD command line, type:\n  (load "{lisp_path}")\n'
                    "Or add lisp-code/ to trusted paths for auto-loading."
                ),
            )

        return CommandResult(ok=True, payload={"backend": "file_ipc", "hwnd": self._hwnd})

    async def status(self) -> CommandResult:
        info = {
            "backend": "file_ipc",
            "hwnd": self._hwnd,
            "ipc_dir": str(self._ipc_dir),
            "capabilities": {k: v for k, v in self.capabilities.__dict__.items()},
        }
        return CommandResult(ok=True, payload=info)

    # --- IPC dispatch ---

    async def _dispatch(self, command: str, params: dict) -> CommandResult:
        """Send a command via file IPC and wait for result."""
        async with self._lock:
            return await self._dispatch_unlocked(command, params)

    async def _dispatch_unlocked(self, command: str, params: dict) -> CommandResult:
        """Core IPC logic (must be called under _lock)."""
        request_id = uuid.uuid4().hex[:12]
        cmd_file = self._ipc_dir / f"autocad_mcp_cmd_{request_id}.json"
        result_file = self._ipc_dir / f"autocad_mcp_result_{request_id}.json"
        tmp_file = cmd_file.with_suffix(".tmp")

        try:
            # Strip None values — the simple LISP JSON parser can't handle null
            clean_params = {k: v for k, v in params.items() if v is not None}
            # Atomic write: write to .tmp, then rename
            payload = {
                "request_id": request_id,
                "command": command,
                "params": clean_params,
                "ts": time.time(),
            }
            tmp_file.write_text(json.dumps(payload), encoding="utf-8")
            tmp_file.rename(cmd_file)

            # Type the fixed dispatch trigger
            self._type_dispatch_trigger()

            # Poll for result
            deadline = time.time() + TIMEOUT
            parse_failures = 0
            while time.time() < deadline:
                if result_file.exists():
                    try:
                        # AutoCAD LISP writes files in Windows-1252 encoding;
                        # try UTF-8 first (covers ASCII), fall back to cp1252
                        try:
                            text = result_file.read_text(encoding="utf-8")
                        except UnicodeDecodeError:
                            text = result_file.read_text(encoding="cp1252")
                        data = json.loads(_sanitize_json(text))
                        # Verify request_id matches
                        if data.get("request_id") == request_id:
                            return CommandResult(
                                ok=data.get("ok", False),
                                payload=data.get("payload"),
                                error=data.get("error"),
                            )
                    except json.JSONDecodeError:
                        # Result file is written atomically (tmp + rename),
                        # so a parse failure never heals — fail fast instead
                        # of reporting a bogus timeout.
                        parse_failures += 1
                        if parse_failures >= 5:
                            return CommandResult(
                                ok=False,
                                error=(
                                    f"Unparseable result (request_id={request_id}): "
                                    f"{text[:300]!r}"
                                ),
                            )
                    except OSError:
                        pass  # File may be partially written, retry
                await asyncio.sleep(POLL_INTERVAL)

            return CommandResult(ok=False, error=f"Timeout waiting for result (request_id={request_id})")

        finally:
            # Cleanup
            for f in (cmd_file, result_file, tmp_file):
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    pass

    def _find_command_line_hwnd(self) -> int | None:
        r"""Find AutoCAD's command-line input window.

        AutoCAD 2026 (full, not LT) hosts the command line in a WPF
        'CLI Palette' palette bar — its input surface is an HwndWrapper
        child. MDIClient (LT) only receives WM_CHAR; WPF ignores posted
        messages, so we must target this window and focus it with a click.
        """
        if sys.platform != "win32" or not self._hwnd:
            return None
        try:
            import ctypes

            import win32gui

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            found: list[int] = []
            cli_bar: list[int] = []

            def pick_wrapper(child_hwnd, _):
                try:
                    if win32gui.GetClassName(child_hwnd).startswith("HwndWrapper"):
                        found.append(child_hwnd)
                        return False
                except Exception:
                    pass
                return True

            def pick_bar(child_hwnd, _):
                try:
                    if win32gui.GetWindowText(child_hwnd) == "CLI Palette":
                        cli_bar.append(child_hwnd)
                        return False
                except Exception:
                    pass
                return True

            # Enumeration must run on the Default desktop
            # (foreign-desktop threads cannot see these children).
            tid = kernel32.GetCurrentThreadId()
            hprev = user32.GetThreadDesktop(tid)
            hdef = user32.OpenDesktopW("Default", 0, False, 0x1FF)
            switched = False
            try:
                if hdef and hdef != hprev:
                    switched = bool(user32.SetThreadDesktop(hdef))
                win32gui.EnumChildWindows(self._hwnd, pick_bar, None)
                if cli_bar:
                    win32gui.EnumChildWindows(cli_bar[0], pick_wrapper, None)
                if not found:
                    # AutoCAD LT fallback: MDIClient receives WM_CHAR fine
                    def pick_mdi(child_hwnd, _):
                        if win32gui.GetClassName(child_hwnd) == "MDIClient":
                            found.append(child_hwnd)
                            return False
                        return True

                    win32gui.EnumChildWindows(self._hwnd, pick_mdi, None)
            finally:
                if switched:
                    user32.SetThreadDesktop(hprev)
                if hdef:
                    user32.CloseDesktop(hdef)
            return found[0] if found else None
        except Exception:
            return None

    def _type_text(self, text: str, execute: bool = True):
        r"""Type text (+ Enter) into AutoCAD's command line via real SendInput.

        Posted WM_CHAR never reaches the WPF command line (AutoCAD 2026),
        so we click the CLI Palette to move focus there and inject real
        keystrokes. Runs on WinSta0\Default because Antigravity's thread
        lives on a private desktop.
        """
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004
            MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004

            class MOUSEINPUT(ctypes.Structure):
                _fields_ = [
                    ("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t),
                ]

            class KEYBDINPUT(ctypes.Structure):
                _fields_ = [
                    ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t),
                ]

            class HARDWAREINPUT(ctypes.Structure):
                _fields_ = [
                    ("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD),
                ]

            class _U(ctypes.Union):
                _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT),
                            ("hi", HARDWAREINPUT)]

            class INPUT(ctypes.Structure):
                _anonymous_ = ("u",)
                _fields_ = [("type", wintypes.DWORD), ("u", _U)]

            if ctypes.sizeof(INPUT) != 40:
                raise RuntimeError(f"unexpected INPUT size {ctypes.sizeof(INPUT)}")

            def send_key(vk_or_char, is_char, up=False):
                i = INPUT(type=1)
                if is_char:
                    i.ki.wVk, i.ki.wScan = 0, vk_or_char
                    i.ki.dwFlags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
                else:
                    i.ki.wVk = vk_or_char
                    i.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
                user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))

            def type_vk(vk):
                send_key(vk, False)
                time.sleep(0.01)
                send_key(vk, False, up=True)

            # --- desktop switch: SetCursorPos/SendInput need Default desktop
            tid = kernel32.GetCurrentThreadId()
            hprev = user32.GetThreadDesktop(tid)
            hdef = user32.OpenDesktopW("Default", 0, False, 0x1FF)
            switched = False
            try:
                if hdef and hdef != hprev:
                    switched = bool(user32.SetThreadDesktop(hdef))

                # Minimize same-process helper windows (e.g. "AutoCAD ... Help")
                # that overlay the CLI Palette — clicks must reach main window.
                try:
                    import win32gui

                    def _hid_obstruct(ch, _acc):
                        try:
                            if win32gui.IsWindowVisible(ch) and ch != self._hwnd:
                                t = win32gui.GetWindowText(ch).lower()
                                if "help" in t:
                                    user32.ShowWindow(ch, 6)  # SW_MINIMIZE
                        except Exception:
                            pass
                        return True

                    win32gui.EnumWindows(_hid_obstruct, None)
                except Exception:
                    pass

                # restore + foreground the main window, RAISE z-order so it
                # sits above any overlapping sibling (no SWP_NOZORDER here).
                if user32.IsIconic(self._hwnd):
                    user32.ShowWindow(self._hwnd, 9)  # SW_RESTORE
                    time.sleep(0.5)
                user32.SetWindowPos(self._hwnd, -1, 0, 0, 0, 0, 0x0003)  # HWND_TOP, NOMOVE|NOSIZE
                fg = user32.GetForegroundWindow()
                if fg and fg != self._hwnd:
                    try:
                        import win32process

                        atid, _ = win32process.GetWindowThreadProcessId(fg)
                        cur = kernel32.GetCurrentThreadId()
                        user32.AttachThreadInput(cur, atid, True)
                        user32.SetForegroundWindow(self._hwnd)
                        user32.AttachThreadInput(cur, atid, False)
                    except Exception:
                        user32.SetForegroundWindow(self._hwnd)
                else:
                    user32.SetForegroundWindow(self._hwnd)
                time.sleep(0.25)

                # click the CLI Palette input row to move focus off the canvas
                target = self._command_hwnd or self._hwnd
                rect = wintypes.RECT()
                if user32.GetWindowRect(target, ctypes.byref(rect)):
                    cx = rect.left + max(10, (rect.right - rect.left) // 4)
                    cy = rect.bottom - max(6, (rect.bottom - rect.top) // 8)
                    user32.SetCursorPos(int(cx), int(cy))
                    time.sleep(0.12)
                    m = INPUT(type=0)
                    m.mi.dwFlags = MOUSEEVENTF_LEFTDOWN
                    user32.SendInput(1, ctypes.byref(m), ctypes.sizeof(INPUT))
                    time.sleep(0.06)
                    m.mi.dwFlags = MOUSEEVENTF_LEFTUP
                    user32.SendInput(1, ctypes.byref(m), ctypes.sizeof(INPUT))
                    time.sleep(0.25)

                # ESC ESC: cancel any stale pending command
                type_vk(0x1B)
                type_vk(0x1B)
                time.sleep(0.05)

                for ch in text:
                    send_key(ord(ch), True)
                    time.sleep(0.006)
                if execute:
                    type_vk(0x0D)  # Enter
                time.sleep(0.1)
            finally:
                if switched:
                    user32.SetThreadDesktop(hprev)
                if hdef:
                    user32.CloseDesktop(hdef)
        except Exception as e:
            log.error("type_text_failed", text=text[:40], error=str(e))

    def _type_dispatch_trigger(self):
        r"""Type '(c:mcp-dispatch)' + Enter into the AutoCAD command line."""
        self._type_text("(c:mcp-dispatch)")

    def _cleanup_stale_files(self):
        """Remove stale IPC files from previous sessions."""
        try:
            now = time.time()
            for pattern in ("autocad_mcp_*.json", "autocad_mcp_*.tmp", "autocad_mcp_lisp_*.lsp"):
                for f in self._ipc_dir.glob(pattern):
                    if now - f.stat().st_mtime > STALE_THRESHOLD:
                        f.unlink(missing_ok=True)
        except OSError:
            pass

    # --- Drawing management ---

    async def drawing_info(self) -> CommandResult:
        return await self._dispatch("drawing-info", {})

    async def drawing_save(self, path: str | None = None) -> CommandResult:
        return await self._dispatch("drawing-save", {"path": path})

    async def drawing_save_as_dxf(self, path: str) -> CommandResult:
        return await self._dispatch("drawing-save-as-dxf", {"path": path})

    async def drawing_create(self, name: str | None = None) -> CommandResult:
        return await self._dispatch("drawing-create", {"name": name})

    async def drawing_purge(self) -> CommandResult:
        return await self._dispatch("drawing-purge", {})

    async def drawing_plot_pdf(self, path: str) -> CommandResult:
        return await self._dispatch("drawing-plot-pdf", {"path": path})

    async def drawing_get_variables(self, names: list[str] | None = None) -> CommandResult:
        if names:
            # Strip $ prefix for AutoCAD compatibility (ezdxf uses $ACADVER, AutoCAD uses ACADVER)
            clean_names = [n.lstrip("$") for n in names]
            names_str = ";".join(clean_names)
        else:
            names_str = ""
        return await self._dispatch("drawing-get-variables", {"names_str": names_str})

    async def drawing_open(self, path: str) -> CommandResult:
        return await self._dispatch("drawing-open", {"path": path})

    # --- Undo / Redo ---

    async def undo(self) -> CommandResult:
        return await self._dispatch("undo", {})

    async def redo(self) -> CommandResult:
        return await self._dispatch("redo", {})

    # --- Freehand LISP execution ---

    async def execute_lisp(self, code: str) -> CommandResult:
        """Execute arbitrary AutoLISP code via temp file.

        File persists for session; cleaned up by _cleanup_stale_files().
        """
        request_id = uuid.uuid4().hex[:12]
        code_file = self._ipc_dir / f"autocad_mcp_lisp_{request_id}.lsp"
        code_file.write_text(code, encoding="utf-8")
        return await self._dispatch("execute-lisp", {
            "code_file": str(code_file).replace("\\", "/")
        })

    async def acad_command(self, args: list[str]) -> CommandResult:
        """Run a raw AutoCAD command via the LISP `command` passthrough.

        Args are the tokens AutoLISP's `command` expects. Spaces inside a token
        are encoded as `~` because the dispatcher splits on `;` and un-escapes
        `~` back to a space.
        """
        if not args:
            return CommandResult(ok=False, error="args must contain at least one command token")
        # str() first: a numeric token such as a radius arrives as an int when it
        # comes through a JSON schema, and int has no .replace.
        encoded = [str(a).replace(" ", "~") for a in args]
        return await self._dispatch("acad-command", {"args_str": ";".join(encoded)})

    # --- Entity operations ---

    async def create_line(self, x1, y1, x2, y2, layer=None, color=None) -> CommandResult:
        return await self._dispatch("create-line", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "layer": layer, "color": color})

    async def create_circle(self, cx, cy, radius, layer=None, color=None) -> CommandResult:
        return await self._dispatch("create-circle", {"cx": cx, "cy": cy, "radius": radius, "layer": layer, "color": color})

    async def create_polyline(self, points, closed=False, layer=None, color=None) -> CommandResult:
        pts_str = ";".join(f"{p[0]},{p[1]}" for p in points)
        return await self._dispatch("create-polyline", {
            "points_str": pts_str, "closed": "1" if closed else "0", "layer": layer, "color": color
        })

    async def create_rectangle(self, x1, y1, x2, y2, layer=None, color=None) -> CommandResult:
        return await self._dispatch("create-rectangle", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "layer": layer, "color": color})

    async def create_arc(self, cx, cy, radius, start_angle, end_angle, layer=None, color=None) -> CommandResult:
        return await self._dispatch("create-arc", {"cx": cx, "cy": cy, "radius": radius, "start_angle": start_angle, "end_angle": end_angle, "layer": layer, "color": color})

    async def create_ellipse(self, cx, cy, major_x, major_y, ratio, layer=None, color=None) -> CommandResult:
        return await self._dispatch("create-ellipse", {"cx": cx, "cy": cy, "major_x": major_x, "major_y": major_y, "ratio": ratio, "layer": layer, "color": color})

    async def create_mtext(self, x, y, width, text, height=2.5, layer=None, color=None) -> CommandResult:
        return await self._dispatch("create-mtext", {"x": x, "y": y, "width": width, "text": text, "height": height, "layer": layer, "color": color})

    async def create_hatch(self, entity_id, pattern="ANSI31") -> CommandResult:
        return await self._dispatch("create-hatch", {"entity_id": entity_id, "pattern": pattern})

    async def entity_list(self, layer=None) -> CommandResult:
        return await self._dispatch("entity-list", {"layer": layer})

    async def entity_count(self, layer=None) -> CommandResult:
        return await self._dispatch("entity-count", {"layer": layer})

    async def entity_get(self, entity_id) -> CommandResult:
        return await self._dispatch("entity-get", {"entity_id": entity_id})

    async def entity_erase(self, entity_id) -> CommandResult:
        return await self._dispatch("entity-erase", {"entity_id": entity_id})

    async def entity_set_color(self, entity_id, color) -> CommandResult:
        # Color travels as a string: JSON numbers would come back through
        # mcp-json-get-number as an atof-truncated float.
        return await self._dispatch("entity-set-color", {"entity_id": entity_id, "color": str(color)})

    async def entity_delete_by(
        self,
        layer=None,
        type=None,
        color=None,
        handles=None,
        last_n=None,
    ) -> CommandResult:
        if not any([layer, type, color, handles, last_n]):
            return CommandResult(ok=False, error="At least one selector required")
        # Handles are a list; the wire format is semicolon-delimited, same
        # convention create_polyline uses for points_str.
        handles_str = ";".join(str(h) for h in handles) if handles else ""
        last_n_str = str(int(last_n)) if last_n else ""
        return await self._dispatch("entity-delete-by", {
            "layer": layer,
            "type": type,
            "color": str(color) if color is not None else None,
            "handles_str": handles_str,
            "last_n_str": last_n_str,
        })

    async def entity_delete_all(self, confirm=False) -> CommandResult:
        if not confirm:
            return CommandResult(ok=False, error="entity_delete_all requires confirm=True")
        return await self._dispatch("entity-delete-all", {"confirm": "1"})

    async def entity_copy(self, entity_id, dx, dy) -> CommandResult:
        return await self._dispatch("entity-copy", {"entity_id": entity_id, "dx": dx, "dy": dy})

    async def entity_move(self, entity_id, dx, dy) -> CommandResult:
        return await self._dispatch("entity-move", {"entity_id": entity_id, "dx": dx, "dy": dy})

    async def entity_rotate(self, entity_id, cx, cy, angle) -> CommandResult:
        return await self._dispatch("entity-rotate", {"entity_id": entity_id, "cx": cx, "cy": cy, "angle": angle})

    async def entity_scale(self, entity_id, cx, cy, factor) -> CommandResult:
        return await self._dispatch("entity-scale", {"entity_id": entity_id, "cx": cx, "cy": cy, "factor": factor})

    async def entity_mirror(self, entity_id, x1, y1, x2, y2) -> CommandResult:
        return await self._dispatch("entity-mirror", {"entity_id": entity_id, "x1": x1, "y1": y1, "x2": x2, "y2": y2})

    async def entity_offset(self, entity_id, distance) -> CommandResult:
        return await self._dispatch("entity-offset", {"entity_id": entity_id, "distance": distance})

    async def entity_array(self, entity_id, rows, cols, row_dist, col_dist) -> CommandResult:
        return await self._dispatch("entity-array", {"entity_id": entity_id, "rows": rows, "cols": cols, "row_dist": row_dist, "col_dist": col_dist})

    async def entity_fillet(self, entity_id1, entity_id2, radius) -> CommandResult:
        return await self._dispatch("entity-fillet", {"id1": entity_id1, "id2": entity_id2, "radius": radius})

    async def entity_chamfer(self, entity_id1, entity_id2, dist1, dist2) -> CommandResult:
        return await self._dispatch("entity-chamfer", {"id1": entity_id1, "id2": entity_id2, "dist1": dist1, "dist2": dist2})

    # --- Layer operations ---

    async def layer_list(self) -> CommandResult:
        return await self._dispatch("layer-list", {})

    async def layer_create(self, name, color="white", linetype="CONTINUOUS") -> CommandResult:
        return await self._dispatch("layer-create", {"name": name, "color": color, "linetype": linetype})

    async def layer_set_current(self, name) -> CommandResult:
        return await self._dispatch("layer-set-current", {"name": name})

    async def layer_set_properties(self, name, color=None, linetype=None, lineweight=None) -> CommandResult:
        return await self._dispatch("layer-set-properties", {"name": name, "color": color, "linetype": linetype, "lineweight": lineweight})

    async def layer_freeze(self, name) -> CommandResult:
        return await self._dispatch("layer-freeze", {"name": name})

    async def layer_thaw(self, name) -> CommandResult:
        return await self._dispatch("layer-thaw", {"name": name})

    async def layer_lock(self, name) -> CommandResult:
        return await self._dispatch("layer-lock", {"name": name})

    async def layer_unlock(self, name) -> CommandResult:
        return await self._dispatch("layer-unlock", {"name": name})

    # --- Block operations ---

    async def block_list(self) -> CommandResult:
        return await self._dispatch("block-list", {})

    async def block_insert(self, name, x, y, scale=1.0, rotation=0.0, block_id=None) -> CommandResult:
        return await self._dispatch("block-insert", {"name": name, "x": x, "y": y, "scale": scale, "rotation": rotation, "block_id": block_id})

    async def block_insert_with_attributes(self, name, x, y, scale=1.0, rotation=0.0, attributes=None) -> CommandResult:
        return await self._dispatch("block-insert-with-attributes", {"name": name, "x": x, "y": y, "scale": scale, "rotation": rotation, "attributes": attributes or {}})

    async def block_get_attributes(self, entity_id) -> CommandResult:
        return await self._dispatch("block-get-attributes", {"entity_id": entity_id})

    async def block_update_attribute(self, entity_id, tag, value) -> CommandResult:
        return await self._dispatch("block-update-attribute", {"entity_id": entity_id, "tag": tag, "value": value})

    async def block_define(self, name, entities) -> CommandResult:
        return await self._dispatch("block-define", {"name": name, "entities": entities})

    # --- Annotation ---

    async def create_text(self, x, y, text, height=2.5, rotation=0.0, layer=None, color=None) -> CommandResult:
        return await self._dispatch("create-text", {"x": x, "y": y, "text": text, "height": height, "rotation": rotation, "layer": layer, "color": color})

    async def create_dimension_linear(self, x1, y1, x2, y2, dim_x, dim_y) -> CommandResult:
        return await self._dispatch("create-dimension-linear", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "dim_x": dim_x, "dim_y": dim_y})

    async def create_dimension_aligned(self, x1, y1, x2, y2, offset) -> CommandResult:
        return await self._dispatch("create-dimension-aligned", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "offset": offset})

    async def create_dimension_angular(self, cx, cy, x1, y1, x2, y2) -> CommandResult:
        return await self._dispatch("create-dimension-angular", {"cx": cx, "cy": cy, "x1": x1, "y1": y1, "x2": x2, "y2": y2})

    async def create_dimension_radius(self, cx, cy, radius, angle) -> CommandResult:
        return await self._dispatch("create-dimension-radius", {"cx": cx, "cy": cy, "radius": radius, "angle": angle})

    async def create_leader(self, points, text) -> CommandResult:
        pts_str = ";".join(f"{p[0]},{p[1]}" for p in points)
        return await self._dispatch("create-leader", {"points_str": pts_str, "text": text})

    # --- P&ID ---

    async def pid_setup_layers(self) -> CommandResult:
        return await self._dispatch("pid-setup-layers", {})

    async def pid_insert_symbol(self, category, symbol, x, y, scale=1.0, rotation=0.0) -> CommandResult:
        return await self._dispatch("pid-insert-symbol", {"category": category, "symbol": symbol, "x": x, "y": y, "scale": scale, "rotation": rotation})

    async def pid_list_symbols(self, category) -> CommandResult:
        return await self._dispatch("pid-list-symbols", {"category": category})

    async def pid_draw_process_line(self, x1, y1, x2, y2) -> CommandResult:
        return await self._dispatch("pid-draw-process-line", {"x1": x1, "y1": y1, "x2": x2, "y2": y2})

    async def pid_connect_equipment(self, x1, y1, x2, y2) -> CommandResult:
        return await self._dispatch("pid-connect-equipment", {"x1": x1, "y1": y1, "x2": x2, "y2": y2})

    async def pid_add_flow_arrow(self, x, y, rotation=0.0) -> CommandResult:
        return await self._dispatch("pid-add-flow-arrow", {"x": x, "y": y, "rotation": rotation})

    async def pid_add_equipment_tag(self, x, y, tag, description="") -> CommandResult:
        return await self._dispatch("pid-add-equipment-tag", {"x": x, "y": y, "tag": tag, "description": description})

    async def pid_add_line_number(self, x, y, line_num, spec) -> CommandResult:
        return await self._dispatch("pid-add-line-number", {"x": x, "y": y, "line_num": line_num, "spec": spec})

    async def pid_insert_valve(self, x, y, valve_type, rotation=0.0, attributes=None) -> CommandResult:
        return await self._dispatch("pid-insert-valve", {"x": x, "y": y, "valve_type": valve_type, "rotation": rotation, "attributes": attributes or {}})

    async def pid_insert_instrument(self, x, y, instrument_type, rotation=0.0, tag_id="", range_value="") -> CommandResult:
        return await self._dispatch("pid-insert-instrument", {"x": x, "y": y, "instrument_type": instrument_type, "rotation": rotation, "tag_id": tag_id, "range_value": range_value})

    async def pid_insert_pump(self, x, y, pump_type, rotation=0.0, attributes=None) -> CommandResult:
        return await self._dispatch("pid-insert-pump", {"x": x, "y": y, "pump_type": pump_type, "rotation": rotation, "attributes": attributes or {}})

    async def pid_insert_tank(self, x, y, tank_type, scale=1.0, attributes=None) -> CommandResult:
        return await self._dispatch("pid-insert-tank", {"x": x, "y": y, "tank_type": tank_type, "scale": scale, "attributes": attributes or {}})

    # --- View ---

    async def zoom_extents(self) -> CommandResult:
        return await self._dispatch("zoom-extents", {})

    async def zoom_window(self, x1, y1, x2, y2) -> CommandResult:
        return await self._dispatch("zoom-window", {"x1": x1, "y1": y1, "x2": x2, "y2": y2})

    async def get_screenshot(self) -> CommandResult:
        if self._screenshot_provider:
            data = self._screenshot_provider.capture()
            if data:
                return CommandResult(ok=True, payload=data)
        return CommandResult(ok=False, error="Screenshot capture failed")
