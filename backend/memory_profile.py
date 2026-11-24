"""CLI for a fresh, pair-verified WSL memory comparison."""
import argparse
import csv
import json
from pathlib import Path
import re
from statistics import median
import struct
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / ".demo/nuclei-vessel-profile"
GPU = ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"]


def wsl(path):
    return f"/mnt/{path.drive[0].lower()}/{path.as_posix()[3:]}"


def read_pairs(path):
    with path.open(newline="") as stream:
        reader = csv.reader(stream)
        next(reader)
        return sorted(tuple(map(int, row)) for row in reader)


def logged_int(pattern, log):
    match = re.search(pattern, log)
    if match is None:
        raise RuntimeError(f"Missing profiling counter: {pattern}")
    return int(match[1])


def measure(command, tag):
    idle = int(subprocess.check_output(GPU, text=True).strip())
    with tag.with_suffix(".gpu.csv").open("w") as gpu:
        sampler = subprocess.Popen([*GPU, "-lms", "10"], stdout=gpu)
        try:
            result = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace", timeout=90)
        finally:
            sampler.terminate()
            sampler.wait()
    tag.with_suffix(".stdout").write_text(result.stdout, encoding="utf-8")
    tag.with_suffix(".stderr").write_text(result.stderr, encoding="utf-8")
    if result.returncode:
        detail = (result.stderr or result.stdout).replace("\0", "").strip()
        raise RuntimeError(f"Engine exited with code {result.returncode}: {detail[-500:]}")
    samples = [int(line) for line in tag.with_suffix(".gpu.csv").read_text().splitlines() if line.strip().isdigit()]
    if not samples:
        raise RuntimeError("No VRAM samples were captured.")
    return result, {
        "cpu_peak_mib": logged_int(r"PEAK_RSS_KIB=(\d+)", result.stderr) / 1024,
        "gpu_sampled_peak_above_idle_mib": max(samples) - idle, "idle_mib": idle,
    }


def mesh_payload(paths):
    vertices = triangles = edges = grid_bytes = 0
    for path in paths:
        with path.open("rb") as stream:
            magic, version, nv, nt, nm, ne, _, has_grid, has_edges = struct.unpack("<II5QBB6x", stream.read(56))
            if (magic, version, nm, has_grid, has_edges) != (0x52334442, 4, nt, 1, 1):
                raise RuntimeError(f"Unexpected prepared geometry layout: {path}")
            stream.seek(56 + 12*nv + 16*nt + 28*ne)
            _, cells = struct.unpack("<fI", stream.read(8))
            if stream.tell() + 28*cells != path.stat().st_size:
                raise RuntimeError(f"Invalid grid layout: {path}")
        vertices += nv
        triangles += nt
        edges += ne
        grid_bytes += 28*cells
    return vertices, triangles, edges, grid_bytes


