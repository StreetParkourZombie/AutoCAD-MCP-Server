"""Tests for the AutoLISP dispatcher source — no AutoCAD needed.

The dispatcher is a plain text file that AutoCAD loads at runtime, so
nothing in pytest exercises it. A paren typo there surfaces to the user
as a dispatcher-not-loaded error, minutes later, with no useful trace.
These tests are cheap insurance against that.

Paren counting has to be string-aware: mcp-split-string is called with ";"
as a literal delimiter, and several JSON payloads embed semicolons. A
naive "everything after ; is a comment" strip deletes real closing parens
and reports a phantom imbalance — which is exactly the false positive that
sent this file's first draft chasing a bug that did not exist.
"""

import re
from pathlib import Path

import pytest

DISPATCHER = Path(__file__).resolve().parents[1] / "lisp-code" / "mcp_dispatch.lsp"


def _skip_literal(src: str, i: int) -> int:
    """Advance past a string or char literal starting at i; return its end."""
    if src[i] == "\\":  # AutoLISP char literal, e.g. \U+0041
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


def scan(src: str) -> tuple[int, int]:
    """Walk the source once, returning (final_depth, unterminated_string_line).

    Strictly single-pass and flag-free: whenever we meet a quote we skip the
    whole literal via _skip_literal, so a ';' inside a string is never
    mistaken for a comment. Keeping a separate in_string flag alongside the
    skip desynchronises the walk and reports phantom imbalance.
    """
    depth = 0
    unterminated = 0
    i = 0
    while i < len(src):
        ch = src[i]
        if ch == '"':
            end = _skip_literal(src, i)
            if end >= len(src):
                unterminated = src.count("\n", 0, i) + 1
            i = end
            continue
        if ch == ";":
            nl = src.find("\n", i)
            i = len(src) if nl == -1 else nl
            continue
        if ch == "\\":
            i += 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
    return depth, unterminated


def code_only(src: str) -> str:
    """Return src with string literals and comments blanked out, preserving offsets.

    Several tests assert that a forbidden AutoLISP idiom is absent. The
    dispatcher explains each of those idioms in its docstrings and comments,
    so searching raw text always matched the prose describing the bug
    rather than the bug. Reusing the same string-aware walk as scan keeps
    every index identical, so the returned text stays line-aligned.
    """
    out = list(src)
    i = 0
    while i < len(src):
        ch = src[i]
        if ch == '"':
            end = _skip_literal(src, i)
            for j in range(i, min(end, len(out))):
                if out[j] != "\n":
                    out[j] = " "
            i = end
            continue
        if ch == ";":
            nl = src.find("\n", i)
            end = len(src) if nl == -1 else nl
            for j in range(i, end):
                if out[j] != "\n":
                    out[j] = " "
            i = end
            continue
        i += 1
    return "".join(out)


def top_level_defuns(src: str) -> list[str]:
    """Names of every top-level defun whose form closes cleanly.

    Walks with the string-aware scanner and records a defun only when depth
    returns to 0 — a defun missing a closing paren swallows everything after
    it, so it never shows up here.
    """
    found: list[str] = []
    pending = None
    depth = 0
    i = 0
    while i < len(src):
        ch = src[i]
        if ch == '"':
            i = _skip_literal(src, i)
            continue
        if ch == ";":
            nl = src.find("\n", i)
            i = len(src) if nl == -1 else nl
            continue
        if ch == "\\":
            i += 2
            continue
        if ch == "(":
            depth += 1
            if depth == 1:
                m = re.match(r"\(defun\s+(mcp-[a-z0-9-]+)", src[i:])
                pending = m.group(1) if m else None
        elif ch == ")":
            depth -= 1
            if depth == 0 and pending:
                found.append(pending)
                pending = None
        i += 1
    return found


@pytest.fixture(scope="module")
def source() -> str:
    assert DISPATCHER.exists(), f"dispatcher missing at {DISPATCHER}"
    return DISPATCHER.read_text(encoding="utf-8")


