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
"""Data preparation routines using the QGIS API.

This module contains functions for reading and clipping source
datasets using the built‑in QGIS processing framework rather
than third‑party libraries such as GeoPandas.  It exposes
minimal functionality for loading the study area, buffering it
and extracting land cover layers.  These helpers can be used by
plugins or scripts to prepare data for further analysis and
avoid external Python dependencies.

The functions in this module return QGIS layer objects
(`QgsVectorLayer`) instead of pandas/GeoPandas dataframes.  As
such, they integrate directly with other QGIS operations and
processing algorithms.

Functions are deliberately conservative in scope: they do not
attempt to replicate the entire pipeline contained in the
original scripts.  Instead, they focus on core operations that
translate well to the QGIS API.  Additional logic such as
filtering by attribute values or merging multiple datasets can
be layered on top by calling code.
"""

from __future__ import annotations

import os
from typing import List, Optional, Tuple

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsProject,
    QgsSpatialIndex,
    QgsVectorLayer,
    QgsWkbTypes,
    edit,
)
from qgis.PyQt.QtCore import QVariant
import processing

from .utils import (
    clip_layer,
    create_extent_layer,
    find_files_fuzzy,
    gpkg_sublayers,
    gpkg_sublayers_with_field,
    make_memory_layer,
    normalize_fields,
    write_vector_layer,
)

# =====================================================================
# 1) Study-area extent extraction
# =====================================================================


def extract_area_extent(
    area_layer_path: str,
    area_name: str,
    name_field: str,
    buffer_dist: float,
) -> Tuple[QgsGeometry, QgsCoordinateReferenceSystem]:
    """Extract the buffered extent of a named study area.

    This helper loads the provided study area layer, selects
    features whose ``name_field`` matches ``area_name`` and
    computes the union of their geometries.  A buffer is then
    applied around the union to enlarge the processing extent.

    Args:
        area_layer_path: Path to the vector layer containing
            polygons for the study areas (ESRI Shapefile or
            GeoPackage).  The layer is expected to contain a
            field whose name is supplied via ``name_field``.
        area_name: Value of the feature(s) to select from the
            ``name_field`` column.  Matching is case‑insensitive
            and leading/trailing whitespace is ignored.
        name_field: Name of the field to use for filtering.  If
            the field does not exist, a ``ValueError`` is raised.
        buffer_dist: Distance in map units (typically metres) to
            buffer the selected geometry by.  A zero or negative
            value will return the unbuffered union.

    Returns:
        A tuple ``(geometry, crs)`` where ``geometry`` is the
        buffered ``QgsGeometry`` and ``crs`` is the coordinate
        reference system of the input layer.

    Raises:
        ValueError: If the input layer is invalid, the field is
            missing or no features match the given name.
    """
    layer = QgsVectorLayer(area_layer_path, "study_area", "ogr")
    if not layer.isValid():
        raise ValueError(f"Cannot read study area layer: {area_layer_path}")

    # Find the field index; perform a case‑insensitive search
    field_index = layer.fields().indexOf(name_field)
    if field_index < 0:
        # Attempt to find a matching field ignoring case
        for idx, fld in enumerate(layer.fields()):
            if fld.name().upper() == name_field.upper():
                field_index = idx
                break
        if field_index < 0:
            raise ValueError(f"Field '{name_field}' not found in study area layer")

    # Build an expression to filter matching features; escape
    # single quotes in the area name
    name_value = area_name.strip()
    # QGIS expressions use single quotes for strings; double up
    # single quotes inside the value
    name_value_escaped = name_value.replace("'", "''")
    expression = f"\"{layer.fields()[field_index].name()}\" = '{name_value_escaped}'"
    request = QgsFeatureRequest().setFilterExpression(expression)
    features = [feat for feat in layer.getFeatures(request)]
    if not features:
        raise ValueError(f"Area '{area_name}' not found in layer '{area_layer_path}'")

    # Compute union of all selected geometries
    geom_union = QgsGeometry.unaryUnion([feat.geometry() for feat in features])
    if buffer_dist and buffer_dist > 0:
        geom_union = geom_union.buffer(buffer_dist, 30)

    return geom_union, layer.crs()


# =====================================================================
# 2) Generic vector file discovery
# =====================================================================


def find_vector_files(
    base_dir: str, keywords: List[str], extensions: Tuple[str, ...] = (".shp", ".gpkg")
) -> List[str]:
    """Recursively search for vector files containing given keywords.

    This utility walks through ``base_dir`` and returns a list
    of paths to files whose names contain any of the provided
    ``keywords`` (case‑insensitive) and whose extension matches
    one of ``extensions``.  Keywords are compared after
    uppercasing both the filename and the keyword.

    Args:
        base_dir: Root directory to walk.
        keywords: List of substrings to look for in filenames.
        extensions: Tuple of acceptable filename extensions.

    Returns:
        A list of absolute file paths.
    """
    ext_low = tuple(ext.lower() for ext in extensions)
    matches: List[str] = []
    for root, _, files in os.walk(base_dir):
        for fname in files:
            fl = fname.lower()
            path = os.path.join(root, fname)
            if fl.endswith(".gpkg"):
                # Compatible GeoPackage : on cible d'abord les SOUS-COUCHES
                # whose name contains a keyword (BD TOPO / OCS GE GPKG case).
                subs = [
                    s
                    for s in gpkg_sublayers(path)
                    if any(kw.upper() in s.upper() for kw in keywords)
                ]
                if subs:
                    matches.extend(f"{path}|layername={s}" for s in subs)
                elif ".gpkg" in ext_low and any(kw.upper() in fname.upper() for kw in keywords):
                    matches.append(path)
            elif fl.endswith(ext_low) and any(kw.upper() in fname.upper() for kw in keywords):
                matches.append(path)
    return matches


# =====================================================================
# 3) OCS GE land-cover processing
# =====================================================================
def process_land_cover_data(
    base_dir: str,
    extent_geom: QgsGeometry,
    crs: QgsCoordinateReferenceSystem,
    save_outputs: bool,
    output_dir: str,
    area_name_clean: str,
) -> QgsVectorLayer:
    """Load and clip land cover (OCS GE) layers using QGIS processing.

    All vector files in ``base_dir`` containing the substring
    ``'OCCUPATION_SOL'`` (case‑insensitive) in their filename are
    treated as land cover layers.  Each is clipped to the
    provided ``extent_geom`` using the native ``clip`` algorithm
    and then merged using ``mergevectorlayers``.  The merged
    layer is returned and optionally written to disk.

    Args:
        base_dir: Directory where land cover files are located.
        extent_geom: Geometry defining the area of interest.
        crs: Coordinate reference system to assign to the extent
            layer used for clipping.
        save_outputs: If ``True``, save the clipped and merged
            layer to ``output_dir``.
        output_dir: Directory to write output files (if
            ``save_outputs`` is ``True``).
        area_name_clean: Clean string used to build output
            filenames.

    Returns:
        A ``QgsVectorLayer`` containing the merged, clipped
        land cover features.

    Raises:
        RuntimeError: If no suitable land cover files are found.
    """
    # Identify candidate files
    # Expanded keywords to catch more OCS GE variations
    land_files = find_vector_files(base_dir, ["OCCUPATION_SOL", "OCS_GE", "OCS"])
    print(f"[DEBUG] Found {len(land_files)} OCS GE files: {land_files}")
    if not land_files:
        raise RuntimeError(
            "No OCS GE files containing 'OCCUPATION_SOL', 'OCS_GE', or 'OCS' were found"
        )

    # Create a temporary layer representing the clipping geometry
    extent_layer = create_extent_layer(extent_geom, crs)

    clipped_layers: List[QgsVectorLayer] = []
    for path in land_files:
        try:
            # Load the layer to check CRS
            lyr = QgsVectorLayer(path, os.path.basename(path), "ogr")
            if not lyr.isValid():
                print(f"⚠️ Invalid layer: {path}")
                continue

            # 1. Reproject if necessary
            if lyr.crs() != crs:
                print(f"ℹ️ Reprojecting {path} from {lyr.crs().authid()} to {crs.authid()}")
                lyr = processing.run(
                    "native:reprojectlayer",
                    {
                        "INPUT": lyr,
                        "TARGET_CRS": crs.authid(),
                        "OPERATION": "",
                        "OUTPUT": "memory:",
                    },
                )["OUTPUT"]

            # 2. Fix Geometries (Crucial step from Python tool)
            lyr = processing.run("native:fixgeometries", {"INPUT": lyr, "OUTPUT": "memory:"})[
                "OUTPUT"
            ]

            # 3. Normalize Fields (Uppercase) & Filter
            # We want to keep CODE_CS, CODE_US, and ID if present.
            # First, rename fields to upper case if needed.
            # Note: QGIS 'refactorfields' is powerful but complex to setup programmatically for dynamic fields.
            # Simpler approach: use 'native:fieldcalculator' to create uppercase copies if missing,
            # or just rely on the fact that usually they are already uppercase or we can find them.

            # Let's try to find the relevant fields case-insensitively
            fields_to_keep = []
            source_fields = lyr.fields()

            # Helper to find field name
            def find_field_name(fields, target):
                for f in fields:
                    if f.name().upper() == target:
                        return f.name()
                return None

            real_code_cs = find_field_name(source_fields, "CODE_CS")
            real_code_us = find_field_name(source_fields, "CODE_US")
            real_id = find_field_name(source_fields, "ID")

            # If critical fields are missing, skip or warn?
            # The Python tool assumes they exist or renames them.
            # If they don't exist, this file might not be useful.
            if not real_code_cs or not real_code_us:
                print(f"⚠️ Missing CODE_CS or CODE_US in {path}. Skipping.")
                continue

            # We will retain these fields.
            # If the names are not exactly "CODE_CS" and "CODE_US", we might want to rename them.
            # For now, let's assume we just keep the ones we found.
            fields_to_keep.append(real_code_cs)
            fields_to_keep.append(real_code_us)
            if real_id:
                fields_to_keep.append(real_id)

            lyr = processing.run(
                "native:retainfields", {"INPUT": lyr, "FIELDS": fields_to_keep, "OUTPUT": "memory:"}
            )["OUTPUT"]

            # 4. Clip
            result = processing.run(
                "native:clip", {"INPUT": lyr, "OVERLAY": extent_layer, "OUTPUT": "memory:"}
            )
            layer = result["OUTPUT"]

            if layer and layer.isValid() and layer.featureCount() > 0:
                clipped_layers.append(layer)

        except Exception as e:
            # Skip problematic files quietly but log error
            print(f"❌ Failed to process {path}: {e}")
            continue

    if not clipped_layers:
        raise RuntimeError("Failed to clip any OCS GE layers to the area of interest")

    # Merge all clipped layers
    if len(clipped_layers) > 1:
        merge_params = {"LAYERS": clipped_layers, "CRS": crs.authid(), "OUTPUT": "memory:"}
        merge_result = processing.run("native:mergevectorlayers", merge_params)
        merged_layer: QgsVectorLayer = merge_result["OUTPUT"]
    else:
        merged_layer = clipped_layers[0]

    if save_outputs and merged_layer and merged_layer.isValid():
        fname = f"OCS_GE_{area_name_clean}.gpkg"
        out_path = os.path.join(output_dir, fname)
        write_vector_layer(merged_layer, out_path)

    return merged_layer


