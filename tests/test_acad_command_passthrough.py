"""Tests for the `acad_command` passthrough — no AutoCAD needed.

`acad_command` is the only path that reaches commands outside the typed
whitelist map, so the two things worth pinning down here are the token
encoding (a bug here silently corrupts a value instead of raising) and the
routing/validation in `server.system`.

The LISP-side policy denylist is *not* covered — it can only be verified
against a live AutoCAD, since every failure mode it protects against is
silent. See docs/Met_Errors.md §8.
"""

import pytest
from unittest.mock import AsyncMock

from autocad_mcp.backends.base import AutoCADBackend, BackendCapabilities, CommandResult
from autocad_mcp.backends.file_ipc import FileIPCBackend


class _CapturingBackend(FileIPCBackend):
    """FileIPCBackend that records the dispatched command instead of writing IPC files."""

    def __init__(self):
        super().__init__()
        self.captured = None

    async def _dispatch(self, op, params):
        self.captured = (op, params)
        return CommandResult(ok=True, payload={"command": op})


@pytest.fixture
def backend():
    return _CapturingBackend()


class TestAcadCommandEncoding:
    @pytest.mark.asyncio
    async def test_tokens_join_with_semicolon(self, backend):
        await backend.acad_command(["_.TRIM", "_All", "", ""])
        op, params = backend.captured
        assert op == "acad-command"
        assert params["args_str"] == "_.TRIM;_All;;"

    @pytest.mark.asyncio
    async def test_empty_token_is_bare_enter(self, backend):
        """An empty string means "accept the default", so it must survive verbatim."""
        await backend.acad_command(["_.LINE", "", ""])
        _, params = backend.captured
        assert params["args_str"] == "_.LINE;;"

    @pytest.mark.asyncio
    async def test_space_becomes_tilde(self, backend):
        """Spaces are escaped to ~ because the LISP side splits the string on ';'."""
        await backend.acad_command(["_.TEXT", "C:/my path/file.txt"])
        _, params = backend.captured
        assert params["args_str"] == "_.TEXT;C:/my~path/file.txt"

    @pytest.mark.asyncio
    async def test_numeric_token_accepted(self, backend):
        """A radius arrives as an int through a JSON schema; int has no .replace."""
        await backend.acad_command(["_.CIRCLE", "127749.0919,88293.0194", 1, ""])
        _, params = backend.captured
        assert params["args_str"] == "_.CIRCLE;127749.0919,88293.0194;1;"

    @pytest.mark.asyncio
    async def test_empty_args_rejected(self, backend):
        result = await backend.acad_command([])
        assert result.ok is False
        assert "at least one command token" in result.error
        assert backend.captured is None


class _MinimalBackend(AutoCADBackend):
    """Concrete subclass exercising only the base-class default of acad_command."""

    @property
    def name(self):
        return "minimal"

    @property
    def capabilities(self):
        return BackendCapabilities()

    async def initialize(self):
        return CommandResult(ok=True)

    async def status(self):
        return CommandResult(ok=True, payload={"backend": "minimal"})


class TestAcadCommandBaseDefault:
    @pytest.mark.asyncio
    async def test_base_backend_refuses(self):
        """Backends without passthrough must refuse, not crash or silently no-op."""
        result = await _MinimalBackend().acad_command(["_.TRIM", ""])
        assert result.ok is False
        assert result.error == "Not supported on this backend"


class TestSystemRouting:
    """`system(acad_command=...)` must validate `data.args` before reaching the backend."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("data", [{}, {"args": None}, {"args": "_.TRIM"}, {"args": []}])
    async def test_bad_args_rejected(self, monkeypatch, data):
        """args must be a non-empty list; a bare string would iterate per character."""
        from autocad_mcp import server as server_mod

        # get_backend is defined in client.py but imported into server.py's
        # namespace, so patching client._backend would not intercept the call.
        monkeypatch.setattr(
            server_mod, "get_backend", AsyncMock(return_value=_MinimalBackend())
        )

        result = await server_mod.system(operation="acad_command", data=data)
        assert '"error"' in str(result)
        assert "non-empty list" in str(result)
