"""Tests for the `view` and `annotation` server tools — no AutoCAD needed.

These two tools had no direct server-level tests (M8ven flagged 6/8 coverage),
so we pin their routing and the two screenshot branches in `view`.
"""

import pytest
from unittest.mock import AsyncMock

from autocad_mcp.backends.base import CommandResult


@pytest.fixture
def backend():
    """AsyncMock backend: every attribute call resolves to a CommandResult."""
    b = AsyncMock()
    b.zoom_extents.return_value = CommandResult(ok=True, payload={"zoom": "extents"})
    b.zoom_window.return_value = CommandResult(ok=True, payload={"zoom": "window"})
    b.get_screenshot.return_value = CommandResult(ok=True, payload="aW1n")
    b.create_text.return_value = CommandResult(ok=True, payload={"handle": "T1"})
    return b


def _patch_backend(monkeypatch, backend):
    from autocad_mcp import server as server_mod

    monkeypatch.setattr(server_mod, "get_backend", AsyncMock(return_value=backend))
    return server_mod


class TestViewRouting:
    @pytest.mark.asyncio
    async def test_zoom_extents_calls_backend(self, monkeypatch, backend):
        server_mod = _patch_backend(monkeypatch, backend)
        result = await server_mod.view(operation="zoom_extents")
        backend.zoom_extents.assert_awaited_once()
        assert '"zoom"' in result

    @pytest.mark.asyncio
    async def test_zoom_window_passes_bounds(self, monkeypatch, backend):
        server_mod = _patch_backend(monkeypatch, backend)
        await server_mod.view(operation="zoom_window", x1=0, y1=0, x2=100, y2=50)
        backend.zoom_window.assert_awaited_once_with(0, 0, 100, 50)

    @pytest.mark.asyncio
    async def test_get_screenshot_returns_image_content(self, monkeypatch, backend):
        """The happy path must return both text and image parts."""
        server_mod = _patch_backend(monkeypatch, backend)
        result = await server_mod.view(operation="get_screenshot")
        assert isinstance(result, list)
        types = [c.type for c in result]
        assert "text" in types and "image" in types

    @pytest.mark.asyncio
    async def test_get_screenshot_backend_failure_returns_json(self, monkeypatch, backend):
        """A failed screenshot must degrade to a JSON error, not raise."""
        backend.get_screenshot.return_value = CommandResult(ok=False, error="no AutoCAD")
        server_mod = _patch_backend(monkeypatch, backend)
        result = await server_mod.view(operation="get_screenshot")
        assert '"error"' in result

    @pytest.mark.asyncio
    async def test_unknown_operation_rejected(self, monkeypatch, backend):
        server_mod = _patch_backend(monkeypatch, backend)
        result = await server_mod.view(operation="destroy_everything")
        assert '"error"' in result


class TestAnnotationRouting:
    @pytest.mark.asyncio
    async def test_create_text_passes_args(self, monkeypatch, backend):
        server_mod = _patch_backend(monkeypatch, backend)
        data = {"x": 10, "y": 20, "text": "HELLO", "height": 5.0, "rotation": 90.0, "layer": "NOTES"}
        await server_mod.annotation(operation="create_text", data=data)
        backend.create_text.assert_awaited_once_with(10, 20, "HELLO", 5.0, 90.0, "NOTES")

    @pytest.mark.asyncio
    async def test_create_text_defaults(self, monkeypatch, backend):
        """Height and rotation default when omitted from data."""
        server_mod = _patch_backend(monkeypatch, backend)
        await server_mod.annotation(operation="create_text", data={"x": 0, "y": 0, "text": "X"})
        backend.create_text.assert_awaited_once_with(0, 0, "X", 2.5, 0.0, None)

    @pytest.mark.asyncio
    async def test_missing_required_field_is_backend_error(self, monkeypatch, backend):
        """Missing 'text' raises KeyError inside the tool; @_safe converts it.

        We assert the decorator's behaviour here so a regression (raw
        exception leaking to the client) is caught.
        """
        server_mod = _patch_backend(monkeypatch, backend)
        result = await server_mod.annotation(operation="create_text", data={"x": 0, "y": 0})
        assert '"error"' in str(result)

    @pytest.mark.asyncio
    async def test_unknown_operation_rejected(self, monkeypatch, backend):
        server_mod = _patch_backend(monkeypatch, backend)
        result = await server_mod.annotation(operation="erase_all_text")
        assert '"error"' in result


class TestToolAnnotations:
    """Every tool must declare all four MCP annotation hints (M8ven finding)."""

    @pytest.mark.asyncio
    async def test_all_tools_declare_four_hints(self):
        from autocad_mcp.server import mcp

        tools = await mcp.list_tools()
        assert len(tools) == 8
        for tool in tools:
            ann = tool.annotations
            for hint in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
                assert getattr(ann, hint, None) is not None, f"{tool.name} missing {hint}"
