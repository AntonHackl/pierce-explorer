"""Small native Pierce runner; all paths are relative to the checkout, not cwd."""

import json
import math
import logging
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import queue
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Pierce"))
from benchmarks.common.executables import pierce_executable
from .catalog import CATALOG, prepared_workload, workload_paths


class DemoError(Exception):
    def __init__(self, message: str, status_code: int = 500, code=None, details=None):
        super().__init__(message)
        self.status_code = status_code
        self.code, self.details = code, details


def parse_metrics(data: dict, stdout: str) -> dict:
    """Use microseconds to avoid rounding small GPU phases to zero milliseconds."""
    def duration(value):
        if not isinstance(value, dict):
            raise ValueError("Missing timing measurement")
        result = value.get("duration_us")
        if isinstance(result, bool) or not isinstance(result, (int, float)):
            raise ValueError("Missing or nonnumeric duration_us")
        if not math.isfinite(result) or result < 0:
            raise ValueError("Invalid duration_us")
        return result

    try:
        total = duration(data["total"])
        phases = {}
        for key, measurement in data["phases"].items():
            name = re.sub(r"_\d+$", "", key.lower())
            phases[name] = phases.get(name, 0) + duration(measurement)
        raytrace = [value for key, value in phases.items() if key.startswith("raytrace_")]
        if not raytrace or "gpu deduplication" not in phases or "download results" not in phases:
            raise ValueError("Expected two-pass query phases are missing")
        query = sum(raytrace) + phases["gpu deduplication"] + phases["download results"]
        matches = re.findall(r"^Unique object pairs:\s*(\d+)\s*$", stdout, re.MULTILINE)
        if len(matches) != 1:
            raise ValueError("Expected one Unique object pairs summary")
        return {"overallTimeMs": total / 1000, "queryTimeMs": query / 1000,
                "resultCount": int(matches[0])}
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError) as error:
        raise DemoError(f"Invalid Pierce output: {error}. See the run files in .demo/runs.") from error


