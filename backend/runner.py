"""Small native Pierce runner; all paths are relative to the checkout, not cwd."""

import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Pierce"))
from benchmarks.common.executables import pierce_executable


class DemoError(Exception):
    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message)
        self.status_code = status_code


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
    def __init__(self, root: Path = ROOT, timeout: float = 120):
        self.root = root.resolve()
        self.executable = pierce_executable(self.root / "Pierce" / "pierce", "query", "pierce_overlap_two_pass")
        self.data_dir = self.root / ".demo" / "data"
        self.runs_dir = self.root / ".demo" / "runs"
        self.timeout = timeout
        self.lock = threading.Lock()

    def health(self) -> dict:
        issues = []
        if not self.executable.is_file():
            issues.append("Pierce executable missing. Run .\\Pierce\\build_windows.ps1 from the repo root.")
        elif not self.ptx_path():
            issues.append("mesh_overlap_edges.ptx missing beside the build. Rebuild Pierce query; keep generated PTX files in place.")
        for name in ("a.pre", "b.pre"):
            path = self.data_dir / name
            if not path.is_file() or path.stat().st_size <= 64:
                issues.append(f"Prepared {name} missing or empty. Run python -m backend.prepare in pierce_windows_build.")
        return {"ready": not issues, "running": self.lock.locked(), "issues": issues}

    def ptx_path(self) -> Path | None:
        # Mirrors native discovery for single- and multi-configuration builds.
        return next((directory / "mesh_overlap_edges.ptx" for directory in
                     (self.executable.parent, self.executable.parent.parent, self.executable.parent.parent.parent)
                     if (directory / "mesh_overlap_edges.ptx").is_file()), None)

    def run(self) -> dict:
        if not self.lock.acquire(blocking=False):
            raise DemoError("Pierce is already running. Wait for the current run to finish.", 409)
        try:
            health = self.health()
            if not health["ready"]:
                raise DemoError(" ".join(health["issues"]), 503)
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=self.runs_dir))
            command = [str(self.executable), "--mesh1", str(self.data_dir / "a.pre"),
                       "--mesh2", str(self.data_dir / "b.pre"), "--runs", "1", "--warmup-runs", "0",
                       "--no-export", "--ptx", str(self.ptx_path()), "--output", str(run_dir / "timing.json")]
            try:
                completed = subprocess.run(command, cwd=run_dir, capture_output=True, text=True,
                                           encoding="utf-8", errors="replace", timeout=self.timeout)
            except subprocess.TimeoutExpired as error:
                # subprocess.run kills and waits for the child before raising.
                (run_dir / "error.log").write_text(f"Timed out after {self.timeout}s\n{error}", encoding="utf-8")
                raise DemoError(f"Pierce timed out after {self.timeout:g} seconds. See {run_dir}.", 504) from error
            except OSError as error:
                (run_dir / "error.log").write_text(str(error), encoding="utf-8")
                raise DemoError(f"Could not launch Pierce: {error}. Start the backend in pierce_windows_build so DLLs are on PATH.") from error
            (run_dir / "stdout.log").write_text(completed.stdout, encoding="utf-8")
            (run_dir / "stderr.log").write_text(completed.stderr, encoding="utf-8")
            if completed.returncode:
                detail = (completed.stderr or completed.stdout).strip()[-1500:]
                raise DemoError(f"Pierce exited with code {completed.returncode}. Activate pierce_windows_build and check the NVIDIA driver. {detail} Logs: {run_dir}")
            try:
                data = json.loads((run_dir / "timing.json").read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise DemoError(f"Pierce did not produce valid timing JSON. See {run_dir}.") from error
            return parse_metrics(data, completed.stdout)
        finally:
            self.lock.release()
