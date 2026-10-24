# -*- coding: utf-8 -*-
#
# FricMaps - Friction and land-cover maps for ecological connectivity modelling
# Copyright (C) 2026  FricMaps contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program.  If not, see <https://www.gnu.org/licenses/>.
#
import logging
import os
import re
import sqlite3
import sys
import unicodedata
from qgis.core import (
    QgsAggregateCalculator,
    QgsExpression,
    QgsVectorLayer,
    QgsVectorFileWriter,
    QgsFeature,
    QgsFields,
    QgsVectorDataProvider,
)
from qgis.PyQt.QtCore import QVariant
import processing

# BD TOPO "new data model" (GeoPackage) -> "legacy model" (Shapefile, upper
# case) field renamings, applied AFTER field names have been upper-cased.
BDTOPO_FIELD_ALIASES = {
    "ETAT_DE_L_OBJET": "ETAT",
    "NATURE_DETAILLEE": "NAT_DETAIL",
    "CLASSE_DE_LARGEUR": "LARGEUR",
}


def normalize_fields(layer, aliases=None):
    """Harmonise field names for legacy/new data-model compatibility.

    - upper-cases ALL field names (GeoPackage deliveries use lower case:
      ``nature`` -> ``NATURE``, ``code_us`` -> ``CODE_US``), aligning them with
      the naming convention of the Shapefile delivery this codebase targets;
    - then applies the explicit renamings (``etat_de_l_objet`` -> ``ETAT``).

    Renaming is performed in place (best effort) on editable layers (memory,
    GeoPackage). No-op if the provider does not support renaming. Returns
    ``layer``.
    """
    if layer is None or not layer.isValid():
        return layer
    amap = dict(BDTOPO_FIELD_ALIASES)
    if aliases:
        amap.update({k.upper(): v for k, v in aliases.items()})

    prov = layer.dataProvider()
    # Safety: only rename MEMORY layers, never an on-disk source file
    # (OGR/GPKG), which would otherwise be modified in place.
    try:
        if prov.name() != "memory":
            return layer
    except Exception:
        return layer
    # Drop any 'fid' field (OGR/GPKG primary key inherited from the source):
    # it causes "UNIQUE constraint failed: ...fid" collisions when writing a new
    # GeoPackage. GPKG regenerates its own primary key.
    try:
        if prov.capabilities() & QgsVectorDataProvider.DeleteAttributes:
            fid_idx = [i for i, f in enumerate(layer.fields()) if f.name().lower() == "fid"]
            if fid_idx:
                prov.deleteAttributes(fid_idx)
                layer.updateFields()
    except Exception:
        logging.getLogger(__name__).debug("Suppressed exception", exc_info=True)
        pass

    if not (prov.capabilities() & QgsVectorDataProvider.RenameAttributes):
        return layer

    existing = {f.name() for f in layer.fields()}
    rename = {}
    taken = set(existing)
    for i, f in enumerate(layer.fields()):
        target = amap.get(f.name().upper(), f.name().upper())
        if target != f.name() and target not in taken:
            rename[i] = target
            taken.discard(f.name())
            taken.add(target)
    if rename:
        try:
            prov.renameAttributes(rename)
            layer.updateFields()
        except Exception:
            logging.getLogger(__name__).debug("Suppressed exception", exc_info=True)
            pass
    return layer


def gpkg_sublayers(gpkg_path):
    """Return the list of feature-layer names inside a GeoPackage (or [])."""
    try:
        con = sqlite3.connect(gpkg_path)
        try:
            rows = con.execute(
                "SELECT table_name FROM gpkg_contents WHERE data_type = 'features'"
            ).fetchall()
        finally:
            con.close()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return []


def gpkg_sublayers_with_field(gpkg_path, field_name):
    """Return the GeoPackage feature layers that contain a given field (case-insensitive).

    Robust way to locate a themed layer by its schema rather than its name
    (e.g. an RPG parcels layer = the one carrying a ``code_group`` field),
    which is robust to naming and vintage differences between deliveries.
    """
    field_up = str(field_name).upper()
    out = []
    try:
        con = sqlite3.connect(gpkg_path)
        try:
            tables = [
                r[0]
                for r in con.execute(
                    "SELECT table_name FROM gpkg_contents WHERE data_type = 'features'"
                )
            ]
            for t in tables:
                cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
                if any(c.upper() == field_up for c in cols):
                    out.append(t)
        finally:
            con.close()
    except Exception:
        logging.getLogger(__name__).debug("Suppressed exception", exc_info=True)
        pass
    return out


def clean_name(name):
    """Sanitise a name so that it can be used in file names."""
    if not name:
        return "Unknown"
    n = name.lower()
    n = unicodedata.normalize("NFKD", n).encode("ASCII", "ignore").decode("utf-8")
    n = re.sub(r"[^a-z0-9]+", "_", n)
    return n.strip("_")


