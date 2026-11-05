"""Local, bounded check execution. Registered programs are trusted, not sandboxed."""
from __future__ import annotations

import atexit
import hashlib
import json
import math
import os
import platform
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .core import HarnessError

LOG_LIMIT = 1024 * 1024
SNAPSHOT_LIMIT = 32 * 1024 * 1024
STORE_LIMIT = 256 * 1024 * 1024
_ACTIVE: dict[int, tuple[subprocess.Popen, object | None]] = {}
_ACTIVE_LOCK = threading.Lock()
_JSON_LOCK = threading.RLock()
_SHELLS = {"sh", "bash", "zsh", "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"}


def _error(code, message):
    raise HarnessError(code, message)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _sha(path, deadline=None):
    result = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            if deadline is not None and time.monotonic() >= deadline:
                _error("TIMEOUT", "Hash collection exceeded the job deadline.")
            size += len(chunk)
            if size > SNAPSHOT_LIMIT:
                _error("RESOURCE_LIMIT", "A tracked file exceeds 32 MiB.")
            result.update(chunk)
    return result.hexdigest()


def _write(path, value):
    with _JSON_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            # Windows readers in another process can briefly hold the old file.
            for attempt in range(8):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError:
                    if attempt == 7:
                        raise
                    time.sleep(0.01 * (attempt + 1))
        finally:
            if temporary.exists():
                temporary.unlink()


def _pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        code = wintypes.DWORD()
        try:
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _files(harness, paths, deadline=None):
    files = {}
    total_size = 0
    enumerated = 0
    for relative in paths:
        path = harness.resolve_path(relative, must_exist=True)
        candidates = path.rglob("*") if path.is_dir() else [path]
        for item in candidates:
            enumerated += 1
            if enumerated > 8192:
                _error("RESOURCE_LIMIT", "Too many input directory entries; narrow tracked paths.")
            if deadline is not None and time.monotonic() >= deadline:
                _error("TIMEOUT", "Input enumeration exceeded the job deadline.")
            if item.is_symlink():
                _error("PATH_OUTSIDE_WORKSPACE", "Tracked symlinks are not accepted.")
            if not item.is_file():
                continue
            resolved = harness.resolve_path(str(item), must_exist=True)
            if resolved == harness.state or harness.state in resolved.parents:
                _error("INVALID_INPUT", "Do not track generated state as an input.")
            if any(part in {".git", ".venv", "node_modules", "__pycache__"} for part in resolved.relative_to(harness.root).parts):
                _error("INVALID_INPUT", "Track source and fixtures, not dependencies or caches.")
            key = resolved.relative_to(harness.root).as_posix()
            if key not in files:
                total_size += resolved.stat().st_size
                if total_size > SNAPSHOT_LIMIT or len(files) >= 2048:
                    _error("RESOURCE_LIMIT", "Tracked inputs exceed 32 MiB or 2048 files.")
                files[key] = resolved
    return files


def _fingerprints(harness, manifest, deadline=None):
    if deadline is None:
        deadline = time.monotonic() + 5
    groups = {}
    combined = {}
    for name, key in [("source", "tracked_source_paths"), ("check", "check_paths"), ("fixture", "fixture_paths"), ("lockfile", "lockfile_paths")]:
        group = _files(harness, manifest[key], deadline)
        entries = {name: _sha(path, deadline) for name, path in group.items()}
        groups[name] = _digest(entries)
        combined.update(group)
    return groups, combined


