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
# =============================================================================
# FricMaps QGIS - config.py
# =============================================================================
"""
Global configuration for the FricMaps QGIS plugin.
Contains default paths, field names, and global parameters used across modules.
"""

import os

# === Default parameters ===
BUFFER_DISTANCE = 5000  # in meters
RESOLUTION = 5  # Raster resolution
NAME_FIELD = "NOM_EPCI"  # Field name used to select study area
DEFAULT_CRS = "EPSG:2154"  # Lambert-93

# === Default folders (can be overwritten by GUI) ===
BASE_DIR = ""
OUTPUT_DIR = ""
STUDY_AREA_SHAPEFILE = ""
# === Outputs ===
SAVE_VECTOR_OUTPUTS = True  # set to False to skip writing intermediate vector layers


# === Utility function to get plugin path ===
def plugin_path():
    """Absolute path of the plugin folder (the repository root)."""
    from .. import PLUGIN_ROOT

    return PLUGIN_ROOT


# === Example derived paths ===
def data_dir():
    return os.path.join(plugin_path(), "data")


def output_dir():
    return os.path.join(plugin_path(), "output")


# Ensure folders exist
os.makedirs(output_dir(), exist_ok=True)