# =====================================================================
# 4) Internal helper: intersecting department codes
# =====================================================================


def _get_intersecting_dept_codes(
    base_dir: str, extent: QgsGeometry, crs_ref: QgsCoordinateReferenceSystem
) -> set[str]:
    """Helper to find department codes intersecting the extent without merging layers."""
    dept_files = []
    for root, _, files in os.walk(base_dir):
        for f in files:
            fl = f.lower()
            path = os.path.join(root, f)
            if fl == "departement.shp":
                dept_files.append(path)
            elif fl.endswith(".gpkg"):
                for s in gpkg_sublayers(path):
                    if s.upper() == "DEPARTEMENT":
                        dept_files.append(f"{path}|layername={s}")

    codes = set()
    if not dept_files:
        return codes

    for path in dept_files:
        lyr = QgsVectorLayer(path, "tmp_dep", "ogr")
        if not lyr.isValid():
            continue

        # Determine the code field (SHP: INSEE_DEP; GPKG new model: code_insee)
        code_field = None
        names = {fld.name().upper(): fld.name() for fld in lyr.fields()}
        for cand in ("INSEE_DEP", "CODE_INSEE", "INSEE"):
            if cand in names:
                code_field = names[cand]
                break
        if not code_field:
            for up, real in names.items():
                if "DEP" in up or "INSEE" in up:
                    code_field = real
                    break

        if not code_field:
            continue

        # Transform extent to layer CRS
        if lyr.crs() != crs_ref:
            try:
                xform = QgsCoordinateTransform(crs_ref, lyr.crs(), QgsProject.instance())
                geom_search = QgsGeometry(extent)
                geom_search.transform(xform)
            except Exception:
                continue
        else:
            geom_search = extent

        # Iterate features
        req = QgsFeatureRequest().setFilterRect(geom_search.boundingBox())
        for f in lyr.getFeatures(req):
            if f.geometry().intersects(geom_search):
                val = f[code_field]
                if val:
                    s_val = str(val)
                    codes.add(s_val)
                    # Add zero-padded version if numeric
                    if s_val.isdigit():
                        codes.add(s_val.zfill(3))  # e.g. "57" -> "057"
                        codes.add(s_val.zfill(2))  # e.g. "5" -> "05"

    print(f"[DEBUG] Found department codes: {codes}")
    return codes


# =====================================================================
# 5) Vegetation and hedgerows (BD TOPO)
# =====================================================================


def process_vegetation_data(
    base_dir: str,
    output_dir: str,
    extent: QgsGeometry,  # extent (QgsGeometry) expressed in crs_ref
    area_name_clean: str,
    crs_ref: QgsCoordinateReferenceSystem,
    save_outputs: bool = True,
) -> Tuple[QgsVectorLayer, QgsVectorLayer]:
    """
    Multi-department BD TOPO vegetation & hedgerows (PyQGIS port of the GeoPandas code).
      1) Find the DEPARTEMENT features intersecting the extent
      2) Collect ZONE_DE_VEGETATION and HAIE* for the relevant departments
      3) Clip to the study extent
      4) Normalise NATURE (unaccent/upper/espaces -> '_')
      5) Extrait HAIE depuis ZONE_DE_VEGETATION (veg_sans_haies)
      6) Buffer 2.5 m for external HAIE and HAIE extracted from vegetation
      7) Merge -> Dissolve -> MultipartToSingleParts (avoids a single feature)
      8) Exporte et renvoie (veg_sans_haies, haies_polygones)
    """

    def runp(alg, params):
        return processing.run(alg, params)

    # Assuming these utility functions are available from a 'utils' module or similar
    # from .utils import make_memory_layer, create_extent_layer, merge_vector_layers_from_paths, clip_layer

    dep_codes = _get_intersecting_dept_codes(base_dir, extent, crs_ref)
    if not dep_codes:
        # Fallback: check if we found files at all?
        # For now, just raise the error as before
        raise RuntimeError("❌ No department intersects the extent (or files not found).")

    extent_layer = create_extent_layer(extent, crs_ref)

    # --- 2) Collecte ZONE_DE_VEGETATION & HAIE* (compatible SHP et sous-couches GPKG)
    vege_files, hedge_files = [], []
    for root, _, files in os.walk(base_dir):
        for f in files:
            up = f.upper()
            full = os.path.join(root, f)
            if not any(code in full for code in dep_codes):
                continue
            if up.endswith(".GPKG"):
                for sub in gpkg_sublayers(full):
                    su = sub.upper()
                    uri = f"{full}|layername={sub}"
                    if "ZONE_DE_VEGETATION" in su or ("VEGETATION" in su and "HAIE" not in su):
                        vege_files.append(uri)
                    if su == "HAIE" or su.startswith("HAIE"):
                        hedge_files.append(uri)
            elif up.endswith(".SHP"):
                if (
                    "ZONE_DE_VEGETATION" in up
                    or (up == "VEGETATION.SHP")
                    or ("VEGETATION" in up and "HAIE" not in up)
                ):
                    vege_files.append(full)
                if up.startswith("HAIE") or "HAIE" in up:
                    hedge_files.append(full)

    if not vege_files:
        raise RuntimeError("❌ No vegetation files found.")
    if not hedge_files:
        raise RuntimeError("❌ No hedge files found.")

    # --- 3) Clip & merge vegetation
    vege_clipped = []
    for p in vege_files:
        lyr = clip_layer(p, extent_layer)
        if lyr:
            vege_clipped.append(lyr)

    veg_merged = None
    if vege_clipped:
        veg_merged = runp(
            "native:mergevectorlayers",
            {"LAYERS": vege_clipped, "CRS": crs_ref.authid(), "OUTPUT": "memory:"},
        )["OUTPUT"]

        # --- 4) Normaliser NATURE
        if veg_merged.fields().lookupField("NATURE") >= 0:
            try:
                veg_merged = runp(
                    "native:fieldcalculator",
                    {
                        "INPUT": veg_merged,
                        "FIELD_NAME": "NATURE",
                        "FIELD_TYPE": 10,
                        "FIELD_LENGTH": 60,
                        "FIELD_PRECISION": 0,
                        "NEW_FIELD": False,
                        "FORMULA": "upper(replace(unaccent(\"NATURE\"),' ','_'))",
                        "OUTPUT": "memory:",
                    },
                )["OUTPUT"]
            except Exception:
                pass

    # Debug export of the clipped/normalised vegetation
    if save_outputs:
        out_veg = os.path.join(output_dir, f"Vegetation_{area_name_clean}.gpkg")
        runp(
            "native:savefeatures",
            {
                "INPUT": (
                    veg_merged
                    if veg_merged and veg_merged.isValid()
                    else make_memory_layer("Polygon", crs_ref, name="veg_empty")
                ),
                "OUTPUT": out_veg,
            },
        )

    # --- 5) Extract hedgerows from the vegetation layer
    hedges_from_veg = None
    veg_without_hedges = (
        veg_merged
        if (veg_merged and veg_merged.isValid())
        else make_memory_layer("Polygon", crs_ref, name="veg_empty")
    )

    if (
        veg_merged
        and veg_merged.isValid()
        and veg_merged.featureCount() > 0
        and veg_merged.fields().indexFromName("NATURE") >= 0
    ):
        expr = "\"NATURE\" = 'HAIE'"
        hedges_from_veg = runp(
            "native:extractbyexpression",
            {"INPUT": veg_merged, "EXPRESSION": expr, "OUTPUT": "memory:"},
        )["OUTPUT"]
        veg_without_hedges = runp(
            "native:extractbyexpression",
            {"INPUT": veg_merged, "EXPRESSION": f"NOT ({expr})", "OUTPUT": "memory:"},
        )["OUTPUT"]

    # --- 6) Clip HAIE* + buffer(2.5 m)
    hedge_buffers = []
    for p in hedge_files:
        lyr = clip_layer(p, extent_layer)
        if not lyr:
            continue
        buf = runp(
            "native:buffer",
            {
                "INPUT": lyr,
                "DISTANCE": 2.5,
                "SEGMENTS": 5,
                "END_CAP_STYLE": 0,
                "JOIN_STYLE": 0,
                "MITER_LIMIT": 2.0,
                "DISSOLVE": False,
                "OUTPUT": "memory:",
            },
        )["OUTPUT"]
        hedge_buffers.append(buf)

    # include hedgerows extracted from vegetation (buffered)
    if hedges_from_veg and hedges_from_veg.isValid() and hedges_from_veg.featureCount() > 0:
        buf_v = runp(
            "native:buffer",
            {
                "INPUT": hedges_from_veg,
                "DISTANCE": 2.5,
                "SEGMENTS": 5,
                "END_CAP_STYLE": 0,
                "JOIN_STYLE": 0,
                "MITER_LIMIT": 2.0,
                "DISSOLVE": False,
                "OUTPUT": "memory:",
            },
        )["OUTPUT"]
        hedge_buffers.append(buf_v)

    # --- 7) Merge -> Dissolve -> MultipartToSingleParts (+ ID)
    if hedge_buffers:
        hedges_merged = runp(
            "native:mergevectorlayers",
            {"LAYERS": hedge_buffers, "CRS": crs_ref.authid(), "OUTPUT": "memory:"},
        )["OUTPUT"]

        hedges_diss = runp(
            "native:dissolve", {"INPUT": hedges_merged, "FIELD": [], "OUTPUT": "memory:"}
        )["OUTPUT"]

        hedges_single = runp(
            "native:multiparttosingleparts", {"INPUT": hedges_diss, "OUTPUT": "memory:"}
        )["OUTPUT"]

        if hedges_single.fields().indexFromName("ID") < 0:
            hedges_single.dataProvider().addAttributes([QgsField("ID", QVariant.Int)])
            hedges_single.updateFields()
        idx_id = hedges_single.fields().indexFromName("ID")
        with edit(hedges_single):
            for i, f in enumerate(hedges_single.getFeatures(), start=1):
                hedges_single.changeAttributeValue(f.id(), idx_id, i)
        hedges_final = hedges_single
    else:
        hedges_final = make_memory_layer("Polygon", crs_ref, name="hedges_empty")

    # --- 8) Exports
    if save_outputs:
        os.makedirs(output_dir, exist_ok=True)
        runp(
            "native:savefeatures",
            {
                "INPUT": veg_without_hedges,
                "OUTPUT": os.path.join(output_dir, f"Vegetation_{area_name_clean}.gpkg"),
            },
        )
        runp(
            "native:savefeatures",
            {
                "INPUT": hedges_final,
                "OUTPUT": os.path.join(output_dir, f"Hedges_{area_name_clean}.gpkg"),
            },
        )

    return veg_without_hedges, hedges_final


# =====================================================================
# 6) RPG agricultural parcels
# =====================================================================


