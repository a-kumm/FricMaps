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
"""Entry point of the FricMaps QGIS plugin.

This directory is the plugin itself: zip it, or drop it into the QGIS plugins
folder, and it installs as-is. It is self-contained, so everything the plugin
needs at run time lives here:

``metadata.txt``, ``icon.png``
    Declared to QGIS.
``main_plugin``
    Menu and toolbar registration, Processing provider.
``dialog``
    The graphical interface.
``processing_algorithm`` / ``processing_provider``
    The scriptable ``QgsProcessingAlgorithm`` and its provider, so the pipeline
    can be driven from Python without opening the interface.
``core``
    Data preparation, rasterisation, weighting and shared helpers.
``resources``
    Default classification table and images used by the interface.

QGIS calls :func:`classFactory` when loading the plugin.
"""

import os

#: Absolute path of this plugin folder. Resources are resolved from it rather
#: than from each module's own ``__file__``, so moving a module around cannot
#: silently break the lookup.
PLUGIN_ROOT = os.path.dirname(os.path.abspath(__file__))


def classFactory(iface):
    """Create and return an instance of the FricMaps plugin.

    QGIS calls this function when loading the plugin, providing the main
    interface.

    Args:
        iface (QgisInterface): The QGIS application interface supplied by QGIS.

    Returns:
        FricMapsPlugin: a ready-to-use plugin instance.
    """
    # Imported lazily so that merely discovering the plugin does not pull in
    # PyQt, GDAL and the whole processing stack.
    from .main_plugin import FricMapsPlugin

    return FricMapsPlugin(iface)
