"""Fixed server-side workload catalog; browser requests never choose file paths."""
from pathlib import Path
import json
import struct

ROOT = Path(__file__).resolve().parents[1]
LARGE_CONFIG = {
    'countA': 200000, 'countB': 200000, 'minSize': 1, 'maxSize': 2,
    'targetSelectivity': 0.000025, 'seed': 42, 'streamingThreshold': 0,
    'reservoirSize': 100000, 'verifySamples': 100000,
    'gridCellSize': 2,
}
CATALOG = {
    'small': {'workloadId': 'small', 'name': 'Small · fixed cubes', 'countA': 1000, 'countB': 1000,
              'selectivityKind': 'exact', 'selectivity': 0.005, 'expectedResultCount': 5000},
    'large': {'workloadId': 'large', 'name': 'Large · random cubes', 'countA': 200000, 'countB': 200000,
              'selectivityKind': 'target', 'selectivity': 0.000025, 'expectedResultCount': None},
}


def validate_prepared(path, count):
    path = Path(path)
    with path.open('rb') as file:
        header = file.read(56)
        if len(header) != 56:
            raise ValueError('Prepared file header is truncated')
        magic, version, vertices, triangles, mappings, edges, total, grid, has_edges = struct.unpack('<IIQQQQQBB6x', header)
        minimum = 56 + vertices * 12 + triangles * 12 + mappings * 4 + edges * 28
        if (magic != 0x52334442 or version != 4 or vertices != count * 8 or triangles != count * 12 or
                mappings != triangles or edges != count * 18 or total != triangles or has_edges != 1 or grid not in (0, 1)):
            raise ValueError('Prepared cube geometry is invalid')
        if grid:
            file.seek(minimum)
            metadata = file.read(8)
            if len(metadata) != 8:
                raise ValueError('Prepared grid is truncated')
            cell_size, cells = struct.unpack('<fI', metadata)
            if not 0 < cell_size < float('inf'):
                raise ValueError('Invalid grid cell size')
            minimum += 8 + cells * 28  # int3 (12) + GridCell (16)
        if path.stat().st_size != minimum:
            raise ValueError('Prepared file size does not match its payload')


def workload_paths(root, workload_id):
    if workload_id not in CATALOG:
        raise ValueError('Unknown workload')
    directory = Path(root) / '.demo' / 'data'
    if workload_id == 'small':
        return directory / 'a.pre', directory / 'b.pre'
    base = (directory / 'large').resolve()
    manifest = json.loads((base / 'current.json').read_text(encoding='utf-8'))
    if manifest['config'] != LARGE_CONFIG:
        raise ValueError('Large preparation settings do not match the catalog')
    paths = tuple((base / manifest['files'][name]['path']).resolve() for name in ('a.pre', 'b.pre'))
    if not all(path.is_relative_to(base) for path in paths):
        raise ValueError('Invalid preparation manifest path')
    for path, name in zip(paths, ('a.pre', 'b.pre')):
        if path.stat().st_size != manifest['files'][name]['sizeBytes']:
            raise ValueError('Large prepared file size changed')
    return paths


def prepared_workload(root, workload_id):
    info = dict(CATALOG[workload_id])
    try:
        paths = workload_paths(root, workload_id)
        for path, count in zip(paths, (info['countA'], info['countB'])):
            validate_prepared(path, count)
        info.update(prepared=True, issues=[])
    except (OSError, ValueError, KeyError, TypeError, struct.error):
        info.update(prepared=False, issues=[f'Prepare this workload with python -m backend.prepare --workload {workload_id}.'])
    return info
