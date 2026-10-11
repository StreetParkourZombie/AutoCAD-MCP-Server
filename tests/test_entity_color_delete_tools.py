"""Tests for entity colour and batch delete — no AutoCAD needed.

Covers the three new operations (entity_set_color, entity_delete_by,
entity_delete_all), colour on the create methods, and colour in the query
payloads. The destructive guards get the most attention here: a batch
delete that silently wipes a drawing when a selector is mistyped is the
worst failure mode this feature can have.
"""

import pytest

from autocad_mcp.backends.base import CommandResult
from autocad_mcp.backends.ezdxf_backend import EzdxfBackend


@pytest.fixture
async def backend():
    b = EzdxfBackend()
    assert (await b.initialize()).ok
    return b


async def _seed(backend, count=3, layer=None, color=None):
    """Create `count` lines and return their handles."""
    handles = []
    for i in range(count):
        r = await backend.create_line(0, i * 10, 100, i * 10, layer=layer, color=color)
        assert r.ok
        handles.append(r.payload["handle"])
    return handles


# ---------------------------------------------------------------------------
# Colour in query payloads (Phase 1)
# ---------------------------------------------------------------------------


class TestColorInQueries:
    async def test_entity_list_reports_bylayer_by_default(self, backend):
        await _seed(backend, count=1)
        r = await backend.entity_list()
        assert r.ok
        assert r.payload["entities"][0]["color"] == 256

    async def test_entity_list_reports_explicit_color(self, backend):
        await _seed(backend, count=1, color="red")
        r = await backend.entity_list()
        assert r.payload["entities"][0]["color"] == 1

    async def test_entity_get_reports_color(self, backend):
        handles = await _seed(backend, count=1, color="blue")
        r = await backend.entity_get(handles[0])
        assert r.ok
        assert r.payload["color"] == 5

    async def test_query_by_color_selector(self, backend):
        """The gap that motivated this: 'which entities are red?' was unanswerable."""
        await _seed(backend, count=2, color="red")
        await _seed(backend, count=2, color="green")
        r = await backend.entity_delete_by(color="red", handles=["nonexistent"])
        # Only the red pair should match, and only the red pair should go.
        assert r.payload["matched"] == 0  # handles filter excludes everything
        r = await backend.entity_delete_by(color="red")
        assert r.payload["deleted"] == 2
        remaining = await backend.entity_list()
        assert remaining.payload["count"] == 2
        assert {e["color"] for e in remaining.payload["entities"]} == {3}


# ---------------------------------------------------------------------------
# entity_set_color (Phase 2)
# ---------------------------------------------------------------------------


class TestSetColor:
    @pytest.mark.parametrize(
        "name,expected",
        [("red", 1), ("yellow", 2), ("green", 3), ("cyan", 4),
         ("blue", 5), ("magenta", 6), ("white", 7), ("grey", 8), ("gray", 8)],
    )
    async def test_color_names_map_to_aci(self, backend, name, expected):
        handles = await _seed(backend, count=1)
        r = await backend.entity_set_color(handles[0], name)
        assert r.ok
        assert r.payload["color"] == expected

    async def test_numeric_color_passes_through(self, backend):
        handles = await _seed(backend, count=1)
        r = await backend.entity_set_color(handles[0], 30)
        assert r.payload["color"] == 30

    async def test_unknown_name_falls_back_to_white(self, backend):
        """A typo must not raise — it degrades to the same default layer_create uses."""
        handles = await _seed(backend, count=1)
        r = await backend.entity_set_color(handles[0], "octarine")
        assert r.ok
        assert r.payload["color"] == 7

    async def test_bylayer_restores_inheritance(self, backend):
        handles = await _seed(backend, count=1, layer="WALLS")
        await backend.entity_set_color(handles[0], "red")
        r = await backend.entity_set_color(handles[0], "bylayer")
        assert r.payload["color"] == 256

    async def test_set_color_round_trips_through_get(self, backend):
        handles = await _seed(backend, count=1)
        await backend.entity_set_color(handles[0], "cyan")
        r = await backend.entity_get(handles[0])
        assert r.payload["color"] == 4

    async def test_unknown_handle_returns_error_not_exception(self, backend):
        r = await backend.entity_set_color("DEADBEEF", "red")
        assert r.ok is False
        assert "not found" in r.error

    async def test_last_keyword(self, backend):
        await _seed(backend, count=1)
        r = await backend.entity_set_color("last", "red")
        assert r.ok
        assert r.payload["color"] == 1


# ---------------------------------------------------------------------------
# Colour on create (Phase 3)
# ---------------------------------------------------------------------------


