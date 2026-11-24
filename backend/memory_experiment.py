"""Run the WSL memory comparison as an isolated profiling process."""
import json
import os
import subprocess
import sys

from .runner import DemoError, ROOT


def run_memory(workload: str) -> dict:
    checkout = os.environ.get("PIERCE_WSL_ROOT", "/root/pierce-explorer-wsl.rcolMI")
    args = [sys.executable, str(ROOT / "backend/memory_profile.py"), "--pierce-root", checkout,
            "--distro", os.environ.get("PIERCE_WSL_DISTRO", "Ubuntu-24.04")]
    if workload == "cubes":
        args.append("--cubes")
    try:
        completed = subprocess.run(args, cwd=ROOT, capture_output=True, encoding="utf-8",
                                   errors="replace", timeout=180)
    except subprocess.TimeoutExpired as error:
        raise DemoError("The WSL memory experiment timed out.", 504) from error
    if completed.returncode:
        detail = completed.stderr.strip().replace("\0", "")[-650:]
        raise DemoError(f"WSL memory experiment failed. {detail}", 503)
    try:
        return json.loads(completed.stdout)
    except ValueError as error:
        raise DemoError("The memory profiler returned invalid data.") from error
