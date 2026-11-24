import copy
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import sys
import time
import struct
from backend.catalog import CATALOG, LARGE_CONFIG, prepared_workload, validate_prepared
from backend.prepare import prepare_large
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.main import app
from backend.runner import DemoError, PierceRunner, parse_metrics
from backend.prepare import generate_demo_cubes


# Mirrors native JSON; the nested query phase must not be added twice.
TIMING = {"total": {"duration_us": 100000}, "phases": {
    "query_1": {"duration_us": 99000},
    "raytrace_mesh1tomesh2_pass1_1": {"duration_us": 111},
    "raytrace_mesh2tomesh1_pass1_1": {"duration_us": 222},
    "raytrace_mesh1tomesh2_pass2_1": {"duration_us": 333},
    "raytrace_mesh2tomesh1_pass2_1": {"duration_us": 444},
    "gpu deduplication_1": {"duration_us": 55},
    "download results_1": {"duration_us": 1},
}}


class DatasetTests(unittest.TestCase):
    def test_exact_size_and_selectivity(self):
        datasets = generate_demo_cubes()
        self.assertEqual(len(datasets["a"]), 1000)
        self.assertEqual(len(datasets["b"]), 1000)
        matches = 0
        for _, a, _ in datasets["a"]:
            for _, b, _ in datasets["b"]:
                if not all(a[0][d] < b[6][d] and b[0][d] < a[6][d] for d in range(3)):
                    continue
                a_contains_b = all(a[0][d] <= b[0][d] and a[6][d] >= b[6][d] for d in range(3))
                b_contains_a = all(b[0][d] <= a[0][d] and b[6][d] >= a[6][d] for d in range(3))
                if not (a_contains_b or b_contains_a):
                    matches += 1
        self.assertEqual(matches, 5000)
        self.assertEqual(matches / (len(datasets["a"]) * len(datasets["b"])), 0.005)