class TestColorOnCreate:
    async def test_create_line_with_color(self, backend):
        r = await backend.create_line(0, 0, 10, 10, color="red")
        assert r.ok
        got = await backend.entity_get(r.payload["handle"])
        assert got.payload["color"] == 1

    async def test_create_without_color_keeps_bylayer(self, backend):
        r = await backend.create_line(0, 0, 10, 10)
        got = await backend.entity_get(r.payload["handle"])
        assert got.payload["color"] == 256

    async def test_create_circle_with_color(self, backend):
        r = await backend.create_circle(0, 0, 5, color="green")
        got = await backend.entity_get(r.payload["handle"])
        assert got.payload["color"] == 3

    async def test_create_polyline_with_color(self, backend):
        r = await backend.create_polyline([[0, 0], [10, 0]], color="magenta")
        got = await backend.entity_get(r.payload["handle"])
        assert got.payload["color"] == 6

    async def test_create_rectangle_forwards_color(self, backend):
        """Rectangle delegates to polyline — the colour has to survive that hop."""
        r = await backend.create_rectangle(0, 0, 10, 10, color="cyan")
        got = await backend.entity_get(r.payload["handle"])
        assert got.payload["color"] == 4

    async def test_create_arc_with_color(self, backend):
        r = await backend.create_arc(0, 0, 5, 0, 90, color="yellow")
        got = await backend.entity_get(r.payload["handle"])
        assert got.payload["color"] == 2

    async def test_create_ellipse_with_color(self, backend):
        r = await backend.create_ellipse(0, 0, 10, 10, 0.5, color="blue")
        got = await backend.entity_get(r.payload["handle"])
        assert got.payload["color"] == 5

    async def test_create_mtext_with_color(self, backend):
        r = await backend.create_mtext(0, 0, 50, "NOTE", color="red")
        got = await backend.entity_get(r.payload["handle"])
        assert got.payload["color"] == 1

    async def test_create_text_with_color(self, backend):
        r = await backend.create_text(0, 0, "LABEL", color="green")
        got = await backend.entity_get(r.payload["handle"])
        assert got.payload["color"] == 3

    async def test_color_and_layer_together(self, backend):
        r = await backend.create_line(0, 0, 10, 10, layer="WALLS", color="red")
        got = await backend.entity_get(r.payload["handle"])
        assert got.payload["layer"] == "WALLS"
        assert got.payload["color"] == 1


# ---------------------------------------------------------------------------
# entity_delete_by (Phase 4)
# ---------------------------------------------------------------------------


class TestDeleteBy:
    async def test_no_selectors_refused(self, backend):
        """An empty call must never mean 'delete everything'."""
        await _seed(backend, count=5)
        r = await backend.entity_delete_by()
        assert r.ok is False
        assert "selector" in r.error
        remaining = await backend.entity_list()
        assert remaining.payload["count"] == 5

    async def test_delete_by_layer(self, backend):
        await _seed(backend, count=3, layer="TEMP")
        await _seed(backend, count=2, layer="KEEP")
        r = await backend.entity_delete_by(layer="TEMP")
        assert r.ok
        assert r.payload["deleted"] == 3
        remaining = await backend.entity_list(layer="KEEP")
        assert remaining.payload["count"] == 2

    async def test_delete_by_type(self, backend):
        await backend.create_line(0, 0, 10, 10)
        await backend.create_circle(0, 0, 5)
        r = await backend.entity_delete_by(type="CIRCLE")
        assert r.payload["deleted"] == 1
        remaining = await backend.entity_list()
        assert remaining.payload["entities"][0]["type"] == "LINE"

    async def test_delete_by_color(self, backend):
        await _seed(backend, count=2, color="red")
        await _seed(backend, count=3, color="green")
        r = await backend.entity_delete_by(color="red")
        assert r.payload["deleted"] == 2
        remaining = await backend.entity_list()
        assert remaining.payload["count"] == 3

    async def test_delete_by_handles(self, backend):
        handles = await _seed(backend, count=5)
        r = await backend.entity_delete_by(handles=[handles[0], handles[3]])
        assert r.payload["deleted"] == 2
        assert sorted(r.payload["handles"]) == sorted([handles[0], handles[3]])

    async def test_delete_by_last_n(self, backend):
        handles = await _seed(backend, count=5)
        r = await backend.entity_delete_by(last_n=2)
        assert r.payload["deleted"] == 2
        assert sorted(r.payload["handles"]) == sorted(handles[-2:])
        remaining = await backend.entity_list()
        assert remaining.payload["count"] == 3

    async def test_selectors_are_anded(self, backend):
        await _seed(backend, count=3, layer="TEMP", color="red")
        await _seed(backend, count=2, layer="TEMP", color="blue")
        r = await backend.entity_delete_by(layer="TEMP", color="red")
        assert r.payload["deleted"] == 3
        remaining = await backend.entity_list(layer="TEMP")
        assert remaining.payload["count"] == 2

    async def test_matched_equals_deleted_when_all_succeed(self, backend):
        await _seed(backend, count=3)
        r = await backend.entity_delete_by(layer="0")
        assert r.payload["matched"] == r.payload["deleted"] == 3

    async def test_no_matches_is_success_with_zero_deleted(self, backend):
        await _seed(backend, count=2)
        r = await backend.entity_delete_by(layer="NOPE")
        assert r.ok
        assert r.payload["matched"] == 0
        assert r.payload["deleted"] == 0

    async def test_last_n_larger_than_drawing_is_safe(self, backend):
        await _seed(backend, count=2)
        r = await backend.entity_delete_by(last_n=99)
        assert r.payload["deleted"] == 2