def create_extent_layer(extent_geom, crs):
    """Create an in-memory polygon layer from a geometry."""
    lyr = QgsVectorLayer(f"Polygon?crs={crs.authid()}", "extent", "memory")
    pr = lyr.dataProvider()
    f = QgsFeature()
    f.setGeometry(extent_geom)
    pr.addFeatures([f])
    lyr.updateExtents()
    return lyr


def make_memory_layer(geom_str, crs, fields=None, name="memory"):
    """Create a generic empty in-memory layer."""
    layer = QgsVectorLayer(f"{geom_str}?crs={crs.authid()}", name, "memory")
    if fields and len(fields) > 0:
        pr = layer.dataProvider()
        # Accept either a list of QgsField or a QgsFields container
        if isinstance(fields, QgsFields):
            attr_list = [fields.at(i) for i in range(fields.count())]
        else:
            attr_list = fields
        pr.addAttributes(attr_list)
        layer.updateFields()
    return layer


def display_text(text):
    """Text as Qt can draw it on this platform.

    On macOS, Qt 5 cannot draw a symbol followed by the emoji variation selector
    (U+FE0F, as in the "⚠️" and "ℹ️" of the log messages): it draws an empty box and
    drops the rest of the line. Without the selector the symbol is drawn in its text
    form. Other platforms get the text unchanged.
    """
    return text.replace("️", "") if sys.platform == "darwin" else text


