"""Live shared-BVH experiments over the prepared meeting datasets."""
import csv
import json
from pathlib import Path
import re
from statistics import median
import struct
import subprocess
from tempfile import TemporaryDirectory

from .runner import DemoError, ROOT, pierce_executable

PIERCE = ROOT / "Pierce/pierce"
DATA = ROOT / ".demo"
PRESETS = {"live": 64, "large": 192}
RUNS = 12


def execute(exe, args, cwd):
    try:
        result = subprocess.run([str(exe), *map(str, args)], cwd=cwd,
                                capture_output=True, text=True, errors="replace", timeout=120)
    except subprocess.TimeoutExpired as error:
        raise DemoError(f"{exe.name} exceeded the experiment time limit.", 504) from error
    except OSError as error:
        raise DemoError(f"Cannot start {exe.name}: {error}", 503) from error
    if result.returncode:
        detail = (result.stderr.strip() or result.stdout.strip())[-900:]
        raise DemoError(f"{exe.name} exited with code {result.returncode}. {detail}")
    return result.stdout


def statistics(trials):
    limits = {}
    for phase in ("total", "trace"):
        if phase in trials[0]:
            values = [t[phase] for t in trials]
            center = median(values)
            limits[phase] = center + 3 * max(1.0, 1.4826 * median(abs(v-center) for v in values))
    kept = [t for t in trials if all(t[k] <= v for k, v in limits.items())]
    keys = trials[0].keys()
    return {"trials": trials, "median": {k: median(t[k] for t in trials) for k in keys},
            "filtered": {k: median(t[k] for t in kept) for k in keys} if kept else None,
            "retained": len(kept)}


def run_experiment(preset):
    a, b = (DATA / "data" / f"{side}.pre" for side in "ab")
    sweep = [b, *(DATA / "profiling-pilot/one-sided-mesh" / f"b-{i}.pre" for i in range(1, 4))]
    tiles = PRESETS[preset]
    scene = DATA / "nuclei-vessel-profile" / f"tiles-{tiles}"
    inputs = [a, *sweep, scene / "n.pre", scene / "v.pre"]
    missing = [str(p.relative_to(ROOT)) for p in inputs if not p.is_file()]
    if missing:
        raise DemoError("Prepared experiment inputs are missing: " + ", ".join(missing), 503)

    directions = []
    expected = {(5*g+i, 5*g+j) for g in range(200) for i in range(5) for j in range(5)}
    overlap = pierce_executable(PIERCE, "query", "pierce_overlap")
    compare = pierce_executable(PIERCE, "query", "pierce_bvh_compare")
    with TemporaryDirectory(prefix="pierce-experiment-") as temporary:
        timing, pairs = (Path(temporary) / f"query.{suffix}" for suffix in ("json", "csv"))
        for target in sweep:
            output = execute(overlap, [
                "--mesh1", a, "--mesh2", target, "--ptx", overlap.parent / "mesh_overlap_edges.ptx",
                "--warmup-runs", "5", "--runs", RUNS, "--output", timing, "--pairs-output", pairs,
            ], temporary)
            with pairs.open(newline="") as stream:
                values = [tuple(map(int, row)) for row in list(csv.reader(stream))[1:]]
            if len(values) != len(expected) or set(values) != expected:
                raise DemoError("The mesh sweep returned a different pair set.")
            phases = json.loads(timing.read_text())["phases"]
            with target.open("rb") as stream:
                triangles = struct.unpack("<II5QBB6x", stream.read(56))[3]
            setup = {key: value["duration_us"]/1000 for key, value in phases.items()
                     if re.search(r"load|build|upload", key, re.I) and "download" not in key}
            trials = [{"aToB": phases[f"raytrace_hash_mesh1tomesh2_{i}"]["duration_us"]/1000,
                       "bToA": phases[f"raytrace_hash_mesh2tomesh1_{i}"]["duration_us"]/1000}
                      for i in range(1, RUNS + 1)]
            stats = {key: statistics([{"trace": row[key]} for row in trials]) for key in trials[0]}
            directions.append({"triangles": triangles, "timings": stats, "setup": setup,
                               "pairs": len(values), "estimatedPairs": int(re.search(
                                   r"Final Estimated Pairs:\s*(\d+)", output)[1])})
        output = execute(compare, [scene/"n.pre", scene/"v.pre", compare.parent, RUNS, 16, "--parallel-only"], temporary)
    rows = list(csv.reader(output.splitlines()))
    verified = [(int(r[1]), int(r[2])) for r in rows if r and r[0] == "verified" and int(r[1]) >= 0]
    if verified != [(i, 33*tiles) for i in range(RUNS)]:
        raise DemoError("The nuclei–vessel comparison did not verify all trials.")
    shared = [r for r in rows if r and r[0] == "shared" and int(r[1]) >= 0]
    parallel = [r for r in rows if r and r[0] == "parallel" and int(r[1]) >= 0]
    if any([int(r[1]) for r in path] != list(range(RUNS)) for path in (shared, parallel)):
        raise DemoError("The nuclei–vessel comparison returned incomplete timing data.")
    setup, = [r for r in rows if r and r[0] == "setup"]
    return {"directions": directions, "refinement": {
        "tiles": tiles, "pairs": 33*tiles, "candidates": int(parallel[0][5]),
        "traceCalls": int(parallel[0][6]), "sharedSetup": float(setup[1]),
        "parallelSetup": float(setup[2]), "treeSetup": float(setup[4]),
        "shared": statistics([dict(zip(("total", "trace", "estimate", "compact", "download"),
                                      map(float, (r[2], r[3], *r[5:8])))) for r in shared]),
        "parallel": statistics([dict(zip(("total", "trace", "filter", "upload", "download", "host"),
                                        map(float, (r[2], r[3], r[4], *r[7:10])))) for r in parallel]),
    }}
