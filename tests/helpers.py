from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from evidence_harness.core import Harness


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="evidence-harness-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.state = self.root / ".evidence-harness"
        self.state.mkdir()
        self.config = self.state / "workspace.json"
        self.write_json(self.config, {
            "schema_version": "1.0", "workspace_root": str(self.root), "state_dir": str(self.state)
        })
        self.harness = Harness(self.config)
        self.harness.init_workspace("init", "Synthetic behavioral tests", [], 180)
        (self.root / "checks").mkdir()
        (self.root / "application").mkdir()
        (self.root / "application" / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.write_json(self.root / "checks" / "fixture.json", [[], [2, 1, 2]])

    @staticmethod
    def write_json(path, value):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def check(self, name="check", *, script=None, fixture="checks/fixture.json", **changes):
        script_path = self.root / "checks" / (name + ".py")
        if script is None:
            script = (
                "import json\nfrom pathlib import Path\n"
                "Path('checks/result.json').write_text(json.dumps("
                "{'correctness': {'status': 'passed', 'checks': 2}, 'metrics': {}}))\n"
            )
        script_path.write_text(script, encoding="utf-8")
        manifest = {
            "schema_version": "1.0", "task_id": name, "kind": "correctness",
            "argv": [sys.executable, str(script_path)], "cwd": ".", "env_allowlist": [],
            "timeout_seconds": 10, "tracked_source_paths": ["application/source.py"],
            "fixture_paths": [fixture], "check_paths": [str(script_path.relative_to(self.root))],
            "lockfile_paths": [], "variant_id": name, "seed": 42,
            "warmups": 0, "repeats": 1,
            "oracle_description": "Synthetic script checks two hand-authored cases.",
            "result_artifact": "checks/result.json", "metric_definitions": [],
            "test_authored_by": "codex", "limitations": ["Synthetic test only."]
        }
        manifest.update(changes)
        manifest_path = self.root / "checks" / (name + ".manifest.json")
        self.write_json(manifest_path, manifest)
        record = self.harness.register_check("register-" + name, str(manifest_path))
        return record, manifest_path, manifest

    def wait_job(self, job, timeout=20):
        from evidence_harness import runner
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = runner.status(self.harness, job["job_id"])
            if result["job_status"] in {"completed", "failed", "interrupted"}:
                return result
            time.sleep(0.025)
        self.fail("Synthetic job did not settle within its test deadline: " + str(job))

    def execute_check(self, record, request_id="execute"):
        job = self.harness.run_check(request_id, record["record_id"])
        return self.wait_job(job)
