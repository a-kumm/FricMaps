"""Tests of tools/benchmark.py and tools/compare_rasters.py.

The raster and study-area tests need GDAL and NumPy (available in any QGIS Python) and are
skipped when they cannot be imported. Run from the repository root with::

    python -m unittest discover -s tests -v
"""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

try:
    import numpy as np
    from osgeo import gdal, ogr, osr

    gdal.UseExceptions()
    ogr.UseExceptions()
    HAVE_GDAL = True
except ImportError:  # pragma: no cover - depends on the interpreter
    HAVE_GDAL = False


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


benchmark = load_tool("benchmark")
compare = load_tool("compare_rasters")


def write_raster(path, array, dtype=None):
    dtype = dtype or gdal.GDT_UInt16
    rows, cols = array.shape
    ds = gdal.GetDriverByName("GTiff").Create(str(path), cols, rows, 1, dtype)
    ds.SetGeoTransform([900000.0, 5.0, 0.0, 6804000.0, 0.0, -5.0])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(2154)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(array)
    ds = None


@unittest.skipUnless(HAVE_GDAL, "GDAL and NumPy are required")
class CompareRastersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        rng = np.random.default_rng(0)
        self.base = rng.choice([0, 1, 36, 39, 40], size=(300, 200)).astype(np.uint16)

    def tearDown(self):
        self.tmp.cleanup()

    def test_identical_rasters(self):
        write_raster(self.dir / "a.tif", self.base)
        write_raster(self.dir / "b.tif", self.base)
        res = compare.compare_pair(str(self.dir / "a.tif"), str(self.dir / "b.tif"))
        self.assertTrue(res["identical"])
        self.assertEqual(res["pixels_different"], 0)
        self.assertEqual(res["percent_identical"], 100.0)

    def test_differences_are_counted_across_strips(self):
        other = self.base.copy()
        other[10, 10:20] = 99  # first strip
        other[250, 0:5] = 98  # last strip
        write_raster(self.dir / "a.tif", self.base)
        write_raster(self.dir / "b.tif", other)
        res = compare.compare_pair(str(self.dir / "a.tif"), str(self.dir / "b.tif"), strip_rows=64)
        expected = int((other != self.base).sum())
        self.assertFalse(res["identical"])
        self.assertEqual(res["pixels_different"], expected)
        self.assertEqual(res["max_abs_diff"], float(np.abs(other.astype(int) - self.base).max()))
        self.assertEqual(res["values_only_in_b"], [98, 99])

    def test_transitions_between_classes(self):
        other = self.base.copy()
        other[other == 39] = 40  # class 39 disappears into class 40
        write_raster(self.dir / "a.tif", self.base)
        write_raster(self.dir / "b.tif", other)
        res = compare.compare_pair(str(self.dir / "a.tif"), str(self.dir / "b.tif"))
        self.assertEqual(res["values_only_in_a"], [39])
        top = res["top_transitions_a_to_b"][0]
        self.assertEqual((top["a"], top["b"]), (39, 40))
        self.assertEqual(top["pixels"], int((self.base == 39).sum()))

    def test_crop_compares_the_common_window_of_a_larger_grid(self):
        larger = np.zeros((self.base.shape[0] + 1, self.base.shape[1] + 1), dtype=np.uint16)
        larger[:-1, :-1] = self.base
        larger[-1, :] = 7  # the extra row and column hold something else
        larger[:, -1] = 7
        write_raster(self.dir / "a.tif", self.base)
        write_raster(self.dir / "b.tif", larger)
        a, b = str(self.dir / "a.tif"), str(self.dir / "b.tif")
        self.assertFalse(compare.compare_pair(a, b)["comparable"])
        res = compare.compare_pair(a, b, crop=True)
        self.assertTrue(res["comparable"])
        self.assertTrue(res["identical_values"])
        self.assertFalse(res["identical"])  # the grids are not the same size
        self.assertEqual(res["compared_window"], {"cols": 200, "rows": 300})

    def test_ignore_dtype(self):
        write_raster(self.dir / "a.tif", self.base)
        write_raster(self.dir / "b.tif", self.base.astype(np.float32), gdal.GDT_Float32)
        a, b = str(self.dir / "a.tif"), str(self.dir / "b.tif")
        self.assertFalse(compare.compare_pair(a, b)["identical"])
        res = compare.compare_pair(a, b, ignore_dtype=True)
        self.assertTrue(res["identical"])
        self.assertEqual(res["pixels_different"], 0)

    def test_grid_size_mismatch_is_not_comparable(self):
        write_raster(self.dir / "a.tif", self.base)
        write_raster(self.dir / "b.tif", self.base[:100])
        res = compare.compare_pair(str(self.dir / "a.tif"), str(self.dir / "b.tif"))
        self.assertFalse(res["comparable"])

    def test_folders_are_matched_by_name_and_exit_status(self):
        a, b = self.dir / "A", self.dir / "B"
        a.mkdir()
        b.mkdir()
        write_raster(a / "Land_Cover_x.tif", self.base)
        write_raster(b / "Land_Cover_x.tif", self.base)
        write_raster(a / "only_a.tif", self.base)
        pairs, unmatched = compare.pairs_from_folders(str(a), str(b))
        self.assertEqual(len(pairs), 1)
        self.assertEqual(unmatched, ["only in A: only_a.tif"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(compare.main([str(a), str(b)]), 1)  # unmatched file -> not equal
            self.assertEqual(
                compare.main(["--pair", str(a / "Land_Cover_x.tif"), str(b / "Land_Cover_x.tif")]),
                0,
            )


class BenchmarkTests(unittest.TestCase):
    def test_build_matrix_runs_reference_once(self):
        matrix = benchmark.build_matrix([2, 5, 10], [0, 5000, 10000], 5, 5000)
        self.assertEqual(matrix, [(2, 5000), (5, 5000), (10, 5000), (5, 0), (5, 10000)])

    def test_aggregate_statistics_and_failures(self):
        def run(repeat, seconds, status="completed", peak=2 * benchmark.GIB):
            return {
                "resolution_m": 5.0,
                "buffer_m": 5000.0,
                "repeat": repeat,
                "status": status,
                "pipeline_seconds": seconds,
                "peak_rss_bytes": peak,
                "child_peak_rss_bytes": None if peak is None else peak + 1,
                "workload": {"grid_pixels": 1_000_000, "valid_pixels": 700_000},
            }

        runs = [run(0, 12.0), run(1, 10.0), run(2, 11.0), run(3, None, "failed", None)]
        (row,) = benchmark.aggregate(runs)
        self.assertEqual((row["n_requested"], row["n_completed"], row["n_failed"]), (4, 3, 1))
        self.assertEqual(row["pipeline_s_median"], 11.0)
        self.assertAlmostEqual(row["pipeline_s_mean"], 11.0)
        self.assertAlmostEqual(row["pipeline_s_sd"], 1.0)
        self.assertEqual((row["pipeline_s_min"], row["pipeline_s_max"]), (10.0, 12.0))
        self.assertEqual(row["first_run_seconds"], 12.0)
        self.assertEqual(row["failure_statuses"], ["failed"])
        self.assertEqual(row["grid_pixels"], 1_000_000)
        self.assertEqual(row["valid_pixels"], 700_000)

    def test_stage_medians_ignore_failed_runs(self):
        runs = [
            {"resolution_m": 5, "buffer_m": 0, "status": "completed", "stages": {"a": 1.0}},
            {"resolution_m": 5, "buffer_m": 0, "status": "completed", "stages": {"a": 3.0}},
            {"resolution_m": 5, "buffer_m": 0, "status": "failed", "stages": {"a": 100.0}},
        ]
        self.assertEqual(benchmark.stage_medians(runs), {"a": {(5, 0): 2.0}})

    def test_latex_table(self):
        rows = [
            {
                "resolution_m": 5.0,
                "buffer_m": 5000.0,
                "grid_pixels": 124_100_000,
                "valid_pixels": 84_700_000,
                "n_requested": 5,
                "n_completed": 5,
                "pipeline_s_median": 528.0,
                "pipeline_s_min": 520.0,
                "pipeline_s_max": 541.0,
                "peak_rss_bytes_max": 3 * benchmark.GIB,
            }
        ]
        text = benchmark.latex_table(rows)
        self.assertIn("5 & 5 & 124.1 & 84.7 & 5/5 & 528.0 [520.0--541.0] & 3.00", text)
        self.assertTrue(text.startswith(r"\begin{tabular}"))

    def test_qgis_process_command(self):
        params = {"EPCI_FILE": "z.gpkg", "RESOLUTION": 5.0, "VERIFY_DATA": False, "TABLE_CSV": None}
        cmd = benchmark.qgis_process_command("qgis_process", params, "/tmp/run")
        self.assertEqual(cmd[:4], ["qgis_process", "run", "fricmaps:build_surfaces", "--"])
        self.assertIn("VERIFY_DATA=false", cmd)
        self.assertIn("OUTPUT_DIR=/tmp/run", cmd)
        self.assertFalse(any(c.startswith("TABLE_CSV") for c in cmd))

    def test_gui_config_is_mapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "epci_file": "zone.gpkg",
                        "name_field": "nom",
                        "area_name": "Zone",
                        "base_dir": "/data",
                        "buffer_dist": 5000,
                        "resolution": 5,
                        "weighting_rules": [{"type": "slope"}],
                        "custom_sources": [],
                    },
                    fh,
                )
            mapped = benchmark.load_gui_config(path)
        self.assertEqual(mapped["study_area"], "zone.gpkg")
        self.assertEqual(mapped["default_resolution"], 5)
        self.assertEqual(json.loads(mapped["weighting_rules"]), [{"type": "slope"}])