class TestDispatcherSyntax:
    def test_parentheses_balance(self, source):
        depth, _ = scan(source)
        assert depth == 0, f"{abs(depth)} unbalanced parenthesis/es in mcp_dispatch.lsp"

    def test_no_unterminated_string(self, source):
        _, line = scan(source)
        assert line == 0, f"a string literal is left open starting near line {line}"

    def test_every_defun_closes_on_its_own(self, source):
        declared = re.findall(r"\(defun\s+(mcp-[a-z0-9-]+)", source)
        closed = top_level_defuns(source)
        missing = sorted(set(declared) - set(closed))
        assert not missing, f"defuns that never close: {missing}"
        assert len(declared) == len(closed), (
            f"{len(declared)} declared, {len(closed)} closed cleanly — "
            "a defun is missing a closing parenthesis"
        )

    def test_defun_names_unique(self, source):
        """A duplicate defun silently shadows the earlier one."""
        names = re.findall(r"\(defun\s+(mcp-[a-z0-9-]+)", source)
        dupes = sorted({n for n in names if names.count(n) > 1})
        assert not dupes, f"duplicate defun names: {dupes}"


class TestDispatcherCoverage:
    def test_every_branch_has_a_handler(self, source):
        """A cond branch pointing at a nonexistent defun errors only at runtime."""
        defined = set(re.findall(r"\(defun\s+(mcp-[a-z0-9-]+)", source))
        branches = re.findall(r'\(= cmd-name "([a-z-]+)"\)\s*\n\s*\((mcp-[a-z-]+)', source)
        missing = [(cmd, fn) for cmd, fn in branches if fn not in defined]
        assert not missing, f"dispatch branches with no handler: {missing}"

    def test_expected_commands_all_routed(self, source):
        """The Python-side EXPECTED_COMMANDS list must have a cond branch."""
        from tests.test_ipc_protocol import TestDispatchMapCoverage

        routed = set(re.findall(r'\(= cmd-name "([a-z-]+)"\)', source))
        unrouted = [c for c in TestDispatchMapCoverage.EXPECTED_COMMANDS if c not in routed]
        assert not unrouted, f"expected commands with no cond branch: {unrouted}"

    def test_create_commands_go_through_color_wrapper(self, source):
        """A create branch bypassing the wrapper ignores the colour argument.

        mcp-create-with-color runs the create first, then entmods the new
        entity — that ordering is what makes colour work at all, since the
        entity has to exist before entmod can touch it.
        """
        wrapped = set(re.findall(r"mcp-create-with-color 'mcp-cmd-create-([a-z-]+)", source))
        expected = {"line", "circle", "polyline", "rectangle", "text", "arc", "ellipse", "mtext"}
        assert expected <= wrapped, f"creates not routed through the colour wrapper: {expected - wrapped}"

    def test_color_wrapper_invokes_handler_with_apply(self, source):
        """AutoLISP cannot call a function held in a variable directly.

        ``(create-fn params)`` is Common Lisp. AutoLISP parses the head of a
        call as a symbol name only, so this raised
        ``bad function: MCP-CMD-CREATE-LINE`` on the live backend and broke
        every colour-carrying create while the whole suite stayed green —
        pytest reads the LISP as text and never calls it. ``apply`` is the
        AutoLISP equivalent and is the only form that works here.
        """
        body = re.search(r"\(defun mcp-create-with-color.*?\n\)", source, re.S)
        assert body, "mcp-create-with-color not found"
        text = body.group(0)
        assert "(apply create-fn (list params))" in text, (
            "handler is not invoked via apply — AutoLISP will raise "
            "'bad function' at runtime"
        )
        # Ignore the defun header, which legitimately names the parameter.
        body_only = text.split(")", 1)[1] if ")" in text else text
        assert not re.search(r"\(\s*create-fn\s+", code_only(body_only)), (
            "(create-fn params) is Common Lisp syntax; AutoLISP cannot "
            "evaluate a variable in the head of a call"
        )

    def test_no_common_lisp_last_with_count(self, source):
        """AutoLISP `last` takes one argument; (last lst n) is a runtime error.

        Found by live testing: every delete_by call failed with
        \"too many arguments\" because of this one form.
        """
        bad = re.findall(r"\(last\s+[^\s()]+[^\s()]*\s+[^\s()]*\s*\)", code_only(source))
        assert not bad, (
            f"two-argument `last` is Common Lisp, not AutoLISP: {bad}"
        )

    def test_take_last_n_avoids_nth_as_tail(self, source):
        """AutoLISP `nth` returns the element, not the sublist at that index.

        Using it to reach a tail the way Common Lisp's nthcdr does yields a
        number where a list is expected — \"bad argument type: listp 3\".
        """
        body = re.search(r"\(defun mcp-take-last-n.*?\n\)", source, re.S)
        assert body, "mcp-take-last-n not found"
        text = body.group(0)
        assert "(last " not in code_only(text).replace("(last head)", ""), (
            "mcp-take-last-n must not call `last` on an element from nth"
        )

    def test_colour_write_appends_to_entget(self, source):
        """A bare (entmod (list (cons 62 aci))) silently does nothing.

        It returns without error but leaves group 62 nil, so the caller is
        told the colour was applied when it never was. Confirmed against
        live AutoCAD: g62 stayed nil with the bare form and became (62 . 3)
        once entget was appended.
        """
        body = re.search(r"\(defun mcp-set-color.*?\n\)", source, re.S)
        assert body, "mcp-set-color not found"
        assert "(append (entget ent)" in body.group(0), (
            "colour must be appended onto the entity's existing DXF data"
        )
        assert "(entmod (list (cons 62" not in code_only(source), (
            "bare entmod on a colour list is a silent no-op in AutoCAD"
        )

    def test_empty_json_string_decodes_to_nil(self, source):
        """An empty JSON value must not decode to a stray quote.

        Live probe returned handles_str as '\"' rather than '' for "". A lone
        quote is truthy in AutoLISP, so every 'is this field set?' guard was
        defeated and delete_by's handle filter rejected the whole drawing.
        """
        body = re.search(r"\(defun mcp-json-get-string.*?\n  \)", source, re.S)
        assert body, "mcp-json-get-string not found"
        text = body.group(0)
        assert "(if (= end-pos pos)" in text, (
            "empty string values must return nil, not a zero-length slice"
        )
        assert "(or (= end-pos pos)" not in text, (
            "the (= end-pos pos) clause forces the scan one step past the "
            "closing quote, which is what produced the stray quote"
        )

    def test_last_n_requires_a_positive_count(self, source):
        """AutoLISP treats only nil as false, so 0 is truthy.

        An absent last_n arrives as an empty string and (atoi "") is 0. That
        zero passed the selector test and then trimmed the match list to
        nothing, so delete_by reported matched=0 against a layer that
        definitely had entities on it.
        """
        body = re.search(r"\(defun mcp-cmd-entity-delete-by.*?\n\)", source, re.S)
        assert body, "mcp-cmd-entity-delete-by not found"
        assert "(> (atoi last-n-str) 0)" in body.group(0), (
            "last_n must require a positive count — AutoLISP treats 0 as true"
        )

    @pytest.mark.parametrize("cmd", ["entity-set-color", "entity-delete-by", "entity-delete-all"])
    def test_new_commands_registered(self, source, cmd):
        assert f'= cmd-name "{cmd}"' in source, f"{cmd} not in the dispatch table"


