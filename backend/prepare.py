"""Run once with: python -m backend.prepare (in pierce_windows_build)."""

import subprocess

from .runner import ROOT, pierce_executable
from pierce.scripts.generate_test_cubes import generate_cube_faces, generate_cube_vertices, write_obj_file


def generate_demo_cubes():
    """200 separated groups × (5 A cubes × 5 B cubes) = exactly 5,000 pairs."""
    datasets = {"a": [], "b": []}
    for group in range(200):
        origin = (10 * (group % 10), 10 * ((group // 10) % 10), 10 * (group // 100))
        for member in range(5):
            object_id = group * 5 + member
            offsets = {"a": (0.011 * member, 0.019 * member, 0.023 * member),
                       "b": (0.6 + 0.021 * member, 0.4 + 0.013 * member, 0.2 + 0.017 * member)}
            for name, offset in offsets.items():
                center = tuple(origin[d] + offset[d] for d in range(3))
                datasets[name].append((object_id, generate_cube_vertices(*center, 2), generate_cube_faces()))
    return datasets


def main():
    executable = pierce_executable(ROOT / "Pierce" / "pierce", "preprocess", "pierce_preprocess")
    if not executable.is_file():
        raise SystemExit("Preprocessor missing. Run .\\Pierce\\build_windows.ps1 first.")
    directory = ROOT / ".demo" / "data"
    directory.mkdir(parents=True, exist_ok=True)
    for name, cubes in generate_demo_cubes().items():
        source = directory / f"{name}.obj"
        write_obj_file(source, cubes)
        # Replace prepared input only after a successful preprocessing invocation.
        pending = directory / f"{name}.pending.pre"
        subprocess.run([str(executable), "--dataset", str(source), "--output-geometry", str(pending),
                        "--output-timing", str(directory / f"{name}-preprocess.json"),
                        "--generate-grid", "--grid-cell-size", "2"], cwd=directory, check=True, timeout=120)
        if not pending.is_file() or pending.stat().st_size <= 64:
            raise SystemExit(f"Preprocessor did not generate valid geometry for {name}.")
        pending.replace(directory / f"{name}.pre")
    print(f"Demo data ready: {directory}\nObjects: 1,000 x 1,000\nExpected unique object pairs: 5,000\nSelectivity: 0.005")


if __name__ == "__main__":
    main()