def load_manifest(harness, manifest_path):
    path = harness.resolve_path(manifest_path, must_exist=True)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        _error("INVALID_INPUT", f"Cannot read manifest: {exc}")
    if not isinstance(manifest, dict):
        _error("INVALID_INPUT", "Manifest must be an object.")
    required = {"task_id", "kind", "argv", "cwd", "env_allowlist", "timeout_seconds", "tracked_source_paths", "fixture_paths", "check_paths", "lockfile_paths", "variant_id", "seed", "warmups", "repeats", "oracle_description", "result_artifact", "metric_definitions"}
    if required - manifest.keys():
        _error("INVALID_INPUT", "Missing manifest fields: " + ", ".join(sorted(required - manifest.keys())))
    if manifest.get("schema_version", "1.0") != "1.0":
        _error("INVALID_INPUT", "Unsupported manifest version.")
    if manifest["kind"] not in {"correctness", "benchmark", "build", "integration"}:
        _error("INVALID_INPUT", "Unknown check kind.")
    for key, low, high in [("timeout_seconds", 1, 120), ("warmups", 0, 3), ("repeats", 1, 10)]:
        value = manifest[key]
        if type(value) is not int or not low <= value <= high:
            _error("INVALID_INPUT", f"{key} must be an integer from {low} to {high}.")
    if type(manifest["seed"]) is not int:
        _error("INVALID_INPUT", "seed must be an integer.")
    argv = manifest["argv"]
    if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x or "\0" in x for x in argv):
        _error("INVALID_INPUT", "argv must be a nonempty array of strings.")
    if Path(argv[0]).name.lower() in _SHELLS or any(x in {"-c", "-e", "--eval", "-EncodedCommand"} for x in argv[1:]):
        _error("TASK_NOT_ALLOWED", "Use a local check script; shell or inline code commands are not accepted.")
    executable = sys.executable if argv[0] in {"python", "python3"} else shutil.which(argv[0])
    if not executable or not Path(executable).is_file():
        _error("TASK_NOT_ALLOWED", "Executable was not found.")
    cwd = harness.resolve_path(manifest["cwd"], must_exist=True)
    if not cwd.is_dir():
        _error("INVALID_INPUT", "cwd must be a directory.")
    for key in ["tracked_source_paths", "fixture_paths", "check_paths", "lockfile_paths", "env_allowlist"]:
        if not isinstance(manifest[key], list) or any(not isinstance(x, str) or not x for x in manifest[key]):
            _error("INVALID_INPUT", f"{key} must be a string array.")
    if not manifest["tracked_source_paths"] or not str(manifest["oracle_description"]).strip():
        _error("INVALID_INPUT", "Source paths and an oracle description are required.")
    if manifest["kind"] in {"correctness", "benchmark"} and not manifest["check_paths"]:
        _error("INVALID_INPUT", "Correctness and benchmark checks need tracked check paths.")
    if any(any(word in name.upper() for word in ["TOKEN", "SECRET", "PASSWORD", "API_KEY", "COOKIE"]) for name in manifest["env_allowlist"]):
        _error("TASK_NOT_ALLOWED", "Do not forward credential environment variables.")
    _, files = _fingerprints(harness, manifest)
    artifact = harness.resolve_path(manifest["result_artifact"])
    if not artifact.name or artifact == path or artifact in files.values():
        _error("INVALID_INPUT", "Result output must not overwrite tracked input or manifest.")
    for key in ["tracked_source_paths", "fixture_paths", "check_paths", "lockfile_paths"]:
        for value in manifest[key]:
            tracked = harness.resolve_path(value, must_exist=True)
            if tracked.is_dir() and tracked in artifact.parents:
                _error("INVALID_INPUT", "Keep result artifacts outside tracked input directories.")
    if not isinstance(manifest["metric_definitions"], list):
        _error("INVALID_INPUT", "metric_definitions must be an array.")
    metric_names = set()
    for metric in manifest["metric_definitions"]:
        if not isinstance(metric, dict) or not {"name", "unit", "direction", "measurement_scope", "aggregation"} <= metric.keys():
            _error("INVALID_INPUT", "Invalid metric definition.")
        if metric["name"] in metric_names or metric["direction"] not in {"lower", "higher"}:
            _error("INVALID_INPUT", "Duplicate metric or invalid metric direction.")
        metric_names.add(metric["name"])
        if metric["name"] == "wall_time_ms" and (metric["unit"], metric["direction"], metric["measurement_scope"]) != ("ms", "lower", "whole_subprocess"):
            _error("INVALID_INPUT", "wall_time_ms is reserved for runner-measured whole subprocess time.")
    return {"manifest": manifest, "manifest_path": str(path), "manifest_sha256": _digest(manifest)}


