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
"""Helper functions for the FricMaps QGIS plugin.

This package groups together modules that perform data preparation,
rasterisation and other utility tasks using the QGIS API and the
Processing framework.  The functions defined here intentionally avoid
third‑party libraries such as geopandas or rasterio in order to make
the plugin compatible with the official QGIS plugin repository.

See the individual modules for details.
"""

__all__ = [
    "data_processing",
    "raster_processing",
    "utils",
    "utils_check",
    "custom_sources",
    "full_pipeline",
]
