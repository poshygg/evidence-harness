from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import tempfile

from evidence_harness.core import Harness, HarnessError
from tests.helpers import WorkspaceCase


class CoreTests(WorkspaceCase):
    def test_identical_request_is_reused_and_changed_payload_is_rejected(self):
        args = dict(request_id="req-1", title="Preserve first occurrence", acceptance_criteria=["[2,1,2] -> [2,1]"], reported_by="codex")
        first = self.harness.record_requirement(**args)
        second = self.harness.record_requirement(**args)
        self.assertEqual(first["record_id"], second["record_id"])
        self.assertTrue(second["reused"])
        with self.assertRaises(HarnessError) as caught:
            self.harness.record_requirement(**{**args, "title": "Different requirement"})
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_path_escape_and_symlink_escape_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="harness-outside-") as outside:
            secret = Path(outside) / "outside.txt"
            secret.write_text("Synthetic outside data", encoding="utf-8")
            with self.assertRaises(HarnessError) as caught:
                self.harness.resolve_path(str(secret), must_exist=True)
            self.assertEqual(caught.exception.code, "PATH_OUTSIDE_WORKSPACE")
            link = self.root / "outside-link"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("This OS/user cannot create symlinks; direct escape was checked.")
            with self.assertRaises(HarnessError) as caught:
                self.harness.resolve_path(str(link / "outside.txt"), must_exist=True)
            self.assertEqual(caught.exception.code, "PATH_OUTSIDE_WORKSPACE")

    def test_state_directory_must_remain_in_workspace(self):
        outside = self.root.parent / (self.root.name + "-outside-state")
        config = self.root / "outside-config.json"
        self.write_json(config, {"schema_version": "1.0", "workspace_root": str(self.root), "state_dir": str(outside)})
        with self.assertRaises(HarnessError) as caught:
            Harness(config)
        self.assertEqual(caught.exception.code, "PATH_OUTSIDE_WORKSPACE")
        self.assertFalse(outside.exists())

    def test_requirements_need_acceptance_criteria(self):
        with self.assertRaises(HarnessError):
            self.harness.record_requirement("empty", "No success criteria", [])

    def test_concurrent_retries_create_one_record(self):
        def write(_):
            return self.harness.record_requirement("concurrent", "Retry case", ["One stored record"])
        with ThreadPoolExecutor(max_workers=4) as pool:
            records = list(pool.map(write, range(8)))
        self.assertEqual(len({item["record_id"] for item in records}), 1)
        self.assertEqual(sum(not item["reused"] for item in records), 1)

    def test_source_and_evidence_references_are_checked(self):
        with self.assertRaises(HarnessError):
            self.harness.record_requirement("bad-source", "Synthetic", ["accept"], source_ref="missing.md")
        with self.assertRaises(HarnessError) as caught:
            self.harness.record_ai_assessment("bad-ref", "Claim", "deferred", "Unverified", evidence_ids=["missing-record"])
        self.assertEqual(caught.exception.code, "EVIDENCE_NOT_FOUND")

    def test_ai_assessment_keeps_reporter_and_unverified_state(self):
        record = self.harness.record_ai_assessment(
            "ai-1", "Set lookup could help", "deferred", "No measurement exists",
            reported_by="codex", evidence_ids=[], remaining_uncertainties=["Input scale"]
        )
        self.assertEqual(record["data"]["reported_by"], "codex")
        self.assertEqual(record["data"]["evidence_state"], "unverified")
        self.assertNotIn("candidate_confirmed", record["data"])

    def test_unselected_alternative_is_rejected(self):
        with self.assertRaises(HarnessError):
            self.harness.record_decision(
                "bad-decision", "Choose approach",
                [{"name": "A"}, {"name": "B"}], "C", "Not a listed option"
            )

    def test_decision_without_execution_evidence_is_provisional(self):
        record = self.harness.record_decision(
            "provisional", "Select a starting point", [{"name": "Simple"}, {"name": "Complex"}],
            "Simple", "Implementation time is limited", evidence_ids=[], reported_by="codex"
        )
        self.assertEqual(record["data"]["decision_status"], "provisional")
        self.assertEqual(record["data"]["evidence_state"], "unverified")

    def test_correction_preserves_original_and_reports_author(self):
        original = self.harness.record_requirement("original", "Original", ["One case"], reported_by="codex")
        revised = self.harness.record_requirement("revised", "Revised", ["Two cases"], reported_by="candidate", supersedes_id=original["record_id"])
        old = self.harness.get_record(original["record_id"])
        self.assertEqual(old["data"]["title"], "Original")
        self.assertEqual(revised["data"]["supersedes_id"], original["record_id"])
        self.assertEqual(revised["data"]["reported_by"], "candidate")

    def test_review_export_is_local_and_does_not_overwrite_a_different_request(self):
        self.harness.record_requirement("review-req", "Synthetic review requirement", ["Case passes"])
        target = self.state / "review.md"
        first = self.harness.export_review_packet("export-1", str(target))
        second = self.harness.export_review_packet("export-1", str(target))
        self.assertEqual(first["record_id"], second["record_id"])
        self.assertIn("Synthetic review requirement", target.read_text(encoding="utf-8"))
        with self.assertRaises(HarnessError):
            self.harness.export_review_packet("export-2", str(target))