def _snapshot(harness, manifest, directory, deadline):
    group_files = {name: _files(harness, manifest[key], deadline) for name, key in [
        ("source", "tracked_source_paths"), ("check", "check_paths"), ("fixture", "fixture_paths"), ("lockfile", "lockfile_paths")]}
    files = {key: value for group in group_files.values() for key, value in group.items()}
    size = sum(path.stat().st_size for path in files.values())
    if size > SNAPSHOT_LIMIT:
        _error("RESOURCE_LIMIT", "Snapshot exceeds 32 MiB; narrow the tracked inputs.")
    store = harness.state / "blobs"
    store.mkdir(parents=True, exist_ok=True)
    used = sum(path.stat().st_size for path in store.iterdir() if path.is_file())
    entries = []
    captured = {}
    for relative, path in sorted(files.items()):
        if time.monotonic() >= deadline:
            _error("TIMEOUT", "Snapshot preparation exceeded the job deadline.")
        digest = _sha(path, deadline)
        blob = store / digest
        if not blob.exists():
            used += path.stat().st_size
            if used > STORE_LIMIT:
                _error("RESOURCE_LIMIT", "Snapshot store exceeds 256 MiB.")
            temporary = store / (uuid.uuid4().hex + ".tmp")
            try:
                copied = 0
                with path.open("rb") as source, temporary.open("xb") as target:
                    for chunk in iter(lambda: source.read(65536), b""):
                        if time.monotonic() >= deadline:
                            _error("TIMEOUT", "Snapshot copy exceeded the job deadline.")
                        copied += len(chunk)
                        if copied > SNAPSHOT_LIMIT:
                            _error("RESOURCE_LIMIT", "A growing input exceeded the snapshot limit.")
                        target.write(chunk)
                os.replace(temporary, blob)
            finally:
                if temporary.exists():
                    temporary.unlink()
        if _sha(blob, deadline) != digest:
            _error("CONTAMINATED_RUN", "Input changed during snapshot capture.")
        captured[relative] = digest
        entries.append({"path": relative, "sha256": digest, "blob": str(blob), "size_bytes": blob.stat().st_size})
    fingerprints = {name: _digest({key: captured[key] for key in group}) for name, group in group_files.items()}
    live, _ = _fingerprints(harness, manifest, deadline)
    if fingerprints != live:
        _error("CONTAMINATED_RUN", "Live inputs differ from the preserved snapshot.")
    _write(directory / "snapshots" / "manifest.json", {"files": entries, "fingerprints": fingerprints})
    return fingerprints


def _windows_job(proc):
    if os.name != "nt":
        return None
    import win32api
    import win32con
    import win32job
    job = win32job.CreateJobObject(None, "")
    try:
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, proc.pid)
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            handle.Close()
        return job
    except Exception:
        job.Close()
        raise


