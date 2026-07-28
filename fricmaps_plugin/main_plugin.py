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
"""Main plugin entry point for the FricMaps QGIS plugin.

This class handles the integration of the plugin into QGIS.
It registers the Processing Provider and connects the toolbar button
to the native Processing Dialog for the FricMaps algorithm.
"""

import logging
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsApplication

# Imported for its side effect: it initialises the QGIS Processing framework
# before this plugin registers its own provider. Never referenced directly.
import processing  # noqa: F401

from . import PLUGIN_ROOT
from .processing_provider import FricMapsProvider
from .dialog import FricMapsDialog


class FricMapsPlugin:
    """FricMaps plugin class."""

    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.action = None
        self.dlg = None

    def initProcessing(self):
        """Register the Processing provider."""
        self.provider = FricMapsProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        """Register the plugin action in the QGIS interface."""
        self.initProcessing()

        # Create an action with a label and (optionally) an icon
        import os

        icon_path = os.path.join(PLUGIN_ROOT, "icon.png")
        self.action = QAction(QIcon(icon_path), "FricMaps…", self.iface.mainWindow())
        self.action.triggered.connect(self.run)

        # Add the action to the plugin menu and toolbar
        self.iface.addPluginToMenu("&FricMaps", self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        """Remove the plugin action and provider."""
        if self.dlg is not None:
            try:
                self.dlg.close()
            except RuntimeError:
                logging.getLogger(__name__).debug("Suppressed exception", exc_info=True)
                pass
            self.dlg = None

        if self.provider:
            try:
                QgsApplication.processingRegistry().removeProvider(self.provider)
            except RuntimeError:
                # Provider C++ object might already be deleted
                logging.getLogger(__name__).debug("Suppressed exception", exc_info=True)
                pass
            self.provider = None

        if self.action:
            self.iface.removePluginMenu("&FricMaps", self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None

    def run(self):
        """Open the unified single-window interface.

        Shown NON-modally and parented to the QGIS main window so the panel
        stays overlaid on QGIS (and in the same macOS Space) instead of being
        pushed to a separate desktop like an application-modal dialog would.
        A single instance is reused and simply raised on subsequent clicks.
        """
        if self.dlg is None:
            self.dlg = FricMapsDialog(self.iface.mainWindow())
            # Drop our reference when the window is closed so it can be re-created.
            self.dlg.finished.connect(self._on_dialog_finished)
        self.dlg.show()
        self.dlg.raise_()
        self.dlg.activateWindow()

    def _on_dialog_finished(self, *args):
        self.dlg = None