@unittest.skipUnless(HAVE_GDAL, "GDAL is required")
class StudyAreaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "zone.gpkg")
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(2154)
        ds = ogr.GetDriverByName("GPKG").CreateDataSource(self.path)
        layer = ds.CreateLayer("zone", srs, ogr.wkbPolygon)
        layer.CreateField(ogr.FieldDefn("nom", ogr.OFTString))
        feature = ogr.Feature(layer.GetLayerDefn())
        feature["nom"] = "Zone Test"
        wkt = (
            "POLYGON((900000 6800000,905000 6800000,905000 6804000,900000 6804000,900000 6800000))"
        )
        feature.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
        layer.CreateFeature(feature)
        ds = None

    def tearDown(self):
        self.tmp.cleanup()

    def test_estimate_grid_matches_pipeline_arithmetic(self):
        # 5 000 m x 4 000 m, buffered by 1 000 m -> 7 000 m x 6 000 m, at 5 m
        grid = benchmark.estimate_grid(self.path, "nom", "  zone TEST ", 1000, 5)
        self.assertEqual(grid, (1200, 1400))

    def test_unknown_area_gives_none(self):
        self.assertIsNone(benchmark.estimate_grid(self.path, "nom", "elsewhere", 0, 5))

    def test_dry_run_prints_plan_and_runs_nothing(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = benchmark.main(
                [
                    "--study-area",
                    self.path,
                    "--name-field",
                    "nom",
                    "--area-name",
                    "Zone Test",
                    "--base-dir",
                    self.tmp.name,
                    "--resolutions",
                    "5",
                    "10",
                    "--buffers",
                    "0",
                    "--dry-run",
                ]
            )
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("run", text)
        # 5 m, default 5 km buffer: (5 000 + 2 x 5 000) x (4 000 + 2 x 5 000) m / 25 m2 = 8.4 M
        self.assertIn("8.4", text)
        # 10 m at the same buffer: 2.1 M; 5 m without buffer: 5 000 x 4 000 / 25 = 0.8 M
        self.assertIn("2.1", text)
        self.assertIn("0.8", text)


if __name__ == "__main__":
    unittest.main()