def _stop(proc, job):
    if os.name == "nt":
        if job is not None:
            try:
                job.Close()
            except Exception:
                pass
        elif proc.poll() is None:
            subprocess.run([str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"), "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=5)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


def _cleanup():
    with _ACTIVE_LOCK:
        processes = list(_ACTIVE.values())
    for proc, job in processes:
        try:
            _stop(proc, job)
        except Exception:
            pass


atexit.register(_cleanup)


def _invoke(harness, manifest, directory, deadline, budget):
    argv = list(manifest["argv"])
    if argv[0] in {"python", "python3"}:
        argv[0] = sys.executable
    safe_keys = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "LC_ALL", "PATHEXT"}
    safe_keys.update(manifest["env_allowlist"])
    env = {key: value for key, value in os.environ.items() if key.upper() in {x.upper() for x in safe_keys}}
    env["PYTHONHASHSEED"] = str(manifest["seed"] % 4294967296)
    env["EVIDENCE_SEED"] = str(manifest["seed"])
    env["PYTHONIOENCODING"] = "utf-8"
    directory.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    proc = subprocess.Popen(argv, cwd=harness.resolve_path(manifest["cwd"], must_exist=True), env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=os.name != "nt", creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    job = None
    try:
        job = _windows_job(proc)
    except Exception as exc:
        _stop(proc, None)
        _error("RESOURCE_LIMIT", f"Cannot establish Windows child-process cleanup: {exc}")
    with _ACTIVE_LOCK:
        _ACTIVE[proc.pid] = (proc, job)
    counts = {"stdout": 0, "stderr": 0}
    captured = {"stdout": 0, "stderr": 0}
    lock = threading.Lock()

    def drain(stream, name):
        with (directory / (name + ".log")).open("wb") as output:
            try:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    counts[name] += len(chunk)
                    with lock:
                        keep = min(len(chunk), budget[0])
                        budget[0] -= keep
                    if keep:
                        output.write(chunk[:keep])
                        captured[name] += keep
            finally:
                stream.close()

    threads = [threading.Thread(target=drain, args=(proc.stdout, "stdout"), daemon=True), threading.Thread(target=drain, args=(proc.stderr, "stderr"), daemon=True)]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
        else:
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                timed_out = True
    finally:
        _stop(proc, job)
        with _ACTIVE_LOCK:
            _ACTIVE.pop(proc.pid, None)
        for thread in threads:
            thread.join(timeout=3)
    duration = (time.perf_counter() - started) * 1000
    info = {"argv": argv, "exit_code": proc.returncode, "wall_time_ms": duration, "timeout": timed_out,
            "stdout": str(directory / "stdout.log"), "stderr": str(directory / "stderr.log"),
            "stdout_bytes": counts["stdout"], "stderr_bytes": counts["stderr"],
            "captured_bytes": sum(captured.values()), "truncated": any(counts[x] > captured[x] for x in counts),
            "process_tree_cleanup": "windows_job_object" if os.name == "nt" else "posix_process_group"}
    _write(directory / "process.json", info)
    return info


def _job_path(harness, job_id):
    record = harness.get_record(job_id)
    if record["kind"] != "job":
        _error("INVALID_INPUT", "Expected a job ID.")
    return harness.state / "jobs" / (job_id + ".json")


def status(harness, job_id):
    path = _job_path(harness, job_id)
    if not path.exists():
        created = datetime.fromisoformat(harness.get_record(job_id)["created_at"].replace("Z", "+00:00"))
        if (datetime.now(timezone.utc) - created).total_seconds() > 2:
            return {"job_id": job_id, "job_status": "interrupted", "run_id": None, "run": None,
                    "error": {"error_code": "PROCESS_INTERRUPTED", "message": "Job state was never persisted; it is not an executed result."}}
        return {"job_id": job_id, "job_status": "queued", "run_id": None, "run": None, "error": None}
    with _JSON_LOCK:
        data = json.loads(path.read_text(encoding="utf-8"))
    if data["job_status"] in {"queued", "running"} and not _pid_alive(data.get("owner_pid")):
        data.update(job_status="interrupted", error={"error_code": "PROCESS_INTERRUPTED", "message": "Owning process ended. Partial logs are not verified results; inspect any remaining child processes before retrying."})
        _write(path, data)
    result = dict(data)
    result["run"] = harness.get_record(data["run_id"]) if data.get("run_id") else None
    if result["run"]:
        result["run"]["data"]["freshness"] = freshness(harness, result["run"]["data"])
    return result


def start_check(harness, request_id, check_id):
    previous = harness.get_request(request_id)
    if previous is not None:
        if previous["kind"] != "job" or previous["data"] != {"check_id": check_id}:
            _error("IDEMPOTENCY_CONFLICT", "request_id already refers to different input.")
        previous_status = status(harness, previous["record_id"])
        return {"job_id": previous["record_id"], "job_status": previous_status["job_status"], "reused": True}
    check = harness.get_record(check_id)
    if check["kind"] != "check":
        _error("INVALID_INPUT", "Expected a registered check ID.")
    registered = check["data"]
    current = load_manifest(harness, registered["manifest_path"])
    if current["manifest_sha256"] != registered["manifest_sha256"]:
        _error("MANIFEST_CHANGED", "Manifest changed; register its new content before running it.")
    record = harness.add_record("job", request_id, {"check_id": check_id})
    job_id = record["record_id"]
    if record["reused"]:
        result = status(harness, job_id)
        return {"job_id": job_id, "job_status": result["job_status"], "reused": True}
    path = _job_path(harness, job_id)
    initial = {"job_id": job_id, "job_status": "queued", "run_id": None, "owner_pid": os.getpid(), "error": None}
    _write(path, initial)
    thread = threading.Thread(target=_execute, args=(harness, job_id, check, initial), daemon=True, name="evidence-check")
    thread.start()
    return {"job_id": job_id, "job_status": "queued", "reused": False}


def _execute(harness, job_id, check, job_state):
    try:
        _execute_body(harness, job_id, check, job_state)
    except Exception as exc:
        job_state.update(job_status="failed", error={"error_code": "IO_ERROR", "message": str(exc)})
    finally:
        lock = harness.state / "jobs" / ".active"
        try:
            if lock.exists() and json.loads(lock.read_text())["job_id"] == job_id:
                lock.unlink()
        except (OSError, ValueError, KeyError):
            pass
        # Publish terminal state only after releasing the completed run's lock.
        _write(_job_path(harness, job_id), job_state)


def _execute_body(harness, job_id, check, job_state):
    manifest = check["data"]["manifest"]
    path = _job_path(harness, job_id)
    directory = harness.state / "runs" / job_id
    directory.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    deadline = started + manifest["timeout_seconds"]
    lock_path = harness.state / "jobs" / ".active"
    acquired = False
    before = None
    results = []
    raw_artifacts = []
    execution = "completed"
    correctness = "not_checked"
    error = None
    try:
        if lock_path.exists():
            try:
                owner = json.loads(lock_path.read_text())
            except (ValueError, OSError):
                _error("RESOURCE_LIMIT", "Cannot inspect the existing runner lock.")
            if _pid_alive(owner.get("pid")):
                _error("RESOURCE_LIMIT", "One check is already running in this workspace.")
            lock_path.unlink()
        try:
            with lock_path.open("x", encoding="utf-8") as stream:
                json.dump({"pid": os.getpid(), "job_id": job_id}, stream)
            acquired = True
        except FileExistsError:
            _error("RESOURCE_LIMIT", "Another check acquired this workspace.")
        job_state["job_status"] = "running"
        _write(path, job_state)
        if load_manifest(harness, check["data"]["manifest_path"])["manifest_sha256"] != check["data"]["manifest_sha256"]:
            _error("MANIFEST_CHANGED", "Manifest changed before execution.")
        before = _snapshot(harness, manifest, directory, deadline)
        artifact = harness.resolve_path(manifest["result_artifact"])
        artifact.parent.mkdir(parents=True, exist_ok=True)
        ownership_path = harness.state / "owned-outputs.json"
        ownership = json.loads(ownership_path.read_text()) if ownership_path.exists() else []
        if artifact.exists() and str(artifact) not in ownership:
            _error("OUTPUT_EXISTS", "Existing unowned output was preserved. Choose a new result_artifact path.")
        if str(artifact) not in ownership:
            ownership.append(str(artifact))
            _write(ownership_path, ownership)
        budget = [LOG_LIMIT]
        for index in range(manifest["warmups"] + manifest["repeats"]):
            iteration = directory / "iterations" / f"{index:03d}"
            if artifact.exists():
                if not artifact.is_file() or artifact.is_symlink():
                    _error("INVALID_INPUT", "Result artifact must be a regular local file.")
                artifact.unlink()
            info = _invoke(harness, manifest, iteration, deadline, budget)
            info["warmup"] = index < manifest["warmups"]
            results.append(info)
            if info["timeout"]:
                _error("TIMEOUT", "The whole job deadline was exceeded.")
            if info["exit_code"] != 0:
                _error("CHECK_FAILED", f"Check exited with code {info['exit_code']}.")
            result = None
            if artifact.exists():
                resolved_artifact = harness.resolve_path(str(artifact), must_exist=True)
                if not resolved_artifact.is_file() or resolved_artifact.stat().st_size > LOG_LIMIT:
                    _error("OUTPUT_INVALID", "Result artifact is not a file or exceeds 1 MiB.")
                shutil.copyfile(resolved_artifact, iteration / "result.json")
                try:
                    result = json.loads(resolved_artifact.read_text(encoding="utf-8-sig"))
                except (ValueError, OSError):
                    _error("OUTPUT_INVALID", "Result artifact must be valid UTF-8 JSON.")
                if not isinstance(result, dict):
                    _error("OUTPUT_INVALID", "Result artifact must be an object.")
            if result is None and manifest["kind"] in {"correctness", "benchmark"}:
                _error("OUTPUT_INVALID", "Exit code zero is insufficient: a correctness result artifact is required.")
            if result is not None:
                verdict = result.get("correctness", {})
                if not isinstance(verdict, dict) or verdict.get("status") not in {"passed", "failed", "not_checked"}:
                    _error("OUTPUT_INVALID", "Result needs correctness.status.")
                correctness = verdict["status"]
                if correctness == "passed" and (type(verdict.get("checks")) is not int or verdict["checks"] < 1):
                    _error("OUTPUT_INVALID", "A passing result must report at least one oracle check.")
                metrics = result.get("metrics", {})
                if not isinstance(metrics, dict):
                    _error("OUTPUT_INVALID", "metrics must be an object.")
                for definition in manifest["metric_definitions"]:
                    if definition["name"] == "wall_time_ms":
                        continue
                    value = metrics.get(definition["name"])
                    if type(value) not in {int, float} or not math.isfinite(value):
                        _error("OUTPUT_INVALID", "Missing or non-finite declared metric.")
                info["reported_metrics"] = metrics
                raw_artifacts.append(str(iteration / "result.json"))
                if correctness != "passed" and manifest["kind"] in {"correctness", "benchmark"}:
                    break
        if time.monotonic() > deadline:
            _error("TIMEOUT", "Result collection exceeded the whole job deadline.")
    except HarnessError as exc:
        error = {"error_code": exc.code, "message": exc.message}
        execution = "timeout" if exc.code == "TIMEOUT" else "resource_limited" if exc.code == "RESOURCE_LIMIT" else "failed"
    except Exception as exc:
        error = {"error_code": "IO_ERROR", "message": str(exc)}
        execution = "failed"
    try:
        # Final integrity collection is bounded separately even after a timeout.
        after, _ = _fingerprints(harness, manifest, time.monotonic() + 2)
    except Exception:
        after = None
    contaminated = before is not None and before != after
    measured = [result for result in results if not result["warmup"]]
    metric_list = [{"name": "wall_time_ms", "unit": "ms", "direction": "lower", "measurement_scope": "whole_subprocess", "measurement_source": "runner", "state": "measured" if measured else "not_measured", "samples": [x["wall_time_ms"] for x in measured]}]
    for definition in manifest["metric_definitions"]:
        if definition["name"] == "wall_time_ms":
            continue
        samples = [x.get("reported_metrics", {}).get(definition["name"]) for x in measured]
        samples = [value for value in samples if type(value) in {int, float} and math.isfinite(value)]
        metric_list.append({**definition, "measurement_source": "check_reported", "state": "measured" if samples else "not_measured", "samples": samples})
    executable = results[0]["argv"][0] if results else manifest["argv"][0]
    env_keys = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "LC_ALL", "PATHEXT"}
    env_keys.update(manifest["env_allowlist"])
    environment = {"os": platform.platform(), "machine": platform.machine(), "python": sys.version,
                   "processor": platform.processor(), "cpu_count": os.cpu_count(), "executable": str(executable),
                   "allowed_env_digest": _digest({key: value for key, value in os.environ.items() if key.upper() in {x.upper() for x in env_keys}})}
    _write(directory / "environment.json", environment)
    payload = {"synthetic": False, "check_id": check["record_id"], "variant_id": manifest["variant_id"],
               "execution_status": execution, "correctness_status": correctness,
               "freshness": "contaminated" if contaminated else "current" if before else "unknown",
               "manifest": manifest, "error": error, "limitations": [
                   "Correctness and custom metrics are reported by the registered check; inspect its oracle.",
                   "Only whole subprocess wall time is measured by the runner. Memory was not measured.",
                   "Registered programs are trusted code, not sandboxed by this harness."],
               "observations": {"argv": results[0]["argv"] if results else manifest["argv"], "cwd": str(harness.resolve_path(manifest["cwd"])),
                   "manifest_sha256": check["data"]["manifest_sha256"], "fingerprints_before": before, "fingerprints_after": after,
                   "environment_sha256": _digest(environment), "snapshot_dir": str(directory / "snapshots"),
                   "exit_code": results[-1]["exit_code"] if results else None, "duration_ms": (time.monotonic() - started) * 1000,
                   "seed": manifest["seed"], "warmups": manifest["warmups"], "repeats": manifest["repeats"],
                   "completed_measured_iterations": len(measured), "metrics": metric_list, "iterations": results,
                   "raw_artifacts": raw_artifacts, "logs": {"directory": str(directory / "iterations"), "limit_bytes": LOG_LIMIT,
                       "captured_bytes": sum(x["captured_bytes"] for x in results), "truncated": any(x["truncated"] for x in results)}}}
    try:
        run = harness.add_record("run", "runner/" + job_id, payload)
        _write(directory / "receipt.json", run)
        job_state.update(job_status="completed" if execution == "completed" else "failed", run_id=run["record_id"], error=error)
    except Exception as exc:
        job_state.update(job_status="failed", error={"error_code": "IO_ERROR", "message": str(exc)})