def process_rpg_data(
    base_dir: str,
    extent_geom: QgsGeometry,
    crs: QgsCoordinateReferenceSystem,
    save_outputs: bool,
    output_dir: str,
    area_name_clean: str,
) -> QgsVectorLayer:
    """Load and clip RPG parcels data for intersecting regions.

    This routine implements a PyQGIS version of the original
    ``process_rpg_data``.  It identifies administrative regions
    intersecting the study area, finds RPG parcel files for those
    regions, clips them to the ``extent_geom`` and merges the
    results into a single memory layer.  Only polygon and
    multipolygon features are kept, and geometries are clipped to
    the extent.

    Args:
        base_dir: Directory containing RPG data (shapefiles).
        extent_geom: Geometry defining the clipping area.
        crs: Target coordinate reference system.
        save_outputs: Whether to write the merged layer to disk.
        output_dir: Directory for saving outputs.
        area_name_clean: Clean area identifier used in output filename.

    Returns:
        A ``QgsVectorLayer`` containing the merged, clipped RPG parcels.
    """
    # Step 1: find region files and determine intersecting region codes
    region_layers: List[QgsVectorLayer] = []
    for root, _, files in os.walk(base_dir):
        for fname in files:
            fl = fname.lower()
            path = None
            if fl == "region.shp":
                path = os.path.join(root, fname)
            elif fl.endswith(".gpkg"):
                gp = os.path.join(root, fname)
                if any(s.upper() == "REGION" for s in gpkg_sublayers(gp)):
                    path = f"{gp}|layername=region"
            if path:
                lyr = QgsVectorLayer(path, "region", "ogr")
                if not lyr.isValid():
                    continue
                # Reproject to target CRS if necessary
                if lyr.crs() != crs:
                    lyr = processing.run(
                        "native:reprojectlayer",
                        {
                            "INPUT": lyr,
                            "TARGET_CRS": crs.authid(),
                            "OPERATION": "",
                            "OUTPUT": "memory:",
                        },
                    )["OUTPUT"]
                region_layers.append(lyr)
    if not region_layers:
        raise RuntimeError("No REGION files found for RPG data")
    merge_regions = processing.run(
        "native:mergevectorlayers",
        {"LAYERS": region_layers, "CRS": crs.authid(), "OUTPUT": "memory:"},
    )["OUTPUT"]
    # Determine region code field.
    #   legacy Shapefile model: INSEE_REG
    #   new GeoPackage model (region layer): code_insee
    names = {fld.name().upper(): fld.name() for fld in merge_regions.fields()}
    code_field = None
    for cand in ("INSEE_REG", "CODE_INSEE", "INSEE", "CODE_REG"):
        if cand in names:
            code_field = names[cand]
            break
    if code_field is None:
        for up, real in names.items():
            if "REG" in up or "INSEE" in up:
                code_field = real
                break
    if code_field is None:
        raise RuntimeError("Cannot determine region code field in REGION layer")
    # Find intersecting region codes
    region_codes: set[str] = set()
    for feat in merge_regions.getFeatures():
        if feat.geometry().intersects(extent_geom):
            val = feat[code_field]
            if val is not None:
                region_codes.add(str(val))
    if not region_codes:
        raise RuntimeError("No regions intersect with the study area")

    # Step 2: find RPG parcel layers (SHP file OR GeoPackage sublayer), robuste
    # robust to naming across vintages/deliveries:
    #   1) par le NOM (token "PARCELLE" : PARCELLES_GRAPHIQUES.shp, RPG_Parcelles…) ;
    #   2) otherwise by SCHEMA (layer carrying a 'code_group' field, which is
    #      the characteristic RPG parcel field) - when the name differs.
    def _rpg_parcel_uris(root_dir):
        by_name, by_field = [], []
        for root, _, files in os.walk(root_dir):
            for fname in files:
                fu = fname.upper()
                full = os.path.join(root, fname)
                if fu.endswith(".SHP"):
                    if "PARCELLE" in fu:
                        by_name.append(full)
                elif fu.endswith(".GPKG"):
                    for ss in gpkg_sublayers(full):
                        if "PARCELLE" in ss.upper():
                            by_name.append(f"{full}|layername={ss}")
                    for ss in gpkg_sublayers_with_field(full, "code_group"):
                        by_field.append(f"{full}|layername={ss}")
        # Name match takes priority; otherwise field-based detection (one layer per gpkg).
        if by_name:
            return by_name
        seen_gpkg = set()
        dedup = []
        for u in by_field:
            gp = u.split("|layername=")[0]
            if gp not in seen_gpkg:
                seen_gpkg.add(gp)
                dedup.append(u)
        return dedup

    all_uris = _rpg_parcel_uris(base_dir)
    rpg_files = [u for u in all_uris if any(code in u for code in region_codes)]
    if not rpg_files:
        rpg_files = all_uris
        if not rpg_files:
            raise RuntimeError("No RPG parcel layer (.shp or .gpkg sublayer) found")
    # Step 3: clip each RPG file to extent.  We avoid filtering on geometry type
    # to ensure that all parcel features (usually polygons) are retained even
    # if their geometry type is non‑standard in the metadata.
    extent_layer = create_extent_layer(extent_geom, crs)
    clipped_layers: List[QgsVectorLayer] = []
    for path in rpg_files:
        try:
            # Clip to extent
            clip_res = processing.run(
                "native:clip", {"INPUT": path, "OVERLAY": extent_layer, "OUTPUT": "memory:"}
            )
            layer = clip_res["OUTPUT"]
            if layer and layer.isValid() and layer.featureCount() > 0:
                clipped_layers.append(layer)
        except Exception:
            continue
    if not clipped_layers:
        raise RuntimeError("Failed to process RPG files for the study area")
    # Step 4: merge all clipped polygon layers
    merged_rpg = processing.run(
        "native:mergevectorlayers",
        {"LAYERS": clipped_layers, "CRS": crs.authid(), "OUTPUT": "memory:"},
    )["OUTPUT"]
    # Save output if requested
    if save_outputs:
        out_path = os.path.join(output_dir, f"RPG_{area_name_clean}.gpkg")
        write_vector_layer(merged_rpg, out_path)
    return merged_rpg


# =====================================================================
# 7) Internal helper: attribute field creation
# =====================================================================


def _ensure_field(layer: QgsVectorLayer, name: str, qvariant_type=QVariant.Double):
    if layer.fields().indexFromName(name) < 0:
        pr = layer.dataProvider()
        pr.addAttributes([QgsField(name, qvariant_type)])
        layer.updateFields()


# =====================================================================
# 8) Hydrographic network (BD TOPO)
# =====================================================================


