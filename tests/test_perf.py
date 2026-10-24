"""Tests of the performance recorder (standard library only; QGIS is not required).

Run from the repository root with::

    python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fricmaps_plugin.core import perf  # noqa: E402


class FakeClock:
    """A controllable clock, so that durations are exact."""

    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_probe(values):
    """A memory probe that returns the given (current, peak) pairs in order."""
    iterator = iter(values)
    last = [(None, None)]

    def probe():
        try:
            last[0] = next(iterator)
        except StopIteration:
            pass
        return last[0]

    return probe


MIB = 1024 * 1024


class RecorderTests(unittest.TestCase):
    def build(self):
        clock = FakeClock()
        probe = make_probe(
            [
                (200 * MIB, 200 * MIB),  # start
                (300 * MIB, 350 * MIB),
                (250 * MIB, 900 * MIB),
                (260 * MIB, 900 * MIB),  # end
            ]
        )
        rec = perf.PerfRecorder(parameters={"resolution": 5.0}, clock=clock, memory_probe=probe)
        return rec, clock

    def test_stage_durations_and_shares(self):
        rec, clock = self.build()
        clock.advance(2.0)
        rec.mark(10, "Land cover")
        clock.advance(6.0)
        rec.mark(60, "Rasterisation")
        clock.advance(2.0)
        rec.finish()
        stages = rec.stages()
        self.assertEqual([s["name"] for s in stages], ["start", "Land cover", "Rasterisation"])
        self.assertEqual([s["seconds"] for s in stages], [2.0, 6.0, 2.0])
        self.assertAlmostEqual(sum(s["share"] for s in stages), 1.0, places=3)
        self.assertAlmostEqual(rec.total_seconds, 10.0)
        self.assertAlmostEqual(sum(s["seconds"] for s in stages), rec.total_seconds, places=3)

    def test_memory_summary(self):
        rec, clock = self.build()
        rec.mark(10, "a")
        rec.mark(20, "b")
        rec.finish()
        mem = rec.memory_summary()
        self.assertEqual(mem["rss_start_bytes"], 200 * MIB)
        self.assertEqual(mem["peak_rss_bytes"], 900 * MIB)
        self.assertEqual(mem["peak_over_start_bytes"], 700 * MIB)

    def test_finish_is_idempotent_and_records_status(self):
        rec, clock = self.build()
        clock.advance(1.0)
        rec.finish("failed")
        clock.advance(5.0)
        rec.finish("completed")
        self.assertEqual(rec.status, "failed")
        self.assertAlmostEqual(rec.total_seconds, 1.0)

    def test_report_is_valid_json_with_expected_sections(self):
        rec, clock = self.build()
        rec.set_workload(pixels=1234, resolution=5.0)
        rec.mark(10, "Land cover")
        clock.advance(1.0)
        rec.finish()
        with tempfile.TemporaryDirectory() as tmp:
            path = rec.write(os.path.join(tmp, "Performance_test.json"))
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertFalse(os.path.exists(path + ".tmp"))
        self.assertEqual(data["schema_version"], perf.SCHEMA_VERSION)
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["workload"]["pixels"], 1234)
        for key in ("memory", "stages", "environment", "parameters", "total_seconds"):
            self.assertIn(key, data)
        self.assertIn("software", data["environment"])
        self.assertIn("python", data["environment"]["software"])

    def test_failing_probe_never_raises(self):
        def broken():
            raise RuntimeError("no memory info")

        rec = perf.PerfRecorder(memory_probe=broken)
        rec.mark(10, "x")
        rec.finish()
        self.assertIsNone(rec.memory_summary()["peak_rss_bytes"])
        self.assertIn("n/a", rec.format_table())

    def test_format_table_mentions_every_stage(self):
        rec, clock = self.build()
        clock.advance(1.0)
        rec.mark(10, "Processing Land Cover (OCS GE)...")
        clock.advance(1.0)
        rec.finish()
        table = rec.format_table()
        self.assertIn("Processing Land Cover", table)
        self.assertIn("Total (completed)", table)


class ActiveRecorderTests(unittest.TestCase):
    def test_module_functions_are_noops_without_recorder(self):
        self.assertIsNone(perf.active())
        perf.mark(10, "nothing")
        perf.set_workload(pixels=1)

    def test_activate_routes_marks_and_restores(self):
        rec = perf.PerfRecorder()
        with perf.activate(rec):
            self.assertIs(perf.active(), rec)
            perf.mark(10, "stage")
            perf.set_workload(pixels=42)
        self.assertIsNone(perf.active())
        rec.finish()
        self.assertEqual(rec.workload["pixels"], 42)
        self.assertEqual(rec.stages()[-1]["name"], "stage")


class EnvironmentTests(unittest.TestCase):
    def test_environment_is_json_serialisable(self):
        env = perf.environment(tempfile.gettempdir())
        json.dumps(env, default=str)
        self.assertIn("cpu_count_logical", env)
        self.assertEqual(set(env["thread_variables"]), set(perf.THREAD_VARIABLES))

    def test_temp_volume_is_reported(self):
        env = perf.environment(tempfile.gettempdir())
        self.assertEqual(env["temp_volume"]["path"], os.path.abspath(tempfile.gettempdir()))
        self.assertIn("tmpdir_variable", env)

    def test_cpu_model_is_a_name_not_a_model_id(self):
        model = perf.environment()["cpu_model"]
        if model is not None:
            self.assertFalse(model.isdigit(), model)

    @unittest.skipUnless(sys.platform.startswith("linux"), "uses /proc")
    def test_memory_snapshot_on_linux(self):
        current, peak = perf.memory_snapshot()
        self.assertIsInstance(current, int)
        self.assertGreater(current, 0)
        self.assertGreaterEqual(peak, current)

    def test_plugin_version_matches_metadata(self):
        version = perf.plugin_version()
        self.assertIsNotNone(version)
        text = (ROOT / "fricmaps_plugin" / "metadata.txt").read_text(encoding="utf-8")
        self.assertIn("version=" + version, text)


if __name__ == "__main__":
    unittest.main()
