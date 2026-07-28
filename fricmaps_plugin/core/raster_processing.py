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
"""
Raster processing utilities for FricMaps (PyQGIS + GDAL).

Functions:
- load_table_from_csv: reads the class/friction table (CSV)
- rasterize_classes_and_friction: rasterises the vector layers into 2 rasters
    * Raster_Classe_<area_name>.tif
    * Raster_Friction_<area_name>.tif

Pure PyQGIS / GDAL implementation (processing.gdal:rasterize + numpy),
with detailed logs (feature counts per filter, missing classes, etc.).
"""

import logging
import os
import datetime
import numpy as np
from typing import Optional

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsGeometry,
    QgsRectangle,
    QgsVectorLayer,
    QgsVectorFileWriter,
    QgsFeatureRequest,
    QgsWkbTypes,
)

import processing

from .table_schema import canonical_header
from qgis.core import QgsProcessingFeedback
from osgeo import gdal

# Continuity buffer applied to LINE/POINT geometries before rasterisation,
# expressed as a multiple of the raster resolution. It guarantees that linear
# barriers (roads, railways, linear watercourses) form a continuous band that
# SCALES with resolution, instead of fragmenting at coarse resolutions.
# Radius = FACTOR x resolution -> band width ~ 2 x FACTOR x resolution.
# Polygon geometries are left unchanged.
LINE_BUFFER_FACTOR = 0.75


# =====================================================================
# 0) SQL FILTER CLEANING / NORMALISATION
# =====================================================================


def apply_SQL_filter(filter_str: str) -> str:
    """
    Clean a filter coming from the CSV so that it is a valid QGIS expression
    (later consumed by extractbyexpression).

    Example of a raw CSV entry:
        " ""CODE_US"" =  'US1.1' "
    -> output:
        CODE_US = 'US1.1'
    """

    if not isinstance(filter_str, str):
        return ""
    expr = filter_str.strip()
    if not expr:
        return ""

    # QGIS accepts well-formed expressions directly: identifiers in double
    # quotes ("FIELD") and string literals in single quotes ('value'). Operator
    # "normalisation" is therefore NO LONGER performed (the previous version
    # turned >= into "> =", <= into "< =", != into "! =", and even corrupted
    # '=' inside string literals -> invalid expression -> class silently lost).
    #
    # Only one case is handled: the legacy format produced by naive CSV
    # parsing, where the whole expression was wrapped in "..." with inner
    # quotes doubled ("" instead of "). It is unescaped ONLY in that case.
    if len(expr) >= 2 and expr[0] == '"' and expr[-1] == '"' and '""' in expr:
        expr = expr[1:-1].replace('""', '"').strip()

    return expr


# =====================================================================
# 1) CSV TABLE READING (PANDAS-FREE)
# =====================================================================


def load_table_from_csv(csv_path: str, vector_layers: dict):
    """
    Read the rasterisation table from a CSV file (';' separator), without any
    pandas dependency.

    Returns a list of dicts sorted by increasing 'COMPILATION_ORDER'.

    Each dict contains:
        - 'SOURCE'            (str, upper)
        - 'COMPILATION_ORDER' (int)
        - 'FRICTION_VALUE'    (int)
        - 'SQL_FILTER'            (str, possibly empty)

    Rows whose 'SOURCE' is absent from vector_layers are discarded.
    """

    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"❌ CSV not found: {csv_path}")

    import csv as _csv

    rows = []

    # Parsed with the csv module (';' delimiter) so that quoting/escaping in
    # the SQL_FILTER field is handled correctly (a naive split(';') corrupted it).
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = _csv.reader(f, delimiter=";")
        try:
            header = next(reader)
        except StopIteration:
            raise ValueError(f"❌ Empty CSV file: {csv_path}")
        header_line = ";".join(header)

        # Legacy column names are mapped onto the canonical schema so that
        # classification tables authored with an earlier version still run.
        header = canonical_header(header_line.strip().split(";"))

        if "SOURCE" not in header:
            raise ValueError("❌ Missing 'SOURCE' column in CSV header.")
        if "COMPILATION_ORDER" not in header:
            raise ValueError("❌ Missing 'COMPILATION_ORDER' column in CSV header.")
        if "FRICTION_VALUE" not in header:
            raise ValueError("❌ Missing 'FRICTION_VALUE' column in CSV header.")

        idx_source = header.index("SOURCE")
        idx_order = header.index("COMPILATION_ORDER")
        idx_friction = header.index("FRICTION_VALUE")
        idx_filter = header.index("SQL_FILTER") if "SQL_FILTER" in header else None
        idx_name = header.index("CLASS_NAME") if "CLASS_NAME" in header else None

        for parts in reader:
            if not parts or all(not p.strip() for p in parts):
                continue

            if len(parts) < len(header):
                # incomplete row -> skip
                continue

            src = parts[idx_source].strip().upper()
            try:
                order = int(parts[idx_order])
                fric = int(parts[idx_friction])
            except Exception:
                # non-numeric value -> skip the row
                logging.getLogger(__name__).debug("Suppressed exception", exc_info=True)
                continue

            filt_raw = ""
            if idx_filter is not None and idx_filter < len(parts):
                filt_raw = parts[idx_filter].strip()

            name_raw = ""
            if idx_name is not None and idx_name < len(parts):
                name_raw = parts[idx_name].strip().upper()

            rows.append(
                {
                    "CLASS_NAME": name_raw,
                    "SOURCE": src,
                    "COMPILATION_ORDER": order,
                    "FRICTION_VALUE": fric,
                    "SQL_FILTER": filt_raw,
                }
            )

    # Drop sources that are not present in the supplied vector layers
    available = {k.upper() for k in vector_layers.keys()}
    rows = [r for r in rows if r["SOURCE"] in available]

    # sort by COMPILATION_ORDER (compilation order)
    rows_sorted = sorted(rows, key=lambda x: x["COMPILATION_ORDER"])
    return rows_sorted


