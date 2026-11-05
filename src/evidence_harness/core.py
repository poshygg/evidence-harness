"""Local evidence ledger. This module has no model or network client."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class HarnessError(Exception):
    """Expected input, state or execution error with a stable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _text(value: Any, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise HarnessError("INVALID_INPUT", f"{name} must be a non-empty string")
    return value


def _texts(value: Any, name: str, *, nonempty: bool = False) -> list[str]:
    if value is None:
        value = []
    if not isinstance(value, list) or len(value) > 100:
        raise HarnessError("INVALID_INPUT", f"{name} must be a list with at most 100 entries")
    result = [_text(item, name) for item in value]
    if nonempty and not result:
        raise HarnessError("INVALID_INPUT", f"{name} must not be empty")
    return result


def _choice(value: Any, name: str, choices: set[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise HarnessError("INVALID_INPUT", f"{name} must be one of {', '.join(sorted(choices))}")
    return value


def _canonical(payload: Any) -> str:
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise HarnessError("INVALID_INPUT", "Payload must contain finite JSON values") from exc
    if len(encoded.encode("utf-8")) > 1024 * 1024:
        raise HarnessError("RESOURCE_LIMIT", "A ledger record cannot exceed 1 MiB")
    return encoded


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Harness:
    """One configured workspace and its local SQLite ledger.

    The workspace is bound by the server launcher, not chosen by arbitrary tool
    calls. Local path checks do not constitute an operating-system sandbox.
    """

    def __init__(self, config_path: str | Path):
        self.config_path = Path(config_path).expanduser().resolve()
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise HarnessError("INVALID_INPUT", f"Cannot read workspace configuration: {exc}") from exc
        if not isinstance(config, dict) or config.get("schema_version") != "1.0":
            raise HarnessError("INVALID_INPUT", "Workspace configuration requires schema_version 1.0")
        root_value = _text(config.get("workspace_root"), "workspace_root")
        state_value = _text(config.get("state_dir"), "state_dir")
        root_path, state_path = Path(root_value).expanduser(), Path(state_value).expanduser()
        if not root_path.is_absolute() or not state_path.is_absolute():
            raise HarnessError("INVALID_INPUT", "workspace_root and state_dir must be absolute paths")
        self.root = root_path.resolve()
        self.state = state_path.resolve()
        if not self.root.is_dir():
            raise HarnessError("WORKSPACE_NOT_FOUND", "workspace_root must be an existing directory")
        if self.state == self.root or not self.state.is_relative_to(self.root):
            raise HarnessError("PATH_OUTSIDE_WORKSPACE", "state_dir must be a separate directory inside workspace_root")
        source_root = Path(__file__).resolve().parents[2]
        if self.state.is_relative_to(source_root) or source_root.is_relative_to(self.state):
            raise HarnessError("INVALID_INPUT", "Private state must not overlap the public source directory")
        self.state.mkdir(parents=True, exist_ok=True)
        if self.state.resolve() != self.state:
            raise HarnessError("PATH_OUTSIDE_WORKSPACE", "State directory changed during initialization")
        self.db_path = self.state / "ledger.sqlite3"
        if any(Path(str(self.db_path) + suffix).is_symlink() for suffix in ("", "-wal", "-shm")):
            raise HarnessError("PATH_OUTSIDE_WORKSPACE", "Ledger must not be a symbolic link")
        try:
            with self._connect() as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS metadata (
                        key TEXT PRIMARY KEY, value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS records (
                        record_id TEXT PRIMARY KEY,
                        request_id TEXT NOT NULL UNIQUE,
                        kind TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS records_kind ON records(kind);
                """)
                db.execute("BEGIN IMMEDIATE")
                stored_root = db.execute("SELECT value FROM metadata WHERE key='workspace_root'").fetchone()
                if stored_root and stored_root[0] != str(self.root):
                    raise HarnessError("INVALID_INPUT", "Ledger is already bound to another workspace root")
                db.execute("INSERT OR IGNORE INTO metadata VALUES ('workspace_root', ?)", (str(self.root),))
                db.execute("INSERT OR IGNORE INTO metadata VALUES ('workspace_id', ?)", (str(uuid.uuid4()),))
                self.workspace_id = db.execute("SELECT value FROM metadata WHERE key='workspace_id'").fetchone()[0]
        except sqlite3.Error as exc:
            raise HarnessError("IO_ERROR", f"Cannot initialize local ledger: {exc}") from exc

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # A new connection per operation permits runner threads to use the ledger.
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def resolve_path(self, path: str | Path, must_exist: bool = False) -> Path:
        if not isinstance(path, (str, Path)) or not str(path).strip():
            raise HarnessError("INVALID_INPUT", "A non-empty workspace path is required")
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            resolved = candidate.resolve(strict=must_exist)
        except (OSError, RuntimeError) as exc:
            raise HarnessError("INVALID_INPUT", f"Cannot resolve workspace path: {path}") from exc
        if not resolved.is_relative_to(self.root):
            raise HarnessError("PATH_OUTSIDE_WORKSPACE", f"Path leaves the configured workspace: {path}")
        return resolved

    def _record(self, row: sqlite3.Row, *, reused: bool = False) -> dict[str, Any]:
        return {
            "schema_version": "1.0", "workspace_id": self.workspace_id,
            "record_id": row["record_id"], "kind": row["kind"],
            "created_at": row["created_at"], "status": "recorded",
            "data": json.loads(row["payload_json"]), "limitations": [], "reused": reused,
        }

    def add_record(self, kind: str, request_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        _text(kind, "kind")
        if not re.fullmatch(r"[a-z][a-z_]{0,31}", kind):
            raise HarnessError("INVALID_INPUT", "Invalid record kind")
        _text(request_id, "request_id")
        if len(request_id) > 200:
            raise HarnessError("INVALID_INPUT", "request_id is too long")
        if not isinstance(payload, dict):
            raise HarnessError("INVALID_INPUT", "Record payload must be an object")
        encoded = _canonical(payload)
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                previous = db.execute("SELECT * FROM records WHERE request_id=?", (request_id,)).fetchone()
                if previous:
                    if previous["kind"] != kind or previous["payload_json"] != encoded:
                        raise HarnessError("IDEMPOTENCY_CONFLICT", "request_id is already associated with different input")
                    return self._record(previous, reused=True)
                record_id = f"{kind}_{uuid.uuid4()}"
                db.execute("INSERT INTO records VALUES (?, ?, ?, ?, ?)", (record_id, request_id, kind, encoded, _utc()))
                row = db.execute("SELECT * FROM records WHERE record_id=?", (record_id,)).fetchone()
                return self._record(row)
        except sqlite3.Error as exc:
            raise HarnessError("IO_ERROR", f"Cannot write evidence record: {exc}") from exc

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        """Look up a persisted request before revalidating mutable inputs."""
        _text(request_id, "request_id")
        if len(request_id) > 200:
            raise HarnessError("INVALID_INPUT", "request_id is too long")
        try:
            with self._connect() as db:
                row = db.execute("SELECT * FROM records WHERE request_id=?", (request_id,)).fetchone()
        except sqlite3.Error as exc:
            raise HarnessError("IO_ERROR", f"Cannot read request record: {exc}") from exc
        return None if row is None else self._record(row)

    def get_record(self, record_id: str) -> dict[str, Any]:
        _text(record_id, "record_id")
        try:
            with self._connect() as db:
                row = db.execute("SELECT * FROM records WHERE record_id=?", (record_id,)).fetchone()
        except sqlite3.Error as exc:
            raise HarnessError("IO_ERROR", f"Cannot read evidence record: {exc}") from exc
        if row is None:
            raise HarnessError("EVIDENCE_NOT_FOUND", f"Evidence record not found: {record_id}")
        return self._record(row)

    def records(self, kind: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise HarnessError("INVALID_INPUT", "limit must be between 1 and 1000")
        try:
            with self._connect() as db:
                if kind is None:
                    rows = db.execute("SELECT * FROM records ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
                else:
                    rows = db.execute("SELECT * FROM records WHERE kind=? ORDER BY rowid DESC LIMIT ?", (kind, limit)).fetchall()
        except sqlite3.Error as exc:
            raise HarnessError("IO_ERROR", f"Cannot list evidence records: {exc}") from exc
        return [self._record(row) for row in rows]

    def _references(self, ids: list[str] | None, name: str, kinds: set[str] | None = None) -> list[str]:
        result = _texts(ids, name)
        for record_id in result:
            record = self.get_record(record_id)
            if kinds is not None and record["kind"] not in kinds:
                raise HarnessError("INVALID_INPUT", f"{name} references an inappropriate record kind: {record['kind']}")
        return result

    def _supersedes(self, record_id: str | None, kind: str) -> str | None:
        if record_id is not None:
            self._references([record_id], "supersedes_id", {kind})
        return record_id

    def init_workspace(self, request_id: str, goal: str, constraints: list[dict[str, Any]] | None = None, time_budget_minutes: int = 180) -> dict[str, Any]:
        _text(goal, "goal")
        if type(time_budget_minutes) is not int or not 1 <= time_budget_minutes <= 1440:
            raise HarnessError("INVALID_INPUT", "time_budget_minutes must be between 1 and 1440")
        constraints = [] if constraints is None else constraints
        if not isinstance(constraints, list) or len(constraints) > 100:
            raise HarnessError("INVALID_INPUT", "constraints must be a list with at most 100 entries")
        for constraint in constraints:
            if not isinstance(constraint, dict):
                raise HarnessError("INVALID_INPUT", "Each constraint must be an object")
            _text(constraint.get("text"), "constraint.text")
            _choice(constraint.get("interpretation_status", "uncertain"), "interpretation_status", {"confirmed", "uncertain"})
            if not constraint.get("source") and constraint.get("interpretation_status") == "confirmed":
                raise HarnessError("INVALID_INPUT", "A confirmed constraint needs a stated source")
        return self.add_record("workspace", request_id, {
            "goal": goal, "constraints": constraints, "time_budget_minutes": time_budget_minutes,
            "workspace_root": str(self.root), "state_dir": str(self.state),
        })

    def record_requirement(self, request_id: str, title: str, acceptance_criteria: list[str], source_ref: str | None = None, source_excerpt: str | None = None, interpretation: str = "", interpretation_status: str = "uncertain", priority: str = "must", check_ids: list[str] | None = None, reported_by: str = "codex", supersedes_id: str | None = None) -> dict[str, Any]:
        _text(title, "title")
        _choice(interpretation_status, "interpretation_status", {"confirmed", "uncertain"})
        _choice(priority, "priority", {"must", "should", "optional"})
        _choice(reported_by, "reported_by", {"candidate", "codex"})
        if source_ref is not None:
            _text(source_ref, "source_ref")
            source_path = self.resolve_path(source_ref.split("#", 1)[0], must_exist=True)
            if not source_path.is_file():
                raise HarnessError("INVALID_INPUT", "source_ref must refer to a local file")
        elif interpretation_status == "confirmed":
            raise HarnessError("INVALID_INPUT", "A requirement without a source must remain uncertain")
        if source_excerpt is not None and not isinstance(source_excerpt, str):
            raise HarnessError("INVALID_INPUT", "source_excerpt must be text or null")
        if not isinstance(interpretation, str):
            raise HarnessError("INVALID_INPUT", "interpretation must be text")
        return self.add_record("requirement", request_id, {
            "title": title, "acceptance_criteria": _texts(acceptance_criteria, "acceptance_criteria", nonempty=True),
            "source_ref": source_ref, "source_excerpt": source_excerpt, "interpretation": interpretation,
            "interpretation_status": interpretation_status, "priority": priority,
            "check_ids": self._references(check_ids, "check_ids", {"check"}),
            "reported_by": reported_by, "supersedes_id": self._supersedes(supersedes_id, "requirement"),
            "verification_state": "unverified",
        })

    def record_decision(self, request_id: str, question: str, alternatives: list[dict[str, Any]], selected: str, reason: str, tradeoffs: list[str] | None = None, evidence_ids: list[str] | None = None, unverified_assumptions: list[str] | None = None, reported_by: str = "codex", supersedes_id: str | None = None) -> dict[str, Any]:
        _text(question, "question")
        _text(reason, "reason")
        _text(selected, "selected")
        _choice(reported_by, "reported_by", {"candidate", "codex"})
        if not isinstance(alternatives, list) or not 2 <= len(alternatives) <= 20:
            raise HarnessError("INVALID_INPUT", "alternatives needs between 2 and 20 objects")
        names = []
        for alternative in alternatives:
            if not isinstance(alternative, dict):
                raise HarnessError("INVALID_INPUT", "Every alternative must be an object")
            names.append(_text(alternative.get("name"), "alternative.name"))
        if selected not in names or len(names) != len(set(names)):
            raise HarnessError("INVALID_INPUT", "selected must identify one unique alternative")
        evidence = self._references(evidence_ids, "evidence_ids")
        return self.add_record("decision", request_id, {
            "question": question, "alternatives": alternatives, "selected": selected, "reason": reason,
            "tradeoffs": _texts(tradeoffs, "tradeoffs"), "evidence_ids": evidence,
            "unverified_assumptions": _texts(unverified_assumptions, "unverified_assumptions"),
            "reported_by": reported_by, "supersedes_id": self._supersedes(supersedes_id, "decision"),
            "evidence_state": "linked_unreviewed" if evidence else "unverified",
            "decision_status": "provisional" if not evidence else "evidence_linked",
        })

    def record_ai_assessment(self, request_id: str, claim: str, action: str, reason: str, conversation_ref: str | None = None, paraphrase: bool = True, evidence_ids: list[str] | None = None, reported_by: str = "codex", remaining_uncertainties: list[str] | None = None, supersedes_id: str | None = None) -> dict[str, Any]:
        _text(claim, "claim")
        _text(reason, "reason")
        _choice(action, "action", {"adopted", "modified", "rejected", "deferred"})
        _choice(reported_by, "reported_by", {"candidate", "codex"})
        if type(paraphrase) is not bool:
            raise HarnessError("INVALID_INPUT", "paraphrase must be boolean")
        if conversation_ref is None and not paraphrase:
            raise HarnessError("INVALID_INPUT", "An unreferenced statement must be marked as a paraphrase")
        if conversation_ref is not None:
            _text(conversation_ref, "conversation_ref")
        evidence = self._references(evidence_ids, "evidence_ids")
        return self.add_record("ai_assessment", request_id, {
            "claim": claim, "action": action, "reason": reason, "conversation_ref": conversation_ref,
            "paraphrase": paraphrase, "evidence_ids": evidence, "reported_by": reported_by,
            "remaining_uncertainties": _texts(remaining_uncertainties, "remaining_uncertainties"),
            "supersedes_id": self._supersedes(supersedes_id, "ai_assessment"),
            "evidence_state": "linked_unreviewed" if evidence else "unverified",
        })

    def register_check(self, request_id: str, manifest_path: str, requirement_ids: list[str] | None = None) -> dict[str, Any]:
        from . import runner
        requirements = self._references(requirement_ids, "requirement_ids", {"requirement"})
        payload = runner.load_manifest(self, manifest_path)
        payload["requirement_ids"] = requirements
        return self.add_record("check", request_id, payload)

    def run_check(self, request_id: str, check_id: str) -> dict[str, Any]:
        from . import runner
        return runner.start_check(self, request_id, check_id)

    def compare_runs(self, baseline_run_id: str, candidate_run_id: str, metric_names: list[str] | None = None, scope: str = "current") -> dict[str, Any]:
        from . import runner
        return runner.compare_runs(self, baseline_run_id, candidate_run_id, metric_names=metric_names, scope=scope)

    def evidence_status(self, max_items: int = 20, job_id: str | None = None) -> dict[str, Any]:
        if type(max_items) is not int or not 1 <= max_items <= 100:
            raise HarnessError("INVALID_INPUT", "max_items must be between 1 and 100")
        from . import runner
        if job_id is not None:
            return runner.status(self, job_id)
        recent = self.records(limit=max_items)
        requirements = self.records("requirement", limit=1000)
        checks = self.records("check", limit=1000)
        runs = self.records("run", limit=1000)
        superseded = {r["data"].get("supersedes_id") for r in requirements if r["data"].get("supersedes_id")}
        summary = []
        for requirement in requirements:
            if requirement["record_id"] in superseded:
                continue
            data = requirement["data"]
            linked_checks = set(data.get("check_ids", []))
            linked_checks.update(c["record_id"] for c in checks if requirement["record_id"] in c["data"].get("requirement_ids", []))
            linked_runs = [r for r in runs if r["data"].get("check_id") in linked_checks]
            evidence = []
            for run in linked_runs:
                evidence.append({"run_id": run["record_id"], "correctness_status": run["data"].get("correctness_status", "not_checked"), "freshness": runner.freshness(self, run["data"])})
            current_results: dict[str, dict[str, Any]] = {}
            for run, observation in zip(linked_runs, evidence):
                if observation["freshness"] == "current":
                    current_results.setdefault(run["data"].get("check_id"), observation)
            verified = bool(linked_checks) and all(current_results.get(check_id, {}).get("correctness_status") == "passed" for check_id in linked_checks)
            state = "checks_passed" if verified else "unverified"
            if any(e["correctness_status"] == "failed" for e in current_results.values()):
                state = "failed"
            summary.append({"requirement_id": requirement["record_id"], "title": data["title"], "state": state, "interpretation_status": data.get("interpretation_status", "uncertain"), "check_ids": sorted(linked_checks), "runs": evidence, "limitation": "Passing linked checks does not establish that every acceptance criterion is covered."})
        return {
            "schema_version": "1.0", "workspace_id": self.workspace_id,
            "status": "observed", "workspace_root": str(self.root), "state_dir": str(self.state),
            "requirements": summary[:max_items], "recent_records": recent,
            "counts": {"requirements_loaded": len(requirements), "checks_loaded": len(checks), "runs_loaded": len(runs)},
            "limitations": ["Lists are bounded; linked evidence does not verify the truth of natural-language claims or candidate understanding.", "No platform submission status has been checked."],
        }

    def export_review_packet(self, request_id: str, output_path: str, max_items: int = 30) -> dict[str, Any]:
        _text(request_id, "request_id")
        if type(max_items) is not int or not 1 <= max_items <= 100:
            raise HarnessError("INVALID_INPUT", "max_items must be between 1 and 100")
        target = self.resolve_path(output_path)
        source_root = Path(__file__).resolve().parents[2]
        if target.is_relative_to(source_root):
            raise HarnessError("INVALID_INPUT", "Private review output cannot be written into public source")
        payload = {"output_path": str(target), "max_items": max_items}
        # Check an existing request before considering overwrites or recomputing a
        # report from later records. Exports are stable for a given request_id.
        with self._connect() as db:
            previous = db.execute("SELECT * FROM records WHERE request_id=?", (request_id,)).fetchone()
        if previous:
            if previous["kind"] != "export" or json.loads(previous["payload_json"]).get("input") != payload:
                raise HarnessError("IDEMPOTENCY_CONFLICT", "request_id is already associated with different input")
            old = self._record(previous, reused=True)
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != old["data"]["sha256"]:
                raise HarnessError("IO_ERROR", "The previously exported report is missing or changed")
            return old
        records = self.records(limit=max_items)
        status = self.evidence_status(max_items=max_items)
        lines = ["# Local evidence review", "", "This report contains stored facts and reported decisions. It is not an answer to a restricted assessment.", "", "## Requirement checks", "", json.dumps(status["requirements"], ensure_ascii=False, indent=2), "", "## Recorded events (newest first)", ""]
        for record in records:
            lines.extend([f"### {record['kind']} / {record['record_id']}", "", f"Recorded UTC: {record['created_at']}", "", "```json", json.dumps(record["data"], ensure_ascii=False, indent=2), "```", ""])
        lines.extend(["## Limitations", "", "- Stored text is reported data, not independently verified truth.", "- Refer to original receipts, logs and snapshots for execution evidence.", "- This report does not confirm platform submission or permit AI/document use during restricted stages.", ""])
        content = "\n".join(lines).encode("utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.resolve_path(target.parent, must_exist=True)
        try:
            # Exclusive creation avoids silently replacing any existing document.
            with target.open("xb") as stream:
                stream.write(content)
        except FileExistsError as exc:
            raise HarnessError("IO_ERROR", "Review output already exists; use a new output path") from exc
        except OSError as exc:
            raise HarnessError("IO_ERROR", f"Cannot export local review: {exc}") from exc
        return self.add_record("export", request_id, {"input": payload, "output_path": str(target), "sha256": hashlib.sha256(content).hexdigest(), "record_ids": [r["record_id"] for r in records]})
