"""AutoCAD MCP Server v3.0 — File IPC + ezdxf headless backend."""

# Warm up pywin32 window enumeration BEFORE any other imports.
# Import-order bug: if structlog (or its rich dependency chain) is imported
# first, later EnumWindows calls fail with
# pywintypes.error: (126, 'EnumWindows', 'The specified module could not be found').
# Enumerating once while the process is still "healthy" keeps it working.
import sys as _sys

if _sys.platform == "win32":
    try:
        import win32gui as _win32gui

        _win32gui.EnumWindows(lambda _h, _a: None, None)
    except Exception:
        pass

__version__ = "3.0.0"
