"""GPU verification: python -m backend.verify_gpu [--baseline old-small.csv]."""
import argparse
import csv
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

from .catalog import workload_paths
from .runner import DemoError, PierceRunner, ROOT, parse_metrics, pierce_executable


def read_pairs(path):
    with path.open(newline='', encoding='utf-8') as file:
        rows = list(csv.DictReader(file))
    pairs = {(int(row['object_id_mesh1']), int(row['object_id_mesh2'])) for row in rows}
    assert len(pairs) == len(rows), 'Duplicate pairs'
    return pairs


def run_cli(key, directory):
    directory.mkdir(parents=True, exist_ok=True)
    a, b = workload_paths(ROOT, key)
    cli = pierce_executable(ROOT / 'Pierce/pierce', 'query', 'pierce_overlap_two_pass')
    timing = directory / 'cli-timing.json'
    completed = subprocess.run([str(cli), '--mesh1', str(a), '--mesh2', str(b), '--runs', '1',
                                '--warmup-runs', '0', '--output', str(timing)],
                               cwd=directory, capture_output=True, text=True, check=True, timeout=120)
    (directory / 'cli.log').write_text(completed.stdout + completed.stderr, encoding='utf-8')
    metrics = parse_metrics(json.loads(timing.read_text()), completed.stdout)
    result = directory / 'mesh_overlap_results.csv'
    assert len(read_pairs(result)) == metrics['resultCount']
    return result, metrics['resultCount']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, help='Optional small CSV captured before the runtime refactor')
    args = parser.parse_args()
    directory = ROOT / '.demo/verification'
    small_csv, small_count = run_cli('small', directory / 'small')
    expected = {(g * 5 + x, g * 5 + y) for g in range(200) for x in range(5) for y in range(5)}
    assert read_pairs(small_csv) == expected and small_count == 5000
    if args.baseline: assert read_pairs(args.baseline.resolve()) == expected
    a, b = workload_paths(ROOT, 'small')
    verify = pierce_executable(ROOT / 'Pierce/pierce', 'query', 'pierce_runtime_verify')
    completed = subprocess.run([str(verify), str(a), str(b), str(small_csv), str(directory / 'zero.pre')],
                               cwd=directory, capture_output=True, text=True, timeout=120)
    (directory / 'runtime.log').write_text(completed.stdout + completed.stderr, encoding='utf-8')
    completed.check_returncode()
    runner = PierceRunner()
    multi = runner.workload('large')['prepared']
    large_count = None
    if multi:
        large_csv, large_count = run_cli('large', directory / 'large')
        la, lb = workload_paths(ROOT, 'large')
        verify = pierce_executable(ROOT / 'Pierce/pierce', 'query', 'pierce_multi_verify')
        completed = subprocess.run([str(verify), str(a), str(b), str(la), str(lb), str(small_csv), str(large_csv)],
                                   cwd=directory, capture_output=True, text=True, timeout=300)
        (directory / 'multi-runtime.log').write_text(completed.stdout + completed.stderr, encoding='utf-8')
        completed.check_returncode()
    # Exercise the real FastAPI routes and worker together without a browser.
    from fastapi.testclient import TestClient
    from .main import app
    snapshots = []
    with patch('backend.main.runner', runner), TestClient(app) as client:
        def post(action, key):
            response = client.post('/api/' + action, json={'workloadId': key})
            assert response.status_code == 200, response.text
            return response.json()
        post('load', 'small')
        if multi: post('load', 'large')
        process = runner.process
        handles = {key: value['pairHandle'] for key, value in runner.resident.items()}
        snapshots.append(client.get('/api/health').json())
        for i in range(100):
            key = 'large' if multi and i % 2 else 'small'
            result = post('run', key)
            assert result['resultCount'] == (large_count if key == 'large' else 5000)
            phases = result['phaseTimingsMs']
            assert set(phases) == {'estimation', 'trace', 'compaction', 'download'}
            assert result['estimatedPairCount'] > 0 and result['hashTableBytes'] > 0
            assert result['rawHitCount'] == (12191778 if key == 'large' else 70000)
            assert 0 < sum(phases.values()) <= result['queryExecutionMs'] + 0.001
            assert abs(sum(phases.values()) - result['benchmarkPhaseSumMs']) < 0.001
            assert runner.resident[key]['pairHandle'] == handles[key]
            assert runner.process is process
            if i in (1, 3, 99): snapshots.append(client.get('/api/health').json())
        status = runner._exchange('status')
        expected_loads = 4 if multi else 2
        assert [status[k] for k in ('datasetReads', 'datasetUploads', 'indexBuilds', 'pipelineCreations')] == [expected_loads] * 3 + [1]
        assert status['completedQueries'] == 100
        records = {w['workloadId']: w for w in client.get('/api/workloads').json()}
        assert records['small']['lastResult']['resultCount'] == 5000
        if multi: assert records['large']['completedQueries'] == records['small']['completedQueries'] == 50
        for command, fields in (('unknown', {}), ('query', {'pairHandle': 'invalid'})):
            try: runner._exchange(command, **fields)
            except DemoError: pass
            else: raise AssertionError('Invalid command or handle accepted')
        key = 'large' if multi else 'small'
        post('unload', key)
        assert not runner.workload(key)['loaded']
        if multi: assert post('run', 'small')['resultCount'] == 5000
        post('load', key)
        assert runner.resident[key]['pairHandle'] != handles[key]
        assert post('run', key)['resultCount'] == (large_count if multi else 5000)
        post('unload', key)
        if multi: post('unload', 'small')
        snapshots.append(client.get('/api/health').json())
        assert runner._exchange('status')['datasetGpuBytes'] == 0
        # Zero-result geometry was created by the native small-runtime verifier.
        data = runner._exchange('load', key='small', mesh1=str(a), mesh2=str(directory / 'zero.pre'))
        handle = data['pairs'][0]['pairHandle']
        data = runner._exchange('query', pairHandle=handle)
        assert data['pairs'][0]['lastResult']['resultCount'] == 0
        report = {'smallResultCount': small_count, 'largeResultCount': large_count,
                  'largeActualSelectivity': large_count / 40000000000 if multi else None,
                  'statusAfter100Queries': status, 'memorySnapshots': snapshots}
        (directory / 'workload-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    assert process.returncode == 0
    completed = subprocess.run([str(runner.executable)], input='not json\n{"version":3,"id":"end","command":"shutdown"}\n',
                               capture_output=True, text=True, check=True, timeout=10)
    malformed, shutdown = map(json.loads, completed.stdout.splitlines())
    assert malformed['ok'] is False and shutdown['ok'] is True
    print(f'PASS: small={small_count:,} pairs; large={large_count if multi else "not prepared"}; CLI/full-pair runtime equality.')
    print('PASS: 100 API/worker queries, lifecycle counters, resident switching, reload, zero results, protocol errors, clean shutdown.')
    print(f'Logs and memory snapshots: {directory}')


if __name__ == '__main__': main()