# =====================================================================
# 2) INTERNAL HELPERS: GRID COMPUTATION / LAYER EXPORT
# =====================================================================


def _compute_grid_from_geom(extent_geom: QgsGeometry, resolution: float):
    """
    Compute the grid (rows, cols, geotransform, rect) from a geometry and a
    resolution, GDAL-style (consistent with gdal:rasterize).
    """

    if isinstance(extent_geom, QgsGeometry):
        rect: QgsRectangle = extent_geom.boundingBox()
    elif isinstance(extent_geom, QgsRectangle):
        rect = extent_geom
    else:
        raise TypeError("extent_geom must be QgsGeometry or QgsRectangle")

    minx, miny, maxx, maxy = rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum()

    cols = int((maxx - minx) / resolution)
    rows = int((maxy - miny) / resolution)

    geotransform = [
        minx,  # top-left x
        resolution,  # pixel width
        0.0,  # rotation
        maxy,  # top-left y
        0.0,  # rotation
        -resolution,  # pixel height (negative, north-up)
    ]

    return rows, cols, geotransform, rect


def _ensure_vector_on_disk(layer: QgsVectorLayer, tmp_dir: str, suffix: str = "") -> str:
    """
    Ensure the layer is written to disk as a GeoPackage.
    Robustly handles the 3-to-5 element return value of writeAsVectorFormatV3.
    """

    import re as _re

    src = layer.source() or ""
    base_path = src.split("|")[0]
    if base_path and os.path.exists(base_path):
        return base_path

    os.makedirs(tmp_dir, exist_ok=True)
    safe_name = _re.sub(r"[^A-Za-z0-9_]+", "_", layer.name())
    if suffix:
        safe_name = f"{safe_name}_{suffix}"

    out_path = os.path.join(tmp_dir, f"{safe_name}.gpkg")

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.fileEncoding = "UTF-8"
    options.layerName = safe_name

    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, out_path, layer.transformContext(), options
    )

    error = result[0]
    new_path = result[1] if len(result) > 1 and result[1] else out_path

    if error != QgsVectorFileWriter.NoError:
        raise RuntimeError(
            f"❌ Failed to export layer '{layer.name()}' to {out_path} (error {error})"
        )

    return new_path


# =====================================================================
# 3) CLASS / FRICTION RASTERISATION
# =====================================================================


