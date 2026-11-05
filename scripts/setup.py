"""Install this server in its own venv and emit connection settings.

Does not install/update Codex, alter host settings, or contact external AI.
Dependency installation uses the Python package index. Runtime data stays in
the specified workspace. Run only where local tool installation is permitted.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import venv
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


class SetupError(Exception):
    pass


def overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def save_once(path: Path, content: str) -> None:
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise SetupError(f"Existing file differs; it was not overwritten: {path}")
        return
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


def run(argv: list[str], cwd: Path | None = None) -> None:
    print("[setup] " + " ".join(argv), file=sys.stderr, flush=True)
    try:
        subprocess.run(argv, cwd=cwd, check=True, shell=False)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise SetupError(f"Command failed: {argv[0]} ({exc})") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, help="Existing task workspace directory")
    parser.add_argument("--skip-install", action="store_true", help="Use an already installed repo venv")
    args = parser.parse_args(argv)
    try:
        if sys.version_info < (3, 11):
            raise SetupError("Python 3.11 or newer is required; do not change the AI host version.")
        workspace = Path(args.workspace).expanduser().resolve(strict=True)
        if not workspace.is_dir():
            raise SetupError("--workspace must be an existing directory")
        state = (workspace / ".evidence-harness").resolve()
        if not state.is_relative_to(workspace) or overlaps(state, REPO):
            raise SetupError("State must stay inside the task workspace and outside the public repository")
        state.mkdir(parents=True, exist_ok=True)
        config = state / "workspace.json"
        config_value = {
            "schema_version": "1.0",
            "workspace_root": str(workspace),
            "state_dir": str(state),
        }
        config_text = json.dumps(config_value, ensure_ascii=False, indent=2) + "\n"
        if config.exists():
            existing = json.loads(config.read_text(encoding="utf-8-sig"))
            if existing != config_value:
                raise SetupError(f"Workspace config differs and was not overwritten: {config}")
        else:
            save_once(config, config_text)

        environment = REPO / ".venv"
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not python.exists():
            if args.skip_install:
                raise SetupError("--skip-install requires an existing, installed .venv")
            print("[setup] Creating isolated .venv", file=sys.stderr, flush=True)
            venv.EnvBuilder(with_pip=True).create(environment)
        if not args.skip_install:
            pip_probe = subprocess.run(
                [str(python), "-c", "import importlib.util; raise SystemExit(0 if importlib.util.find_spec('pip') else 1)"],
                check=False,
                shell=False,
            )
            if pip_probe.returncode:
                # Some uv-created environments omit pip. Bootstrap it only here.
                run([str(python), "-m", "ensurepip"])
            requirements = REPO / "requirements.lock"
            if not requirements.exists():
                requirements = REPO / "requirements.in"
            run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(requirements)])
            run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-deps", "-e", str(REPO)])

        server_args = ["-m", "evidence_harness", "serve", "--workspace", str(config)]
        # On POSIX, resolving the venv's python symlink would point at the base
        # interpreter and lose this environment's installed packages.
        server_entry = {"command": str(python.absolute()), "args": server_args}
        mcp_settings = {"mcpServers": {"evidence_harness": server_entry}}
        settings_json = json.dumps(mcp_settings, ensure_ascii=False, indent=2) + "\n"
        settings_path = state / "mcp-servers.json"
        save_once(settings_path, settings_json)
        toml_path = state / "codex-config.toml"
        toml_text = (
            "# Add only this entry if the host permits it; preserve existing settings.\n"
            "[mcp_servers.evidence_harness]\n"
            f"command = {json.dumps(server_entry['command'], ensure_ascii=False)}\n"
            f"args = {json.dumps(server_args, ensure_ascii=False)}\n"
            "startup_timeout_sec = 10\n"
            "tool_timeout_sec = 60\n"
        )
        save_once(toml_path, toml_text)
        run([str(python), "-m", "evidence_harness", "doctor", "--workspace", str(config)])
        run([str(python), str(REPO / "scripts" / "protocol_smoke.py"), "--workspace", str(config)])
        print("\nSetup and local MCP initialize/list/call checks passed.")
        print("Workspace config:", config)
        print("MCP JSON:", settings_path)
        print("Codex TOML snippet:", toml_path)
        print("Use the target platform's MCP settings; this script did not edit them.")
        print("The mcpServers wrapper is a candidate format; preserve the platform's documented shape.")
        print(settings_json, end="")
        return 0
    except (SetupError, OSError, ValueError) as exc:
        print(f"SETUP_ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
