"""Prepare and execute an unrelated synthetic end-to-end example locally."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time

from evidence_harness.core import Harness, HarnessError
from evidence_harness import runner


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def prepare(root):
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("Use a new or empty synthetic workspace; existing files are never replaced.")
    source = Path(__file__).resolve().parent
    app = root / "synthetic-example"
    app.mkdir()
    for name in ["baseline.py", "candidate.py", "check.py", "fixture.json"]:
        shutil.copyfile(source / name, app / name)
    config = root / ".evidence-harness" / "workspace.json"
    write_json(config, {"schema_version": "1.0", "workspace_root": str(root), "state_dir": str(config.parent)})
    paths = {}
    for variant in ["baseline", "candidate"]:
        manifest = {
            "schema_version": "1.0", "task_id": "synthetic-" + variant, "kind": "benchmark",
            "argv": [sys.executable, "synthetic-example/check.py", "--variant", variant,
                     "--fixture", "synthetic-example/fixture.json", "--result", "synthetic-example/result.json"],
            "cwd": ".", "env_allowlist": [], "timeout_seconds": 30,
            "tracked_source_paths": ["synthetic-example/" + variant + ".py"],
            "fixture_paths": ["synthetic-example/fixture.json"],
            "check_paths": ["synthetic-example/check.py"], "lockfile_paths": [],
            "variant_id": variant, "seed": 42, "warmups": 1, "repeats": 5,
            "oracle_description": "Seven hand-authored integer-list cases with exact expected outputs.",
            "result_artifact": "synthetic-example/result.json", "metric_definitions": [],
            "test_authored_by": "codex",
            "limitations": [
                "Synthetic examples only; no real task data.",
                "Whole-process time includes startup and imports; this is not isolated algorithm latency.",
                "Tiny hand-authored cases do not establish scaling or worst-case performance."
            ]
        }
        paths[variant] = app / (variant + ".manifest.json")
        write_json(paths[variant], manifest)
    return config, paths


def settle(harness, job):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        status = runner.status(harness, job["job_id"])
        if status["job_status"] in {"completed", "failed", "interrupted"}:
            if status["job_status"] != "completed":
                raise RuntimeError("Synthetic job failed: " + json.dumps(status))
            return status
        time.sleep(0.05)
    raise TimeoutError("Synthetic job did not settle.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", help="New or empty directory; defaults to a new temporary directory.")
    args = parser.parse_args()
    root = Path(args.workspace).expanduser().resolve() if args.workspace else Path(tempfile.mkdtemp(prefix="evidence-synthetic-demo-")).resolve()
    config, paths = prepare(root)
    harness = Harness(config)
    harness.init_workspace("demo-init", "Demonstrate traceable checks with unrelated synthetic data", [], 180)
    requirement = harness.record_requirement(
        "demo-requirement", "Remove duplicate integers while preserving their first occurrence",
        ["Each of seven hand-authored cases matches its expected output"],
        source_ref="synthetic-example/fixture.json", interpretation="Synthetic example contract", interpretation_status="confirmed",
        reported_by="codex"
    )
    results = {}
    for variant in ["baseline", "candidate"]:
        check = harness.register_check("register-" + variant, str(paths[variant]), [requirement["record_id"]])
        results[variant] = settle(harness, harness.run_check("run-" + variant, check["record_id"]))
    comparison = harness.compare_runs(results["baseline"]["run_id"], results["candidate"]["run_id"])
    harness.record_ai_assessment(
        "demo-ai", "The alternative may improve lookup cost", "deferred",
        "The measured scope includes process startup and tiny inputs; broad performance claims remain unverified.",
        evidence_ids=[results["baseline"]["run_id"], results["candidate"]["run_id"]], reported_by="codex",
        remaining_uncertainties=["Scaling", "Algorithm-only latency", "Memory"]
    )
    review = harness.export_review_packet("demo-review", str(root / ".evidence-harness" / "review.md"))
    print(json.dumps({"workspace": str(root), "config": str(config), "comparison": comparison, "review": review}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (HarnessError, ValueError, RuntimeError, TimeoutError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
