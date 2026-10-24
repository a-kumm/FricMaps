"""Tests of the merging of layers from mixed Shapefile / GeoPackage deliveries.

BD TOPO departments may be delivered in different formats, and the same attribute is
then defined differently (``SALINITE`` is 3-character text in a Shapefile and a boolean
in a GeoPackage). Merging them must not depend on the order of the layers, which comes
from the file system and so differs between operating systems.

They need QGIS (PyQGIS and Processing) and are skipped without it. Run from the
repository root, with the Python interpreter of QGIS::

    python -m unittest discover -s tests -v
"""

import itertools
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

HAVE_QGIS = True
try:
    for candidate in ("/usr/share/qgis/python/plugins",):
        if os.path.isdir(candidate) and candidate not in sys.path:
            sys.path.append(candidate)
    from qgis.core import QgsApplication, QgsFeature, QgsGeometry, QgsPointXY, QgsVectorLayer
    from qgis.PyQt.QtCore import QVariant

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _APP = QgsApplication.instance() or QgsApplication([], False)
    if QgsApplication.instance() is _APP:
        _APP.initQgis()
    import processing  # noqa: F401
    from processing.core.Processing import Processing

    Processing.initialize()
    from fricmaps_plugin.core.utils import (
        harmonize_fields_for_merge,
        merge_vector_layers,
        walk_sorted,
    )
except Exception:  # pragma: no cover - depends on the interpreter
    HAVE_QGIS = False


def point_layer(name, field, values):
    """A memory layer with one point per value of a single field (``name:type(len)``)."""
    layer = QgsVectorLayer(f"Point?crs=EPSG:2154&field={field}", name, "memory")
    features = []
    for i, value in enumerate(values):
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(900000 + i, 6800000)))
        feature.setAttributes([value])
        features.append(feature)
    layer.dataProvider().addFeatures(features)
    return layer


def values(layer, name):
    """Values of a field as text, NULL as ``None`` (first), whatever the PyQGIS version."""
    index = layer.fields().lookupField(name)
    out = []
    for feature in layer.getFeatures():
        value = feature.attributes()[index]
        out.append(None if value is None or str(value) == "NULL" else str(value))
    return sorted(out, key=lambda v: (v is not None, v or ""))


@unittest.skipUnless(HAVE_QGIS, "QGIS (PyQGIS + Processing) not available")
class MergeMixedDeliveriesTest(unittest.TestCase):
    def test_boolean_and_fixed_width_text_merge_in_any_order(self):
        # Shapefile D088, GeoPackage D070, Shapefile D054: the order seen on macOS made
        # native:mergevectorlayers fail ("String of length 5 exceeds maximum field length (3)").
        shp_a = point_layer("shp_a", "SALINITE:string(3)", ["Non", "Non"])
        gpkg = point_layer("gpkg", "salinite:boolean", [False, True, None])
        shp_b = point_layer("shp_b", "SALINITE:string(3)", ["Oui"])
        for order in itertools.permutations([shp_a, gpkg, shp_b]):
            with self.subTest(order=[lyr.name() for lyr in order]):
                merged = merge_vector_layers(list(order), "EPSG:2154")
                self.assertEqual(merged.featureCount(), 6)
                # Booleans take the Shapefile values, which the filters test.
                self.assertEqual(
                    values(merged, "SALINITE"), [None, "Non", "Non", "Non", "Oui", "Oui"]
                )

    def test_unlimited_text_longer_than_fixed_width_is_kept_whole(self):
        shp = point_layer("shp", "NATURE:string(5)", ["Route"])
        gpkg = point_layer("gpkg", "nature:string(0)", ["Route à 2 chaussées"])
        for order in ([shp, gpkg], [gpkg, shp]):
            with self.subTest(order=[lyr.name() for lyr in order]):
                merged = merge_vector_layers(order, "EPSG:2154")
                self.assertEqual(values(merged, "NATURE"), ["Route", "Route à 2 chaussées"])

    def test_layers_that_already_agree_are_not_rewritten(self):
        shp = point_layer("shp", "NATURE:string(12)", ["Route"])
        gpkg = point_layer("gpkg", "nature:string(0)", ["Chemin"])  # fits in 12
        other = point_layer("other", "NATURE:string(8)", ["Sentier"])
        out = harmonize_fields_for_merge([shp, gpkg, other])
        self.assertIs(out[0], shp)
        self.assertIs(out[2], other)  # fixed widths are reconciled by the merge itself
        self.assertEqual(out[1].fields()[0].length(), 12)

    def test_numbers_of_different_types_become_the_widest(self):
        ints = point_layer("ints", "NB:integer", [2])
        reals = point_layer("reals", "NB:double", [2.5])
        merged = merge_vector_layers([ints, reals], "EPSG:2154")
        self.assertEqual(merged.fields().field("NB").type(), QVariant.Double)
        self.assertEqual(values(merged, "NB"), ["2.0", "2.5"])

    def test_walk_sorted_does_not_depend_on_creation_order(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ("D088", "D070", "D054"):
                os.makedirs(os.path.join(root, name))
                open(os.path.join(root, name, "layer.shp"), "w").close()
            for name in ("b.gpkg", "a.shp"):
                open(os.path.join(root, name), "w").close()
            found = [
                os.path.relpath(os.path.join(r, f), root)
                for r, _, files in walk_sorted(root)
                for f in files
            ]
        self.assertEqual(
            found,
            ["a.shp", "b.gpkg", "D054/layer.shp", "D070/layer.shp", "D088/layer.shp"],
        )


if __name__ == "__main__":
    unittest.main()