# ---------------------------------------------------------------------------
# entity_delete_all (Phase 5)
# ---------------------------------------------------------------------------


class TestDeleteAll:
    async def test_requires_confirm(self, backend):
        await _seed(backend, count=5)
        r = await backend.entity_delete_all()
        assert r.ok is False
        assert "confirm" in r.error
        remaining = await backend.entity_list()
        assert remaining.payload["count"] == 5

    async def test_confirm_false_is_refused(self, backend):
        await _seed(backend, count=3)
        r = await backend.entity_delete_all(confirm=False)
        assert r.ok is False
        remaining = await backend.entity_list()
        assert remaining.payload["count"] == 3

    async def test_confirm_true_wipes_drawing(self, backend):
        await _seed(backend, count=4)
        r = await backend.entity_delete_all(confirm=True)
        assert r.ok
        assert r.payload["matched"] == 4
        assert r.payload["deleted"] == 4
        remaining = await backend.entity_list()
        assert remaining.payload["count"] == 0


# ---------------------------------------------------------------------------
# Server-level routing (Phase 2/4/5 reachability through the MCP tool)
# ---------------------------------------------------------------------------


class TestServerRouting:
    @pytest.fixture
    def mock_backend(self, monkeypatch):
        from unittest.mock import AsyncMock

        from autocad_mcp import server as server_mod

        b = AsyncMock()
        b.entity_set_color.return_value = CommandResult(ok=True, payload={"color": 1})
        b.entity_delete_by.return_value = CommandResult(ok=True, payload={"deleted": 0})
        b.entity_delete_all.return_value = CommandResult(ok=True, payload={"deleted": 0})
        b.create_line.return_value = CommandResult(ok=True, payload={"handle": "H1"})
        monkeypatch.setattr(server_mod, "get_backend", AsyncMock(return_value=b))
        return server_mod, b

    @pytest.mark.asyncio
    async def test_set_color_routes_through(self, mock_backend):
        server_mod, b = mock_backend
        result = await server_mod.entity(operation="set_color", entity_id="AB", data={"color": "red"})
        b.entity_set_color.assert_awaited_once_with("AB", "red")
        assert '"ok":true' in result

    @pytest.mark.asyncio
    async def test_set_color_defaults_to_bylayer(self, mock_backend):
        server_mod, b = mock_backend
        await server_mod.entity(operation="set_color", entity_id="AB")
        b.entity_set_color.assert_awaited_once_with("AB", "bylayer")

    @pytest.mark.asyncio
    async def test_create_line_forwards_color(self, mock_backend):
        server_mod, b = mock_backend
        await server_mod.entity(operation="create_line", x1=0, y1=0, x2=1, y2=1, data={"color": "red"})
        b.create_line.assert_awaited_once_with(0, 0, 1, 1, None, "red")

    @pytest.mark.asyncio
    async def test_delete_by_passes_all_selectors(self, mock_backend):
        server_mod, b = mock_backend
        await server_mod.entity(operation="delete_by", data={
            "layer": "TEMP", "type": "LINE", "color": "red",
            "handles": ["A", "B"], "last_n": 5,
        })
        b.entity_delete_by.assert_awaited_once_with(
            layer="TEMP", type="LINE", color="red", handles=["A", "B"], last_n=5
        )

    @pytest.mark.asyncio
    async def test_delete_by_with_no_data(self, mock_backend):
        server_mod, b = mock_backend
        await server_mod.entity(operation="delete_by")
        b.entity_delete_by.assert_awaited_once_with(
            layer=None, type=None, color=None, handles=None, last_n=None
        )

    @pytest.mark.asyncio
    async def test_delete_all_defaults_confirm_false(self, mock_backend):
        server_mod, b = mock_backend
        await server_mod.entity(operation="delete_all")
        b.entity_delete_all.assert_awaited_once_with(False)

    @pytest.mark.asyncio
    async def test_delete_all_with_confirm(self, mock_backend):
        server_mod, b = mock_backend
        await server_mod.entity(operation="delete_all", data={"confirm": True})
        b.entity_delete_all.assert_awaited_once_with(True)

    @pytest.mark.asyncio
    async def test_backend_error_surfaces_as_error(self, mock_backend):
        server_mod, b = mock_backend
        b.entity_delete_all.return_value = CommandResult(ok=False, error="requires confirm=True")
        result = await server_mod.entity(operation="delete_all")
        assert '"error"' in result