class MetricsTests(unittest.TestCase):
    def test_microseconds_and_benchmark_definition(self):
        self.assertEqual(parse_metrics(TIMING, "Unique object pairs: 1\n"),
                         {"overallTimeMs": 100, "queryTimeMs": 1.166, "resultCount": 1})

    def test_zero_results_and_submillisecond_phases(self):
        data = copy.deepcopy(TIMING)
        for phase in data["phases"].values():
            phase["duration_us"] = 0
        self.assertEqual(parse_metrics(data, "Unique object pairs: 0\r\n")["resultCount"], 0)
        self.assertEqual(parse_metrics(data, "Unique object pairs: 0\n")["queryTimeMs"], 0)

    def test_missing_invalid_or_negative_measurements(self):
        for value in (None, {}, {"duration_us": "100"}, {"duration_us": -1},
                      {"duration_us": True}, {"duration_us": float("nan")},
                      {"duration_us": float("inf")}):
            with self.subTest(value=value), self.assertRaises(DemoError):
                parse_metrics({**TIMING, "total": value}, "Unique object pairs: 1\n")

    def test_missing_phases_count_and_duplicate_summary(self):
        for data, stdout in (({"total": TIMING["total"], "phases": {}}, "Unique object pairs: 1"),
                             (TIMING, "no summary"),
                             (TIMING, "Unique object pairs: 1\nUnique object pairs: 2\n"),
                             ([], "Unique object pairs: 1")):
            with self.subTest(stdout=stdout), self.assertRaises(DemoError):
                parse_metrics(data, stdout)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runner = PierceRunner(Path(self.temp.name), timeout=2)
        self.addCleanup(self.runner.close)
        self.fixture = Path(__file__).with_name("worker_fixture.py")
        self.real_popen = subprocess.Popen

    def prepare(self):
        self.runner.executable.parent.mkdir(parents=True, exist_ok=True)
        self.runner.executable.touch()
        (self.runner.executable.parent / "mesh_overlap_edges.ptx").touch()
        self.runner.data_dir.mkdir(parents=True)
        for name in ("a.pre", "b.pre"):
            path = self.runner.data_dir / name
            header = struct.pack('<IIQQQQQBB6x', 0x52334442, 4, 8000, 12000, 12000, 18000, 12000, 0, 1)
            with path.open('wb') as file:
                file.write(header)
                file.truncate(56 + 792000)

    def prepare_large_stub(self):
        base = self.runner.data_dir / 'large'
        base.mkdir()
        records = {}
        for name in ('a.pre', 'b.pre'):
            path = base / name
            path.write_bytes(bytes(100))
            records[name] = {'path': name, 'sizeBytes': 100}
        (base / 'current.json').write_text(json.dumps({'config': LARGE_CONFIG, 'files': records}))
        original = prepared_workload
        def prepared(root, key):
            if key == 'large': return {**CATALOG[key], 'prepared': True, 'issues': []}
            return original(root, key)
        patcher = patch('backend.runner.prepared_workload', side_effect=prepared)
        patcher.start()
        self.addCleanup(patcher.stop)

    def worker(self, mode="normal"):
        def spawn(command, **kwargs):
            self.assertEqual(Path(command[0]).name, self.runner.executable.name)
            return self.real_popen([sys.executable, str(self.fixture), mode], **kwargs)
        return patch("backend.runner.subprocess.Popen", side_effect=spawn)

    def test_missing_prerequisites_and_unloaded_query(self):
        self.assertFalse(self.runner.health()["ready"])
        with self.assertRaises(DemoError) as error: self.runner.load('small')
        self.assertEqual(error.exception.status_code, 503)
        with self.assertRaises(DemoError) as error: self.runner.run('small')
        self.assertEqual(error.exception.status_code, 409)
        self.assertFalse(self.runner.lock.locked())
        self.assertFalse(self.runner.unload('small')['loaded'])
        self.prepare()
        self.assertTrue(self.runner.health()['ready'])
        self.assertFalse(self.runner.workload('large')['prepared'])
        with self.worker():
            self.assertTrue(self.runner.load('small')['loaded'])
            with self.assertRaises(DemoError) as error: self.runner.load('large')
            self.assertEqual(error.exception.status_code, 503)
            self.assertEqual(self.runner.run('small')['resultCount'], 5000)

    def test_missing_ptx(self):
        self.prepare()
        self.runner.ptx_path().unlink()
        self.assertIn("ptx", self.runner.health()["issues"][0])

    def test_registry_reuse_isolation_metrics_and_logs(self):
        self.prepare(); self.prepare_large_stub()
        with self.worker() as spawn:
            self.runner.load('small'); self.runner.load('large')
            process = self.runner.process
            handles = {key: pair['pairHandle'] for key, pair in self.runner.resident.items()}
            self.runner.load('small')
            for _ in range(3):
                self.assertEqual(self.runner.run('small')['actualSelectivity'], 0.005)
                self.assertEqual(self.runner.run('large')['actualSelectivity'], 0.000025)
            self.assertEqual(self.runner.workload('small')['completedQueries'], 3)
            self.assertEqual(self.runner.workload('large')['completedQueries'], 3)
            self.assertEqual(self.runner.workload('small')['lastResult']['resultCount'], 5000)
            self.assertEqual(self.runner.workload('large')['lastResult']['resultCount'], 1000000)
            self.assertEqual(self.runner.workload('small')['lastResult']['phaseTimingsMs'], {
                'estimation': 0.5, 'trace': 1.2, 'compaction': 0.25, 'download': 0.05,
            })
            self.assertEqual({key: pair['pairHandle'] for key, pair in self.runner.resident.items()}, handles)
            self.runner.unload('large'); self.runner.unload('large')
            self.assertTrue(self.runner.workload('small')['loaded'])
            self.assertIsNone(self.runner.workload('large')['lastResult'])
            self.runner.load('large')
            self.assertNotEqual(handles['large'], self.runner.resident['large']['pairHandle'])
            self.assertEqual(self.runner.workload('small')['completedQueries'], 3)
            self.assertEqual(self.runner.workload('large')['completedQueries'], 0)
            self.assertIs(self.runner.process, process)
            self.runner.close()
            self.assertEqual(process.returncode, 0)
            self.assertEqual(spawn.call_count, 1)
        session, = self.runner.runs_dir.iterdir()
        requests = [json.loads(line) for line in (session / 'requests.jsonl').read_text().splitlines()]
        self.assertEqual(sum(r['request']['command'] == 'load' for r in requests), 3)
        self.assertTrue(all(r['request']['version'] == 3 and r['backendOperationMs'] >= 0 for r in requests))
        self.assertTrue((session / 'operations.jsonl').is_file())

    def test_diagnostics_pipe_is_drained(self):
        self.prepare()
        with self.worker('diagnostics'):
            self.runner.load('small'); self.runner.run('small')
            session = self.runner.session_dir
            self.runner.close()
        self.assertGreater((session / 'stderr.log').stat().st_size, 65536)

    def test_zero_result(self):
        self.prepare()
        with self.worker('zero'):
            self.runner.load('small')
            self.assertEqual(self.runner.run('small')['resultCount'], 0)

    def test_older_worker_is_rejected(self):
        self.prepare()
        with self.worker('old_worker'):
            with self.assertRaises(DemoError):
                self.runner.load('small')
            self.assertFalse(self.runner.workload('small')['loaded'])

    def test_failure_invalidates_all_pairs_restart_and_lock_release(self):
        self.prepare(); self.prepare_large_stub()
        for mode, status in (('timeout', 504), ('crash', 500), ('malformed', 500), ('wrong_id', 500),
                             ('negative', 500), ('negative_phase', 500)):
            with self.subTest(mode=mode):
                self.runner.close()
                self.runner.timeout = 0.2 if mode == 'timeout' else 2
                with self.worker(mode):
                    self.runner.load('small'); self.runner.load('large')
                    process = self.runner.process
                    with self.assertRaises(DemoError) as error: self.runner.run('large')
                    self.assertEqual(error.exception.status_code, status)
                    self.assertEqual(self.runner.resident, {})
                    self.assertFalse(self.runner.lock.locked())
                    self.assertIsNotNone(process.poll())
                with self.worker():
                    self.runner.load('small')
                    self.assertEqual(self.runner.run('small')['resultCount'], 5000)

    def test_memory_budget_preserves_all_pairs_and_maps_to_422(self):
        self.prepare(); self.prepare_large_stub()
        with self.worker('budget'), patch('backend.main.runner', self.runner), TestClient(app) as client:
            self.runner.load('small'); self.runner.load('large')
            process = self.runner.process
            response = client.post('/api/run', json={'workloadId': 'large'})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()['detail']['code'], 'memory_budget_exceeded')
            self.assertGreater(response.json()['detail']['requiredBytes'], response.json()['detail']['availableBytes'])
            self.assertEqual(set(self.runner.resident), {'small', 'large'})
            self.assertIs(self.runner.process, process)
            self.assertIsNone(process.poll())
            self.assertFalse(self.runner.lock.locked())

    def test_load_error_launch_error_idle_crash(self):
        self.prepare()
        with self.worker('load_error'):
            with self.assertRaises(DemoError): self.runner.load('small')
            self.assertEqual(self.runner.resident, {})
        self.runner.close()
        with patch('backend.runner.subprocess.Popen', side_effect=OSError('missing DLL')):
            with self.assertRaises(DemoError): self.runner.load('small')
        with self.worker():
            self.runner.load('small')
            self.runner.process.kill(); self.runner.process.wait()
            self.assertFalse(self.runner.health()['workerAvailable'])
            self.assertEqual(self.runner.resident, {})
            self.runner.load('small')
            self.assertTrue(self.runner.workload('small')['loaded'])

    def test_invalid_unload_invalidates_worker(self):
        self.prepare()
        with self.worker('bad_unload'):
            self.runner.load('small')
            process = self.runner.process
            with self.assertRaises(DemoError): self.runner.unload('small')
            self.assertEqual(self.runner.resident, {})
            self.assertIsNotNone(process.poll())

    def test_hung_shutdown_is_bounded_and_reaped(self):
        self.prepare(); self.runner.timeout = 0.2
        with self.worker('hung_shutdown'):
            self.runner.load('small')
            process = self.runner.process
            start = time.monotonic(); self.runner.close()
            self.assertLess(time.monotonic() - start, 5)
            self.assertIsNotNone(process.poll())

    def test_api_routing_validation_concurrency_and_refresh(self):
        self.prepare(); self.prepare_large_stub()
        responses = []
        with patch('backend.main.runner', self.runner), self.worker('slow'), TestClient(app) as client:
            for body in (None, {}, {'workloadId': 'other'}, {'workloadId': 'small', 'mesh1': 'x'}):
                response = client.post('/api/load', json=body)
                self.assertEqual(response.status_code, 422)
            self.assertEqual(client.post('/api/run', json={'workloadId': 'small'}).status_code, 409)
            for key in CATALOG:
                self.assertEqual(client.post('/api/load', json={'workloadId': key}).status_code, 200)
            thread = threading.Thread(target=lambda: responses.append(client.post('/api/run', json={'workloadId': 'large'})))
            thread.start()
            deadline = time.monotonic() + 2
            while self.runner.operation != 'query' and time.monotonic() < deadline: time.sleep(.005)
            try:
                health = client.get('/api/health').json()
                self.assertTrue(health['running']); self.assertEqual(health['operationWorkload'], 'large')
                self.assertEqual(len(client.get('/api/workloads').json()), 2)
                for endpoint in ('run', 'load', 'unload'):
                    self.assertEqual(client.post('/api/' + endpoint, json={'workloadId': 'small'}).status_code, 409)
            finally: thread.join(5)
            self.assertEqual(responses[0].status_code, 200)
            self.assertEqual(responses[0].json()['workloadId'], 'large')
            records = {w['workloadId']: w for w in client.get('/api/workloads').json()}
            self.assertEqual(records['small']['completedQueries'], 0)
            self.assertEqual(records['large']['lastResult']['resultCount'], 1000000)