def run(checkout, distro, cubes):
    key = "cubes" if cubes else "vessels"
    folder = SCENE / "memory/live-runs" / key
    folder.mkdir(parents=True, exist_ok=True)
    cube_data = SCENE / "memory/cubes-tiled-50000"
    meshes = [cube_data/f"{side}.pre" for side in "ab"] if cubes else [SCENE/"n.pre", SCENE/"v.pre"]
    tiles = [cube_data/f"{side}.compressed.dt" for side in "ab"] if cubes else [
        SCENE/"intersecting_n_nv1_nu200_vs100_r30.dt", SCENE/"intersecting_v_nv1_nu200_vs100_r30.dt"]
    missing = [str(path) for path in [*meshes, *tiles] if not path.is_file()]
    if missing:
        raise RuntimeError("Missing prepared inputs: " + ", ".join(missing))
    vertices, triangles, edges, grid = mesh_payload(meshes)
    expected = None if cubes else read_pairs(SCENE/"matched-pairs.csv")
    expected_count = 250000 if cubes else 33
    checkout = checkout.rstrip("/")
    bin_path = checkout + "/Pierce/pierce/query/build/bin"
    libraries = checkout + "/.optix-runtime:/usr/lib/wsl/lib:/usr/local/cuda-12.8/lib64"
    prefix = ["wsl.exe", "-d", distro, "-u", "root", "--", "/usr/bin/timeout", "80",
              "/usr/bin/time", "-f", "PEAK_RSS_KIB=%M"]
    trials = {"pierce": [], "tdbase": []}
    buffers = {"hashTable": [], "resultBuffer": [], "hostResults": [], "workspace": []}
    profile_counters = {
        "bvhA": "BVH A bytes", "bvhB": "BVH B bytes",
        "buildScratchA": "build scratch A bytes", "buildScratchB": "build scratch B bytes",
        "launcherGpu": "launcher GPU bytes",
        "contextGpuDelta": "OptiX context GPU delta", "launcherGpuDelta": "launcher GPU delta",
        "estimatorGpu": "estimator GPU scratch bytes",
        "estimatorHost": "estimator host matched-cell bytes",
    }
    buffers.update({key: [] for key in profile_counters})
    for i in range(5):
        order = ("pierce", "tdbase") if i % 2 == 0 else ("tdbase", "pierce")
        for mode in order:
            tag = folder / f"{mode}-{i}"
            if mode == "pierce":
                pair_path = tag.with_suffix(".pairs.csv")
                command = [*prefix, "env", "LD_LIBRARY_PATH="+libraries, bin_path+"/pierce_overlap",
                           "--mesh1", wsl(meshes[0]), "--mesh2", wsl(meshes[1]),
                           "--ptx", bin_path+"/mesh_overlap_edges.ptx", "--profile-memory", "--warmup-runs", "0", "--runs", "1",
                           "--output", wsl(tag.with_suffix(".json")), "--pairs-output", wsl(pair_path)]
                result, memory = measure(command, tag)
                actual = read_pairs(pair_path)
                for name, label in profile_counters.items():
                    buffers[name].append(logged_int("Memory " + label + r": (-?\d+)", result.stdout))
                for name, label in (("hashTable", "Hash Table Allocated Bytes"),
                                    ("resultBuffer", "Result Buffer Allocated Bytes"),
                                    ("hostResults", "Result Buffer Used Bytes")):
                    buffers[name].append(logged_int(label + r": (\d+)", result.stdout))
            else:
                command = [*prefix, "bash", wsl(SCENE.parent/"tdbase-wsl/run.sh"),
                           "join", "-q", "intersect", "-g", "-p", "-t", "4", "--cn", "4",
                           "--tile1", wsl(tiles[0]), "--tile2", wsl(tiles[1])]
                result, memory = measure(command, tag)
                if "GPU 0 started to check intersect" not in result.stderr:
                    raise RuntimeError("TDBase did not report GPU execution.")
                buffers["workspace"].append(logged_int(r"(\d+) MB memory size is allocated", result.stderr) * 2**20)
                actual = sorted(tuple(map(int, row.split())) for row in result.stdout.splitlines()
                                if re.fullmatch(r"\d+\s+\d+", row))
            if len(actual) != expected_count or len(set(actual)) != expected_count:
                raise RuntimeError(f"{mode} run {i+1} returned an unexpected pair count or duplicates.")
            if expected is None:
                expected = actual
            if actual != expected:
                raise RuntimeError(f"{mode} run {i+1} returned different object pairs.")
            trials[mode].append(memory)

    summary = {
        "key": key, "title": "50k × 50k tiled cubes" if cubes else "Nuclei and vessels",
        "pairs": expected_count, "vertices": vertices, "triangles": triangles, "edges": edges,
        **{mode: {k: median(row[k] for row in rows) for k in rows[0]} for mode, rows in trials.items()},
        "allocations": {"positions": 12*vertices, "indices": 12*triangles, "triangleIds": 4*triangles,
                        "edgeEndpoints": 24*edges, "edgeIds": 4*edges, "grid": grid,
                        **{k: median(values) for k, values in buffers.items()}},
    }
    (folder/"summary.json").write_text(json.dumps({"summary": summary, "trials": trials}, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pierce-root", required=True)
    parser.add_argument("--distro", default="Ubuntu-24.04")
    parser.add_argument("--cubes", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.pierce_root, args.distro, args.cubes)))
