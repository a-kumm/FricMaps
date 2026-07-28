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
Custom (user-defined) data sources for FricMaps.

This module lets a user plug ANY additional vector dataset (e.g. street
lights / "lampadaires") into the existing friction pipeline **without writing
a single line of Python**. A custom source is described entirely by data (a
dict, serialised as JSON by the GUI) and processed by a single generic loader.

The generic loader reuses the existing helpers in ``utils.py`` so that a custom
source is prepared exactly like the built-in ones: located → merged →
reprojected → clipped → optionally filtered → optionally buffered → optionally
dissolved. The returned ``QgsVectorLayer`` is tagged with the source key and
handed to the rasterisation step, which is already source-agnostic.

Data model (one entry per custom source)::

    {
        "source_key":        "LAMPADAIRES",   # matches SOURCE column in the CSV
        "label":             "Lampadaires",   # human-readable, GUI only
        "enabled":           True,
        "detection_mode":    "token",          # "token" | "path" | "layer"
        "token":             "LAMPADAIRE",     # fuzzy token searched in base_dir
        "path":              "",               # explicit file/dir (mode=path)
        "layer_id":          "",               # QGIS layer id/name (mode=layer)
        "extensions":        [".shp", ".gpkg", ".geojson"],
        "buffer_m":          15.0,             # 0 = no buffer (mandatory for points)
        "dissolve":          False,
        "field_filter":      "",               # optional QGIS SQL expression
        "weighting_enabled": True,
        "weighting_bands":   [ {"min": 0, "max": 10, "weight": 3.0}, ... ],
        "required":          False             # if False, absence never blocks the run
    }