def process_hydrography_network(
    base_dir: str,
    output_dir: str,
    extent: QgsGeometry,  # emprise (QgsGeometry) dans crs_ref
    area_named_clean: str,
    crs_ref: QgsCoordinateReferenceSystem,
    save_outputs: bool = True,
) -> QgsVectorLayer:
    """
    BD TOPO hydrography pipeline (SURFACE + TRONCON), aligned on the GeoPandas script.
    NOTE: a FINAL CLIP removes the overshoot introduced by the buffers.
    """
    # --- 0) Preparation
    debug_dir = os.path.join(output_dir, "hydro_processing")
    if save_outputs:
        os.makedirs(debug_dir, exist_ok=True)

    # Ensure an extent layer (same CRS) is available for clipping
    extent_layer = create_extent_layer(extent, crs_ref)

    # --- 1) sources
    surface_files = find_files_fuzzy(base_dir, "SURFACE_HYDROGRAPHIQUE")
    section_files = find_files_fuzzy(base_dir, "TRONCON_HYDROGRAPHIQUE")
    if not surface_files or not section_files:
        raise RuntimeError(
            "❌ Missing hydrography data (SURFACE_HYDROGRAPHIQUE and/or TRONCON_HYDROGRAPHIQUE)."
        )

    # --- 2) SURFACES: clip + POS_SOL >= 0 (numeric)
    clipped_surfaces: List[QgsVectorLayer] = []
    for src_path in surface_files:
        # Load + preventive reprojection (avoids inaccurate clipping)
        lyr_src = QgsVectorLayer(src_path, "temp_hydro_surf", "ogr")
        if not lyr_src.isValid():
            continue

        if lyr_src.crs() != crs_ref:
            lyr_src = processing.run(
                "native:reprojectlayer",
                {"INPUT": lyr_src, "TARGET_CRS": crs_ref.authid(), "OUTPUT": "memory:"},
            )["OUTPUT"]

        # Clip initial
        lyr = processing.run(
            "native:clip", {"INPUT": lyr_src, "OVERLAY": extent_layer, "OUTPUT": "memory:"}
        )["OUTPUT"]

        if not lyr or not lyr.isValid() or lyr.featureCount() == 0:
            continue

        if lyr.fields().indexFromName("POS_SOL") >= 0:
            lyr = processing.run(
                "native:extractbyexpression",
                {
                    "INPUT": lyr,
                    "EXPRESSION": 'coalesce(to_real("POS_SOL"), -999999) >= 0',
                    "OUTPUT": "memory:",
                },
            )["OUTPUT"]
        clipped_surfaces.append(lyr)

    if not clipped_surfaces:
        surfaces = make_memory_layer("Polygon", crs_ref, name="Surface_Hydro_vide")
    elif len(clipped_surfaces) == 1:
        surfaces = clipped_surfaces[0]
    else:
        surfaces = processing.run(
            "native:mergevectorlayers",
            {"LAYERS": clipped_surfaces, "CRS": crs_ref.authid(), "OUTPUT": "memory:"},
        )["OUTPUT"]

    # clean topo
    if surfaces and surfaces.isValid() and surfaces.featureCount() > 0:
        surfaces = processing.run("native:fixgeometries", {"INPUT": surfaces, "OUTPUT": "memory:"})[
            "OUTPUT"
        ]

    # --- 3) TRONÇONS : clip + POS_SOL + FICTIF != 'OUI'
    clipped_sections: List[QgsVectorLayer] = []
    for src_path in section_files:
        # Load + preventive reprojection
        lyr_src = QgsVectorLayer(src_path, "temp_hydro_line", "ogr")
        if not lyr_src.isValid():
            continue

        if lyr_src.crs() != crs_ref:
            lyr_src = processing.run(
                "native:reprojectlayer",
                {"INPUT": lyr_src, "TARGET_CRS": crs_ref.authid(), "OUTPUT": "memory:"},
            )["OUTPUT"]

        # Clip initial
        lyr = processing.run(
            "native:clip", {"INPUT": lyr_src, "OVERLAY": extent_layer, "OUTPUT": "memory:"}
        )["OUTPUT"]

        if not lyr or not lyr.isValid() or lyr.featureCount() == 0:
            continue

        if lyr.fields().indexFromName("POS_SOL") >= 0:
            lyr = processing.run(
                "native:extractbyexpression",
                {
                    "INPUT": lyr,
                    "EXPRESSION": 'coalesce(to_real("POS_SOL"), -999999) >= 0',
                    "OUTPUT": "memory:",
                },
            )["OUTPUT"]

        if lyr.fields().indexFromName("FICTIF") >= 0:
            lyr = processing.run(
                "native:extractbyexpression",
                {
                    "INPUT": lyr,
                    "EXPRESSION": "upper(trim(coalesce(\"FICTIF\",'NON'))) <> 'OUI'",
                    "OUTPUT": "memory:",
                },
            )["OUTPUT"]

        clipped_sections.append(lyr)

    if not clipped_sections:
        sections = make_memory_layer("LineString", crs_ref, name="Troncon_Hydro_vide")
    elif len(clipped_sections) == 1:
        sections = clipped_sections[0]
    else:
        sections = processing.run(
            "native:mergevectorlayers",
            {"LAYERS": clipped_sections, "CRS": crs_ref.authid(), "OUTPUT": "memory:"},
        )["OUTPUT"]

    # --- 4) Buffer 2.5 m (this is what creates the overshoot)
    if sections and sections.isValid() and sections.featureCount() > 0:
        sections_buf = processing.run(
            "native:buffer",
            {
                "INPUT": sections,
                "DISTANCE": 2.5,
                "SEGMENTS": 5,
                "END_CAP_STYLE": 0,
                "JOIN_STYLE": 0,
                "MITER_LIMIT": 2,
                "DISSOLVE": False,
                "OUTPUT": "memory:",
            },
        )["OUTPUT"]
        sections_buf = processing.run(
            "native:fixgeometries", {"INPUT": sections_buf, "OUTPUT": "memory:"}
        )["OUTPUT"]
    else:
        sections_buf = make_memory_layer("Polygon", crs_ref, name="Troncon_buffer_vide")

    # --- 5) Split PERMANENT / INTERMITTENT + difference
    # --- 5) Split PERMANENT / INTERMITTENT + difference
    if sections_buf.fields().indexFromName("PERSISTANC") >= 0:
        perm = processing.run(
            "native:extractbyexpression",
            {
                "INPUT": sections_buf,
                "EXPRESSION": "upper(trim(coalesce(\"PERSISTANC\",''))) = 'PERMANENT'",
                "OUTPUT": "memory:",
            },
        )["OUTPUT"]
        interm = processing.run(
            "native:extractbyexpression",
            {
                "INPUT": sections_buf,
                "EXPRESSION": "upper(trim(coalesce(\"PERSISTANC\",''))) = 'INTERMITTENT'",
                "OUTPUT": "memory:",
            },
        )["OUTPUT"]

        if perm and perm.featureCount() > 0 and interm and interm.featureCount() > 0:
            # Optimization: Do NOT dissolve 'perm'. native:difference handles it.
            interm_clean = processing.run(
                "native:difference", {"INPUT": interm, "OVERLAY": perm, "OUTPUT": "memory:"}
            )["OUTPUT"]
            sections_clean = processing.run(
                "native:mergevectorlayers",
                {"LAYERS": [perm, interm_clean], "CRS": crs_ref, "OUTPUT": "memory:"},
            )["OUTPUT"]
        else:
            sections_clean = sections_buf
    else:
        sections_clean = sections_buf

    # --- 6) Remove reaches overlapping SURFACES
    if (
        surfaces
        and surfaces.isValid()
        and surfaces.featureCount() > 0
        and sections_clean
        and sections_clean.isValid()
        and sections_clean.featureCount() > 0
    ):
        # Optimization: Do NOT dissolve 'surfaces'.
        sections_no_surface = processing.run(
            "native:difference", {"INPUT": sections_clean, "OVERLAY": surfaces, "OUTPUT": "memory:"}
        )["OUTPUT"]
    else:
        sections_no_surface = sections_clean

    # --- 7) Transfert LARGEUR (max)
    _ensure_field(surfaces, "LARGEUR", QVariant.Double)
    if sections_no_surface.fields().indexFromName("LARGEUR") >= 0 and surfaces.featureCount() > 0:
        params = {
            "INPUT": surfaces,
            "JOIN": sections_no_surface,
            "PREDICATE": [0],
            "JOIN_FIELDS": ["LARGEUR"],
            "SUMMARIES": [5],
            "DISCARD_NONMATCHING": False,
            "OUTPUT": "memory:",
        }
        try:
            joined = processing.run("native:joinbylocationsummary", params)["OUTPUT"]
        except Exception:
            joined = processing.run("qgis:joinbylocationsummary", params)["OUTPUT"]

        idx_max = joined.fields().indexFromName("LARGEUR_max")
        idx_larg = joined.fields().indexFromName("LARGEUR")
        if idx_max >= 0 and idx_larg >= 0:
            with edit(joined):
                for f in joined.getFeatures():
                    f["LARGEUR"] = f["LARGEUR_max"]
                    joined.updateFeature(f)
        surfaces_with_width = joined
    else:
        surfaces_with_width = surfaces

    # --- 8) SOURCE + field harmonisation + final merge
    _ensure_field(surfaces_with_width, "SOURCE", QVariant.String)
    _ensure_field(sections_no_surface, "SOURCE", QVariant.String)
    with edit(surfaces_with_width):
        for f in surfaces_with_width.getFeatures():
            f["SOURCE"] = "surface"
            surfaces_with_width.updateFeature(f)
    with edit(sections_no_surface):
        for f in sections_no_surface.getFeatures():
            f["SOURCE"] = "troncon"
            sections_no_surface.updateFeature(f)

    # harmoniser champs
    surf_names = [f.name() for f in surfaces_with_width.fields()]
    sect_names = [f.name() for f in sections_no_surface.fields()]
    common = [n for n in surf_names if n in sect_names]

    def _retain_fields(vlayer: QgsVectorLayer, wanted: List[str]) -> QgsVectorLayer:
        return processing.run(
            "native:retainfields", {"INPUT": vlayer, "FIELDS": wanted, "OUTPUT": "memory:"}
        )["OUTPUT"]

    surf_view = _retain_fields(surfaces_with_width, common) if common else surfaces_with_width
    sect_view = _retain_fields(sections_no_surface, common) if common else sections_no_surface

    hydro_merged = processing.run(
        "native:mergevectorlayers",
        {"LAYERS": [surf_view, sect_view], "CRS": crs_ref, "OUTPUT": "memory:"},
    )["OUTPUT"]

    # --- 9) FINAL CLIP (removes buffer overshoot)
    # Re-clip whatever overshoots (caused by the 2.5 m end buffers)
    hydro_final = processing.run(
        "native:clip", {"INPUT": hydro_merged, "OVERLAY": extent_layer, "OUTPUT": "memory:"}
    )["OUTPUT"]

    # --- 10) Export
    if save_outputs and hydro_final and hydro_final.isValid() and hydro_final.featureCount() > 0:
        write_vector_layer(hydro_final, os.path.join(output_dir, f"Hydro_{area_named_clean}.gpkg"))
    else:
        print("⚠️ Hydro layer not saved (empty or invalid).")

    return hydro_final


# =====================================================================
# 9) Technical infrastructure (BD TOPO)
# =====================================================================


def process_technical_infrastructure(
    base_dir: str,
    output_dir: str,
    extent: QgsGeometry,  # extent as QgsGeometry (in crs_ref)
    area_name_clean: str,
    crs_ref: QgsCoordinateReferenceSystem,
    save_outputs: bool = True,
) -> QgsVectorLayer:
    """
    Traite BD TOPO pour : CIMETIERE, RESERVOIR, TERRAIN_DE_SPORT, POSTE_DE_TRANSFORMATION, AERODROME.
    """

    # Relevant columns (BD TOPO names may vary between vintages)
    columns_by_type = {
        "CIMETIERE": ["ID", "NATURE", "NAT_DETAIL", "ETAT"],
        "RESERVOIR": ["ID", "NATURE", "ETAT", "HAUTEUR"],
        "TERRAIN_DE_SPORT": ["ID", "ETAT", "NAT_DETAIL", "NATURE"],
        "POSTE_DE_TRANSFORMATION": ["ID", "TOPONYME", "ETAT"],
        "AERODROME": ["ID", "CATEGORIE", "NATURE", "USAGE", "FICTIF", "ETAT"],
    }

    layers = list(columns_by_type.keys())

    # Extent layer in the target CRS (IMPORTANT: `extent` must already be in `crs_ref`)
    extent_layer = create_extent_layer(extent, crs_ref)

    # Build a **generic** in-memory output layer (UnknownGeometry) and add the
    # features while preserving their original geometry type. To avoid mixing
    # geometry types in a single layer, features are merged **per type**:
    collected_by_geom = {"Point": [], "LineString": [], "Polygon": []}

    # Helper: add a batch of features to a container, keeping only the useful columns + TYPE
    def _collect_filtered(src_layer: QgsVectorLayer, keep_cols: List[str], fixed_type_value: str):
        if not src_layer or not src_layer.isValid() or src_layer.featureCount() == 0:
            return

        # Determine the geometry type
        geom_str = QgsWkbTypes.displayString(src_layer.wkbType()).split()[
            -1
        ]  # 'Point', 'LineString', 'Polygon'
        if geom_str not in collected_by_geom:
            # unsupported geometry (multiXYZZ etc.) -> fall back to the simple type
            if "POINT" in geom_str.upper():
                geom_str = "Point"
            elif "LINE" in geom_str.upper():
                geom_str = "LineString"
            else:
                geom_str = "Polygon"

        # Prepare an in-memory layer with the filtered fields + TYPE
        fields = QgsFields()
        for k in keep_cols:
            if k in [f.name() for f in src_layer.fields()]:
                fields.append(src_layer.fields().field(src_layer.fields().indexFromName(k)))
        # Add TYPE if missing
        if "TYPE" not in [f.name() for f in fields]:
            fields.append(QgsField("TYPE", QVariant.String))

        out = make_memory_layer(geom_str, crs_ref, fields, f"{fixed_type_value}_mem")
        pr = out.dataProvider()

        # Normalisation cas AERODROME + autres via lecture attributaire
        # (On filtre en amont pour ne copier que ce qui convient)
        feats_to_add = []
        for feat in src_layer.getFeatures():
            attrs = {f.name(): feat[f.name()] for f in src_layer.fields()}
            # Build the filtered attribute dict
            kept_attrs = []
            for k in keep_cols:
                if k in attrs:
                    kept_attrs.append(attrs[k])
                else:
                    # Colonne utile absente -> None
                    kept_attrs.append(None)

            # Filtres
            if fixed_type_value == "AERODROME":
                # cols possibles : ETAT, NATURE, USAGE, FICTIF
                etat = str(attrs.get("ETAT", "")).strip().lower()
                nature = str(attrs.get("NATURE", "")).strip().lower()
                usage = str(attrs.get("USAGE", "")).strip().lower()
                fictif = str(attrs.get("FICTIF", "")).strip().lower()
                if not (
                    etat == "en service"
                    and nature == "aérodrome"
                    and usage == "civil"
                    and fictif == "non"
                ):
                    continue
            else:
                etat = str(attrs.get("ETAT", "")).strip().upper()
                if etat and etat != "EN SERVICE":
                    continue

            # Construire la nouvelle feature
            nf = QgsFeature(out.fields())
            nf.setGeometry(feat.geometry())
            # Fill the filtered attributes + TYPE
            # Ensure the field order (keep_cols then TYPE) by rebuilding a
            # field -> index mapping
            field_order = [f.name() for f in out.fields()]
            values = []
            idx_keep = 0
            for name in field_order:
                if name == "TYPE":
                    values.append(fixed_type_value)
                else:
                    values.append(kept_attrs[idx_keep] if idx_keep < len(kept_attrs) else None)
                    idx_keep += 1
            nf.setAttributes(values)
            feats_to_add.append(nf)

        if feats_to_add:
            pr.addFeatures(feats_to_add)
            out.updateExtents()
            collected_by_geom[geom_str].append(out)

    # === Recherche et traitement, couche par couche ===
    # On cherche directement par nom de classe (fichiers BD TOPO ont les tokens dans le nom)
    name_tokens = {
        "CIMETIERE": "CIMETIERE",
        "RESERVOIR": "RESERVOIR",
        "TERRAIN_DE_SPORT": "TERRAIN_DE_SPORT",
        "POSTE_DE_TRANSFORMATION": "POSTE_DE_TRANSFORMATION",
        "AERODROME": "AERODROME",
    }

    for layer_name in layers:
        token = name_tokens[layer_name]
        cand_files = find_files_fuzzy(base_dir, token, exts=(".shp", ".gpkg"))
        if not cand_files:
            continue
        for src in cand_files:
            # Clip to the extent (QGIS reprojects if needed)
            try:
                res = processing.run(
                    "native:clip", {"INPUT": src, "OVERLAY": extent_layer, "OUTPUT": "memory:"}
                )
                clipped = res["OUTPUT"]
            except Exception:
                clipped = None

            if not clipped or clipped.featureCount() == 0:
                continue
            # Collecter avec filtres et mapping de colonnes
            _collect_filtered(clipped, columns_by_type[layer_name], layer_name)

    # === Final merge per geometry type, then global merge ===
    merged_layers = []
    for gtype, parts in collected_by_geom.items():
        if not parts:
            continue
        if len(parts) == 1:
            merged_layers.append(parts[0])
        else:
            res = processing.run(
                "native:mergevectorlayers", {"LAYERS": parts, "CRS": crs_ref, "OUTPUT": "memory:"}
            )
            merged_layers.append(res["OUTPUT"])

    if not merged_layers:
        # Nothing found -> return an empty layer (polygon by default)
        return make_memory_layer("Polygon", crs_ref, QgsFields(), "Technical_infra_empty")

    if len(merged_layers) == 1:
        final_layer = merged_layers[0]
    else:
        res = processing.run(
            "native:mergevectorlayers",
            {"LAYERS": merged_layers, "CRS": crs_ref, "OUTPUT": "memory:"},
        )
        final_layer = res["OUTPUT"]

    # Export
    if save_outputs and final_layer and final_layer.isValid() and final_layer.featureCount() > 0:
        out_path = os.path.join(output_dir, f"Technical_infrastructures_{area_name_clean}.gpkg")
        write_vector_layer(final_layer, out_path)

    return final_layer