def rasterize_classes_and_friction(
    table_df: list,
    vector_layers: dict,
    extent_geom: QgsGeometry,
    crs_ref: QgsCoordinateReferenceSystem,
    resolution: float,
    output_dir: str,
    area_name_clean: str,
    log_dir: Optional[str] = None,
    feedback: Optional[QgsProcessingFeedback] = None,
    work_dir: Optional[str] = None,
):
    """
    Rasterise the supplied layers into 2 deliverables:
        - Land_Cover_<area_name_clean>.tif
        - Friction_<area_name_clean>.tif

    Parameters:
        vector_layers : dict { 'OCS': QgsVectorLayer, 'RPG': QgsVectorLayer, ... }
        table_df      : liste de dicts (load_table_from_csv)
                         champs : SOURCE, SQL_FILTER, COMPILATION_ORDER, FRICTION_VALUE
        extent_geom   : study-area geometry (buffered extent)
        crs_ref       : CRS commun
        resolution    : taille de pixel (m)
        output_dir    : output directory
        area_name_clean : sanitised study-area name
        log_dir       : log directory (defaults to output_dir)
        feedback      : QgsProcessingFeedback optionnel
    """

    if feedback is None:
        feedback = QgsProcessingFeedback()
    if log_dir is None:
        log_dir = output_dir
    # Scratch files go to work_dir so that output_dir only ever holds the
    # deliverables. Defaults to output_dir to preserve the previous behaviour
    # for any caller that does not supply one.
    if work_dir is None:
        work_dir = output_dir

    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)
    os.makedirs(work_dir, exist_ok=True)

    logs: list[str] = []

    def log(msg: str):
        logs.append(msg)
        print(msg)
        try:
            feedback.pushInfo(msg)
        except Exception:
            logging.getLogger(__name__).debug("Suppressed exception", exc_info=True)
            pass

    # Harmonise vector_layers keys
    vector_layers = {k.upper(): v for k, v in vector_layers.items()}
    table_sources = sorted({row["SOURCE"] for row in table_df})
    available_sources = list(vector_layers.keys())

    # --- Log header ---
    log(f"🗓️ Log generated : {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"EPCI : {area_name_clean}")
    log("=" * 60 + "\n")
    log("📋 Requested sources: " + str(table_sources))
    log("🗂️ Disponibles : " + str(available_sources))

    missing_sources = [s for s in table_sources if s not in available_sources]
    if missing_sources:
        log("⚠️ Sources missing from the supplied layers: " + str(missing_sources))

    # Grid computation
    rows, cols, geotransform, rect = _compute_grid_from_geom(extent_geom, resolution)
    log(f"🧮 Grid rows={rows}, cols={cols}, res={resolution}")

    # Output arrays
    raster_classes = np.zeros((rows, cols), dtype=np.uint16)
    raster_permeability = np.zeros((rows, cols), dtype=np.uint16)

    classes_rasterized = set()

    # Temporary directory for exported layers
    tmp_vlayers_dir = os.path.join(work_dir, "_tmp_vlayers")
    os.makedirs(tmp_vlayers_dir, exist_ok=True)

    # -----------------------------------------------------------------
    # Normalisation of vegetation NATURE values (mapping)
    # -----------------------------------------------------------------
    veg_mapping = {
        "Bois": "BOIS",
        "Forêt fermée de conifères": "FORET_FERMEE_DE_CONIFERES",
        "Forêt fermée de feuillus": "FORET_FERMEE_DE_FEUILLUS",
        "Forêt fermée mixte": "FORET_FERMEE_MIXTE",
        "Forêt ouverte": "FORET_OUVERTE",
        "Lande ligneuse": "LANDE_LIGNEUSE",
        "Peupleraie": "PEUPLERAIE",
        "Verger": "VERGER",
        "Vigne": "VIGNE",
    }

    if "VEGETATION" in vector_layers and vector_layers["VEGETATION"] is not None:
        veg_layer = vector_layers["VEGETATION"]
        veg_layer.startEditing()
        for f in veg_layer.getFeatures():
            old = f["NATURE"]
            if old in veg_mapping:
                f["NATURE"] = veg_mapping[old]
                veg_layer.updateFeature(f)
        veg_layer.commitChanges()

    # -----------------------------------------------------------------
    # Loop over every table row (already sorted by COMPILATION_ORDER)
    # -----------------------------------------------------------------
    for idx, row in enumerate(table_df):
        source_name = str(row["SOURCE"]).strip().upper()
        filter_raw = str(row.get("SQL_FILTER", "")).strip()

        # Clean the filter into a valid QGIS expression
        filter_expr = apply_SQL_filter(filter_raw)

        try:
            class_val = int(row["COMPILATION_ORDER"])
            friction_val = int(row["FRICTION_VALUE"])
        except Exception as e:
            log(f"⚠️ Numeric conversion error (row {idx}, source {source_name}) -> {e}")
            continue

        if source_name not in vector_layers:
            log(f"⚠️ Source layer not found: {source_name}")
            continue

        base_layer: QgsVectorLayer = vector_layers[source_name]
        if base_layer is None or not base_layer.isValid():
            log(f"⚠️ Layer {source_name} is invalid -> skipped.")
            continue

        # --- Build the filtered layer ---
        if filter_expr:
            try:
                res_extract = processing.run(
                    "native:extractbyexpression",
                    {
                        "INPUT": base_layer,
                        "EXPRESSION": filter_expr,
                        "OUTPUT": "TEMPORARY_OUTPUT",
                    },
                    feedback=feedback,
                )
                layer_to_raster = res_extract["OUTPUT"]
            except Exception as e:
                log(f"❌ extractbyexpression failed for {source_name} [{filter_expr}] -> {e}")
                continue
        else:
            layer_to_raster = base_layer

        if layer_to_raster is None or not layer_to_raster.isValid():
            log(f"⚠️ Invalid filtered layer for {source_name} [{filter_expr}]")
            continue

        # Count features in the filtered layer
        count_obj = layer_to_raster.featureCount()

        if not filter_expr:
            log(f"📥 {source_name} (No filters) → {count_obj} objects")
        else:
            log(f"🔍 {source_name} | filter : {filter_expr} → {count_obj} objects")

        if count_obj == 0:
            log(f"ℹ️ No objects for {source_name} with filter : {filter_expr}")
            # IMPORTANT: the class is still considered "present" even when no
            # pixel is actually filled, to keep class codes stable.
            classes_rasterized.add(class_val)
            log(
                f"🔢 Add : CLASSE {class_val} | permeability {friction_val} | objects : {count_obj}\n"
            )
            continue

        # --- Resolution-scalable continuity buffer for LINES / POINTS ---
        # Widens line/point geometries proportionally to the resolution, to
        # avoid barrier fragmentation (roads, railways, linear watercourses)
        # and diagonal leakage. Polygon layers (land cover, crops, built-up
        # areas, already-buffered hedges) are left untouched.
        try:
            gtype = QgsWkbTypes.geometryType(layer_to_raster.wkbType())
            if gtype in (QgsWkbTypes.LineGeometry, QgsWkbTypes.PointGeometry):
                buf_dist = LINE_BUFFER_FACTOR * float(resolution)
                if buf_dist > 0:
                    _b = processing.run(
                        "native:buffer",
                        {
                            "INPUT": layer_to_raster,
                            "DISTANCE": buf_dist,
                            "SEGMENTS": 5,
                            "END_CAP_STYLE": 0,
                            "JOIN_STYLE": 0,
                            "MITER_LIMIT": 2,
                            "DISSOLVE": False,
                            "OUTPUT": "memory:",
                        },
                    )["OUTPUT"]
                    if _b is not None and _b.isValid():
                        layer_to_raster = _b
                        log(
                            f"↔️ {source_name}: continuity buffer {buf_dist:.1f} m "
                            f"(~{2*LINE_BUFFER_FACTOR:.1f}x resolution)"
                        )
        except Exception as e:
            log(f"⚠️ Continuity buffer skipped for {source_name}: {e}")

        # Ensure the filtered layer is available on disk
        try:
            vector_path = _ensure_vector_on_disk(
                layer_to_raster, tmp_vlayers_dir, suffix=source_name
            )
        except Exception as e:
            log(f"❌ Unable to stage layer '{source_name}' on disk -> {e}")
            continue

        # gdal:rasterize parameters
        # NOTE: UNITS=0 -> WIDTH/HEIGHT are pixel counts (cols/rows)
        params = {
            "INPUT": vector_path,
            "FIELD": None,
            "BURN": 1,
            "UNITS": 0,  # 0 = pixels, 1 = georeferenced units
            "WIDTH": cols,
            "HEIGHT": rows,
            "EXTENT": f"{rect.xMinimum()},{rect.xMaximum()},{rect.yMinimum()},{rect.yMaximum()}",
            "NODATA": 0,
            "OPTIONS": "",
            "DATA_TYPE": 5,  # UInt16
            "INIT": 0,
            "INVERT": False,
            "EXTRA": "-at",  # ALL_TOUCHED: burn every touched pixel -> no gaps
            "OUTPUT": "TEMPORARY_OUTPUT",
        }
        # No WHERE clause: the layer is already filtered

        try:
            res = processing.run("gdal:rasterize", params, feedback=feedback)
        except Exception as e:
            log(f"❌ gdal:rasterize failed for {source_name} [{filter_expr}] -> {e}")
            continue

        mask_path = res.get("OUTPUT")
        if not mask_path or not os.path.exists(mask_path):
            log(f"❌ Raster mask not created for {source_name}")
            continue

        # Read the raster mask with GDAL
        try:
            ds = gdal.Open(mask_path)
            if ds is None:
                log(f"❌ Unable to open the raster mask for {source_name}")
                continue

            band = ds.GetRasterBand(1)
            mask_np = band.ReadAsArray().astype(np.uint16)

            if mask_np.shape != (rows, cols):
                log(f"⚠️ Mismatch mask={mask_np.shape} attendu=({rows}, {cols}) → cropping")
                min_rows = min(rows, mask_np.shape[0])
                min_cols = min(cols, mask_np.shape[1])
                mask_np = mask_np[:min_rows, :min_cols]
                raster_classes = raster_classes[:min_rows, :min_cols]
                raster_permeability = raster_permeability[:min_rows, :min_cols]
                rows, cols = min_rows, min_cols

        except Exception as e:
            log(f"❌ GDAL error while reading the raster mask for {source_name} -> {e}")
            continue

        # Application classe / friction
        raster_classes[mask_np == 1] = class_val
        raster_permeability[mask_np == 1] = friction_val
        classes_rasterized.add(class_val)

        log(f"🔢 Add : CLASSE {class_val} | permeability {friction_val} | objects : {count_obj}\n")

    # -----------------------------------------------------------------
    # GeoTIFF export with GDAL
    # -----------------------------------------------------------------
    driver = gdal.GetDriverByName("GTiff")

    path_classes = os.path.join(output_dir, f"Land_Cover_{area_name_clean}.tif")
    dst_class = driver.Create(path_classes, cols, rows, 1, gdal.GDT_UInt16)
    dst_class.SetGeoTransform(geotransform)
    dst_class.SetProjection(crs_ref.toWkt())
    dst_class.GetRasterBand(1).WriteArray(raster_classes)
    dst_class.GetRasterBand(1).SetNoDataValue(0)
    dst_class.FlushCache()
    dst_class = None

    path_friction = os.path.join(output_dir, f"Friction_{area_name_clean}.tif")
    dst_fric = driver.Create(path_friction, cols, rows, 1, gdal.GDT_UInt16)
    dst_fric.SetGeoTransform(geotransform)
    dst_fric.SetProjection(crs_ref.toWkt())
    dst_fric.GetRasterBand(1).WriteArray(raster_permeability)
    dst_fric.GetRasterBand(1).SetNoDataValue(0)
    dst_fric.FlushCache()
    dst_fric = None

    expected_classes = sorted({row["COMPILATION_ORDER"] for row in table_df})
    missing = [c for c in expected_classes if c not in classes_rasterized]

    log(f"✅ Class raster exported: {path_classes}")
    log(f"✅ Friction raster exported: {path_friction}")
    log("🚫 Classes manquantes : " + str(missing))
    if len(expected_classes) > 0:
        log(
            f"🌟 Rasterisation : {len(classes_rasterized)}/{len(expected_classes)} classes "
            f"({100 * len(classes_rasterized) / len(expected_classes):.1f}%)"
        )

    log("\n" + "=" * 60)
    log("End.\n")

    # Write the log file to disk
    log_path = os.path.join(log_dir, f"Rasterization_log_{area_name_clean}.txt")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"🗓️ Log generated : {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"EPCI : {area_name_clean}\n")
        f.write("=" * 60 + "\n\n")
        f.write("\n".join(logs))
        f.write("\n")

    return path_classes, path_friction, log_path


# =====================================================================
# 4) RGE ALTI (DEM) pre-processing
# =====================================================================


def process_dtm_from_tiles(geom_extent, crs_ref, base_dir, output_mnt_path, resolution=5.0):
    """
    Merge the RGE ALTI tiles (asc/tif) then clip the DEM to the study extent,
    ensuring the final DEM is aligned on the same grid as the class/friction
    rasterisation (same logic as _compute_grid_from_geom).

    Étapes :
    1. Scan every DEPARTEMENT layer available under base_dir
    2. Select the departments intersecting the extent (geom_extent)
    3. For each department, look for RGEALTI_...DXXX... directories
       then the tiles via dalles.shp (NOM_DALLE field) and the .asc/.tif files
    4. Merge the tiles with GDAL.BuildVRT + GDAL.Warp (target resolution)
    5. Clip the DEM to an extent ALIGNED on the rasterisation grid:
       - rows = int((maxy - miny) / res)
       - cols = int((maxx - minx) / res)
       - xmin_aligned = xmin
       - xmax_aligned = xmin + cols * res
       - ymax_aligned = ymax
       - ymin_aligned = ymax - rows * res
    """

    import os
    import glob
    from osgeo import gdal
    from qgis.core import (
        QgsVectorLayer,
        QgsGeometry,
        QgsCoordinateTransform,
        QgsProject,
    )

    # === 1) Locate the DEPARTEMENT layer (SHP file OR GPKG sublayer) ===
    from .utils import gpkg_sublayers

    dep_files = []
    for root, _, files in os.walk(base_dir):
        for file in files:
            fl = file.lower()
            full = os.path.join(root, file)
            if fl == "departement.shp":
                dep_files.append(full)
            elif fl.endswith(".gpkg"):
                for sub in gpkg_sublayers(full):
                    if sub.upper() == "DEPARTEMENT":
                        dep_files.append(f"{full}|layername={sub}")

    if not dep_files:
        raise FileNotFoundError(
            "❌ No DEPARTEMENT layer (.shp or .gpkg sublayer) found under base_dir."
        )

    # === 2) Iterate over each DEPARTEMENT layer and collect intersecting codes ===
    dept_codes = set()

    for fp in dep_files:
        lyr = QgsVectorLayer(fp, "dep", "ogr")
        if not lyr.isValid():
            print(f"⚠️ Invalid department layer: {fp}")
            continue

        # Reproject the extent into this layer's CRS
        geom_for_layer = QgsGeometry(geom_extent)
        if lyr.crs() != crs_ref:
            try:
                tr = QgsCoordinateTransform(crs_ref, lyr.crs(), QgsProject.instance())
                geom_for_layer.transform(tr)
            except Exception as e:
                print(f"⚠️ Unable to reproject the extent to {lyr.crs().authid()}: {e}")
                continue

        bb_layer = geom_for_layer.boundingBox()

        # Detect the department code field (name containing 'DEP' or 'CODE')
        fields = lyr.fields()
        field_names = [f.name() for f in fields]
        code_field = None
        for name in field_names:
            up = name.upper()
            if "DEP" in up or "CODE" in up:
                code_field = name
                break

        if code_field is None:
            print(
                f"⚠️ No department code field found in {fp}. " f"Champs disponibles : {field_names}"
            )
            continue

        # Coarse spatial filter using the bounding box
        req = QgsFeatureRequest().setFilterRect(bb_layer)

        for f in lyr.getFeatures(req):
            # Fine filter: exact geometric intersection
            if not f.geometry() or not f.geometry().intersects(geom_for_layer):
                continue
            val = f[code_field]
            if val is None:
                continue
            dept_codes.add(str(val).zfill(3))

    if not dept_codes:
        raise RuntimeError(
            "❌ No intersecting department, or no usable department code field "
            "in the DEPARTEMENT layers."
        )

    dept_codes = sorted(dept_codes)
    print(f"   -> Departments intersecting the DEM: {dept_codes}")

    # === 3) Collect the RGE ALTI tiles for these departments ===
    tile_paths = []

    for code in dept_codes:
        # Example directory: RGEALTI_2-0_5M_ASC_LAMB93-IGN69_D067_2021-11-02
        rge_dirs = [
            os.path.join(base_dir, d)
            for d in os.listdir(base_dir)
            if d.startswith("RGEALTI_") and f"D{code}" in d
        ]

        if not rge_dirs:
            print(f"⚠️ No RGEALTI_...D{code}... directory found under {base_dir}")
            continue

        for rge_dir in rge_dirs:
            # Look for dalles.shp in the directory tree
            tiles_shp_list = glob.glob(os.path.join(rge_dir, "**", "dalles.shp"), recursive=True)
            if not tiles_shp_list:
                print(f"⚠️ No dalles.shp found under {rge_dir}")
                continue

            tiles_shp = tiles_shp_list[0]
            tiles_layer = QgsVectorLayer(tiles_shp, "tiles", "ogr")
            if not tiles_layer.isValid():
                print(f"⚠️ Invalid tile-index layer: {tiles_shp}")
                continue

            # Extent expressed in the tile-index CRS
            geom_for_tiles = QgsGeometry(geom_extent)
            if tiles_layer.crs() != crs_ref:
                try:
                    tr = QgsCoordinateTransform(crs_ref, tiles_layer.crs(), QgsProject.instance())
                    geom_for_tiles.transform(tr)
                except Exception as e:
                    print(f"⚠️ Unable to reproject the extent to {tiles_layer.crs().authid()}: {e}")
                    continue

            bb_tiles = geom_for_tiles.boundingBox()
            req_tiles = QgsFeatureRequest().setFilterRect(bb_tiles)

            for ft in tiles_layer.getFeatures(req_tiles):
                if not ft.geometry() or not ft.geometry().intersects(geom_for_tiles):
                    continue
                if "NOM_DALLE" not in ft.fields().names():
                    continue

                tile_name = str(ft["NOM_DALLE"])

                # Find the matching tile file (.asc or .tif)
                found_path = None
                for ext in (".asc", ".tif"):
                    candidates = glob.glob(
                        os.path.join(rge_dir, "**", tile_name + ext), recursive=True
                    )
                    if candidates:
                        found_path = candidates[0]
                        break

                if found_path:
                    tile_paths.append(found_path)

    if not tile_paths:
        raise FileNotFoundError("❌ No RGE ALTI tile found for the selected departments.")

    print(f"   -> {len(tile_paths)} RGE ALTI tiles selected for the DEM.")

    # === 4) Merge the tiles via VRT + GDAL.Warp (target resolution) ===
    tmp_merge = output_mnt_path.replace(".tif", "_merge.tif")

    vrt = gdal.BuildVRT("", tile_paths)
    if vrt is None:
        raise RuntimeError("❌ Unable to build the VRT for the DEM.")

    # Global merge (not yet clipped to the study extent)
    gdal.Warp(
        tmp_merge,
        vrt,
        format="GTiff",
        xRes=resolution,
        yRes=resolution,
        dstSRS=crs_ref.toWkt(),
    )
    vrt = None

    if not os.path.exists(tmp_merge):
        raise RuntimeError("❌ DEM merge failed: file not created.")

    # === 5) Compute an extent ALIGNED on the rasterisation grid ===
    # Mirrors the logic of _compute_grid_from_geom
    rect = geom_extent.boundingBox()
    xmin = rect.xMinimum()
    ymin = rect.yMinimum()
    xmax = rect.xMaximum()
    ymax = rect.yMaximum()

    # Column / row counts (as in rasterize_classes_and_friction)
    cols = int((xmax - xmin) / resolution)
    rows = int((ymax - ymin) / resolution)

    # Aligned bounds (identical to the rasterisation grid)
    xmax_aligned = xmin + cols * resolution
    ymin_aligned = ymax - rows * resolution

    # === 6) Final clip to the aligned extent ===
    gdal.Warp(
        output_mnt_path,
        tmp_merge,
        format="GTiff",
        outputBounds=(xmin, ymin_aligned, xmax_aligned, ymax),
        xRes=resolution,
        yRes=resolution,
        dstSRS=crs_ref.toWkt(),
        dstNodata=-9999,
    )

    if not os.path.exists(output_mnt_path):
        raise RuntimeError("❌ DEM clipping failed (file not created).")

    # Clean up the intermediate merge (optional)
    try:
        if os.path.exists(tmp_merge):
            os.remove(tmp_merge)
    except Exception:
        logging.getLogger(__name__).debug("Suppressed exception", exc_info=True)
        pass

    return output_mnt_path


# =====================================================================
# 5) Apply slope weighting
# =====================================================================


def apply_slope_weighting(
    path_permeability, path_dtm, path_output, slope_weights=None, max_friction=10000
):
    """
    Compute a friction weighting based on terrain slope.

    Assumptions:
    - The DEM (path_dtm) has already been prepared by process_dtm_from_tiles,
      hence it shares the CRS, the resolution and the grid alignment of the
      friction raster (path_permeability).

    Étapes :
    1) Slope computation in degrees via gdal:slope
    2) Load the friction and slope rasters as numpy arrays
    3) Harmonise array shapes if needed (crop to the common rows/cols)
    4) Apply the multiplicative factor according to slope:
           0–30°   → ×1
          30–40°   → ×10
          >=40°    → ×1000
    5) Clip the weighted friction to max_friction and export as GeoTIFF
    """

    import os
    import numpy as np
    import processing
    from osgeo import gdal

    # Safety: make sure the DEM actually exists
    if not os.path.exists(path_dtm):
        raise FileNotFoundError(f"❌ DEM not found for slope weighting: {path_dtm}")

    # 1) Compute slope directly on the aligned DEM
    slope_path = path_dtm.replace(".tif", "_slope.tif")
    processing.run(
        "gdal:slope", {"INPUT": path_dtm, "SCALE": 1, "AS_PERCENT": False, "OUTPUT": slope_path}
    )

    # 2) Load friction + slope
    fric_ds = gdal.Open(path_permeability)
    if fric_ds is None:
        raise RuntimeError(f"❌ Unable to open the friction raster: {path_permeability}")
    friction = fric_ds.GetRasterBand(1).ReadAsArray().astype(np.float32)

    slp_ds = gdal.Open(slope_path)
    if slp_ds is None:
        raise RuntimeError(f"❌ Unable to open the slope raster: {slope_path}")
    slope = slp_ds.GetRasterBand(1).ReadAsArray().astype(np.float32)

    # 3) Harmonise array shapes if required (safety)
    if slope.shape != friction.shape:
        print(
            f"⚠️ Mismatch slope={slope.shape} vs friction={friction.shape} → "
            f"cropping to the common minimum size."
        )
        min_rows = min(slope.shape[0], friction.shape[0])
        min_cols = min(slope.shape[1], friction.shape[1])
        slope = slope[:min_rows, :min_cols]
        friction = friction[:min_rows, :min_cols]

    # 4) Weights as a function of slope (dynamic)
    w = np.ones_like(friction, dtype=np.float32)
    if slope_weights:
        for weight_config in slope_weights:
            try:
                min_s = float(weight_config.get("min", 0))
                max_s = float(weight_config.get("max", 0))
                weight_val = float(weight_config.get("weight", 1.0))
                w[(slope >= min_s) & (slope < max_s)] = weight_val
            except Exception as e:
                print(f"⚠️ Failed to apply slope weight {weight_config}: {e}")
    else:
        # Default values when no parameter is supplied
        w[(slope >= 30) & (slope < 40)] = 10.0
        w[slope >= 40] = 1000.0

    result = np.clip(friction * w, 0, max_friction).astype(np.uint16)

    # 5) Export: use the friction raster as template
    driver = gdal.GetDriverByName("GTiff")
    out = driver.CreateCopy(path_output, fric_ds, strict=0)
    out.GetRasterBand(1).WriteArray(result)
    out.GetRasterBand(1).SetNoDataValue(0)
    out = None

    # Clean up intermediate rasters
    try:
        if os.path.exists(slope_path):
            os.remove(slope_path)
    except Exception:
        logging.getLogger(__name__).debug("Suppressed exception", exc_info=True)
        pass

    return path_output


# =====================================================================
# 6) Apply distance-based multiplicative weighting around a class
# =====================================================================


def apply_class_distance_weighting(
    path_permeability,
    path_raster_classes,
    target_class_code,
    weight_bands,
    path_output,
    max_friction=10000,
):
    """
    Multiplicative friction weighting as a function of the distance to a target
    class (built-up areas, street lights, or any custom source).

    Same mechanics as the former "distance to buildings" weighting: pixels of
    the target class are isolated, a pixel-wise Euclidean distance is computed
    with gdal:proximity, then a multiplicative factor is applied per distance
    band (min/max/weight).

    Args:
        path_permeability   : input friction raster.
        path_raster_classes : class raster.
        target_class_code   : class code to weight around.
        weight_bands        : list of dicts {"min", "max", "weight"} (metres).
        path_output         : output raster.
        max_friction        : friction ceiling applied after weighting.
    """

    import os
    import numpy as np
    from osgeo import gdal
    import processing

    # --- Load friction ---
    fric_ds = gdal.Open(path_permeability)
    if fric_ds is None:
        raise RuntimeError(f"Unable to open {path_permeability}")
    friction = fric_ds.GetRasterBand(1).ReadAsArray().astype(np.float32)

    # --- Load classes ---
    cls_ds = gdal.Open(path_raster_classes)
    if cls_ds is None:
        raise RuntimeError(f"Unable to open {path_raster_classes}")
    classes = cls_ds.GetRasterBand(1).ReadAsArray()

    # --- Mask of target class ---
    building_mask = classes == target_class_code
    if not np.any(building_mask):
        print(f"ℹ️ No pixel of class {target_class_code} -> no weighting applied.")
        driver = gdal.GetDriverByName("GTiff")
        driver.CreateCopy(path_output, fric_ds)
        return path_output

    # 1) Binary mask: 1 = target class / 0 = other (gdal:proximity target = 1).
    #    The class code is embedded in the temporary file names to avoid any
    #    collision when several weightings are chained.
    # Scratch rasters are placed beside the OUTPUT, not beside the land-cover
    # raster: the latter is a deliverable sitting in the clean output folder.
    _scratch = os.path.dirname(path_output) or os.path.dirname(path_raster_classes)
    _stem = os.path.splitext(os.path.basename(path_raster_classes))[0]
    bina_path = os.path.join(_scratch, f"{_stem}_bin4dist_{target_class_code}.tif")
    processing.run(
        "gdal:rastercalculator",
        {
            "INPUT_A": path_raster_classes,
            "BAND_A": 1,
            "FORMULA": f"(A=={target_class_code})",
            "RTYPE": 1,
            "NO_DATA": 0,
            "OUTPUT": bina_path,
        },
    )

    # 2) Euclidean distance via GDAL Proximity
    # VALUES='1': distance computed towards pixels equal to 1
    # UNITS=0: georeferenced (metres for a projected CRS), UNITS=1: pixels
    dist_path = os.path.join(_scratch, f"{_stem}_dist_gdal_{target_class_code}.tif")
    processing.run(
        "gdal:proximity",
        {
            "INPUT": bina_path,
            "BAND": 1,
            "VALUES": "1",  # Target pixel values
            "UNITS": 0,  # 0 = Georeferenced coordinates (meters), 1 = Pixels
            "DISTUNITS": 0,  # 0 = GEO, 1 = PIXEL
            "NODATA": -1,  # Nodata value for distance
            "OUTPUT": dist_path,
            "OPTIONS": "",
        },
    )

    # 3) Load the distance raster (already in metres)
    ds_dist = gdal.Open(dist_path)
    if ds_dist is None:
        raise RuntimeError(f"Unable to open the distance raster: {dist_path}")
    dist_m = ds_dist.GetRasterBand(1).ReadAsArray().astype(float)

    # 4) Apply the dynamic distance thresholds
    w = np.ones_like(friction, dtype=np.float32)
    if weight_bands:
        for weight_config in weight_bands:
            try:
                min_d = float(weight_config.get("min", 0))
                max_d = float(weight_config.get("max", 0))
                weight_val = float(weight_config.get("weight", 1.0))

                # Handling boundary condition (similar to old behavior logic where dist=50 belongs to lower bound)
                w[(dist_m > min_d) & (dist_m <= max_d)] = weight_val
            except Exception as e:
                print(f"⚠️ Failed to apply distance weight {weight_config}: {e}")
    else:
        # Default values when no parameter is supplied
        w[dist_m <= 50] = 2.5
        w[(dist_m > 50) & (dist_m <= 100)] = 2.0
        w[(dist_m > 100) & (dist_m <= 200)] = 1.5

    result = np.clip(friction * w, 0, max_friction).astype(np.uint16)

    # 5) Final export
    driver = gdal.GetDriverByName("GTiff")
    out = driver.CreateCopy(path_output, fric_ds)
    out.GetRasterBand(1).WriteArray(result)
    out.GetRasterBand(1).SetNoDataValue(0)
    out = None

    # Clean-up
    for p in (bina_path, dist_path):
        try:
            os.remove(p)
        except Exception:
            logging.getLogger(__name__).debug("Suppressed exception", exc_info=True)
            pass

    print(f"🟢 Friction weighted by distance to class {target_class_code}: {path_output}")
    return path_output


# =====================================================================
# 6bis) Backward compatibility: distance-to-buildings weighting
# =====================================================================


def apply_building_distance_weighting(
    path_permeability, path_raster_classes, building_class_code, building_weights, path_output
):
    """Backward-compatibility wrapper around apply_class_distance_weighting.

    Preserves the historical API used by the pipeline for distance-to-buildings
    weighting. All the logic now lives in the generic
    apply_class_distance_weighting function.
    """
    return apply_class_distance_weighting(
        path_permeability=path_permeability,
        path_raster_classes=path_raster_classes,
        target_class_code=building_class_code,
        weight_bands=building_weights,
        path_output=path_output,
        max_friction=10000,
    )


# =====================================================================
# 7) Interpolation (NumPy Native - NO SCIPY MODE)
# =====================================================================


def replace_obstacle_friction_by_local_interp(
    path_friction, path_classes, target_class_codes, path_output, nodata_value=0, window_size=10
):
    """
    Replace target pixels by the modal (most frequent) value of their neighbours.
    Utilise NumPy pur (np.unique) au lieu de scipy.stats.mode.
    """
    print(f"\n🔄 Obstacle interpolation {target_class_codes} (native NumPy)...")
    import numpy as np
    from osgeo import gdal

    # Read
    ds_fric = gdal.Open(path_friction)
    friction = ds_fric.GetRasterBand(1).ReadAsArray().astype(np.float32)
    ds_cls = gdal.Open(path_classes)
    classes = ds_cls.GetRasterBand(1).ReadAsArray()

    # Masque cibles
    mask_target = np.isin(classes, target_class_codes)
    if not np.any(mask_target):
        gdal.GetDriverByName("GTiff").CreateCopy(path_output, ds_fric)
        return path_output

    pad = window_size // 2
    padded_friction = np.pad(friction, pad_width=pad, mode="reflect")
    result = friction.copy()

    target_indices = np.argwhere(mask_target)
    count = len(target_indices)

    for idx, (i, j) in enumerate(target_indices):
        i_p, j_p = i + pad, j + pad
        window = padded_friction[i_p - pad : i_p + pad + 1, j_p - pad : j_p + pad + 1].flatten()

        # Exclude the centre pixel
        window = np.delete(window, len(window) // 2)

        # Filtres voisins (Pas de NoData, Pas de 10000, Pas de NaN)
        valid_neighbors = window[(window != nodata_value) & (window != 10000) & (~np.isnan(window))]

        if valid_neighbors.size > 0:
            # --- MODE AVEC NUMPY PUR ---
            vals, counts = np.unique(valid_neighbors, return_counts=True)
            mode_val = vals[np.argmax(counts)]
            result[i, j] = mode_val

        if idx % 10000 == 0 and idx > 0:
            print(f"  -> {idx}/{count} pixels processed...")

    # Write
    driver = gdal.GetDriverByName("GTiff")
    out = driver.CreateCopy(path_output, ds_fric, strict=0)
    out.GetRasterBand(1).WriteArray(result)
    out = None

    print(f"✅ Interpolation complete: {path_output}")
    return path_output


# =====================================================================
# 8) Correction Biais (NumPy Native - NO SCIPY)
# =====================================================================


def replace_class3_1000_by_local_mode(
    path_friction, path_classes, output_path, window_size=11, nodata_value=0
):
    """
    Corrige biais Classe 3 & 1000.
    Utilise NumPy pur (np.unique) au lieu de scipy.stats.mode.
    """
    print("\n🔄 Correction biais Classe 3 & 1000 (NumPy Natif)...")
    import numpy as np
    from osgeo import gdal

    ds_fric = gdal.Open(path_friction)
    friction = ds_fric.GetRasterBand(1).ReadAsArray().astype(np.float32)
    ds_cls = gdal.Open(path_classes)
    classes = ds_cls.GetRasterBand(1).ReadAsArray()

    mask_target = (classes == 3) & (friction == 1000)
    if not np.any(mask_target):
        gdal.GetDriverByName("GTiff").CreateCopy(output_path, ds_fric)
        return output_path

    pad = window_size // 2
    padded_friction = np.pad(friction, pad_width=pad, mode="reflect")
    result = friction.copy()

    target_indices = np.argwhere(mask_target)
    count = len(target_indices)

    for idx, (i, j) in enumerate(target_indices):
        i_p, j_p = i + pad, j + pad
        window = padded_friction[i_p - pad : i_p + pad + 1, j_p - pad : j_p + pad + 1].flatten()

        window = np.delete(window, len(window) // 2)
        # Filter: keep everything except NoData/NaN (10000 is allowed)
        valid_neighbors = window[(window != nodata_value) & (~np.isnan(window))]

        if valid_neighbors.size > 0:
            # --- MODE AVEC NUMPY PUR ---
            vals, counts = np.unique(valid_neighbors, return_counts=True)
            mode_val = vals[np.argmax(counts)]
            result[i, j] = mode_val

        if idx % 10000 == 0 and idx > 0:
            print(f"  -> {idx}/{count} pixels processed...")

    driver = gdal.GetDriverByName("GTiff")
    out = driver.CreateCopy(output_path, ds_fric, strict=0)
    out.GetRasterBand(1).WriteArray(result)
    out = None

    print(f"✅ Bias correction complete: {output_path}")
    return output_path
