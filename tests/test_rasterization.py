"""Tests of the rasterisation step on a small synthetic study area.

They need QGIS (PyQGIS, Processing and GDAL) and are skipped without it. Run from the
repository root, with the Python interpreter of QGIS::

    python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

HAVE_QGIS = True
try:
    for candidate in ("/usr/share/qgis/python/plugins",):
        if os.path.isdir(candidate) and candidate not in sys.path:
            sys.path.append(candidate)
    import numpy as np
    from osgeo import gdal
    from qgis.core import (
        QgsApplication,
        QgsCoordinateReferenceSystem,
        QgsFeature,
        QgsGeometry,
        QgsProcessingUtils,
        QgsRectangle,
        QgsVectorLayer,
    )

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _APP = QgsApplication.instance() or QgsApplication([], False)
    if QgsApplication.instance() is _APP:
        _APP.initQgis()
    import processing  # noqa: F401
    from processing.core.Processing import Processing

    Processing.initialize()
    from fricmaps_plugin.core import raster_processing as rp
except Exception:  # pragma: no cover - depends on the interpreter
    HAVE_QGIS = False

RESOLUTION = 5.0
X0, Y0 = 900000.0, 6800000.0
COLS, ROWS = 20, 12  # a 100 m x 60 m study area


def box(x_min, y_min, x_max, y_max):
    """A rectangle placed in the study area (coordinates relative to its south-west corner)."""
    return QgsGeometry.fromRect(QgsRectangle(X0 + x_min, Y0 + y_min, X0 + x_max, Y0 + y_max))


def polygon_layer(name, boxes):
    """A memory layer with one polygon per ``(CODE, box)`` pair."""
    layer = QgsVectorLayer("Polygon?crs=EPSG:2154&field=CODE:string", name, "memory")
    features = []
    for code, rectangle in boxes:
        feature = QgsFeature(layer.fields())
        feature.setGeometry(rectangle)
        feature["CODE"] = code
        features.append(feature)
    layer.dataProvider().addFeatures(features)
    return layer


def study_area():
    return box(0, 0, COLS * 5, ROWS * 5)


def expected_arrays():
    """Hand-computed result for the layers of :func:`run_rasterisation`.

    Edges of the rectangles fall in the middle of pixels (never on pixel borders), so the
    set of pixels touched by each polygon is unambiguous. Rows count from the north.
    """
    classes = np.zeros((ROWS, COLS), dtype=np.uint16)
    friction = np.zeros((ROWS, COLS), dtype=np.uint16)
    classes[:, :] = 1  # class 1: the whole area
    friction[:, :] = 100
    # class 2: x 12.5-37.5 m (columns 2-7), y 12.5-32.5 m (rows 5-9 counted from the north)
    classes[5:10, 2:8] = 2
    friction[5:10, 2:8] = 5
    # class 3, drawn last: x 57.5-72.5 m (columns 11-14), y 2.5-57.5 m (rows 0-11)
    classes[0:12, 11:15] = 3
    friction[0:12, 11:15] = 1000
    return classes, friction


def run_rasterisation(output_dir, work_dir):
    table = [
        {"SOURCE": "A", "COMPILATION_ORDER": 1, "FRICTION_VALUE": 100, "SQL_FILTER": ""},
        {
            "SOURCE": "A",
            "COMPILATION_ORDER": 2,
            "FRICTION_VALUE": 5,
            "SQL_FILTER": "\"CODE\" = 'forest'",
        },
        {"SOURCE": "B", "COMPILATION_ORDER": 3, "FRICTION_VALUE": 1000, "SQL_FILTER": ""},
    ]
    # Layer A holds the whole area (class 1) and a forest rectangle (class 2, compiled
    # after class 1 and therefore on top of it); B is a strip drawn last (class 3).
    layer_a = polygon_layer(
        "A", [("field", box(0, 0, COLS * 5, ROWS * 5)), ("forest", box(12.5, 12.5, 37.5, 32.5))]
    )
    layer_b = polygon_layer("B", [("strip", box(57.5, 2.5, 72.5, 57.5))])
    return rp.rasterize_classes_and_friction(
        table_df=table,
        vector_layers={"A": layer_a, "B": layer_b},
        extent_geom=study_area(),
        crs_ref=QgsCoordinateReferenceSystem("EPSG:2154"),
        resolution=RESOLUTION,
        output_dir=output_dir,
        area_name_clean="synthetic",
        work_dir=work_dir,
    )


def read(path):
    ds = gdal.Open(path)
    array = ds.GetRasterBand(1).ReadAsArray()
    ds = None
    return array


def leftover_masks():
    """Rasters left by gdal:rasterize in the QGIS temporary folder."""
    folder = QgsProcessingUtils.tempFolder()
    found = []
    for root, _, files in os.walk(folder):
        found += [os.path.join(root, f) for f in files if f.lower().endswith(".tif")]
    return found


@unittest.skipUnless(HAVE_QGIS, "QGIS (PyQGIS, Processing, GDAL) is required")
class RasterisationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "out")
        self.work = os.path.join(self.tmp.name, "work")

    def tearDown(self):
        self.tmp.cleanup()

    def test_rasters_match_the_hand_computed_result(self):
        classes_path, friction_path, _ = run_rasterisation(self.out, self.work)
        expected_classes, expected_friction = expected_arrays()
        self.assertTrue(np.array_equal(read(classes_path), expected_classes))
        self.assertTrue(np.array_equal(read(friction_path), expected_friction))

    def test_masks_are_bytes(self):
        seen = []
        real_run = rp.processing.run

        def spy(name, params, *args, **kwargs):
            if name == "gdal:rasterize":
                seen.append(params["DATA_TYPE"])
            return real_run(name, params, *args, **kwargs)

        with mock.patch.object(rp.processing, "run", side_effect=spy):
            run_rasterisation(self.out, self.work)
        self.assertTrue(seen)
        self.assertEqual(set(seen), {0})  # 0 = Byte; 5 would be Float32

    def test_no_mask_is_left_behind(self):
        before = set(leftover_masks())
        run_rasterisation(self.out, self.work)
        self.assertEqual(set(leftover_masks()) - before, set())

    def test_unreadable_mask_stops_the_run_with_a_clear_message(self):
        real_open = rp.gdal.Open

        def broken(path, *args, **kwargs):
            if str(path).endswith("OUTPUT.tif"):
                return None
            return real_open(path, *args, **kwargs)

        with mock.patch.object(rp.gdal, "Open", side_effect=broken):
            with self.assertRaises(RuntimeError) as ctx:
                run_rasterisation(self.out, self.work)
        message = str(ctx.exception)
        self.assertIn("raster mask", message)
        self.assertIn("class", message)
        self.assertIn("TMPDIR", message)

    def test_missing_optional_source_is_still_only_a_warning(self):
        table = [
            {"SOURCE": "ABSENT", "COMPILATION_ORDER": 1, "FRICTION_VALUE": 1, "SQL_FILTER": ""}
        ]
        classes_path, _, _ = rp.rasterize_classes_and_friction(
            table_df=table,
            vector_layers={},
            extent_geom=study_area(),
            crs_ref=QgsCoordinateReferenceSystem("EPSG:2154"),
            resolution=RESOLUTION,
            output_dir=self.out,
            area_name_clean="synthetic",
            work_dir=self.work,
        )
        self.assertEqual(int(read(classes_path).max()), 0)

    def test_inherited_fid_does_not_prevent_staging(self):
        # Shapefile features (no fid) merged before GeoPackage features (fid 1, 2): the
        # GeoPackage written for gdal:rasterize used to reject the duplicate keys (error 7)
        # and the class was dropped. Hydrography of mixed deliveries hit this on Linux.
        layer = QgsVectorLayer(
            "Point?crs=EPSG:2154&field=fid:long&field=NATURE:string", "Matching features", "memory"
        )
        features = []
        for i, fid in enumerate([None, None, 1, 2]):
            feature = QgsFeature(layer.fields())
            feature.setGeometry(box(i, 0, i + 1, 1).centroid())
            feature.setAttributes([fid, "x"])
            features.append(feature)
        layer.dataProvider().addFeatures(features)
        path = rp._ensure_vector_on_disk(layer, self.work, suffix="HYDRO")
        staged = QgsVectorLayer(path, "staged", "ogr")
        self.assertEqual(staged.featureCount(), 4)


if __name__ == "__main__":
    unittest.main()