class PreparationTests(unittest.TestCase):
    def test_invalid_prepared_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.pre'
            path.write_bytes(bytes(100))
            with self.assertRaises(ValueError): validate_prepared(path, 1000)

    def test_failed_generation_preserves_current_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / '.demo/data/large'; base.mkdir(parents=True)
            current = base / 'current.json'
            current.write_text('{"previous": true}')
            generator = root / 'Pierce/pierce/scripts/generate_cubes_by_selectivity.py'
            generator.parent.mkdir(parents=True); generator.write_text('# fixture')
            executable = root / 'preprocess.exe'; executable.write_bytes(b'fixture')
            with patch('backend.prepare.run_step', side_effect=RuntimeError('generation failed')):
                with self.assertRaises(RuntimeError): prepare_large(root, executable)
            self.assertEqual(json.loads(current.read_text()), {'previous': True})

    def test_complete_preparation_reuse_and_failure_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / '.demo/data/large'; base.mkdir(parents=True)
            generator = root / 'Pierce/pierce/scripts/generate_cubes_by_selectivity.py'
            generator.parent.mkdir(parents=True); generator.write_text('# fixture')
            executable = root / 'preprocess.exe'; executable.write_bytes(b'fixture')
            def step(command, destination, name):
                if name == 'generation.log':
                    for option in ('--output-a', '--output-b'):
                        Path(command[command.index(option) + 1]).write_bytes(b'obj fixture')
                else:
                    Path(command[command.index('--output-geometry') + 1]).write_bytes(b'prepared fixture')
            with patch('backend.prepare.run_step', side_effect=step) as run, \
                 patch('backend.prepare.validate_prepared'), \
                 patch('backend.prepare.subprocess.check_output', side_effect=lambda command, **kwargs: 'revision' if 'rev-parse' in command else ''):
                prepare_large(root, executable)
                first = (base / 'current.json').read_text()
                self.assertEqual(run.call_count, 3)
                prepare_large(root, executable)
                self.assertEqual(run.call_count, 3)
                manifest = json.loads(first)
                self.assertEqual(manifest['config'], LARGE_CONFIG)
                self.assertEqual(manifest['engineRevision'], 'revision')
                # Force regeneration through changed generator provenance, then
                # fail the second preprocessing step: old manifest stays valid.
                generator.write_text('# changed fixture')
                def fail_second(command, destination, name):
                    if name == 'b-preprocess.log': raise RuntimeError('preprocess failed')
                    step(command, destination, name)
                run.side_effect = fail_second
                with self.assertRaises(RuntimeError): prepare_large(root, executable)
                self.assertEqual((base / 'current.json').read_text(), first)
                for record in manifest['files'].values():
                    self.assertTrue((base / record['path']).is_file())

    def test_manifest_path_cannot_escape_data_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / '.demo/data/large'; base.mkdir(parents=True)
            records = {name: {'path': '../../escape.pre', 'sizeBytes': 100} for name in ('a.pre','b.pre')}
            (base / 'current.json').write_text(json.dumps({'config': LARGE_CONFIG, 'files': records}))
            self.assertFalse(prepared_workload(Path(directory), 'large')['prepared'])


if __name__ == '__main__': unittest.main()
