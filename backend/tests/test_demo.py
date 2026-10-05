import copy
import json
from pathlib import Path
import subprocess
import tempfile
import threading
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
        self.runner = PierceRunner(Path(self.temp.name), timeout=0.2)

    def prepare(self):
        self.runner.executable.parent.mkdir(parents=True, exist_ok=True)
        self.runner.executable.touch()
        (self.runner.executable.parent / "mesh_overlap_edges.ptx").touch()
        self.runner.data_dir.mkdir(parents=True)
        for name in ("a.pre", "b.pre"):
            (self.runner.data_dir / name).write_bytes(bytes(100))

    def fake_run(self, command, **kwargs):
        Path(command[command.index("--output") + 1]).write_text(json.dumps(TIMING))
        self.assertTrue(Path(kwargs["cwd"]).is_absolute())
        self.assertEqual(command[command.index("--runs") + 1], "1")
        self.assertEqual(command[command.index("--warmup-runs") + 1], "0")
        self.assertIn("--no-export", command)
        return subprocess.CompletedProcess(command, 0, "Unique object pairs: 1\n", "")

    def test_missing_prerequisites(self):
        self.assertFalse(self.runner.health()["ready"])
        with self.assertRaises(DemoError) as raised:
            self.runner.run()
        self.assertEqual(raised.exception.status_code, 503)
        self.assertFalse(self.runner.lock.locked())

    def test_missing_ptx(self):
        self.prepare()
        self.runner.ptx_path().unlink()
        self.assertIn("ptx", self.runner.health()["issues"][0])

    def test_repeated_runs_have_isolated_output_and_logs(self):
        self.prepare()
        with patch("backend.runner.subprocess.run", side_effect=self.fake_run):
            self.assertEqual(self.runner.run()["resultCount"], 1)
            self.assertEqual(self.runner.run()["resultCount"], 1)
        directories = list(self.runner.runs_dir.iterdir())
        self.assertEqual(len(directories), 2)
        for directory in directories:
            self.assertTrue((directory / "stdout.log").is_file())
            self.assertTrue((directory / "timing.json").is_file())

    def test_child_failure_timeout_and_launch_error_release_lock(self):
        self.prepare()
        outcomes = [(subprocess.CompletedProcess([], 7, "", "GPU failed"), 500),
                    (subprocess.TimeoutExpired("pierce", 0.2), 504), (OSError("missing DLL"), 500)]
        for outcome, status in outcomes:
            kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
            with self.subTest(status=status), patch("backend.runner.subprocess.run", **kwargs):
                with self.assertRaises(DemoError) as raised:
                    self.runner.run()
                self.assertEqual(raised.exception.status_code, status)
                self.assertFalse(self.runner.lock.locked())

    def test_missing_and_malformed_timing_file(self):
        self.prepare()
        def invalid(command, **kwargs):
            Path(kwargs["cwd"], "timing.json").write_text("not JSON")
            return subprocess.CompletedProcess(command, 0, "Unique object pairs: 1", "")
        for kwargs in ({"return_value": subprocess.CompletedProcess([], 0, "", "")}, {"side_effect": invalid}):
            with patch("backend.runner.subprocess.run", **kwargs), self.assertRaises(DemoError):
                self.runner.run()
            self.assertFalse(self.runner.lock.locked())

    def test_health_responsive_and_concurrent_run_rejected(self):
        self.prepare()
        started, finish = threading.Event(), threading.Event()
        responses = []
        def blocking(command, **kwargs):
            started.set()
            if not finish.wait(5):
                raise AssertionError("Test did not release subprocess")
            return self.fake_run(command, **kwargs)
        with patch("backend.main.runner", self.runner), patch("backend.runner.subprocess.run", side_effect=blocking), TestClient(app) as client:
            thread = threading.Thread(target=lambda: responses.append(client.post("/api/run")))
            thread.start()
            try:
                self.assertTrue(started.wait(5))
                health = client.get("/api/health")
                self.assertEqual(health.status_code, 200)
                self.assertTrue(health.json()["running"])
                self.assertEqual(client.post("/api/run").status_code, 409)
            finally:
                finish.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(responses[0].status_code, 200)
            self.assertEqual(responses[0].json()["resultCount"], 1)


if __name__ == "__main__":
    unittest.main()