# =====================================================================
# 10) Internal helper: tolerant float conversion
# =====================================================================


def _to_float(v):
    """Robust conversion, equivalent to pandas.to_numeric(errors='coerce')."""
    try:
        if v is None:
            return None
        s = str(v).strip()
        if s == "" or s.lower() in {"nan", "none"}:
            return None
        s = s.replace(",", ".")
        return float(s)
    except Exception:
        return None


# =====================================================================
# 11) Wildlife crossings (ORFeH)
# =====================================================================


def process_wildlife_crossing(
    base_dir: str,
    output_dir: str,
    extent: QgsGeometry,  # extent (same CRS as crs_ref)
    crs_ref: QgsCoordinateReferenceSystem,
    area_name_clean: str,
    save_outputs: bool = True,
) -> Tuple[QgsVectorLayer, QgsVectorLayer]:
    """
    ORFeH — traduction 1:1 du script GeoPandas.
    """

    # 1) locate the ORFeH file (same name as in the original script)
    # find_files_fuzzy returns a list, we take the first one
    candidates = find_files_fuzzy(base_dir, "ORFeH_NATIONAL_fusione_V2", exts=(".shp", ".gpkg"))
    crossing_file = candidates[0] if candidates else None

    empty_pts = make_memory_layer("Point", crs_ref, name="ORFeH_points_empty")
    empty_buf = make_memory_layer("Polygon", crs_ref, name="ORFeH_buffer_empty")

    if not crossing_file:
        print("[ORFeH] source introuvable")
        return empty_pts, empty_buf

    # 2) clip (QGIS reprojects if needed)
    extent_layer = create_extent_layer(extent, crs_ref)
    clipped: QgsVectorLayer = processing.run(
        "native:clip", {"INPUT": crossing_file, "OVERLAY": extent_layer, "OUTPUT": "memory:"}
    )["OUTPUT"]

    if (not clipped) or (not clipped.isValid()) or clipped.featureCount() == 0:
        print("[ORFeH] after clip: 0 features")
        return empty_pts, empty_buf

    print(f"[ORFeH] after clip: {clipped.featureCount()} features")

    # 3) fields used (exactly those of the pandas script)
    needed = ["Franch_Ong", "OA_Type_p", "OA_Franc_p", "OA_Larg_p", "OA_Long_p"]
    present = {n for n in needed if clipped.fields().indexFromName(n) >= 0}
    for n in needed:
        if n not in present:
            print(f"[ORFeH]⚠️ champ manquant : {n}")

    # 4) passability + type filter (STRICT, as in GeoPandas: accents preserved)
    #    Franch_Ong.upper() == 'FRANCHISSABLE'
    #    OA_Type_p.upper() != 'TUNNEL OU TRANCHÉE COUVERTE'
    kept_after_pass_type = []
    for f in clipped.getFeatures():
        franch = (str(f["Franch_Ong"]) if "Franch_Ong" in present else "").upper()
        if franch != "FRANCHISSABLE":
            continue
        type_up = (str(f["OA_Type_p"]) if "OA_Type_p" in present else "").upper()
        if type_up == "TUNNEL OU TRANCHÉE COUVERTE":
            continue
        kept_after_pass_type.append(f)

    print(f"[ORFeH] after passability + type filter: {len(kept_after_pass_type)} features")

    if not kept_after_pass_type:
        # nothing to keep -> return empty
        return empty_pts, empty_buf

    # 5) appliquer la logique valid_crossing du script d'origine
    #    passage_type = OA_Franc_p (minuscules)
    #    - 'passage sous ilt' : largeur >= 20 ET (longueur NA ou <= 32)
    #    - 'passage sur ilt' : largeur >= 12
    final_feats = []
    for f in kept_after_pass_type:
        passage_type = str(f["OA_Franc_p"]).strip().lower() if "OA_Franc_p" in present else ""
        w = _to_float(f["OA_Larg_p"]) if "OA_Larg_p" in present else None
        length = _to_float(f["OA_Long_p"]) if "OA_Long_p" in present else None

        keep = True
        if passage_type == "passage sous ilt":
            keep = (w is not None and w >= 20) and (length is None or length <= 32)
        elif passage_type == "passage sur ilt":
            keep = w is not None and w >= 12

        if keep:
            final_feats.append(f)

    print(f"[ORFeH] kept (type/dimensions): {len(final_feats)} features")

    # 6) build the retained-points layer (same fields as the clipped input)
    if not final_feats:
        return empty_pts, empty_buf

    kept_layer = make_memory_layer("Point", crs_ref, clipped.fields(), "ORFeH_kept")
    pr = kept_layer.dataProvider()
    new_feats = []
    for f in final_feats:
        nf = QgsFeature(kept_layer.fields())
        nf.setGeometry(f.geometry())
        nf.setAttributes([f[i.name()] for i in clipped.fields()])
        new_feats.append(nf)
    pr.addFeatures(new_feats)
    kept_layer.updateExtents()

    # 7) buffer 10 m
    buf_layer = processing.run(
        "native:buffer",
        {
            "INPUT": kept_layer,
            "DISTANCE": 10.0,
            "SEGMENTS": 5,
            "END_CAP_STYLE": 0,
            "JOIN_STYLE": 0,
            "MITER_LIMIT": 2,
            "DISSOLVE": False,
            "OUTPUT": "memory:",
        },
    )["OUTPUT"]

    print(f"[ORFeH] 10 m buffers: {buf_layer.featureCount() if buf_layer else 0} features")

    # 8) exports (format au choix — ici GPKG pour la robustesse)
    if save_outputs and kept_layer.isValid() and kept_layer.featureCount() > 0:
        write_vector_layer(kept_layer, os.path.join(output_dir, f"ORFeH_{area_name_clean}.gpkg"))
    if save_outputs and buf_layer and buf_layer.isValid() and buf_layer.featureCount() > 0:
        write_vector_layer(
            buf_layer, os.path.join(output_dir, f"ORFeH_{area_name_clean}_buffer10m.gpkg")
        )

    return kept_layer, buf_layer


# =====================================================================
# 12) Linear transport infrastructure (ILT)
# =====================================================================


