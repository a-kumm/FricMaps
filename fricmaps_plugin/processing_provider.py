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

from qgis.core import QgsProcessingProvider
from .processing_algorithm import FricMapsAlgorithm


class FricMapsProvider(QgsProcessingProvider):

    def loadAlgorithms(self):
        self.addAlgorithm(FricMapsAlgorithm())

    def id(self):
        return "fricmaps"

    def name(self):
        return "FricMaps"

    def icon(self):
        return QgsProcessingProvider.icon(self)

    def longName(self):
        return self.name()
