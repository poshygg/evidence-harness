"""Actual MCP initialize/list/call smoke test using a local stdio subprocess."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


EXPECTED_TOOLS = {
    "init_workspace", "record_requirement", "record_decision", "record_ai_assessment",
    "register_check", "run_check", "compare_runs", "evidence_status", "export_review_packet",
}


async def check(config_path: str, python: str) -> dict:
    params = StdioServerParameters(
        command=python,
        args=["-m", "evidence_harness", "serve", "--workspace", str(Path(config_path).resolve())],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            listing = await session.list_tools()
            names = {tool.name for tool in listing.tools}
            if names != EXPECTED_TOOLS:
                raise RuntimeError(f"Tool catalog mismatch: {sorted(names)}")
            result = await session.call_tool("evidence_status", {"max_items": 3})
            if result.isError:
                raise RuntimeError("evidence_status returned an MCP error")
            structured = getattr(result, "structuredContent", None)
            if structured is None:
                for block in result.content:
                    if getattr(block, "type", None) == "text":
                        try:
                            structured = json.loads(block.text)
                            break
                        except json.JSONDecodeError:
                            continue
            if not isinstance(structured, dict) or structured.get("status") == "error":
                raise RuntimeError(f"Invalid evidence_status result: {structured}")
            return {
                "status": "passed",
                "protocol_version": initialized.protocolVersion,
                "server_name": initialized.serverInfo.name,
                "tools": sorted(names),
                "actual_call": "evidence_status",
                "workspace_id": structured.get("workspace_id"),
                "scope": "local stdio MCP; not a connection from the target exam platform",
            }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--timeout", type=float, default=40)
    args = parser.parse_args()

    async def bounded():
        async with asyncio.timeout(args.timeout):
            return await check(args.workspace, args.python)

    try:
        result = asyncio.run(bounded())
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
