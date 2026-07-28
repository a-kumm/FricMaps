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
import unicodedata
from qgis.core import (
    QgsVectorLayer,
    QgsVectorFileWriter,
    QgsFeature,
    QgsFields,
    QgsVectorDataProvider,
)
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
    for root, _, files in os.walk(base_dir):
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

    merged = processing.run(
        "native:mergevectorlayers", {"LAYERS": layers, "CRS": crs.authid(), "OUTPUT": "memory:"}
    )["OUTPUT"]
    return normalize_fields(merged)


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