def process_linear_transport_infrastructure(
    base_dir: str,
    output_dir: str,
    extent: QgsGeometry,
    area_name_clean: str,
    crs_ref: QgsCoordinateReferenceSystem,
    ocs_layer: QgsVectorLayer,
    orfeh_buffer_layer: Optional[QgsVectorLayer] = None,
    save_outputs: bool = True,
) -> QgsVectorLayer:
    # ---------- Helpers ----------
    def _clip_merge(paths, crs, extent_layer) -> Optional[QgsVectorLayer]:
        clipped = []
        for p in paths:
            try:
                lyr = processing.run(
                    "native:clip", {"INPUT": p, "OVERLAY": extent_layer, "OUTPUT": "memory:"}
                )["OUTPUT"]
                if lyr and lyr.isValid() and lyr.featureCount() > 0:
                    # Harmonise les noms de champs en MAJUSCULES (nature→NATURE…)
                    # pour que les filtres/normalisations suivants fonctionnent
                    # for both the legacy and the new data model.
                    clipped.append(normalize_fields(lyr))
            except Exception:
                continue
        if not clipped:
            return None
        if len(clipped) == 1:
            return clipped[0]
        return processing.run(
            "native:mergevectorlayers",
            {"LAYERS": clipped, "CRS": crs.authid(), "OUTPUT": "memory:"},
        )["OUTPUT"]

    def _ensure_field(layer: QgsVectorLayer, name: str, qtype=QVariant.Int):
        if layer.fields().indexFromName(name) < 0:
            layer.dataProvider().addAttributes([QgsField(name, qtype)])
            layer.updateFields()

    def _set_const_fields(layer: QgsVectorLayer, values: dict):
        with edit(layer):
            for f in layer.getFeatures():
                for k, v in values.items():
                    idx = layer.fields().indexFromName(k)
                    if idx >= 0:
                        layer.changeAttributeValue(f.id(), idx, v)

    def _variable_buffer_safe(layer: QgsVectorLayer, field_name: str) -> QgsVectorLayer:
        if not layer or not layer.isValid() or layer.featureCount() == 0:
            return layer
        try:
            out = processing.run(
                "qgis:variabledistancebuffer",
                {
                    "INPUT": layer,
                    "FIELD": field_name,
                    "SEGMENTS": 5,
                    "DISSOLVE": False,
                    "OUTPUT": "memory:",
                },
            )["OUTPUT"]
            if out and out.isValid():
                return out
        except Exception:
            pass
        lyr = processing.run("native:fixgeometries", {"INPUT": layer, "OUTPUT": "memory:"})[
            "OUTPUT"
        ]
        lyr = processing.run("native:multiparttosingleparts", {"INPUT": lyr, "OUTPUT": "memory:"})[
            "OUTPUT"
        ]
        out = processing.run(
            "native:geometrybyexpression",
            {
                "INPUT": lyr,
                "EXPRESSION": f'buffer($geometry, coalesce("{field_name}", 0), 5)',
                "OUTPUT": "memory:",
            },
        )["OUTPUT"]
        return out

    def _remove_segments_by_orfeh(
        roads_layer: QgsVectorLayer, rails_layer: QgsVectorLayer, orfeh_buf: QgsVectorLayer
    ) -> (QgsVectorLayer, QgsVectorLayer):
        if not orfeh_buf or not orfeh_buf.isValid() or orfeh_buf.featureCount() == 0:
            return roads_layer, rails_layer

        orf_fix = processing.run("native:fixgeometries", {"INPUT": orfeh_buf, "OUTPUT": "memory:"})[
            "OUTPUT"
        ]
        orf_dis = processing.run(
            "native:dissolve", {"INPUT": orf_fix, "FIELD": [], "OUTPUT": "memory:"}
        )["OUTPUT"]
        orf_geom = None
        for ff in orf_dis.getFeatures():
            orf_geom = ff.geometry() if orf_geom is None else orf_geom.combine(ff.geometry())
        if orf_geom is None:
            return roads_layer, rails_layer

        correspondence_types = {
            "autoroute": ["TYPE AUTOROUTIER", "BRETELLE"],
            "route": ["ROUTE À 1 CHAUSSÉE", "ROUTE À 2 CHAUSSÉES", "ROND-POINT"],
            "chemin ou sentier": ["CHEMIN", "ROUTE EMPIERRÉE", "SENTIER"],
            "voie ferree": [
                "VOIE FERRÉE PRINCIPALE",
                "VOIE DE SERVICE",
                "SANS OBJET",
                "TRAMWAY",
                "MÉTRO",
                "FUNICULAIRE OU CRÉMAILLÈRE",
                "LGV",
            ],
            "lgv": [
                "VOIE FERRÉE PRINCIPALE",
                "VOIE DE SERVICE",
                "SANS OBJET",
                "TRAMWAY",
                "MÉTRO",
                "FUNICULAIRE OU CRÉMAILLÈRE",
                "LGV",
            ],
        }
        merge_roads = {
            "route nationale": "route",
            "route departementale": "route",
            "autre route": "route",
        }
        rail_synonyms = {
            "voie ferree": "voie ferree",
            "voie ferrée": "voie ferree",
            "lgv": "lgv",
            "ligne grande vitesse": "lgv",
        }

        delete_road_natures, delete_rail_natures = set(), set()
        orfeh_fields = [fi.name() for fi in orfeh_buf.fields()]
        has_pass = "OA_Franc_p" in orfeh_fields
        has_type = "ILT_Type" in orfeh_fields

        for f in orfeh_buf.getFeatures():
            if has_pass:
                fr = str(f["OA_Franc_p"]).strip().lower()
                if fr not in ("passage sous ilt", "passage sur ilt"):
                    continue
            raw_type = str(f["ILT_Type"]).strip().lower() if has_type else ""
            lti_type = rail_synonyms.get(raw_type, merge_roads.get(raw_type, raw_type))
            if lti_type in correspondence_types:
                targets = correspondence_types[lti_type]
                if lti_type in ("voie ferree", "lgv"):
                    delete_rail_natures.update(targets)
                else:
                    delete_road_natures.update(targets)

        def _copy_filtered(src: QgsVectorLayer, is_road: bool) -> QgsVectorLayer:
            out = QgsVectorLayer(
                f"{QgsWkbTypes.displayString(src.wkbType())}?crs={src.crs().authid()}",
                src.name() + "_kept",
                "memory",
            )
            out.dataProvider().addAttributes(
                [QgsField(fi.name(), fi.type()) for fi in src.fields()]
            )
            out.updateFields()

            n_idx = src.fields().indexFromName("NATURE")
            tgt = delete_road_natures if is_road else delete_rail_natures

            feats_keep = []
            for g in src.getFeatures():
                nat = str(g["NATURE"]).upper() if n_idx >= 0 else ""
                if nat not in tgt:
                    feats_keep.append(g)
                else:
                    if not g.geometry().intersects(orf_geom):
                        feats_keep.append(g)

            if feats_keep:
                pr = out.dataProvider()
                new_feats = []
                for f0 in feats_keep:
                    nf = QgsFeature(out.fields())
                    nf.setGeometry(f0.geometry())
                    nf.setAttributes([f0[i.name()] for i in src.fields()])
                    new_feats.append(nf)
                pr.addFeatures(new_feats)
                out.updateExtents()
            return out

        roads_fixed = processing.run(
            "native:fixgeometries", {"INPUT": roads_layer, "OUTPUT": "memory:"}
        )["OUTPUT"]
        rails_fixed = processing.run(
            "native:fixgeometries", {"INPUT": rails_layer, "OUTPUT": "memory:"}
        )["OUTPUT"]
        return _copy_filtered(roads_fixed, True), _copy_filtered(rails_fixed, False)

    # ---------- 0) Preparation ----------
    debug_dir = os.path.join(output_dir, "debug_LTI")
    if save_outputs:
        os.makedirs(debug_dir, exist_ok=True)
    extent_layer = create_extent_layer(extent, crs_ref)

    # ---------- 1) Departments intersecting the extent ----------
    dep_codes = _get_intersecting_dept_codes(base_dir, extent, crs_ref)
    if not dep_codes:
        raise RuntimeError("❌ No department intersects the extent for the ILT step")

    # ---------- 2) Collecte ROUTES / RAILS (SHP fichiers OU sous-couches GPKG) ----------
    roads_files, rails_files = [], []
    for root, _, files in os.walk(base_dir):
        for f in files:
            full = os.path.join(root, f)
            up = f.upper()
            if not any(c in full for c in dep_codes):
                continue
            if up.endswith(".GPKG"):
                for sub in gpkg_sublayers(full):
                    su = sub.upper()
                    uri = f"{full}|layername={sub}"
                    if "TRONCON_DE_ROUTE" in su:
                        roads_files.append(uri)
                    elif "TRONCON_DE_VOIE_FERREE" in su:
                        rails_files.append(uri)
            elif up.endswith(".SHP"):
                if "TRONCON_DE_ROUTE" in up:
                    roads_files.append(full)
                elif "TRONCON_DE_VOIE_FERREE" in up:
                    rails_files.append(full)

    print(f"[DEBUG] Found {len(roads_files)} road files: {roads_files}")
    print(f"[DEBUG] Found {len(rails_files)} rail files: {rails_files}")

    if not roads_files:
        raise RuntimeError(f"❌ ROADS data not found for {sorted(dep_codes)}")
    if not rails_files:
        raise RuntimeError(f"❌ RAILWAY data not found for {sorted(dep_codes)}")

    # ---------- 3) Clip + filtres ----------
    roads = _clip_merge(roads_files, crs_ref, extent_layer)
    rails = _clip_merge(rails_files, crs_ref, extent_layer)
    if not roads or not roads.isValid():
        raise RuntimeError("Échec chargement ROUTES")
    if not rails or not rails.isValid():
        raise RuntimeError("Échec chargement VOIES FERRÉES")

    print(f"[ILT] roads after clip: {roads.featureCount()}, rails: {rails.featureCount()}")

    # POS_SOL >= 0 (CAST explicite comme pandas.to_numeric)
    if roads.fields().indexFromName("POS_SOL") >= 0:
        roads = processing.run(
            "native:extractbyexpression",
            {
                "INPUT": roads,
                "EXPRESSION": 'coalesce(to_real("POS_SOL"), -9999) >= 0',
                "OUTPUT": "memory:",
            },
        )["OUTPUT"]
    if rails.fields().indexFromName("POS_SOL") >= 0:
        rails = processing.run(
            "native:extractbyexpression",
            {
                "INPUT": rails,
                "EXPRESSION": 'coalesce(to_real("POS_SOL"), -9999) >= 0',
                "OUTPUT": "memory:",
            },
        )["OUTPUT"]

    print(f"[ILT] after POS_SOL>=0 -> roads: {roads.featureCount()}, rails: {rails.featureCount()}")

    # Normalisations texte
    if roads.fields().indexFromName("NATURE") >= 0:
        roads = processing.run(
            "native:fieldcalculator",
            {
                "INPUT": roads,
                "FIELD_NAME": "NATURE",
                "FIELD_TYPE": 2,
                "FIELD_LENGTH": 60,
                "NEW_FIELD": False,
                "FORMULA": 'upper("NATURE")',
                "OUTPUT": "memory:",
            },
        )["OUTPUT"]
    if roads.fields().indexFromName("FICTIF") >= 0:
        roads = processing.run(
            "native:fieldcalculator",
            {
                "INPUT": roads,
                "FIELD_NAME": "FICTIF",
                "FIELD_TYPE": 2,
                "FIELD_LENGTH": 20,
                "NEW_FIELD": False,
                "FORMULA": "upper(coalesce(\"FICTIF\",'NON'))",
                "OUTPUT": "memory:",
            },
        )["OUTPUT"]
    if roads.fields().indexFromName("ACCES_VL") >= 0:
        roads = processing.run(
            "native:fieldcalculator",
            {
                "INPUT": roads,
                "FIELD_NAME": "ACCES_VL",
                "FIELD_TYPE": 2,
                "FIELD_LENGTH": 60,
                "NEW_FIELD": False,
                "FORMULA": 'upper("ACCES_VL")',
                "OUTPUT": "memory:",
            },
        )["OUTPUT"]
    if rails.fields().indexFromName("NATURE") >= 0:
        rails = processing.run(
            "native:fieldcalculator",
            {
                "INPUT": rails,
                "FIELD_NAME": "NATURE",
                "FIELD_TYPE": 2,
                "FIELD_LENGTH": 60,
                "NEW_FIELD": False,
                "FORMULA": 'upper("NATURE")',
                "OUTPUT": "memory:",
            },
        )["OUTPUT"]

    # FICTIF != 'OUI'
    if roads.fields().indexFromName("FICTIF") >= 0:
        roads = processing.run(
            "native:extractbyexpression",
            {
                "INPUT": roads,
                "EXPRESSION": "upper(coalesce(\"FICTIF\",'NON')) <> 'OUI'",
                "OUTPUT": "memory:",
            },
        )["OUTPUT"]

    print(f"[ILT] after FICTIF!='OUI' -> roads: {roads.featureCount()}")

    # ---------- 4) Suppression ENTIEREMENT des segments sous/sur ILT (ORFeH) ----------
    if (
        orfeh_buffer_layer
        and orfeh_buffer_layer.isValid()
        and orfeh_buffer_layer.featureCount() > 0
    ):
        before_r, before_ra = roads.featureCount(), rails.featureCount()
        roads, rails = _remove_segments_by_orfeh(roads, rails, orfeh_buffer_layer)
        print(
            f"[ILT] after ORFeH removal -> roads: {roads.featureCount()} (-{before_r - roads.featureCount()}), "
            f"rails: {rails.featureCount()} (−{before_ra - rails.featureCount()})"
        )

    # ---------- 5) Buffer variable ----------
    if roads.fields().indexFromName("BUFFER_M") < 0:
        roads.dataProvider().addAttributes([QgsField("BUFFER_M", QVariant.Double)])
        roads.updateFields()
    roads = processing.run(
        "native:fieldcalculator",
        {
            "INPUT": roads,
            "FIELD_NAME": "BUFFER_M",
            "FIELD_TYPE": 1,
            "FIELD_LENGTH": 20,
            "FIELD_PRECISION": 3,
            "NEW_FIELD": False,
            "FORMULA": (
                'case when to_real("LARGEUR") is null or to_real("LARGEUR") < 5 '
                'then 2.5 else (to_real("LARGEUR")/2.0) + 1 end'
            ),
            "OUTPUT": "memory:",
        },
    )["OUTPUT"]
    roads_buf = _variable_buffer_safe(roads, "BUFFER_M")

    if rails.fields().indexFromName("BUFFER_M") < 0:
        rails.dataProvider().addAttributes([QgsField("BUFFER_M", QVariant.Double)])
        rails.updateFields()
    rails = processing.run(
        "native:fieldcalculator",
        {
            "INPUT": rails,
            "FIELD_NAME": "BUFFER_M",
            "FIELD_TYPE": 1,
            "FIELD_LENGTH": 20,
            "FIELD_PRECISION": 3,
            "NEW_FIELD": False,
            "FORMULA": (
                "case "
                'when to_real("NB_VOIES") is null or to_real("NB_VOIES") <= 1 then 3.5 '
                "else case "
                '     when (10.5 + 2.5 * (to_real("NB_VOIES") - 4)) < 20 '
                '     then (10.5 + 2.5 * (to_real("NB_VOIES") - 4)) '
                "     else 20 "
                "end "
                "end"
            ),
            "OUTPUT": "memory:",
        },
    )["OUTPUT"]
    rails_buf = _variable_buffer_safe(rails, "BUFFER_M")

    # ---------- 6) Hierarchisation ----------
    non_drive = processing.run(
        "native:extractbyexpression",
        {
            "INPUT": roads_buf,
            "EXPRESSION": "(\"NATURE\" in ('SENTIER','CHEMIN')) AND "
            "upper(coalesce(\"ACCES_VL\",'')) = 'PHYSIQUEMENT IMPOSSIBLE'",
            "OUTPUT": "memory:",
        },
    )["OUTPUT"]

    built_expr = "\"CODE_US\" IN ('US2','US235','US3','US5')"
    if ocs_layer and ocs_layer.isValid() and ocs_layer.fields().indexFromName("CODE_US") >= 0:
        built = processing.run(
            "native:extractbyexpression",
            {"INPUT": ocs_layer, "EXPRESSION": built_expr, "OUTPUT": "memory:"},
        )["OUTPUT"]
        if built and built.featureCount() > 0:
            built = processing.run("native:dissolve", {"INPUT": built, "OUTPUT": "memory:"})[
                "OUTPUT"
            ]
            non_drive = processing.run(
                "native:difference", {"INPUT": non_drive, "OVERLAY": built, "OUTPUT": "memory:"}
            )["OUTPUT"]

    carross = processing.run(
        "native:extractbyexpression",
        {
            "INPUT": roads_buf,
            "EXPRESSION": "(\"NATURE\" = 'ROUTE EMPIERRÉE') OR "
            "(\"NATURE\" = 'CHEMIN' AND upper(coalesce(\"ACCES_VL\",'')) <> 'PHYSIQUEMENT IMPOSSIBLE')",
            "OUTPUT": "memory:",
        },
    )["OUTPUT"]

    rails_other = processing.run(
        "native:extractbyexpression",
        {
            "INPUT": rails_buf,
            "EXPRESSION": '"NATURE" IN '
            "('VOIE FERRÉE PRINCIPALE','VOIE DE SERVICE','SANS OBJET','TRAMWAY','MÉTRO','FUNICULAIRE OU CRÉMAILLÈRE')",
            "OUTPUT": "memory:",
        },
    )["OUTPUT"]

    routes_cl = processing.run(
        "native:extractbyexpression",
        {
            "INPUT": roads_buf,
            "EXPRESSION": "\"NATURE\" IN ('ROUTE À 1 CHAUSSÉE','ROUTE À 2 CHAUSSÉES','ROND-POINT')",
            "OUTPUT": "memory:",
        },
    )["OUTPUT"]

    autoroutes = processing.run(
        "native:extractbyexpression",
        {
            "INPUT": roads_buf,
            "EXPRESSION": "\"NATURE\" IN ('TYPE AUTOROUTIER','BRETELLE')",
            "OUTPUT": "memory:",
        },
    )["OUTPUT"]

    lgv = processing.run(
        "native:extractbyexpression",
        {"INPUT": rails_buf, "EXPRESSION": "\"NATURE\" = 'LGV'", "OUTPUT": "memory:"},
    )["OUTPUT"]

    for lyr, perm, draw in [
        (non_drive, 6, 6),
        (carross, 5, 5),
        (rails_other, 4, 4),
        (routes_cl, 3, 3),
        (autoroutes, 2, 2),
        (lgv, 1, 1),
    ]:
        if lyr and lyr.isValid() and lyr.featureCount() > 0:
            _ensure_field(lyr, "PERM", QVariant.Int)
            _ensure_field(lyr, "DRAW_ORDER", QVariant.Int)
            _set_const_fields(lyr, {"PERM": perm, "DRAW_ORDER": draw})

    parts = [
        lyr
        for lyr in [non_drive, carross, rails_other, routes_cl, autoroutes, lgv]
        if lyr and lyr.isValid() and lyr.featureCount() > 0
    ]

    if not parts:
        return make_memory_layer("Polygon", crs_ref, QgsFields(), f"ILT_{area_name_clean}_empty")

    ilt = (
        parts[0]
        if len(parts) == 1
        else processing.run(
            "native:mergevectorlayers",
            {"LAYERS": parts, "CRS": crs_ref.authid(), "OUTPUT": "memory:"},
        )["OUTPUT"]
    )

    if save_outputs and ilt and ilt.isValid() and ilt.featureCount() > 0:
        write_vector_layer(ilt, os.path.join(debug_dir, "LTI_Hierarchised_Debug.gpkg"))
        write_vector_layer(ilt, os.path.join(output_dir, f"ILT_{area_name_clean}.gpkg"))

    return ilt


