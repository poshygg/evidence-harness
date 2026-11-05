import hashlib
import json
from pathlib import Path
import tempfile
import time
import sys

from evidence_harness.core import HarnessError
from evidence_harness import runner
from tests.helpers import WorkspaceCase


class RunnerTests(WorkspaceCase):
    def test_registration_validates_timeout_and_repeat_limits(self):
        # Create one valid manifest, then submit independent invalid versions.
        _, manifest_path, manifest = self.check()
        for field, bad_value in [("timeout_seconds", 0), ("repeats", 0), ("repeats", 11), ("warmups", -1)]:
            with self.subTest(field=field, value=bad_value):
                changed = {**manifest, field: bad_value}
                self.write_json(manifest_path, changed)
                with self.assertRaises(HarnessError):
                    self.harness.register_check("bad-" + field + str(bad_value), str(manifest_path))

    def test_registration_rejects_workspace_escape(self):
        _, manifest_path, manifest = self.check()
        with tempfile.TemporaryDirectory(prefix="harness-outside-source-") as outside:
            outside_file = Path(outside) / "outside.py"
            outside_file.write_text("VALUE = 0\n", encoding="utf-8")
            manifest["tracked_source_paths"] = [str(outside_file)]
            self.write_json(manifest_path, manifest)
            with self.assertRaises(HarnessError) as caught:
                self.harness.register_check("escape", str(manifest_path))
            self.assertEqual(caught.exception.code, "PATH_OUTSIDE_WORKSPACE")

    def test_manifest_change_is_rejected_before_execution(self):
        record, manifest_path, manifest = self.check()
        manifest["repeats"] = 2
        self.write_json(manifest_path, manifest)
        with self.assertRaises(HarnessError) as caught:
            self.harness.run_check("changed", record["record_id"])
        self.assertEqual(caught.exception.code, "MANIFEST_CHANGED")

    def test_retry_of_existing_job_ignores_later_manifest_edits(self):
        record, manifest_path, manifest = self.check()
        job = self.harness.run_check("original-job", record["record_id"])
        settled = self.wait_job(job)
        self.assertEqual(settled["job_status"], "completed", settled)
        manifest["repeats"] = 2
        self.write_json(manifest_path, manifest)
        retry = self.harness.run_check("original-job", record["record_id"])
        self.assertTrue(retry["reused"])
        self.assertEqual(retry["job_id"], job["job_id"])
        self.assertEqual(runner.status(self.harness, retry["job_id"])["run_id"], settled["run_id"])
        self.assertEqual(len(self.harness.records(kind="run")), 1)

    def test_large_tracked_file_is_rejected_at_registration(self):
        large = self.root / "application" / "large.bin"
        with large.open("wb") as stream:
            stream.seek(33 * 1024 * 1024 - 1)
            stream.write(b"\0")
        with self.assertRaises(HarnessError) as caught:
            self.check(name="large-input", tracked_source_paths=["application/large.bin"])
        self.assertEqual(caught.exception.code, "RESOURCE_LIMIT")

    def test_existing_unowned_output_file_is_preserved(self):
        protected = self.root / "README.md"
        original = "Existing user-authored content must survive.\n"
        protected.write_text(original, encoding="utf-8")
        record, _, _ = self.check(result_artifact="README.md")
        try:
            job = self.harness.run_check("protected-output", record["record_id"])
        except HarnessError as error:
            self.assertEqual(error.code, "OUTPUT_EXISTS")
        else:
            settled = self.wait_job(job)
            self.assertEqual(settled["job_status"], "failed", settled)
            self.assertEqual(settled["error"]["error_code"], "OUTPUT_EXISTS")
        self.assertEqual(protected.read_text(encoding="utf-8"), original)

    def test_actual_run_records_samples_and_reuses_job(self):
        record, _, _ = self.check(warmups=1, repeats=3)
        job = self.harness.run_check("actual", record["record_id"])
        repeated = self.harness.run_check("actual", record["record_id"])
        self.assertEqual(job["job_id"], repeated["job_id"])
        status = self.wait_job(job)
        self.assertEqual(status["job_status"], "completed", status)
        payload = status["run"]["data"]
        self.assertEqual(payload["execution_status"], "completed")
        self.assertEqual(payload["correctness_status"], "passed")
        observed = payload["observations"]
        latency = next(m for m in observed["metrics"] if m["name"] == "wall_time_ms")
        self.assertEqual(len(latency["samples"]), 3)
        self.assertTrue(all(v >= 0 for v in latency["samples"]))
        self.assertEqual(latency["measurement_scope"], "whole_subprocess")
        self.assertEqual(runner.freshness(self.harness, payload), "current")
        snapshot = self.harness.resolve_path(observed["snapshot_dir"], must_exist=True)
        stored = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
        entries = {item["path"]: item for item in stored["files"]}
        self.assertIn("application/source.py", entries)
        self.assertIn("checks/fixture.json", entries)
        for relative in ["application/source.py", "checks/fixture.json"]:
            entry = entries[relative]
            blob = self.harness.resolve_path(entry["blob"], must_exist=True)
            self.assertEqual(blob.read_bytes(), (self.root / relative).read_bytes())
            self.assertEqual(hashlib.sha256(blob.read_bytes()).hexdigest(), entry["sha256"])

    def test_exit_zero_without_result_is_not_correctness_pass(self):
        record, _, _ = self.check(script="print('completed process but no correctness evidence')\n")
        status = self.execute_check(record)
        self.assertEqual(status["job_status"], "failed", status)
        self.assertNotEqual(status["run"]["data"]["correctness_status"], "passed")

    def test_old_result_file_cannot_make_new_execution_pass(self):
        self.write_json(self.root / "checks" / "result.json", {"correctness": {"status": "passed", "checks": 2}, "metrics": {}})
        record, _, _ = self.check(script="print('no new artifact')\n")
        status = self.execute_check(record)
        self.assertNotEqual(status["run"]["data"]["correctness_status"], "passed")

    def test_reported_failure_is_preserved(self):
        script = "import json\nfrom pathlib import Path\nPath('checks/result.json').write_text(json.dumps({'correctness': {'status': 'failed', 'checks': 1}, 'metrics': {}}))\n"
        record, _, _ = self.check(script=script)
        status = self.execute_check(record)
        self.assertEqual(status["run"]["data"]["correctness_status"], "failed", status)

    def test_source_changes_mark_evidence_stale(self):
        record, _, _ = self.check()
        status = self.execute_check(record)
        (self.root / "application" / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.assertEqual(runner.freshness(self.harness, status["run"]["data"]), "stale")

    def test_same_conditions_compare_but_fixture_change_blocks_comparison(self):
        # The same check implementation and same fixture are used across variants.
        first, first_path, base_manifest = self.check(name="base", repeats=2)
        base_status = self.execute_check(first, "run-base")
        candidate_manifest = {**base_manifest, "task_id": "candidate", "variant_id": "candidate"}
        candidate_path = self.root / "checks" / "candidate.manifest.json"
        self.write_json(candidate_path, candidate_manifest)
        candidate = self.harness.register_check("register-candidate", str(candidate_path))
        candidate_status = self.execute_check(candidate, "run-candidate")
        comparison = self.harness.compare_runs(base_status["run_id"], candidate_status["run_id"])
        data = comparison.get("data", comparison)
        self.assertTrue(data["comparable"], comparison)
        self.write_json(self.root / "checks" / "fixture-other.json", [[5, 5]])
        changed = {**candidate_manifest, "task_id": "other-fixture", "fixture_paths": ["checks/fixture-other.json"]}
        other_path = self.root / "checks" / "other.manifest.json"
        self.write_json(other_path, changed)
        other_check = self.harness.register_check("register-other", str(other_path))
        other_status = self.execute_check(other_check, "run-other")
        try:
            comparison = self.harness.compare_runs(base_status["run_id"], other_status["run_id"])
        except HarnessError as error:
            self.assertEqual(error.code, "COMPARISON_BLOCKED")
        else:
            self.assertFalse(comparison.get("data", comparison)["comparable"])

    def test_current_comparison_rejects_stale_but_historical_can_use_snapshots(self):
        record, _, _ = self.check()
        first = self.execute_check(record, "history-1")
        second = self.execute_check(record, "history-2")
        (self.root / "application" / "source.py").write_text("VALUE = 3\n", encoding="utf-8")
        try:
            comparison = self.harness.compare_runs(first["run_id"], second["run_id"])
        except HarnessError as error:
            self.assertEqual(error.code, "COMPARISON_BLOCKED")
        else:
            self.assertFalse(comparison.get("data", comparison)["comparable"])
        historical = self.harness.compare_runs(first["run_id"], second["run_id"], scope="historical")
        self.assertTrue(historical.get("data", historical)["comparable"])

    def _run_argument_variants(self, candidate_size):
        shared = str(self.root / "checks" / "base-arguments.py")
        base, _, manifest = self.check(
            name="base-arguments", variant_id="baseline", repeats=2,
            argv=[sys.executable, shared, "--size", "1000", "--variant", "baseline"]
        )
        baseline = self.execute_check(base, "argument-base")
        self.assertEqual(baseline["job_status"], "completed", baseline)
        manifest = {
            **manifest, "task_id": "candidate-arguments", "variant_id": "candidate",
            "argv": [sys.executable, shared, "--size", candidate_size, "--variant", "candidate"]
        }
        candidate_path = self.root / "checks" / "candidate-arguments.manifest.json"
        self.write_json(candidate_path, manifest)
        check = self.harness.register_check("register-candidate-arguments", str(candidate_path))
        candidate = self.execute_check(check, "argument-candidate")
        self.assertEqual(candidate["job_status"], "completed", candidate)
        return baseline["run_id"], candidate["run_id"]

    def test_different_nonvariant_arguments_block_comparison(self):
        baseline_id, candidate_id = self._run_argument_variants("10")
        with self.assertRaises(HarnessError) as caught:
            self.harness.compare_runs(baseline_id, candidate_id)
        self.assertEqual(caught.exception.code, "COMPARISON_BLOCKED")

    def test_variant_argument_alone_can_be_normalized_for_comparison(self):
        baseline_id, candidate_id = self._run_argument_variants("1000")
        comparison = self.harness.compare_runs(baseline_id, candidate_id)
        self.assertTrue(comparison.get("data", comparison)["comparable"])

    def test_timeout_stops_parent_and_child_processes(self):
        script = (
            "import subprocess, sys, time\n"
            "child = \"import time; from pathlib import Path; time.sleep(4); Path('checks/child-survived.txt').write_text('alive')\"\n"
            "subprocess.Popen([sys.executable, '-c', child])\n"
            "time.sleep(30)\n"
        )
        record, _, _ = self.check(script=script, timeout_seconds=1)
        started = time.monotonic()
        status = self.execute_check(record)
        self.assertEqual(status["run"]["data"]["execution_status"], "timeout", status)
        self.assertLess(time.monotonic() - started, 10)
        time.sleep(max(0, 4.5 - (time.monotonic() - started)))
        self.assertFalse((self.root / "checks" / "child-survived.txt").exists(), "Child survived the job timeout.")

    def test_stdout_limit_is_reported_without_unbounded_capture(self):
        script = (
            "import json\nfrom pathlib import Path\n"
            "Path('checks/result.json').write_text(json.dumps({'correctness': {'status': 'passed', 'checks': 1}, 'metrics': {}}))\n"
            "print('x' * (2 * 1024 * 1024))\n"
        )
        record, _, _ = self.check(script=script)
        status = self.execute_check(record)
        logs = status["run"]["data"]["observations"]["logs"]
        self.assertTrue(logs["truncated"])
        self.assertLessEqual(logs["captured_bytes"], logs["limit_bytes"])