class PierceRunner:
    """Own one native worker; its reader threads never perform GPU operations."""

    def __init__(self, root: Path = ROOT, timeout: float = 120):
        self.root = root.resolve()
        self.executable = pierce_executable(self.root / "Pierce" / "pierce", "query", "pierce_worker")
        self.data_dir = self.root / ".demo" / "data"
        self.runs_dir = self.root / ".demo" / "runs"
        self.timeout = timeout
        self.lock = threading.Lock()
        self.process = None
        self.responses = None
        self.readers = []
        self.session_dir = None
        self.operation = None
        self.operation_workload = None
        self.resident = {}
        self.memory = None
        self.checkpoints = []
        self.workload_errors = {}
        self.last_error = None
        self.runtime_initialization_ms = 0

    def _invalidate(self):
        self.resident = {}
        self.memory = None
        self.checkpoints = []
        self.runtime_initialization_ms = 0

    def health(self) -> dict:
        issues = []
        if not self.executable.is_file():
            issues.append("Pierce worker missing. Rebuild Pierce query with .\\Pierce\\build_windows.ps1 -Only query.")
        elif not self.ptx_path():
            issues.append("mesh_overlap_edges.ptx missing. Rebuild Pierce query and keep PTX files in place.")
        alive = self.process is not None and self.process.poll() is None
        if self.process is not None and not alive:
            self._invalidate()
            self.last_error = "Pierce worker stopped. Load a workload to start it again."
        return {"ready": not issues, "running": self.lock.locked(), "issues": issues,
                "workerAvailable": alive, "operation": self.operation,
                "operationWorkload": self.operation_workload, "memory": self.memory,
                "checkpoints": self.checkpoints, "lastError": self.last_error}

    def workloads(self):
        self.health()
        return [self.workload(workload_id) for workload_id in CATALOG]

    def workload(self, workload_id):
        info = prepared_workload(self.root, workload_id)
        resident = self.resident.get(workload_id)
        return {**info, "loaded": resident is not None,
                "loadInfo": {"datasetLoadMs": resident["datasetLoadMs"],
                             "runtimeInitializationMs": self.runtime_initialization_ms} if resident else None,
                "completedQueries": resident["completedQueries"] if resident else 0,
                "datasetGpuBytes": resident["datasetGpuBytes"] if resident else 0,
                "lastResult": resident["lastResult"] if resident else None,
                "lastError": self.workload_errors.get(workload_id)}

    def ptx_path(self) -> Path | None:
        return next((directory / "mesh_overlap_edges.ptx" for directory in
                     (self.executable.parent, self.executable.parent.parent, self.executable.parent.parent.parent)
                     if (directory / "mesh_overlap_edges.ptx").is_file()), None)

    def _start(self):
        if self.process is not None and self.process.poll() is None:
            return
        self._stop()
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.session_dir = Path(tempfile.mkdtemp(prefix="worker-", dir=self.runs_dir))
        responses = queue.Queue()
        try:
            process = subprocess.Popen([str(self.executable), "--ptx", str(self.ptx_path())],
                                       cwd=self.session_dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="strict",
                                       bufsize=1)
        except OSError as error:
            raise DemoError(f"Could not launch Pierce: {error}. Activate pierce_windows_build so DLLs are on PATH.") from error
        self.process, self.responses = process, responses

        def read_responses():
            try:
                while True:
                    line = process.stdout.readline(1024 * 1024)
                    if not line:
                        break
                    if not line.endswith("\n"):
                        raise ValueError("Worker response exceeded framing limit")
                    responses.put(line)
            except (OSError, UnicodeError, ValueError) as error:
                responses.put(error)
            finally:
                responses.put(EOFError("Pierce worker closed its output"))
                process.stdout.close()

        def read_diagnostics():
            try:
                with (self.session_dir / "stderr.log").open("w", encoding="utf-8") as log:
                    while True:
                        chunk = process.stderr.read(4096)
                        if not chunk:
                            break
                        log.write(chunk)
                        log.flush()
            except (OSError, UnicodeError):
                pass
            finally:
                process.stderr.close()

        self.readers = [threading.Thread(target=read_responses, daemon=True),
                        threading.Thread(target=read_diagnostics, daemon=True)]
        for thread in self.readers:
            thread.start()

    def _stop(self):
        process, self.process = self.process, None
        self._invalidate()
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass
        for thread in self.readers:
            thread.join(timeout=5)
        self.readers = []
        self.responses = None

    def _exchange(self, command, **arguments):
        request_id = uuid.uuid4().hex
        request = {"version": 3, "id": request_id, "command": command, **arguments}
        start = time.perf_counter()
        response = None
        try:
            if self.process is None or self.process.poll() is not None:
                raise EOFError("Pierce worker is unavailable")
            self.process.stdin.write(json.dumps(request) + "\n")
            self.process.stdin.flush()
            try:
                line = self.responses.get(timeout=self.timeout)
            except queue.Empty as error:
                raise TimeoutError("Pierce operation timed out") from error
            if isinstance(line, Exception):
                raise line
            response = json.loads(line)
            if (not isinstance(response, dict) or type(response.get("version")) is not int or
                    response["version"] != 3 or response.get("id") != request_id or
                    type(response.get("ok")) is not bool):
                raise ValueError("Invalid worker response or request ID")
            if not response["ok"]:
                if not isinstance(response.get("error"), str):
                    raise ValueError("Invalid worker error")
                if isinstance(response.get("data"), dict):
                    self._apply_status(response["data"])
                if response.get("code") == "memory_budget_exceeded":
                    details = response.get("details")
                    if not isinstance(details, dict):
                        raise ValueError("Memory error details missing")
                    for key in ("requiredBytes", "availableBytes", "reservedBytes"):
                        self._number(details, key, integer=True)
                    raise DemoError(response["error"], 422, "memory_budget_exceeded", details)
                raise DemoError(response["error"])
            if not isinstance(response.get("data"), dict):
                raise ValueError("Worker response data missing")
            self._apply_status(response["data"])
            return response["data"]
        except DemoError:
            raise
        except (OSError, UnicodeError, ValueError, EOFError, TimeoutError) as error:
            self._stop()
            status = 504 if isinstance(error, TimeoutError) else 500
            raise DemoError(f"{error}. Load cubes to restart Pierce. Logs: {self.session_dir}", status) from error
        finally:
            self._log("requests.jsonl", {"request": request, "response": response,
                                         "backendOperationMs": (time.perf_counter() - start) * 1000})

    def _log(self, name, record):
        if self.session_dir:
            try:
                with (self.session_dir / name).open("a", encoding="utf-8") as log:
                    log.write(json.dumps(record) + "\n")
            except OSError:
                logging.exception("Could not write Pierce diagnostic log")

    @staticmethod
    def _number(data, key, integer=False):
        value = data.get(key)
        if (type(value) not in (int, float) or not math.isfinite(value) or value < 0 or
                (integer and type(value) is not int)):
            raise ValueError(f"Invalid worker measurement: {key}")
        return value

    def _apply_status(self, data):
        pairs = data.get("pairs")
        if not isinstance(pairs, list):
            raise ValueError("Worker pair registry missing")
        initialization = self._number(data, "runtimeInitializationUs") / 1000
        memory = data.get("memory")
        if memory is not None:
            if not isinstance(memory, dict) or not isinstance(memory.get("device"), str):
                raise ValueError("Invalid GPU snapshot")
            for key in ("totalBytes", "freeBytes", "reservedBytes"):
                self._number(memory, key, integer=True)
        checkpoints = data.get("checkpoints")
        if not isinstance(checkpoints, list):
            raise ValueError("Invalid allocation checkpoints")
        resident = {}
        handles = set()
        for pair in pairs:
            if not isinstance(pair, dict):
                raise ValueError("Invalid pair status")
            key, handle = pair.get("key"), pair.get("pairHandle")
            if key not in CATALOG or key in resident or not isinstance(handle, str) or not handle or handle in handles:
                raise ValueError("Invalid workload key or pair handle")
            handles.add(handle)
            result = pair.get("lastResult")
            count = self._number(pair, "completedQueries", integer=True)
            if result is not None:
                if not isinstance(result, dict):
                    raise ValueError("Invalid query metrics")
                result_count = self._number(result, "resultCount", integer=True)
                phases = result.get("phaseTimingsUs")
                if not isinstance(phases, dict):
                    raise ValueError("Invalid query phase timings")
                phase_timings = {name: self._number(phases, name, integer=True) / 1000
                                 for name in ("estimation", "trace", "compaction", "download")}
                result = {"workloadId": key, "queryExecutionMs": self._number(result, "queryExecutionUs") / 1000,
                          "benchmarkPhaseSumMs": self._number(result, "benchmarkPhaseSumUs") / 1000,
                          "phaseTimingsMs": phase_timings,
                          "resultCount": result_count, "completedQueries": count,
                          "actualSelectivity": result_count / (CATALOG[key]["countA"] * CATALOG[key]["countB"]),
                          "rawHitCount": self._number(result, "rawHitCount", integer=True),
                          "estimatedPairCount": self._number(result, "estimatedPairCount", integer=True),
                          "hashTableBytes": self._number(result, "hashTableBytes", integer=True),
                          "resultBufferBytes": self._number(result, "resultBufferBytes", integer=True)}
            if bool(count) != (result is not None):
                raise ValueError("Query count and metrics disagree")
            resident[key] = {"pairHandle": handle, "datasetLoadMs": self._number(pair, "datasetLoadUs") / 1000,
                             "datasetGpuBytes": self._number(pair, "datasetGpuBytes", integer=True),
                             "completedQueries": count, "lastResult": result}
        self.resident, self.memory, self.checkpoints = resident, memory, checkpoints
        self.runtime_initialization_ms = initialization

    def _operate(self, operation, workload_id):
        if workload_id not in CATALOG:
            raise DemoError("Unknown workload", 422)
        if not self.lock.acquire(blocking=False):
            raise DemoError("Pierce is busy. Wait for the current operation to finish.", 409)
        self.operation, self.operation_workload = operation, workload_id
        operation_start = time.perf_counter()
        try:
            health = self.health()
            pair = self.resident.get(workload_id)
            if operation == "load":
                if not pair:
                    info = self.workload(workload_id)
                    if not health["ready"] or not info["prepared"]:
                        raise DemoError(" ".join(health["issues"] + info["issues"]), 503)
                    paths = workload_paths(self.root, workload_id)
                    self._start()
                    self._exchange("load", key=workload_id, mesh1=str(paths[0]), mesh2=str(paths[1]))
                    if workload_id not in self.resident:
                        raise ValueError("Load did not publish requested workload")
            elif operation == "unload":
                if pair:
                    self._exchange("unload", pairHandle=pair["pairHandle"])
                    if workload_id in self.resident:
                        raise ValueError("Unload did not remove requested workload")
            else:
                if not pair:
                    raise DemoError("Load this workload before running a query.", 409)
                prior_count = pair["completedQueries"]
                self._exchange("query", pairHandle=pair["pairHandle"])
                if workload_id not in self.resident or self.resident[workload_id]["completedQueries"] != prior_count + 1:
                    raise ValueError("Query did not update requested workload")
            self.workload_errors.pop(workload_id, None)
            self.last_error = None
            return self.resident[workload_id]["lastResult"] if operation == "query" else self.workload(workload_id)
        except ValueError as error:
            self._stop()
            self.last_error = str(error)
            raise DemoError(f"Invalid Pierce output: {error}. Load workloads to restart Pierce.") from error
        except DemoError as error:
            if error.status_code != 409:
                self.workload_errors[workload_id] = str(error)
            if error.status_code >= 500:
                self.last_error = str(error)
            raise
        finally:
            self._log("operations.jsonl", {"operation": operation, "workloadId": workload_id,
                                          "backendOperationMs": (time.perf_counter() - operation_start) * 1000})
            self.operation, self.operation_workload = None, None
            self.lock.release()

    def load(self, workload_id):
        return self._operate("load", workload_id)

    def run(self, workload_id):
        return self._operate("query", workload_id)

    def unload(self, workload_id):
        return self._operate("unload", workload_id)

    def close(self):
        with self.lock:
            try:
                if self.process is not None and self.process.poll() is None:
                    saved_timeout = self.timeout
                    try:
                        self.timeout = min(self.timeout, 3)
                        self._exchange("shutdown")
                        self.process.wait(timeout=3)
                    finally:
                        self.timeout = saved_timeout
            except (DemoError, subprocess.TimeoutExpired):
                pass
            finally:
                self._stop()