# =====================================================================
# 13) Fences and ground-mounted photovoltaic farms
# =====================================================================


def process_fences_and_solar(
    base_dir: str,
    output_dir: str,
    extent: QgsGeometry,  # extent as QgsGeometry (same CRS as crs_ref)
    area_name_clean: str,
    crs_ref: QgsCoordinateReferenceSystem,  # ex: EPSG:2154
) -> QgsVectorLayer:
    """
    Native PyQGIS - Fences & photovoltaic plants (OSM & custom data)
      - Cherche "CPV_OSM_Clean.shp" et "France_Fences_Rural.gpkg"
      - Reprojects if needed, clips to the extent
      - CPV: keeps only FID, name (when present), + TYPE='SOLAR'
      - Fences : buffer 5 m + TYPE='FENCE'
      - Fusionne et exporte (GeoPackage)

    Return a merged in-memory layer (empty if no source is available).
    """

    # -- helpers locaux --
    def _find_path(base_dir: str, exact_name: str) -> Optional[str]:
        for root, _, files in os.walk(base_dir):
            for f in files:
                if f == exact_name:
                    return os.path.join(root, f)
        return None

    def _create_extent_layer(extent_geom: QgsGeometry, crs) -> QgsVectorLayer:
        lyr = QgsVectorLayer(f"Polygon?crs={crs.authid()}", "extent", "memory")
        pr = lyr.dataProvider()
        feat = QgsFeature()
        feat.setGeometry(extent_geom)
        pr.addFeatures([feat])
        lyr.updateExtents()
        return lyr

    def _reproject_if_needed(layer_or_path, target_crs) -> QgsVectorLayer:
        lyr = (
            layer_or_path
            if isinstance(layer_or_path, QgsVectorLayer)
            else QgsVectorLayer(layer_or_path, "src", "ogr")
        )
        if not lyr or not lyr.isValid():
            return lyr
        if lyr.crs() != target_crs:
            return processing.run(
                "native:reprojectlayer",
                {
                    "INPUT": lyr,
                    "TARGET_CRS": target_crs.authid(),
                    "OPERATION": "",
                    "OUTPUT": "memory:",
                },
            )["OUTPUT"]
        return lyr

    def _clip_layer(input_layer_or_path, extent_layer) -> Optional[QgsVectorLayer]:
        try:
            res = processing.run(
                "native:clip",
                {"INPUT": input_layer_or_path, "OVERLAY": extent_layer, "OUTPUT": "memory:"},
            )
            out = res["OUTPUT"]
            return out if out and out.isValid() and out.featureCount() > 0 else None
        except Exception:
            return None

    def _retain_fields(layer: QgsVectorLayer, fields: List[str]) -> QgsVectorLayer:
        # conserve uniquement les champs de la liste s'ils existent
        existing = [f for f in fields if layer.fields().indexFromName(f) >= 0]
        if not existing:
            return layer
        return processing.run(
            "native:retainfields", {"INPUT": layer, "FIELDS": existing, "OUTPUT": "memory:"}
        )["OUTPUT"]

    def _add_const_field(layer: QgsVectorLayer, name: str, value, qtype=QVariant.String):
        if layer.fields().indexFromName(name) < 0:
            layer.dataProvider().addAttributes([QgsField(name, qtype)])
            layer.updateFields()
        idx = layer.fields().indexFromName(name)
        # Use edit buffer safely
        layer.startEditing()
        for f in layer.getFeatures():
            layer.changeAttributeValue(f.id(), idx, value)
        layer.commitChanges()

    def _merge_layers(layers: List[QgsVectorLayer], crs) -> QgsVectorLayer:
        layers = [lyr for lyr in layers if lyr and lyr.isValid() and lyr.featureCount() > 0]
        if not layers:
            # empty layer by default
            empty = QgsVectorLayer(f"Polygon?crs={crs.authid()}", "Solar_Fences_empty", "memory")
            return empty
        if len(layers) == 1:
            return layers[0]
        return processing.run(
            "native:mergevectorlayers", {"LAYERS": layers, "CRS": crs.authid(), "OUTPUT": "memory:"}
        )["OUTPUT"]

    # -- logs doux --
    print("\n=== Fences/Solar: processing start ===")

    # 1) Localisation des sources
    path_cpv = _find_path(base_dir, "CPV_OSM_Clean.shp")
    path_fences = _find_path(base_dir, "France_Fences_Rural.gpkg")
    print(f"[F&S] CPV path: {path_cpv or '- not found -'}")
    print(f"[F&S] Fences path: {path_fences or '- not found -'}")

    extent_layer = _create_extent_layer(extent, crs_ref)

    layers_out = []

    # 2) CPV / SOLAR
    if path_cpv:
        cpv_src = _reproject_if_needed(path_cpv, crs_ref)
        cpv_clip = _clip_layer(cpv_src, extent_layer)
        if cpv_clip:
            # Fix geometries first
            cpv_clip = processing.run(
                "native:fixgeometries", {"INPUT": cpv_clip, "OUTPUT": "memory:"}
            )["OUTPUT"]

            # Check geometry type. If Point/MultiPoint, buffer it to create Polygons.
            # Solar panels are often points in OSM.
            wkb_type = cpv_clip.wkbType()
            if QgsWkbTypes.geometryType(wkb_type) == QgsWkbTypes.PointGeometry:
                print("[F&S] CPV is Point/MultiPoint. Buffering by 5m to create Polygons.")
                cpv_clip = processing.run(
                    "native:buffer",
                    {
                        "INPUT": cpv_clip,
                        "DISTANCE": 5.0,
                        "SEGMENTS": 5,
                        "DISSOLVE": False,
                        "OUTPUT": "memory:",
                    },
                )["OUTPUT"]

            # keep FID + name when present
            cpv_keep = _retain_fields(cpv_clip, ["FID", "name"])
            _add_const_field(cpv_keep, "TYPE", "SOLAR", QVariant.String)
            # index spatial (perf)
            processing.run("native:createspatialindex", {"INPUT": cpv_keep})
            print(f"[F&S] CPV kept: {cpv_keep.featureCount()} features")
            layers_out.append(cpv_keep)
        else:
            print("[F&S] CPV: aucun objet intersectant l’emprise")

    # 3) Fences (buffer 5 m)
    if path_fences:
        fn_src = _reproject_if_needed(path_fences, crs_ref)
        fn_clip = _clip_layer(fn_src, extent_layer)
        if fn_clip:
            # Fix geometries first
            fn_clip = processing.run(
                "native:fixgeometries", {"INPUT": fn_clip, "OUTPUT": "memory:"}
            )["OUTPUT"]

            fn_buf = processing.run(
                "native:buffer",
                {
                    "INPUT": fn_clip,
                    "DISTANCE": 5.0,
                    "SEGMENTS": 5,
                    "END_CAP_STYLE": 0,
                    "JOIN_STYLE": 0,
                    "MITER_LIMIT": 2.0,
                    "DISSOLVE": False,
                    "OUTPUT": "memory:",
                },
            )["OUTPUT"]

            # Keep only relevant fields to avoid schema mismatch
            fn_buf = _retain_fields(fn_buf, ["name", "NAME"])
            _add_const_field(fn_buf, "TYPE", "FENCE", QVariant.String)

            print(f"[F&S] Fences buffered (5 m): {fn_buf.featureCount()} features")
            layers_out.append(fn_buf)
        else:
            print("[F&S] Fences: aucun objet intersectant l’emprise")

    # 4) Fusion
    merged = _merge_layers(layers_out, crs_ref)
    print(f"[F&S] Final merge: {merged.featureCount()} features")

    # 5) Export
    try:
        from . import config as cfg

        save_flag = getattr(cfg, "SAVE_VECTOR_OUTPUTS", True)
    except Exception:
        save_flag = True

    if save_flag and merged and merged.isValid():
        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, f"Solar_Fences_{area_name_clean}.gpkg")
        # Use robust writer
        write_vector_layer(merged, out_path)
        print(f"[F&S] Export: {out_path}")

    return merged


