"""CLI bypass cho AutoCAD MCP — gọi thẳng server qua stdio, bỏ qua MCP client
bridge đang lỗi ('Invalid request parameters').

Sử dụng:
  python mcp_cli.py                          # list tools
  python mcp_cli.py drawing info             # drawing/info
  python mcp_cli.py layer list               # layer/list
  python mcp_cli.py drawing info --screenshot # kèm ảnh chụp màn hình
  python mcp_cli.py layer create --data '{"name":"WALLS","color":"1"}'

Trả về: JSON kết quả ở stdout, exit code 0 = ok, 1 = tool error, 2 = bad usage.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_CMD = str(REPO_ROOT / ".venv" / "Scripts" / "python.exe")
SERVER_ARGS = ["-m", "autocad_mcp"]
SERVER_CWD = str(REPO_ROOT)


async def _call(tool: str, operation: str, data_json: str | None, screenshot: bool) -> int:
    args: dict = {"operation": operation, "include_screenshot": screenshot}
    if data_json:
        try:
            args["data"] = json.loads(data_json)
        except json.JSONDecodeError as e:
            print(f"ERROR: --data khong phai JSON hop le: {e}", file=sys.stderr)
            return 2
    else:
        args["data"] = None

    params = StdioServerParameters(command=SERVER_CMD, args=SERVER_ARGS, cwd=SERVER_CWD)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, args)

            out = []
            for c in res.content:
                t = getattr(c, "text", None)
                if t is None:
                    out.append(str(c)[:200])
                    continue
                try:
                    out.append(json.loads(t))
                except (json.JSONDecodeError, ValueError):
                    out.append(t)
            payload = out[0] if len(out) == 1 else out
            print(json.dumps(payload, ensure_ascii=False, indent=2))

            if res.isError:
                return 1
            if isinstance(payload, dict) and payload.get("ok") is False:
                return 1
            return 0


async def _list_tools() -> int:
    params = StdioServerParameters(command=SERVER_CMD, args=SERVER_ARGS, cwd=SERVER_CWD)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            for t in tools.tools:
                desc = (t.description or "").split("\n")[0]
                print(f"{t.name:12s} {desc}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Goi AutoCAD MCP server truc tiep qua stdio (bypass bridge MCP client).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("tool", nargs="?", help="Tool name (drawing, entity, layer, block, annotation, pid, view, system)")
    p.add_argument("operation", nargs="?", default="", help="Operation (info, list, create...)")
    p.add_argument("--data", dest="data", default=None, help="JSON object for data")
    p.add_argument("--screenshot", action="store_true", help="Include screenshot")
    p.add_argument("--list", action="store_true", help="List tools")
    ns = p.parse_args()

    if ns.list or not ns.tool:
        return asyncio.run(_list_tools())
    if not ns.operation:
        print("ERROR: thieu operation. Dung --list hoac xem mo ta tool.", file=sys.stderr)
        return 2
    return asyncio.run(_call(ns.tool, ns.operation, ns.data, ns.screenshot))


if __name__ == "__main__":
    sys.exit(main())
