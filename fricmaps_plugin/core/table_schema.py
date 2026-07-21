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
"""Canonical schema of the classification (rasterisation) table.

This module is deliberately free of any dependency - no QGIS, no GDAL, no
NumPy - so that both the GUI editor and the processing pipeline can share a
single definition of the column names without pulling heavy imports into the
dialog at plugin load time.

Column names are language-independent and use SCREAMING_SNAKE_CASE, so that a
classification table stays readable and machine-parsable regardless of the
interface language it was authored in.
"""

# Canonical column names, in their default order.
COLUMNS = (
    "CLASS_NAME",
    "COMPILATION_ORDER",
    "DESCRIPTION",
    "SOURCE",
    "SQL_FILTER",
    "FRICTION_VALUE",
)

# Columns the pipeline cannot run without.
REQUIRED_COLUMNS = ("SOURCE", "COMPILATION_ORDER", "FRICTION_VALUE")

# Column names used by FricMaps before the schema was standardised, mapped onto
# the canonical ones. Tables authored with an earlier version therefore keep
# loading unchanged, and are rewritten with canonical names on the next save.
LEGACY_ALIASES = {
    "NOM_CLASSE": "CLASS_NAME",
    "ORDRE_COMPILATION": "COMPILATION_ORDER",
    "DESCRIPTION_CLASSE": "DESCRIPTION",
    "FILTRE": "SQL_FILTER",
    "COEFF_FRICTION": "FRICTION_VALUE",
}


def canonical_column(name):
    """Return the canonical name of a single column.

    Strips whitespace and a possible UTF-8 BOM, upper-cases, then resolves
    legacy names. Unknown names are returned unchanged (upper-cased), so a user
    may keep extra columns of their own in the table.
    """
    key = str(name).replace("﻿", "").strip().upper()
    return LEGACY_ALIASES.get(key, key)


def canonical_header(header):
    """Normalise a whole CSV header row onto the canonical column names."""
    return [canonical_column(h) for h in header]
