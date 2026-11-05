import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.helpers import WorkspaceCase


class ProtocolTests(WorkspaceCase):
    def test_fixed_older_protocol_initialize_and_call(self):
        async def exercise():
            environment = {key: os.environ[key] for key in ["PATH", "SYSTEMROOT", "TEMP", "TMP"] if key in os.environ}
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "evidence_harness", "serve", "--workspace", str(self.config),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=environment,
            )
            async def exchange(message):
                process.stdin.write((json.dumps(message) + "\n").encode())
                await process.stdin.drain()
                raw = await asyncio.wait_for(process.stdout.readline(), timeout=15)
                self.assertTrue(raw, "The stdio server closed before returning a response.")
                return json.loads(raw)
            try:
                initialized = await exchange({
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "older-protocol-test", "version": "1.0"}},
                })
                self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
                process.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode())
                await process.stdin.drain()
                listed = await exchange({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
                self.assertIn("evidence_status", {tool["name"] for tool in listed["result"]["tools"]})
                called = await exchange({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "evidence_status", "arguments": {"max_items": 5}}})
                self.assertNotIn("error", called)
                self.assertFalse(called["result"].get("isError", False))
            finally:
                if process.returncode is None:
                    process.terminate()
                try:
                    await asyncio.wait_for(process.communicate(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.communicate()
        asyncio.run(exercise())

    def test_initialize_discovery_and_actual_tool_call(self):
        async def exercise():
            source = str(Path(__file__).resolve().parents[1] / "src")
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "evidence_harness", "serve", "--workspace", str(self.config)],
                env={"PYTHONPATH": source},
            )
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    self.assertTrue(initialized.protocolVersion)
                    listing = await session.list_tools()
                    names = {tool.name for tool in listing.tools}
                    expected = {"init_workspace", "record_requirement", "record_decision", "register_check", "run_check", "compare_runs", "record_ai_assessment", "evidence_status", "export_review_packet"}
                    self.assertTrue(expected.issubset(names), names)
                    result = await session.call_tool("record_requirement", {"request_id": "protocol-req", "title": "Protocol synthetic case", "acceptance_criteria": ["Tool call creates a record"]})
                    self.assertFalse(result.isError, result)
                    text = "\n".join(getattr(item, "text", "") for item in result.content)
                    payload = json.loads(text)
                    self.assertIn("record_id", payload)
                    self.assertEqual(payload["data"]["title"], "Protocol synthetic case")
                    status = await session.call_tool("evidence_status", {"max_items": 10})
                    self.assertFalse(status.isError, status)
                    invalid = await session.call_tool("record_requirement", {"request_id": "protocol-invalid", "title": "Missing criteria"})
                    self.assertTrue(invalid.isError)
        asyncio.run(exercise())
