"""Run once with: python -m backend.prepare (in pierce_windows_build)."""

import subprocess
import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path
from .catalog import LARGE_CONFIG, validate_prepared

from .runner import ROOT, pierce_executable
from pierce.scripts.generate_test_cubes import generate_cube_faces, generate_cube_vertices, write_obj_file


def generate_demo_cubes():
    """200 separated groups Ã— (5 A cubes Ã— 5 B cubes) = exactly 5,000 pairs."""
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


def digest(path):
    with Path(path).open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def run_step(command, directory, log_name):
    log = directory / log_name
    try:
        with log.open('w', encoding='utf-8') as output:
            subprocess.run([str(arg) for arg in command], cwd=directory, stdout=output,
                           stderr=subprocess.STDOUT, check=True, timeout=1800)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f'Preparation failed: {error}. See {log}') from error


def preprocess(executable, source, target, directory, name):
    run_step([executable, '--dataset', source, '--output-geometry', target,
              '--output-timing', directory / f'{name}-preprocess.json',
              '--generate-grid', '--grid-cell-size', '2'], directory, f'{name}-preprocess.log')


def prepare_small(root, executable):
    directory = root / '.demo/data'
    directory.mkdir(parents=True, exist_ok=True)
    for name, cubes in generate_demo_cubes().items():
        source = directory / f'{name}.obj'
        write_obj_file(source, cubes)
        pending = directory / f'{name}.pending.pre'
        preprocess(executable, source, pending, directory, name)
        validate_prepared(pending, 1000)
        pending.replace(directory / f'{name}.pre')
    print('Small ready: 1,000 x 1,000 cubes; exactly 5,000 pairs.')


def prepare_large(root, executable):
    base = root / '.demo/data/large'
    base.mkdir(parents=True, exist_ok=True)
    generator = root / 'Pierce/pierce/scripts/generate_cubes_by_selectivity.py'
    provenance = {'generatorSha256': digest(generator), 'preprocessorSha256': digest(executable)}
    current = base / 'current.json'
    if current.is_file():
        try:
            existing = json.loads(current.read_text(encoding='utf-8'))
            if existing['config'] == LARGE_CONFIG and existing['provenance'] == provenance:
                for name, count in (('a.pre', 200000), ('b.pre', 200000)):
                    file = (base / existing['files'][name]['path']).resolve()
                    if not file.is_relative_to(base.resolve()):
                        raise ValueError('Manifest path escapes preparation directory')
                    validate_prepared(file, count)
                for name, record in existing['files'].items():
                    file = (base / record['path']).resolve()
                    if not file.is_relative_to(base.resolve()) or digest(file) != record['sha256']:
                        raise ValueError('Prepared input hash changed')
                print('Reusing complete large preparation.')
                return
        except (OSError, ValueError, KeyError, TypeError):
            pass
    generation = base / ('generation-' + uuid.uuid4().hex)
    generation.mkdir()
    print(f'Generating large cubes in {generation}', flush=True)
    run_step([sys.executable, generator, '--num-cubes-a', '200000', '--num-cubes-b', '200000',
              '--min-size', '1', '--max-size', '2', '--selectivity', '0.000025', '--seed', '42',
              '--streaming-threshold', '0', '--reservoir-size', '100000', '--verify-samples', '100000',
              '--output-a', generation / 'a.obj', '--output-b', generation / 'b.obj'], generation, 'generation.log')
    for name in ('a', 'b'):
        print(f'Preprocessing large collection {name.upper()}...', flush=True)
        preprocess(executable, generation / f'{name}.obj', generation / f'{name}.pre', generation, name)
        validate_prepared(generation / f'{name}.pre', 200000)
    revision = subprocess.check_output(['git', '-C', str(root / 'Pierce'), 'rev-parse', 'HEAD'], text=True).strip()
    dirty = bool(subprocess.check_output(['git', '-C', str(root / 'Pierce'), 'status', '--porcelain'], text=True).strip())
    files = {}
    for name in ('a.obj', 'b.obj', 'a.pre', 'b.pre'):
        file = generation / name
        files[name] = {'path': file.relative_to(base).as_posix(), 'sizeBytes': file.stat().st_size, 'sha256': digest(file)}
    manifest = {'config': LARGE_CONFIG, 'provenance': provenance, 'engineRevision': revision,
                'engineDirty': dirty, 'files': files, 'preprocessing': {'generateGrid': True, 'gridCellSize': 2}}
    text = json.dumps(manifest, indent=2)
    (generation / 'manifest.json').write_text(text, encoding='utf-8')
    pending = base / ('current-' + uuid.uuid4().hex + '.json')
    pending.write_text(text, encoding='utf-8')
    pending.replace(current)
    print('Large ready: 200,000 x 200,000 random cubes; target selectivity 0.000025 (actual count measured by query).')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workload', choices=['small', 'large', 'all'], default='small')
    args = parser.parse_args()
    executable = pierce_executable(ROOT / 'Pierce/pierce', 'preprocess', 'pierce_preprocess')
    if not executable.is_file():
        raise SystemExit('Preprocessor missing. Run .\\Pierce\\build_windows.ps1 first.')
    try:
        if args.workload in ('small', 'all'): prepare_small(ROOT, executable)
        if args.workload in ('large', 'all'): prepare_large(ROOT, executable)
    except (RuntimeError, OSError, ValueError) as error:
        raise SystemExit(str(error)) from error


if __name__ == '__main__':
    main()