"""

from __future__ import annotations
import logging

import os
import json
from typing import List, Dict, Optional, Any

from qgis.core import (
    QgsVectorLayer,
    QgsGeometry,
    QgsCoordinateReferenceSystem,
    QgsProject,
    QgsWkbTypes,
)
import processing

from .utils import (
    find_files_fuzzy,
    merge_vector_layers_from_paths,
    create_extent_layer,
    clip_layer,
    clean_name,
)

# Default vector extensions scanned when detecting a source by token.
DEFAULT_EXTENSIONS = [".shp", ".gpkg", ".geojson"]


# =====================================================================
# 1) CONFIG PARSING / NORMALISATION
# =====================================================================


def normalize_source_definition(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Validate and normalise a single custom-source definition.

    Accepts both the flat structure produced by the GUI and the nested
    structure documented in the architecture plan. Returns a clean flat dict,
    or ``None`` if the entry is unusable (missing/invalid source key).
    """
    if not isinstance(raw, dict):
        return None

    # Accept nested blocks (detection/geometry/distance_weighting) transparently.
    detection = raw.get("detection", {}) if isinstance(raw.get("detection"), dict) else {}
    geometry = raw.get("geometry", {}) if isinstance(raw.get("geometry"), dict) else {}
    weighting = (
        raw.get("distance_weighting", {}) if isinstance(raw.get("distance_weighting"), dict) else {}
    )

    def pick(*keys, default=None):
        """Return the first non-None value among flat keys / nested blocks."""
        for src, key in keys:
            if src is None:
                continue
            if key in src and src[key] is not None:
                return src[key]
        return default

    source_key = pick((raw, "source_key"), (raw, "SOURCE"), default="")
    source_key = str(source_key).strip().upper()
    if not source_key:
        return None

    extensions = pick((raw, "extensions"), (detection, "extensions"), default=None)
    if not isinstance(extensions, list) or not extensions:
        extensions = list(DEFAULT_EXTENSIONS)
    extensions = [
        str(e).lower() if str(e).startswith(".") else "." + str(e).lower() for e in extensions
    ]

    bands_raw = pick((raw, "weighting_bands"), (weighting, "bands"), default=None) or []
    bands = []
    for b in bands_raw:
        try:
            bands.append(
                {
                    "min": float(b.get("min", 0)),
                    "max": float(b.get("max", 0)),
                    "weight": float(b.get("weight", 1.0)),
                }
            )
        except (AttributeError, TypeError, ValueError):
            logging.getLogger(__name__).debug("Suppressed exception", exc_info=True)
            continue

    def as_bool(v, default=False):
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "oui", "on")
        if v is None:
            return default
        return bool(v)

    def as_float(v, default=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    return {
        "source_key": source_key,
        "label": str(pick((raw, "label"), default=source_key)),
        "enabled": as_bool(pick((raw, "enabled"), default=True), True),
        "detection_mode": str(pick((raw, "detection_mode"), (detection, "mode"), default="token"))
        .strip()
        .lower(),
        "token": str(pick((raw, "token"), (detection, "token"), default="")).strip(),
        "path": str(pick((raw, "path"), (detection, "path"), default="")).strip(),
        "layer_id": str(pick((raw, "layer_id"), (detection, "layer_id"), default="")).strip(),
        "extensions": extensions,
        "buffer_m": as_float(pick((raw, "buffer_m"), (geometry, "buffer_m"), default=0.0)),
        "dissolve": as_bool(pick((raw, "dissolve"), (geometry, "dissolve"), default=False)),
        "field_filter": str(pick((raw, "field_filter"), default="")).strip(),
        "weighting_enabled": as_bool(
            pick((raw, "weighting_enabled"), (weighting, "enabled"), default=False)
        ),
        "weighting_bands": bands,
        "required": as_bool(pick((raw, "required"), default=False), False),
    }


def load_custom_sources_config(raw: Any) -> List[Dict[str, Any]]:
    """Parse the ``custom_sources`` parameter into a clean list of definitions.

    ``raw`` may be a JSON string, a list of dicts, or ``None``. Duplicate
    source keys are dropped (first occurrence wins). Only enabled entries with
    a usable key are returned.
    """
    if raw is None:
        return []

    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []

    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []

    out: List[Dict[str, Any]] = []
    seen = set()
    for entry in raw:
        definition = normalize_source_definition(entry)
        if not definition:
            continue
        if not definition["enabled"]:
            continue
        key = definition["source_key"]
        if key in seen:
            continue
        seen.add(key)
        out.append(definition)
    return out


# =====================================================================
# 2) GENERIC LOADER
# =====================================================================


def _resolve_source_paths(definition: Dict[str, Any], base_dir: str, log) -> List[str]:
    """Return the list of on-disk vector paths for a custom source."""
    mode = definition["detection_mode"]
    exts = tuple(definition["extensions"])

    if mode == "path":
        p = definition["path"]
        if not p:
            log(f"⚠️ [{definition['source_key']}] Mode 'path' selected but no path provided.")
            return []
        if os.path.isfile(p):
            return [p]
        if os.path.isdir(p):
            found = []
            for root, _, files in os.walk(p):
                for f in files:
                    if f.lower().endswith(exts):
                        found.append(os.path.join(root, f))
            return found
        log(f"⚠️ [{definition['source_key']}] Chemin introuvable : {p}")
        return []

    if mode == "layer":
        # Resolved later (needs QgsProject); handled in process_generic_source.
        return []

    # Default: token-based fuzzy search inside base_dir.
    token = definition["token"] or definition["source_key"]
    if not token:
        return []
    return find_files_fuzzy(base_dir, token, exts=exts)


def _resolve_project_layer(definition: Dict[str, Any]):
    """Resolve a custom source declared as an already-loaded QGIS layer."""
    ident = definition.get("layer_id", "") or definition.get("token", "")
    if not ident:
        return None
    project = QgsProject.instance()
    # Try by id first, then by (case-insensitive) name.
    lyr = project.mapLayer(ident)
    if lyr is not None:
        return lyr
    for candidate in project.mapLayers().values():
        if candidate.name().strip().lower() == ident.strip().lower():
            return candidate
    return None


def process_generic_source(
    definition: Dict[str, Any],
    base_dir: str,
    extent_geom: QgsGeometry,
    crs: QgsCoordinateReferenceSystem,
    output_dir: str,
    area_name_clean: str,
    save_outputs: bool = True,
    log=None,
) -> Optional[QgsVectorLayer]:
    """Prepare a single custom vector source for rasterisation.

    Pipeline: locate → merge + reproject → clip to extent → optional SQL
    filter → optional buffer → optional dissolve. Returns a ready
    ``QgsVectorLayer`` tagged with the source key, or ``None`` if no data was
    found (never raises: failures are logged and swallowed so one bad custom
    source cannot break the whole run).
    """
    if log is None:

        def log(msg):
            print(msg)

    key = definition["source_key"]

    try:
        # 1) Locate the data --------------------------------------------------
        if definition["detection_mode"] == "layer":
            base_layer = _resolve_project_layer(definition)
            if base_layer is None:
                log(f"⚠️ [{key}] Project layer not found -> source skipped.")
                return None
            # Reproject if needed by routing through the merge helper.
            merged = merge_vector_layers_from_paths([base_layer.source().split("|")[0]], crs)
        else:
            paths = _resolve_source_paths(definition, base_dir, log)
            if not paths:
                log(
                    f"⚠️ [{key}] No file detected (mode={definition['detection_mode']}, "
                    f"token='{definition['token']}') -> source skipped."
                )
                return None
            log(f"📥 [{key}] {len(paths)} file(s) detected.")
            merged = merge_vector_layers_from_paths(paths, crs)

        if merged is None or not merged.isValid():
            log(f"⚠️ [{key}] Unable to merge/read the data -> source skipped.")
            return None

        # 2) Clip to study extent --------------------------------------------
        extent_layer = create_extent_layer(extent_geom, crs)
        clipped = clip_layer(merged, extent_layer)
        if clipped is None or not clipped.isValid() or clipped.featureCount() == 0:
            log(f"ℹ️ [{key}] No feature left within the study extent after clipping.")
            return None
        layer = clipped

        # 3) Optional attribute filter ---------------------------------------
        flt = definition.get("field_filter", "")
        if flt:
            try:
                res = processing.run(
                    "native:extractbyexpression",
                    {"INPUT": layer, "EXPRESSION": flt, "OUTPUT": "memory:"},
                )
                if res["OUTPUT"] and res["OUTPUT"].isValid():
                    layer = res["OUTPUT"]
                    log(f"🔍 [{key}] Filter applied: {flt} -> {layer.featureCount()} features.")
            except Exception as e:
                log(f"⚠️ [{key}] SQL filter ignored (invalid): {flt} -> {e}")

        # 4) Buffer (mandatory for points to have a raster footprint) ---------
        buffer_m = definition.get("buffer_m", 0.0) or 0.0
        geom_type = QgsWkbTypes.geometryType(layer.wkbType())
        is_point = geom_type == QgsWkbTypes.PointGeometry
        if buffer_m > 0:
            try:
                res = processing.run(
                    "native:buffer",
                    {
                        "INPUT": layer,
                        "DISTANCE": buffer_m,
                        "SEGMENTS": 8,
                        "END_CAP_STYLE": 0,
                        "JOIN_STYLE": 0,
                        "MITER_LIMIT": 2,
                        "DISSOLVE": bool(definition.get("dissolve", False)),
                        "OUTPUT": "memory:",
                    },
                )
                if res["OUTPUT"] and res["OUTPUT"].isValid():
                    layer = res["OUTPUT"]
                    log(
                        f"🟢 [{key}] Buffer {buffer_m} m applied -> {layer.featureCount()} features."
                    )
            except Exception as e:
                log(f"⚠️ [{key}] Buffer failed -> falling back to raw geometry: {e}")
        elif is_point:
            log(
                f"⚠️ [{key}] Point data WITHOUT buffer: they will be nearly "
                f"invisible in the raster. Set a buffer > 0."
            )
        elif definition.get("dissolve", False):
            try:
                res = processing.run("native:dissolve", {"INPUT": layer, "OUTPUT": "memory:"})
                if res["OUTPUT"] and res["OUTPUT"].isValid():
                    layer = res["OUTPUT"]
            except Exception as e:
                log(f"⚠️ [{key}] Dissolve failed: {e}")

        # 5) Tag + optional save ---------------------------------------------
        layer.setName(key)

        if save_outputs and output_dir:
            try:
                from .utils import write_vector_layer

                out_path = os.path.join(
                    output_dir, f"Custom_{clean_name(key)}_{area_name_clean}.gpkg"
                )
                write_vector_layer(layer, out_path)
                log(f"💾 [{key}] Saved: {out_path}")
            except Exception as e:
                log(f"⚠️ [{key}] Save skipped: {e}")

        log(f"✅ [{key}] Custom source ready: {layer.featureCount()} features.")
        return layer

    except Exception as e:
        log(f"❌ [{key}] Unexpected error -> source skipped: {e}")
        return None