def write_vector_layer(layer, path):
    """Write a vector layer to disk.

    Excludes any ``fid`` field so that the GeoPackage regenerates its own
    primary key (avoids ``UNIQUE constraint failed: ...fid`` with GPKG inputs).
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    options = QgsVectorFileWriter.SaveVectorOptions()
    try:
        keep = [i for i, f in enumerate(layer.fields()) if f.name().lower() != "fid"]
        if len(keep) != len(layer.fields()):
            options.attributes = keep
    except Exception:
        logging.getLogger(__name__).debug("Suppressed exception", exc_info=True)
        pass
    QgsVectorFileWriter.writeAsVectorFormatV3(layer, path, layer.transformContext(), options)


def find_files_fuzzy(base_dir, token, exts=(".shp", ".gpkg")):
    """Find layers whose name contains a token (case-insensitive).

    Supports both Shapefile **and** GeoPackage deliveries:
    - a .shp (or similar) file whose NAME contains the token -> file path;
    - a GeoPackage holding a SUBLAYER whose name contains the token -> URI
      ``path.gpkg|layername=<sublayer>`` (BD TOPO GeoPackage case, where every
      theme is a sublayer of a single .gpkg);
    - otherwise, a .gpkg whose FILE NAME contains the token -> .gpkg path.
    """
    token_up = token.upper()
    ext_up = tuple(e.upper() for e in exts)
    out = []
    for root, _, files in walk_sorted(base_dir):
        for f in files:
            fu = f.upper()
            path = os.path.join(root, f)
            if fu.endswith(".GPKG"):
                subs = [s for s in gpkg_sublayers(path) if token_up in s.upper()]
                if subs:
                    out.extend(f"{path}|layername={s}" for s in subs)
                elif token_up in fu and ".GPKG" in ext_up:
                    out.append(path)
            elif fu.endswith(ext_up) and token_up in fu:
                out.append(path)
    return out


def merge_vector_layers_from_paths(paths, crs):
    """Merge several vector layers given their paths or layer URIs."""
    layers = []
    for p in paths:
        lyr = QgsVectorLayer(p, os.path.basename(p), "ogr")
        if not lyr or not lyr.isValid():
            continue
        if lyr.crs() != crs:
            lyr = processing.run(
                "native:reprojectlayer",
                {"INPUT": lyr, "TARGET_CRS": crs.authid(), "OUTPUT": "memory:"},
            )["OUTPUT"]
        layers.append(lyr)

    if not layers:
        return None
    if len(layers) == 1:
        return normalize_fields(layers[0])

    return normalize_fields(merge_vector_layers(layers, crs.authid()))


# =====================================================================
# Deterministic discovery and merging of mixed Shapefile / GeoPackage deliveries
# =====================================================================


def walk_sorted(base_dir):
    """``os.walk`` in sorted order.

    ``os.walk`` lists entries in file-system order, which differs between ext4, APFS
    and NTFS. The order in which input layers are found is the order in which they are
    merged, so it must not depend on the machine.
    """
    for root, dirs, files in os.walk(base_dir):
        dirs.sort()  # in place: os.walk descends in this order
        yield root, dirs, sorted(files)


_NUMERIC_TYPES = {
    QVariant.Int,
    QVariant.UInt,
    QVariant.LongLong,
    QVariant.ULongLong,
    QVariant.Double,
}


def harmonize_fields_for_merge(layers):
    """Give same-named fields compatible definitions before ``native:mergevectorlayers``.

    BD TOPO is delivered per department, as Shapefile or GeoPackage, and a study area
    across a department boundary can mix both. The same attribute is then defined
    differently: ``FICTIF`` or ``SALINITE`` is 3-character text (``Oui``/``Non``) in a
    Shapefile and a boolean in a GeoPackage, and text has a fixed width in a Shapefile
    but none (length 0) in a GeoPackage. ``native:mergevectorlayers`` turns a type
    conflict into text of unlimited length, but the next fixed-width layer narrows it
    again, so the merge fails or succeeds depending on the order of the layers
    ("String of length 5 exceeds maximum field length (3)").

    Fields are matched case-insensitively, as the merge does. For each conflict:

    - a boolean becomes ``Oui``/``Non`` text, the Shapefile values the filters of this
      plugin test (``upper("FICTIF") <> 'OUI'``);
    - numbers of different types become the widest type;
    - any other type conflict becomes text;
    - text of unlimited length mixed with fixed-width text takes the largest width if
      every value fits in it, otherwise every layer gets unlimited length.

    Only the layers with a field to change are rewritten (``native:refactorfields``);
    the others are returned as they are.
    """
    layers = [
        lyr if isinstance(lyr, QgsVectorLayer) else QgsVectorLayer(str(lyr), "merge_input", "ogr")
        for lyr in layers
    ]
    groups = {}
    for li, lyr in enumerate(layers):
        for fi, field in enumerate(lyr.fields()):
            groups.setdefault(field.name().upper(), []).append((li, fi, field))

    changes = {}  # (layer index, field index) -> (type, length, precision, expression)
    for members in groups.values():
        if len({(f.type(), f.length(), f.precision()) for _, _, f in members}) < 2:
            continue
        types = {f.type() for _, _, f in members}
        if len(types) == 1 and QVariant.String not in types:
            continue  # same non-text type: only width or precision differ, merged as is
        if types <= _NUMERIC_TYPES:
            widest = QVariant.Double if QVariant.Double in types else QVariant.LongLong
            for li, fi, f in members:
                if f.type() != widest:
                    changes[(li, fi)] = (widest, 0, 0, QgsExpression.quotedColumnRef(f.name()))
            continue

        text = []  # (layer index, field index, expression or None if already text, width)
        for li, fi, f in members:
            col = QgsExpression.quotedColumnRef(f.name())
            if f.type() == QVariant.String:
                text.append((li, fi, None, f.length()))
            elif f.type() == QVariant.Bool:
                expr = f"CASE WHEN {col} IS NULL THEN NULL WHEN {col} THEN 'Oui' ELSE 'Non' END"
                text.append((li, fi, expr, 3))
            else:
                text.append((li, fi, f"to_string({col})", 0))
        width = max(w for *_, w in text)
        if any(w == 0 for *_, w in text):
            longest = 0
            for li, fi, expr, w in text:
                if w == 0:
                    col = expr or QgsExpression.quotedColumnRef(layers[li].fields()[fi].name())
                    value, _ = layers[li].aggregate(QgsAggregateCalculator.Max, f"length({col})")
                    longest = max(longest, int(value or 0))
            if longest > width:
                width = 0
        for li, fi, expr, w in text:
            if expr is not None or (w == 0) != (width == 0):
                col = expr or QgsExpression.quotedColumnRef(layers[li].fields()[fi].name())
                changes[(li, fi)] = (QVariant.String, width, 0, col)

    harmonized = []
    for li, lyr in enumerate(layers):
        if not any(key[0] == li for key in changes):
            harmonized.append(lyr)
            continue
        mapping = []
        for fi, f in enumerate(lyr.fields()):
            if (li, fi) in changes:
                ftype, length, precision, expr = changes[(li, fi)]
                mapping.append(
                    {
                        "name": f.name(),
                        "type": ftype,
                        "length": length,
                        "precision": precision,
                        "expression": expr,
                    }
                )
            else:
                mapping.append(
                    {
                        "name": f.name(),
                        "type": f.type(),
                        "sub_type": f.subType(),
                        "type_name": f.typeName(),
                        "length": f.length(),
                        "precision": f.precision(),
                        "expression": QgsExpression.quotedColumnRef(f.name()),
                    }
                )
        harmonized.append(
            processing.run(
                "native:refactorfields",
                {"INPUT": lyr, "FIELDS_MAPPING": mapping, "OUTPUT": "memory:"},
            )["OUTPUT"]
        )
    return harmonized


def merge_vector_layers(layers, crs):
    """``native:mergevectorlayers`` into memory, on layers made compatible first.

    Use this instead of calling the algorithm directly whenever the layers may come
    from different deliveries (see :func:`harmonize_fields_for_merge`).
    """
    return processing.run(
        "native:mergevectorlayers",
        {"LAYERS": harmonize_fields_for_merge(layers), "CRS": crs, "OUTPUT": "memory:"},
    )["OUTPUT"]


def clip_layer(input_layer, overlay_layer):
    """Clip a layer by another one.

    Output field names are harmonised to UPPER CASE (legacy/new data-model
    compatibility: ``nature`` -> ``NATURE``, etc.).
    """
    try:
        out = processing.run(
            "native:clip", {"INPUT": input_layer, "OVERLAY": overlay_layer, "OUTPUT": "memory:"}
        )["OUTPUT"]
        if out and out.isValid() and out.featureCount() > 0:
            return normalize_fields(out)
        return None
    except Exception:
        return None