# =====================================================================
# 14) Dense built-up areas (BD TOPO)
# =====================================================================


def process_dense_built_areas(
    base_dir: str,
    output_dir: str,
    extent: QgsGeometry,
    area_name_clean: str,
    crs_ref: QgsCoordinateReferenceSystem,
    density_threshold: float = 5.0,
    save_outputs: bool = True,
) -> QgsVectorLayer:
    """
    Identify densely built-up areas from BD TOPO:
    - collect ZONE_CONSTRUITE and BATIMENT for the intersecting departments
    - clip to the study extent
    - compute the density (buildings per km2)
    - keep the areas where:
          * density >= density_threshold
          * nb bâtiments >= (density_threshold - 1)
    """

    # ================= Helpers =================
    def _extent_layer(geom, crs):
        lyr = QgsVectorLayer(f"Polygon?crs={crs.authid()}", "extent", "memory")
        pr = lyr.dataProvider()
        f = QgsFeature()
        f.setGeometry(geom)
        pr.addFeatures([f])
        lyr.updateExtents()
        return lyr

    def _merge(layers: List[QgsVectorLayer]) -> Optional[QgsVectorLayer]:
        layers = [lyr for lyr in layers if lyr and lyr.isValid() and lyr.featureCount() > 0]
        if not layers:
            return None
        if len(layers) == 1:
            return layers[0]
        return processing.run(
            "native:mergevectorlayers",
            {"LAYERS": layers, "CRS": crs_ref.authid(), "OUTPUT": "memory:"},
        )["OUTPUT"]

    def _empty():
        fields = QgsFields()
        fields.append(QgsField("NB_BATI", QVariant.Int))
        fields.append(QgsField("DENS_BT", QVariant.Double))
        lyr = QgsVectorLayer(f"Polygon?crs={crs_ref.authid()}", "dense_empty", "memory")
        lyr.dataProvider().addAttributes([QgsField(f.name(), f.type()) for f in fields])
        lyr.updateFields()
        return lyr

    # ================= 1) Departments =================
    dep_codes = _get_intersecting_dept_codes(base_dir, extent, crs_ref)
    if not dep_codes:
        raise RuntimeError("❌ No DEPARTEMENT intersecting extent.")

    ext_layer = _extent_layer(extent, crs_ref)

    # ================= 2) ZONE_CONSTRUITE =================
    zone_files = []
    for root, _, files in os.walk(base_dir):
        for f in files:
            if "ZONE_CONSTRUITE" in f.upper():
                full = os.path.join(root, f)
                if any(code in full for code in dep_codes):
                    zone_files.append(full)

    if not zone_files:
        return _empty()

    zone_clipped = []
    for p in zone_files:
        try:
            out = processing.run(
                "native:clip", {"INPUT": p, "OVERLAY": ext_layer, "OUTPUT": "memory:"}
            )["OUTPUT"]
            if out and out.isValid() and out.featureCount() > 0:
                zone_clipped.append(out)
        except Exception:
            pass

    zone = _merge(zone_clipped)
    if not zone:
        return _empty()

    zone = processing.run("native:dissolve", {"INPUT": zone, "FIELD": [], "OUTPUT": "memory:"})[
        "OUTPUT"
    ]
    zone = processing.run("native:fixgeometries", {"INPUT": zone, "OUTPUT": "memory:"})["OUTPUT"]
    zone = processing.run("native:multiparttosingleparts", {"INPUT": zone, "OUTPUT": "memory:"})[
        "OUTPUT"
    ]

    # ================= 3) BATIMENT =================
    bat_files = []
    for root, _, files in os.walk(base_dir):
        for f in files:
            fl = f.lower()
            full = os.path.join(root, f)
            uris = []
            if fl == "batiment.shp":
                uris = [full]
            elif fl.endswith(".gpkg"):
                uris = [
                    f"{full}|layername={sub}"
                    for sub in gpkg_sublayers(full)
                    if sub.upper() == "BATIMENT"
                ]
            for uri in uris:
                if (not dep_codes) or any(code in full for code in dep_codes):
                    bat_files.append(uri)

    if not bat_files:
        raise RuntimeError("❌ No BATIMENT layer (.shp or .gpkg sublayer) found.")

    bat_clipped = []
    for p in bat_files:
        try:
            out = processing.run(
                "native:clip", {"INPUT": p, "OVERLAY": ext_layer, "OUTPUT": "memory:"}
            )["OUTPUT"]
            if out and out.isValid() and out.featureCount() > 0:
                bat_clipped.append(out)
        except Exception:
            pass

    buildings = _merge(bat_clipped)
    buildings = processing.run("native:fixgeometries", {"INPUT": buildings, "OUTPUT": "memory:"})[
        "OUTPUT"
    ]

    # ================= 4) SpatialIndex (API moderne) =================
    b_feats = list(buildings.getFeatures())
    b_index = QgsSpatialIndex()
    b_index.addFeatures(b_feats)

    b_dict = {f.id(): f for f in b_feats}

    # ================= 5) Couche finale =================
    # Create fields explicitly matching what we want to write
    out_fields = QgsFields()

    # Identify fields to copy (excluding ID/FID to avoid PK conflicts)
    fields_to_copy = []
    for fld in zone.fields():
        if fld.name().upper() not in ("ID", "FID", "OGC_FID"):
            fields_to_copy.append(fld)
            out_fields.append(QgsField(fld.name(), fld.type()))

    # Add our new fields
    out_fields.append(QgsField("NB_BATI", QVariant.Int))
    out_fields.append(QgsField("DENS_BT", QVariant.Double))

    dense = QgsVectorLayer(f"Polygon?crs={crs_ref.authid()}", "Dense_built_zones", "memory")
    pr_out = dense.dataProvider()
    # FIX: addAttributes expects a list of QgsField, not QgsFields object directly
    pr_out.addAttributes([out_fields.at(i) for i in range(out_fields.count())])
    dense.updateFields()

    kept = []

    for z in zone.getFeatures():
        geom = z.geometry()
        if geom is None or geom.isEmpty():
            continue

        area_km2 = geom.area() / 1_000_000
        if area_km2 <= 0:
            continue

        cand = b_index.intersects(geom.boundingBox())
        count = 0
        for fid in cand:
            bf = b_dict[fid]
            if bf.geometry().intersects(geom):
                count += 1

        dens = count / area_km2

        if count >= (density_threshold - 1) and dens >= density_threshold:
            nf = QgsFeature(dense.fields())
            nf.setGeometry(geom)

            # Construct attributes matching out_fields
            attrs = [z[f.name()] for f in fields_to_copy]
            attrs.append(count)
            attrs.append(dens)

            nf.setAttributes(attrs)
            kept.append(nf)

    if kept:
        pr_out.addFeatures(kept)
        dense.updateExtents()
    else:
        dense = _empty()

    # ================= 6) Export =================
    if save_outputs:
        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, f"dense_built_zones_{area_name_clean}.gpkg")
        write_vector_layer(dense, out_path)

    return dense