def freshness(harness, run):
    observations = run.get("observations") or {}
    before = observations.get("fingerprints_before")
    if not before:
        return "unknown"
    if run.get("freshness") == "contaminated" or before != observations.get("fingerprints_after"):
        return "contaminated"
    try:
        current, _ = _fingerprints(harness, run["manifest"], time.monotonic() + 2)
    except Exception:
        return "stale"
    return "current" if current == before else "stale"


def compare_runs(harness, baseline_run_id, candidate_run_id, metric_names=None, scope="current"):
    if scope not in {"current", "historical"}:
        _error("INVALID_INPUT", "scope must be current or historical.")
    first = harness.get_record(baseline_run_id)
    second = harness.get_record(candidate_run_id)
    if first["kind"] != "run" or second["kind"] != "run":
        _error("INVALID_INPUT", "Both IDs must refer to actual runs.")
    a, b = first["data"], second["data"]
    blockers = []
    for label, run in [("baseline", a), ("candidate", b)]:
        if run.get("synthetic") is not False or run.get("execution_status") != "completed" or run.get("correctness_status") != "passed":
            blockers.append(label + " lacks completed actual execution with passing correctness")
        state = freshness(harness, run)
        if state in {"contaminated", "unknown"} or (state == "stale" and scope == "current"):
            blockers.append(label + " freshness is " + state)
        obs = run.get("observations") or {}
        if obs.get("completed_measured_iterations") != run["manifest"]["repeats"]:
            blockers.append(label + " has incomplete measured repetitions")
    ao, bo = a["observations"], b["observations"]
    for key in ["check", "fixture", "lockfile"]:
        if (ao.get("fingerprints_before") or {}).get(key) != (bo.get("fingerprints_before") or {}).get(key):
            blockers.append(key + " fingerprint differs")
    if ao.get("environment_sha256") != bo.get("environment_sha256"):
        blockers.append("execution environment differs")
    def normalized_argv(run):
        argv = list(run["observations"]["argv"])
        variant = run["manifest"]["variant_id"]
        for index in range(1, len(argv)):
            if argv[index - 1] in {"--variant", "--variant-id"} and argv[index] == variant:
                argv[index] = "<VARIANT>"
            elif argv[index] in {"--variant=" + variant, "--variant-id=" + variant}:
                argv[index] = argv[index].split("=", 1)[0] + "=<VARIANT>"
        return argv
    if normalized_argv(a) != normalized_argv(b):
        blockers.append("non-variant command arguments differ")
    if ao.get("cwd") != bo.get("cwd"):
        blockers.append("working directory differs")
    for key in ["seed", "warmups", "repeats", "oracle_description", "kind", "metric_definitions"]:
        if a["manifest"][key] != b["manifest"][key]:
            blockers.append(key + " differs")
    am = {x["name"]: x for x in ao["metrics"]}
    bm = {x["name"]: x for x in bo["metrics"]}
    names = metric_names if metric_names is not None else sorted(set(am) & set(bm))
    if not isinstance(names, list) or not names:
        blockers.append("no metrics selected")
        names = []
    summaries = {}
    for name in names:
        if name not in am or name not in bm:
            blockers.append(str(name) + " is missing")
            continue
        x, y = am[name], bm[name]
        if any(x.get(key) != y.get(key) for key in ["unit", "direction", "measurement_scope", "measurement_source", "aggregation"]):
            blockers.append(name + " metric definitions differ")
            continue
        if not x["samples"] or not y["samples"]:
            blockers.append(name + " was not measured")
            continue
        left, right = statistics.median(x["samples"]), statistics.median(y["samples"])
        delta = right - left
        improvement = None if left == 0 else ((left - right) if x["direction"] == "lower" else delta) / abs(left) * 100
        summaries[name] = {"unit": x["unit"], "measurement_scope": x["measurement_scope"], "direction": x["direction"],
                           "baseline": {"median": left, "min": min(x["samples"]), "max": max(x["samples"]), "n": len(x["samples"]), "samples": x["samples"]},
                           "candidate": {"median": right, "min": min(y["samples"]), "max": max(y["samples"]), "n": len(y["samples"]), "samples": y["samples"]},
                           "absolute_delta": delta, "observed_improvement_percent": improvement}
    if blockers:
        _error("COMPARISON_BLOCKED", "; ".join(blockers))
    return {"workspace_id": harness.workspace_id, "comparable": True, "scope": scope, "baseline_run_id": baseline_run_id,
            "candidate_run_id": candidate_run_id, "metric_summaries": summaries, "limitations": [
                "Descriptive observations only; small samples do not establish general superiority or optimality.",
                "Historical comparisons do not verify current source code.", "Custom metrics and oracle correctness require review of the check implementation."]}
