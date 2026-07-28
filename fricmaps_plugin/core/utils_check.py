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
from typing import List, Dict, Optional
from qgis.core import (
    QgsVectorLayer,
    QgsGeometry,
    QgsFeatureRequest,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
    QgsMessageLog,
    Qgis,
)


def _gpkg_layers_matching(gpkg_path, name_patterns):
    """Return the layer names inside a GeoPackage whose name contains one of the
    given (upper-case) patterns. Reads only the gpkg_contents metadata table via
    sqlite3 (fast, dependency-free), so it works even on very large GeoPackages.
    """
    out = []
    try:
        con = sqlite3.connect(gpkg_path)
        try:
            rows = con.execute(
                "SELECT table_name FROM gpkg_contents WHERE data_type = 'features'"
            ).fetchall()
        finally:
            con.close()
        for (tname,) in rows:
            if tname and any(p in tname.upper() for p in name_patterns):
                out.append(tname)
    except Exception:
        # Not a valid gpkg / locked / no gpkg_contents → just skip it.
        logging.getLogger(__name__).debug("Suppressed exception", exc_info=True)
        pass
    return out


def check_required_datasets(
    extent_geom: QgsGeometry,
    crs_ref: QgsCoordinateReferenceSystem,
    base_dir: str,
    checks_config: Optional[List[Dict[str, str]]] = None,
    log_callback=None,
):
    """
    Verify that the required datasets (BD TOPO, OCS GE, RPG) are available for
    the departments/regions intersecting the study area, using PyQGIS.

    Args:
        extent_geom: Study-area geometry (buffered or not).
        crs_ref: Coordinate reference system of the study area.
        base_dir: Root directory holding the input data.
        checks_config: List of dicts describing the checks to perform.
                       Ex: [{"type": "BD TOPO", "niveau": "DEPARTEMENT"}]
        log_callback: Logging callback (optional).

    Raises:
        RuntimeError: If missing datasets are detected.
    """

    if checks_config is None:
        checks_config = [
            {"type": "BD TOPO", "niveau": "DEPARTEMENT"},
            {"type": "OCS GE", "niveau": "DEPARTEMENT"},
            {"type": "RPG", "niveau": "REGION"},
        ]

    def log(msg):
        if log_callback:
            log_callback(msg)
        else:
            QgsMessageLog.logMessage(msg, "FricMaps", Qgis.Info)

    log("🔍 Checking required datasets...")

    missing_data_errors = []

    for check in checks_config:
        data_type = check["type"].upper()
        level = check["niveau"].upper()  # DEPARTEMENT or REGION

        layer_name_pattern = "DEPARTEMENT" if level == "DEPARTEMENT" else "REGION"
        # Accept several spellings of the admin layer name (FR/EN, singular/plural).
        if level == "DEPARTEMENT":
            name_patterns = ("DEPARTEMENT", "DEPARTEMENTS", "DEPT")
        else:
            name_patterns = ("REGION", "REGIONS")
        admin_exts = (".SHP", ".GEOJSON", ".JSON", ".GML", ".TAB")
        admin_files = []

        # 1. Locate the administrative layers (DEPARTEMENT / REGION).
        #    a) vector files named DEPARTEMENT/REGION (SHP, GeoJSON, ...)
        #    b) SUBLAYERS inside GeoPackages (BD TOPO GPKG case: the
        #       'departement'/'region' layers live INSIDE the .gpkg, so no file
        #       carries that name).
        for root, _, files in os.walk(base_dir):
            for file in files:
                fu = file.upper()
                path = os.path.join(root, file)
                if fu.endswith(admin_exts) and any(p in fu for p in name_patterns):
                    admin_files.append(path)
                elif fu.endswith(".GPKG"):
                    for sub in _gpkg_layers_matching(path, name_patterns):
                        admin_files.append(f"{path}|layername={sub}")

        if not admin_files:
            # Non-blocking: without an administrative layer the codes cannot be
            # determined, so this dataset is simply left unverified.
            log(
                f"⚠️ Administrative boundary *{layer_name_pattern}* not found "
                f"(.shp/.gpkg/.geojson) -> {data_type} verification skipped."
            )
            continue

        # 2. Identify the intersecting administrative codes
        intersected_codes = set()

        for admin_path in admin_files:
            lyr = QgsVectorLayer(admin_path, "admin_layer", "ogr")
            if not lyr.isValid():
                continue

            # On-the-fly reprojection of the extent for the intersection test
            search_geom = QgsGeometry(extent_geom)
            if lyr.crs() != crs_ref:
                try:
                    xform = QgsCoordinateTransform(crs_ref, lyr.crs(), QgsProject.instance())
                    search_geom.transform(xform)
                except Exception as e:
                    log(f"⚠️ CRS transformation error for {admin_path}: {e}")
                    continue

            # Identify the code field. Depending on the vintage/format it is
            # named INSEE_DEP/INSEE_REG (BD TOPO Shapefile) OR code_insee
            # (BD TOPO 3.x GeoPackage, read from the departement/region layer).
            if level == "DEPARTEMENT":
                field_candidates = ["INSEE_DEP", "CODE_INSEE", "INSEE", "CODE_DEP"]
            else:
                field_candidates = ["INSEE_REG", "CODE_INSEE", "INSEE", "CODE_REG"]

            lower_map = {f.name().lower(): f.name() for f in lyr.fields()}
            code_field = None
            for cand in field_candidates:
                if cand.lower() in lower_map:
                    code_field = lower_map[cand.lower()]
                    break
            if not code_field:
                # Fuzzy fallback: any field containing DEP/REG/INSEE.
                for f in lyr.fields():
                    fu = f.name().upper()
                    if (
                        "INSEE" in fu
                        or ("DEP" in fu and level == "DEPARTEMENT")
                        or ("REG" in fu and level == "REGION")
                    ):
                        code_field = f.name()
                        break

            if not code_field:
                continue

            # Intersection spatiale
            # Optimisation: bounding-box filter first
            request = QgsFeatureRequest().setFilterRect(search_geom.boundingBox())
            for feat in lyr.getFeatures(request):
                if feat.geometry().intersects(search_geom):
                    val = feat[code_field]
                    if val is not None:
                        # Normalisation (e.g. 5 -> 05)
                        s_val = str(val)
                        if level == "DEPARTEMENT":
                            # Ensure 3 digits for departments if numeric (e.g. 57 -> 057)
                            intersected_codes.add(s_val.zfill(3) if s_val.isdigit() else s_val)
                        else:
                            intersected_codes.add(s_val)

        if not intersected_codes:
            log(f"⚠️ No {level} feature intersects for {data_type}. Check the extent.")
            continue

        log(f"ℹ️ {level}s intersecting for {data_type}: {sorted(list(intersected_codes))}")

        # 3. Check that the data exist for these codes by scanning base_dir

        # Normalised dataset pattern (e.g. "BD TOPO" -> "BDTOPO"). HYPHENS are
        # stripped as well, so that folders such as "OCS-GE_..." also match.
        norm_data_type = data_type.upper().replace(" ", "").replace("_", "").replace("-", "")

        def extract_code_from_path(path_str, level_mode):
            # Look for _Dxxx or _Rxx
            path_str = path_str.upper()
            if level_mode == "DEPARTEMENT":
                # Match _D057, _D57, _D057_ etc.
                # Regex more flexible: _D followed by digits (2 or 3)
                return re.findall(r"_D(\d{2,3}[AB]?)", path_str)
            else:
                return re.findall(r"_R(\d{2})", path_str)

        # Collect every directory that "looks like" the dataset
        candidate_paths = []
        for root, dirs, _ in os.walk(base_dir):
            for d in dirs:
                d_upper = d.upper().replace(" ", "").replace("_", "").replace("-", "")
                if norm_data_type in d_upper:
                    candidate_paths.append(d)

        # Cross-check
        found_codes = set()
        for code in intersected_codes:
            code_found = False

            for d_name in candidate_paths:
                codes_in_name = extract_code_from_path(d_name, level)

                # Direct check
                if code in codes_in_name:
                    code_found = True
                    break

                # Flexible check (handle "57" vs "057")
                if level == "DEPARTEMENT":
                    # If code=057, check if 57 is in list
                    if code.isdigit():
                        c_int = int(code)
                        for c_extracted in codes_in_name:
                            if c_extracted.isdigit() and int(c_extracted) == c_int:
                                code_found = True
                                break
                    if code_found:
                        break

            if code_found:
                found_codes.add(code)

        missing_codes = sorted(list(intersected_codes - found_codes))

        if missing_codes:
            error_msg = f"❌ Missing data for {data_type} ({level}): codes {missing_codes}"
            missing_data_errors.append(error_msg)
            log(error_msg)
        else:
            log(f"✅ {data_type} data complete.")

    if missing_data_errors:
        full_error = "\n".join(missing_data_errors)
        raise RuntimeError(
            "Dataset verification failed:\n"
            + full_error
            + "\n\n➡️ If you are confident about your data, untick 'Verify required "
            "datasets before processing' (tab 1) to force the run."
        )