class TestColorHelpers:
    def test_aci_mapping_matches_python(self, source):
        """The LISP and Python colour maps must agree, or the two backends disagree."""
        from autocad_mcp.backends.ezdxf_backend import EzdxfBackend

        body = re.search(r"\(defun mcp-color-to-aci.*?\n\)", source, re.S)
        assert body, "mcp-color-to-aci not found"
        lisp_map = dict(
            (name, int(aci))
            for name, aci in re.findall(r'\(= s "([A-Z]+)"\) (\d+)\)', body.group(0))
        )
        for name in lisp_map:
            assert EzdxfBackend._color_to_int(name.lower()) == lisp_map[name], (
                f"{name}: LISP says {lisp_map[name]}, Python says "
                f"{EzdxfBackend._color_to_int(name.lower())}"
            )

    def test_group_code_62_used_for_colour(self, source):
        """DXF group 62 is the ACI colour; 420 is true colour. Names use 62."""
        assert "(assoc 62 ent-data)" in source, "entity queries never read group 62"
        assert "(cons 62 aci)" in source, "colour writes never use group 62"

    def test_handles_captured_before_entdel(self, source):
        """entget returns nil for an erased entity — handle must be read first."""
        body = re.search(r"\(defun mcp-cmd-entity-delete-by.*?\n\)", source, re.S)
        assert body, "mcp-cmd-entity-delete-by not found"
        text = body.group(0)
        read = text.index('(cdr (assoc 5 (entget target)))')
        delete = text.index("(entdel target)")
        assert read < delete, "handle is read after entdel — it will always be nil"
