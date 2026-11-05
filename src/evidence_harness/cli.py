"""Command line access to the same local core used by the MCP server."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__


TOOLS = (
    "init_workspace",
    "record_requirement",
    "record_decision",
    "record_ai_assessment",
    "register_check",
    "run_check",
    "compare_runs",
    "evidence_status",
    "export_review_packet",
)


def dump(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evidence-harness")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "doctor", "call"):
        command = sub.add_parser(name)
        command.add_argument("--workspace", required=True, help="Workspace config JSON")
        if name == "call":
            command.add_argument("--tool", required=True, choices=TOOLS)
            command.add_argument("--arguments", default="{}", help="JSON object")
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            from .server import serve

            serve(args.workspace)
            return 0

        from .core import Harness, HarnessError

        harness = Harness(args.workspace)
        if args.command == "doctor":
            sdk_version = importlib.metadata.version("mcp")
            from mcp.server.fastmcp import FastMCP  # noqa: F401

            dump(
                {
                    "status": "ready" if sdk_version == "1.26.0" else "unsupported_sdk",
                    "version": __version__,
                    "python": platform.python_version(),
                    "mcp_sdk": sdk_version,
                    "workspace_config": str(Path(args.workspace).resolve()),
                    "workspace_root": str(harness.root),
                    "state_dir": str(harness.state),
                    "workspace_id": harness.workspace_id,
                    "transport": "stdio",
                    "tool_count": len(TOOLS),
                    "limitations": [
                        "Doctor is a local setup check, not a host connection check.",
                        "Registered check code is not a security sandbox.",
                    ],
                }
            )
            return 0 if sdk_version == "1.26.0" else 2
        arguments = json.loads(args.arguments)
        if not isinstance(arguments, dict):
            raise ValueError("--arguments must be a JSON object")
        result = getattr(harness, args.tool)(**arguments)
        if args.tool == "run_check" and isinstance(result, dict) and result.get("job_id"):
            # A CLI invocation must stay alive while its in-process worker runs.
            # The stdio MCP adapter still returns the job id immediately.
            check = harness.get_record(arguments["check_id"])
            timeout = check.get("data", {}).get("manifest", {}).get("timeout_seconds", 30)
            deadline = time.monotonic() + float(timeout) + 5
            while True:
                job = harness.evidence_status(job_id=result["job_id"])
                if job.get("job_status") not in {"queued", "running"}:
                    result = job
                    break
                if time.monotonic() >= deadline:
                    raise HarnessError("CLI_WAIT_TIMEOUT", "Job did not finish within its deadline plus 5 seconds")
                time.sleep(0.1)
        dump(result)
        failed = isinstance(result, dict) and (
            result.get("status") == "error" or result.get("job_status") in {"failed", "interrupted"}
        )
        if isinstance(result, dict) and isinstance(result.get("run"), dict):
            observation = result["run"].get("data", {})
            failed = failed or observation.get("correctness_status") == "failed"
        return 1 if failed else 0
    except Exception as exc:
        from .core import HarnessError

        if isinstance(exc, HarnessError):
            dump({"status": "error", "error_code": exc.code, "message": exc.message})
        else:
            dump({"status": "error", "error_code": "CLI_ERROR", "message": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
