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
"""FricMaps full processing pipeline, built on the QGIS API.

This module implements the complete FricMaps workflow. It orchestrates data preparation,
vector processing, rasterization, and post-processing steps (bias correction,
scenarios) using native QGIS algorithms and GDAL.
"""

from __future__ import annotations

import os
from typing import Dict, Optional, Callable

from qgis.core import QgsVectorLayer

from .utils import clean_name
from .data_processing import (
    extract_area_extent,
    process_land_cover_data,
    process_vegetation_data,
    process_rpg_data,
    process_hydrography_network,
    process_technical_infrastructure,
    process_wildlife_crossing,
    process_linear_transport_infrastructure,
    process_fences_and_solar,
    process_dense_built_areas,
)
from .utils_check import check_required_datasets
from .custom_sources import load_custom_sources_config, process_generic_source
from .raster_processing import (
    load_table_from_csv,
    rasterize_classes_and_friction,
    process_dtm_from_tiles,
    apply_slope_weighting,
    apply_building_distance_weighting,
    apply_class_distance_weighting,
    replace_obstacle_friction_by_local_interp,
    replace_class3_1000_by_local_mode,
)


def run_pipeline(
    epci_file: str,
    base_dir: str,
    output_dir: str,
    area_name: str,
    name_field: str,
    buffer_dist: float = 5000.0,
    resolution: float = 5.0,
    table_csv: Optional[str] = None,
    building_code: int = 29,
    save_vector_outputs: bool = True,
    only_vectors: bool = False,
    skip_vectors: bool = False,
    slope_weights: str = "[]",
    building_weights: str = "[]",
    custom_sources: Optional[str] = None,
    weighting_rules: Optional[str] = None,
    verify_data: bool = True,
    num_workers: int = 1,
    progress_callback: Optional[Callable[[int, str], None]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, str]:
    """Execute the complete FricMaps workflow.

    Args:
        epci_file: Path to the study area layer (SHP/GPKG).
        base_dir: Root directory containing input data.
        output_dir: Directory where outputs will be written.
        area_name: Name of the area to process.
        name_field: Attribute field name for area selection.
        buffer_dist: Buffer distance around the study area (meters).
        resolution: Output raster resolution (meters).
        table_csv: Path to the classification/friction table (CSV).
        building_code: Class code for buildings (default 29).
        save_vector_outputs: Whether to save intermediate vector layers.
        num_workers: Number of parallel workers (unused in this version).
        progress_callback: Function to report progress (percent, message).
        log_callback: Function to log messages.

    Returns:
        Dictionary containing paths to key output files.
    """
    if not os.path.isfile(epci_file):
        raise FileNotFoundError(f"Study area file not found: {epci_file}")
    if not os.path.isdir(base_dir):
        raise FileNotFoundError(f"Base data directory not found: {base_dir}")
    os.makedirs(output_dir, exist_ok=True)
    # output_dir holds only the deliverables (land cover, friction, weighted
    # friction, scenarios). Every intermediate product - clipped vector layers,
    # DEM, per-step weighting rasters, scratch and debug folders - goes here.
    work_dir = os.path.join(output_dir, "intermediate")
    os.makedirs(work_dir, exist_ok=True)

    def log(msg: str):
        if log_callback:
            log_callback(msg)
        print(msg)

    def progress(percent: int, msg: str):
        if progress_callback:
            progress_callback(percent, msg)
        log(f"[{percent}%] {msg}")

    import json

    # Parse weighting parameters
    try:
        slope_w_list = json.loads(slope_weights) if slope_weights else []
    except json.JSONDecodeError:
        log("⚠️ Invalid JSON for slope weights. Using default.")
        slope_w_list = []

    try:
        build_w_list = json.loads(building_weights) if building_weights else []
    except json.JSONDecodeError:
        log("⚠️ Invalid JSON for building weights. Using default.")
        build_w_list = []

    # Parse unified weighting rules (new engine). Empty → legacy weighting path.
    try:
        rules_list = json.loads(weighting_rules) if weighting_rules else []
        if not isinstance(rules_list, list):
            rules_list = []
    except json.JSONDecodeError:
        log("⚠️ Invalid JSON for weighting rules. Falling back to legacy weighting.")
        rules_list = []

    # Parse custom (user-defined) sources
    custom_defs = load_custom_sources_config(custom_sources)
    if custom_defs:
        log(
            f"🧩 {len(custom_defs)} custom source(s): "
            + ", ".join(d["source_key"] for d in custom_defs)
        )

    # 1. Extract and buffer study area
    progress(5, f"Extracting study area: {area_name}")
    extent_geom, crs = extract_area_extent(
        area_layer_path=epci_file,
        area_name=area_name,
        name_field=name_field,
        buffer_dist=buffer_dist,
    )

    area_clean = clean_name(area_name)
    # Use main output_dir directly or create a subdir? debug.py uses OUTPUT_DIR directly.
    # But full_pipeline.py previously created a subdir. Let's stick to output_dir to match debug.py structure if possible,
    # or better, use a subdir to keep things clean if the user didn't specify one.
    # The previous implementation created a subdir. Let's keep that for safety, or just use output_dir.
    # debug.py uses OUTPUT_DIR directly. Let's use output_dir directly to match debug.py exactly.

    log(f"CRS: {crs.authid()}")
    log(f"Extent: {extent_geom.boundingBox().toString()}")

    # 1.5 Check required datasets
    # NOTE: only the built-in socle datasets (BD TOPO, OCS GE, RPG) are verified
    # here. Custom sources are intentionally non-blocking — their generic loader
    # handles missing data gracefully (logs a warning and skips the source).
    if verify_data:
        progress(7, "Checking required datasets...")
        try:
            check_required_datasets(
                extent_geom=extent_geom, crs_ref=crs, base_dir=base_dir, log_callback=log
            )
        except RuntimeError as e:
            # Re-raise to stop pipeline and show error
            raise RuntimeError(f"Data verification failed:\n{e}")
    else:
        log("⏭️ Dataset verification disabled by the user.")

    # 2. Vector Processing

    # Initialize variables to ensure they exist
    ocs_layer = None
    veg_without_hedges = None
    hedges_final = None
    rpg_layer = None
    hydro_layer = None
    tech_layer = None
    ilt_layer = None
    fences_solar_layer = None
    dense_layer = None
    crossings_buffer = None  # Needed for ILT processing

    if skip_vectors:
        log("⏩ Skipping vector processing. Loading existing layers from output directory...")

        def load_layer(filename, layer_name):
            path = os.path.join(work_dir, filename)
            if not os.path.exists(path):
                log(f"⚠️ Layer not found: {path}. Skipping.")
                return None
            lyr = QgsVectorLayer(path, layer_name, "ogr")
            if not lyr.isValid():
                log(f"⚠️ Invalid layer: {path}. Skipping.")
                return None
            log(f"Loaded {layer_name}: {path}")
            return lyr

        try:
            ocs_layer = load_layer(f"OCS_GE_{area_clean}.gpkg", "OCS")
            veg_without_hedges = load_layer(f"Vegetation_{area_clean}.gpkg", "VEGETATION")
            hedges_final = load_layer(f"Hedges_{area_clean}.gpkg", "HEDGES")
            rpg_layer = load_layer(f"RPG_{area_clean}.gpkg", "RPG")
            hydro_layer = load_layer(f"Hydro_{area_clean}.gpkg", "HYDRO")
            tech_layer = load_layer(f"Technical_infrastructures_{area_clean}.gpkg", "TECH_INFRA")
            # Wildlife crossings not used in rasterization directly?
            # It seems process_wildlife_crossing is called but return value not used in vector_sources?
            # Checking vector_sources below... it uses 'LTI' which comes from process_linear_transport_infrastructure
            # process_linear_transport_infrastructure uses crossings_buffer.
            # If skipping, we assume ILT is already done.

            ilt_layer = load_layer(f"ILT_{area_clean}.gpkg", "LTI")
            fences_solar_layer = load_layer(f"Solar_Fences_{area_clean}.gpkg", "SOLAR_FENCES")
            dense_layer = load_layer(f"dense_built_zones_{area_clean}.gpkg", "BUILT_AREA")

        except Exception as e:
            raise RuntimeError(f"Failed to load existing vectors: {e}")

    else:
        # --- Normal Processing ---

        # OCS GE
        progress(10, "Processing Land Cover (OCS GE)...")
        ocs_layer = process_land_cover_data(
            base_dir=base_dir,
            extent_geom=extent_geom,
            crs=crs,
            save_outputs=save_vector_outputs,
            output_dir=work_dir,
            area_name_clean=area_clean,
        )
        log(f"OCS GE: {ocs_layer.featureCount()} features")

        # Vegetation & Hedges
        progress(15, "Processing Vegetation & Hedges...")
        veg_without_hedges, hedges_final = process_vegetation_data(
            base_dir=base_dir,
            output_dir=work_dir,
            extent=extent_geom,
            area_name_clean=area_clean,
            crs_ref=crs,
            save_outputs=save_vector_outputs,
        )
        log(
            f"Vegetation: {veg_without_hedges.featureCount()} | Hedges: {hedges_final.featureCount()}"
        )

        # RPG
        progress(20, "Processing RPG...")
        try:
            rpg_layer = process_rpg_data(
                base_dir=base_dir,
                extent_geom=extent_geom,
                crs=crs,
                save_outputs=save_vector_outputs,
                output_dir=work_dir,
                area_name_clean=area_clean,
            )
            log(f"RPG: {rpg_layer.featureCount()} features")
        except Exception as e:
            rpg_layer = None
            log(f"⚠️ RPG processing failed or no data: {e}")

        # Hydrography
        progress(25, "Processing Hydrography...")
        try:
            hydro_layer = process_hydrography_network(
                base_dir=base_dir,
                output_dir=work_dir,
                extent=extent_geom,
                area_named_clean=area_clean,
                crs_ref=crs,
                save_outputs=save_vector_outputs,
            )
            log(f"Hydrography: {hydro_layer.featureCount()} features")
        except Exception as e:
            hydro_layer = None
            log(f"⚠️ Hydrography processing failed or no data: {e}")

        # Technical Infrastructure
        progress(30, "Processing Technical Infrastructure...")
        try:
            tech_layer = process_technical_infrastructure(
                base_dir=base_dir,
                output_dir=work_dir,
                extent=extent_geom,
                area_name_clean=area_clean,
                crs_ref=crs,
                save_outputs=save_vector_outputs,
            )
            log(f"Tech. Infrastructure: {tech_layer.featureCount()} features")
        except Exception as e:
            tech_layer = None
            log(f"⚠️ Technical Infrastructure processing failed or no data: {e}")

        # Wildlife Crossings
        progress(35, "Processing Wildlife Crossings...")
        try:
            crossings_layer, crossings_buffer = process_wildlife_crossing(
                base_dir=base_dir,
                output_dir=work_dir,
                extent=extent_geom,
                crs_ref=crs,
                area_name_clean=area_clean,
                save_outputs=save_vector_outputs,
            )
            log(
                f"Crossings: {crossings_layer.featureCount()} | Buffers: {crossings_buffer.featureCount()}"
            )
        except Exception as e:
            log(f"⚠️ Wildlife Crossings processing failed or no data: {e}")

        # ILT (Linear Transport Infrastructure)
        progress(40, "Processing ILT...")
        try:
            ilt_layer = process_linear_transport_infrastructure(
                base_dir=base_dir,
                output_dir=work_dir,
                extent=extent_geom,
                area_name_clean=area_clean,
                crs_ref=crs,
                ocs_layer=ocs_layer,
                orfeh_buffer_layer=crossings_buffer,
                save_outputs=save_vector_outputs,
            )
            log(f"ILT: {ilt_layer.featureCount()} features")
        except Exception as e:
            ilt_layer = None
            log(f"⚠️ ILT processing failed or no data: {e}")

        # Fences & Solar
        progress(45, "Processing Fences & Solar...")
        try:
            fences_solar_layer = process_fences_and_solar(
                base_dir=base_dir,
                output_dir=work_dir,
                extent=extent_geom,
                area_name_clean=area_clean,
                crs_ref=crs,
            )
            log(f"Fences/Solar: {fences_solar_layer.featureCount()} features")
        except Exception as e:
            fences_solar_layer = None
            log(f"⚠️ Fences/Solar processing failed or no data: {e}")

        # Dense Built-up Areas
        progress(50, "Processing Dense Built-up Areas...")
        try:
            dense_layer = process_dense_built_areas(
                base_dir=base_dir,
                output_dir=work_dir,
                extent=extent_geom,
                area_name_clean=area_clean,
                crs_ref=crs,
                density_threshold=5,
                save_outputs=save_vector_outputs,
            )
            log(f"Dense Areas: {dense_layer.featureCount()} features")
        except Exception as e:
            dense_layer = None
            log(f"⚠️ Dense Areas processing failed or no data: {e}")

    # 2.5 Custom (user-defined) sources — generic, data-driven loader
    custom_layers = {}
    if custom_defs:
        progress(52, "Processing custom sources...")
        for definition in custom_defs:
            layer = process_generic_source(
                definition=definition,
                base_dir=base_dir,
                extent_geom=extent_geom,
                crs=crs,
                output_dir=work_dir,
                area_name_clean=area_clean,
                save_outputs=save_vector_outputs,
                log=log,
            )
            if layer is not None:
                custom_layers[definition["source_key"]] = layer

    # --- Check for Vector Only Mode ---
    if only_vectors:
        log("🛑 Vector Only Mode: Stopping pipeline after vector processing.")
        return {
            "OUTPUT_FOLDER_PATH": output_dir,
            "MESSAGE": "Vector processing completed. Rasterization skipped.",
        }

    # 3. Prepare Vector Sources for Rasterization
    vector_sources = {
        "OCS": ocs_layer,
        "RPG": rpg_layer,
        "VEGETATION": veg_without_hedges,
        "HEDGES": hedges_final,
        "HYDRO": hydro_layer,
        "LTI": ilt_layer,
        "TECH_INFRA": tech_layer,
        "SOLAR_FENCES": fences_solar_layer,
        "BUILT_AREA": dense_layer,
    }
    # Merge custom sources (they participate in rasterisation exactly like the
    # built-in ones via their SOURCE key in the classification table).
    for src_key, src_layer in custom_layers.items():
        vector_sources[src_key] = src_layer

    # 4. Load Classification Table
    progress(55, "Loading Classification Table...")
    if not table_csv or not os.path.isfile(table_csv):
        # Try default path if not provided
        default_csv = os.path.join(base_dir, "Table_Raster.csv")
        if os.path.isfile(default_csv):
            table_csv = default_csv
            log(f"Using default table: {table_csv}")
        else:
            raise FileNotFoundError("Classification table not found.")

    table_data = load_table_from_csv(table_csv, vector_sources)
    log(f"Loaded {len(table_data)} rules from table.")

    # 5. Rasterization
    progress(60, "Rasterizing Classes & Friction...")
    classes_path, friction_path, log_path = rasterize_classes_and_friction(
        table_df=table_data,
        vector_layers=vector_sources,
        extent_geom=extent_geom,
        crs_ref=crs,
        resolution=resolution,
        output_dir=output_dir,
        work_dir=work_dir,
        area_name_clean=area_clean,
        log_dir=work_dir,
        feedback=None,
    )
    log(f"Classes Raster: {classes_path}")
    log(f"Friction Raster: {friction_path}")

    # 6. DTM Processing
    progress(70, "Processing DTM (RGE ALTI)...")
    mnt_path = os.path.join(work_dir, f"MNT_{area_clean}.tif")
    try:
        process_dtm_from_tiles(
            geom_extent=extent_geom,
            crs_ref=crs,
            base_dir=base_dir,
            output_mnt_path=mnt_path,
            resolution=resolution,
        )
        log(f"DTM exported: {mnt_path}")
    except Exception as e:
        mnt_path = None
        log(f"❌ DTM processing failed: {e}")

    # 7-8. WEIGHTING — unified rules engine
    # A rule = {"type": "slope"|"distance", "target": <str>, "enabled": bool, "bands": [...]}.
    #   slope    → multiplier by DTM slope (degrees).
    #   distance → multiplier by distance to a target class. The target is resolved into
    #              class code(s) by CLASS_NAME, by custom SOURCE key, by "__BUILDING__"
    #              (the Building Class Code), or by a raw integer code.
    def _resolve_target_codes(target):
        t = str(target).strip().upper()
        if not t:
            return []
        if t in ("__BUILDING__", "BUILDING", "BATI", "BÂTI"):
            return [int(building_code)]
        if t.lstrip("-").isdigit():
            return [int(t)]
        return sorted(
            {
                int(r["COMPILATION_ORDER"])
                for r in table_data
                if str(r.get("CLASS_NAME", "")).strip().upper() == t
                or str(r.get("SOURCE", "")).strip().upper() == t
            }
        )

    cur_path = friction_path

    # Unified engine is used as soon as the caller provides the weighting_rules
    # parameter (even an empty list = "no weighting"). Only truly legacy callers
    # (weighting_rules is None) fall back to the old slope+building defaults.
    if weighting_rules is not None:
        progress(75, "Applying weighting rules...")
        step = 0
        for rule in rules_list:
            if not isinstance(rule, dict) or rule.get("enabled") is False:
                continue
            bands = rule.get("bands") or []
            if not bands:
                continue
            rtype = str(rule.get("type", "distance")).strip().lower()
            step += 1
            if rtype == "slope":
                if not mnt_path:
                    log("⚠️ 'slope' rule skipped: DEM unavailable.")
                    continue
                out_path = os.path.join(work_dir, f"Friction_W{step}_slope_{area_clean}.tif")
                try:
                    apply_slope_weighting(
                        path_permeability=cur_path,
                        path_dtm=mnt_path,
                        path_output=out_path,
                        slope_weights=bands,
                        max_friction=10000,
                    )
                    cur_path = out_path
                    log(f"⛰️ Slope weighting -> {out_path}")
                except Exception as e:
                    log(f"❌ Slope weighting failed: {e}")
            else:  # distance
                codes = _resolve_target_codes(rule.get("target", ""))
                if not codes:
                    log(
                        f"⚠️ 'distance' rule skipped: target '{rule.get('target')}' not found "
                        f"in the classification table."
                    )
                    continue
                for cc in codes:
                    out_path = os.path.join(work_dir, f"Friction_W{step}_c{cc}_{area_clean}.tif")
                    try:
                        apply_class_distance_weighting(
                            path_permeability=cur_path,
                            path_raster_classes=classes_path,
                            target_class_code=cc,
                            weight_bands=bands,
                            path_output=out_path,
                            max_friction=10000,
                        )
                        cur_path = out_path
                        log(f"🎯 Distance weighting (class {cc}) -> {out_path}")
                    except Exception as e:
                        log(f"❌ Distance weighting (class {cc}) failed: {e}")
        dist_built_path = cur_path

    else:
        # ---- Legacy weighting (backward compatible with older configs) ----
        progress(75, "Applying Slope Weighting...")
        slope_weighted_path = friction_path
        if mnt_path:
            slope_weighted_path = os.path.join(work_dir, f"Friction_SlopeWeighted_{area_clean}.tif")
            try:
                apply_slope_weighting(
                    path_permeability=friction_path,
                    path_dtm=mnt_path,
                    path_output=slope_weighted_path,
                    slope_weights=slope_w_list,
                    max_friction=10000,
                )
                log(f"Slope weighted friction: {slope_weighted_path}")
            except Exception as e:
                log(f"❌ Slope weighting failed: {e}")
                slope_weighted_path = friction_path

        progress(80, "Applying Building Distance Weighting...")
        dist_built_path = os.path.join(work_dir, f"Friction_BuildingWeighted_{area_clean}.tif")
        try:
            apply_building_distance_weighting(
                path_permeability=slope_weighted_path,
                path_raster_classes=classes_path,
                building_class_code=building_code,
                building_weights=build_w_list,
                path_output=dist_built_path,
            )
            log(f"Building weighted friction: {dist_built_path}")
        except Exception as e:
            log(f"❌ Building distance weighting failed: {e}")
            dist_built_path = slope_weighted_path

        if custom_defs:
            progress(83, "Applying custom source weighting...")
            for definition in custom_defs:
                if not definition.get("weighting_enabled") or not definition.get("weighting_bands"):
                    continue
                src_key = definition["source_key"]
                if src_key not in custom_layers:
                    continue
                class_codes = sorted(
                    {
                        int(r["COMPILATION_ORDER"])
                        for r in table_data
                        if str(r["SOURCE"]).strip().upper() == src_key
                    }
                )
                for class_code in class_codes:
                    weighted_path = os.path.join(
                        work_dir,
                        f"Friction_CustomWeighted_{clean_name(src_key)}_{class_code}_{area_clean}.tif",
                    )
                    try:
                        apply_class_distance_weighting(
                            path_permeability=dist_built_path,
                            path_raster_classes=classes_path,
                            target_class_code=class_code,
                            weight_bands=definition["weighting_bands"],
                            path_output=weighted_path,
                            max_friction=10000,
                        )
                        dist_built_path = weighted_path
                        log(
                            f"🟡 [{src_key}] Distance weighting (class {class_code}): {weighted_path}"
                        )
                    except Exception as e:
                        log(f"❌ [{src_key}] Distance weighting failed (class {class_code}): {e}")

    # 9. Bias Correction
    progress(85, "Correcting Bias (Class 3 & 1000)...")
    final_friction_path = os.path.join(output_dir, f"Friction_Weighted_{area_clean}.tif")
    try:
        replace_class3_1000_by_local_mode(
            path_friction=dist_built_path,
            path_classes=classes_path,
            output_path=final_friction_path,
            window_size=11,
            nodata_value=0,
        )
        log(f"Final corrected friction: {final_friction_path}")
    except Exception as e:
        log(f"❌ Bias correction failed: {e}")
        final_friction_path = dist_built_path

    # 10. Scenarios
    progress(90, "Generating Scenarios...")

    # Scenario A: No Fences (Codes 39, 40)
    scenario_nf_path = os.path.join(output_dir, f"Scenario_No_Fences_{area_clean}.tif")
    try:
        replace_obstacle_friction_by_local_interp(
            path_friction=final_friction_path,
            path_classes=classes_path,
            target_class_codes=[39, 40],
            path_output=scenario_nf_path,
            nodata_value=0,
            window_size=11,
        )
        log(f"Scenario 'No Fences': {scenario_nf_path}")
    except Exception as e:
        log(f"❌ Scenario 'No Fences' failed: {e}")

    # Scenario B: No ILT (Codes 35, 36, 37, 38)
    scenario_of_path = os.path.join(output_dir, f"Scenario_No_LTI_{area_clean}.tif")
    try:
        replace_obstacle_friction_by_local_interp(
            path_friction=final_friction_path,
            path_classes=classes_path,
            target_class_codes=[35, 36, 37, 38],
            path_output=scenario_of_path,
            nodata_value=0,
            window_size=11,
        )
        log(f"Scenario 'No ILT': {scenario_of_path}")
    except Exception as e:
        log(f"❌ Scenario 'No ILT' failed: {e}")

    # Scenario C: No Barriers (All above)
    scenario_np_path = os.path.join(output_dir, f"Scenario_No_Barriers_{area_clean}.tif")
    try:
        replace_obstacle_friction_by_local_interp(
            path_friction=final_friction_path,
            path_classes=classes_path,
            target_class_codes=[35, 36, 37, 38, 39, 40],
            path_output=scenario_np_path,
            nodata_value=0,
            window_size=11,
        )
        log(f"Scenario 'No Barriers': {scenario_np_path}")
    except Exception as e:
        log(f"❌ Scenario 'No Barriers' failed: {e}")

    progress(100, "Pipeline completed successfully!")

    result = {
        # The four deliverables written at the root of output_dir.
        "classes_raster": classes_path,
        "friction_raster": friction_path,
        "friction_weighted_raster": final_friction_path,
        "scenario_no_fences": scenario_nf_path,
        "scenario_no_lti": scenario_of_path,
        "scenario_no_barriers": scenario_np_path,
    }
    if save_vector_outputs:
        # Just returning one example vector, but many were saved
        result["processed_vector"] = os.path.join(work_dir, f"OCS_processed_{area_clean}.gpkg")

    return result
