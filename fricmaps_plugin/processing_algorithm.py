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

from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterFile,
    QgsProcessingParameterString,
    QgsProcessingParameterField,
    QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingOutputFolder,
    QgsProcessingOutputRasterLayer,
    QgsProcessingException,
    QgsMessageLog,
    Qgis,
    QgsApplication,
)
from .core.full_pipeline import run_pipeline


class FricMapsAlgorithm(QgsProcessingAlgorithm):

    def tr(self, string):
        return QgsApplication.translate("FricMapsAlgorithm", string)

    # Constants for parameter names
    EPCI_FILE = "EPCI_FILE"
    BASE_DIR = "BASE_DIR"
    OUTPUT_DIR = "OUTPUT_DIR"
    AREA_NAME = "AREA_NAME"
    NAME_FIELD = "NAME_FIELD"
    BUFFER_DIST = "BUFFER_DIST"
    RESOLUTION = "RESOLUTION"
    TABLE_CSV = "TABLE_CSV"
    BUILDING_CODE = "BUILDING_CODE"
    SAVE_VECTORS = "SAVE_VECTORS"
    ONLY_VECTORS = "ONLY_VECTORS"
    SKIP_VECTORS = "SKIP_VECTORS"
    SLOPE_WEIGHTS = "SLOPE_WEIGHTS"
    BUILDING_WEIGHTS = "BUILDING_WEIGHTS"
    CUSTOM_SOURCES = "CUSTOM_SOURCES"
    WEIGHTING_RULES = "WEIGHTING_RULES"
    VERIFY_DATA = "VERIFY_DATA"

    # Output keys. They mirror the keys returned by run_pipeline(), so a script
    # gets the path of every deliverable back instead of having to rebuild the
    # filenames itself.
    OUTPUT_FOLDER = "OUTPUT_FOLDER_PATH"
    OUTPUT_LAND_COVER = "OUTPUT_LAND_COVER"
    OUTPUT_FRICTION = "OUTPUT_FRICTION"
    OUTPUT_FRICTION_WEIGHTED = "OUTPUT_FRICTION_WEIGHTED"
    OUTPUT_SCENARIO_NO_FENCES = "OUTPUT_SCENARIO_NO_FENCES"
    OUTPUT_SCENARIO_NO_LTI = "OUTPUT_SCENARIO_NO_LTI"
    OUTPUT_SCENARIO_NO_BARRIERS = "OUTPUT_SCENARIO_NO_BARRIERS"

    #: Maps a run_pipeline() result key onto the algorithm output it feeds.
    _RESULT_TO_OUTPUT = {
        "classes_raster": OUTPUT_LAND_COVER,
        "friction_raster": OUTPUT_FRICTION,
        "friction_weighted_raster": OUTPUT_FRICTION_WEIGHTED,
        "scenario_no_fences": OUTPUT_SCENARIO_NO_FENCES,
        "scenario_no_lti": OUTPUT_SCENARIO_NO_LTI,
        "scenario_no_barriers": OUTPUT_SCENARIO_NO_BARRIERS,
    }

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.EPCI_FILE, "Study Area Layer (EPCI)", types=[QgsProcessing.TypeVectorPolygon]
            )
        )

        self.addParameter(
            QgsProcessingParameterField(
                self.NAME_FIELD,
                "Name Field",
                parentLayerParameterName=self.EPCI_FILE,
                type=QgsProcessingParameterField.String,
            )
        )

        self.addParameter(
            QgsProcessingParameterString(
                self.AREA_NAME,
                "Area Name (Value to filter)",
                defaultValue="CC du Pays de Wissembourg",
            )
        )

        self.addParameter(
            QgsProcessingParameterFile(
                self.BASE_DIR, "Base Data Directory", behavior=QgsProcessingParameterFile.Folder
            )
        )

        self.addParameter(
            QgsProcessingParameterFile(
                self.OUTPUT_DIR, "Output Directory", behavior=QgsProcessingParameterFile.Folder
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.BUFFER_DIST,
                "Buffer Distance (m)",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=5000.0,
                minValue=0.0,
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.RESOLUTION,
                "Resolution (m)",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=5.0,
                minValue=0.1,
            )
        )

        self.addParameter(
            QgsProcessingParameterFile(
                self.TABLE_CSV,
                "Classification Table (CSV)",
                behavior=QgsProcessingParameterFile.File,
                extension="csv",
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.BUILDING_CODE,
                "Building Class Code",
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=29,
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.SAVE_VECTORS, "Save Intermediate Vector Layers", defaultValue=True
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.ONLY_VECTORS, "Vector Processing Only", defaultValue=False
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.SKIP_VECTORS, "Skip Vector Processing (Use Existing)", defaultValue=False
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.SLOPE_WEIGHTS, "Slope Weights (JSON)", defaultValue="[]", optional=True
            )
        )

        self.addParameter(
            QgsProcessingParameterString(
                self.BUILDING_WEIGHTS,
                "Building Distance Weights (JSON)",
                defaultValue="[]",
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterString(
                self.CUSTOM_SOURCES, "Custom Sources (JSON)", defaultValue="[]", optional=True
            )
        )

        self.addParameter(
            QgsProcessingParameterString(
                self.WEIGHTING_RULES, "Weighting Rules (JSON)", defaultValue="[]", optional=True
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.VERIFY_DATA, "Verify required datasets before processing", defaultValue=True
            )
        )

        # Outputs
        self.addOutput(QgsProcessingOutputFolder(self.OUTPUT_FOLDER, "Output folder"))
        for key, label in (
            (self.OUTPUT_LAND_COVER, "Land-cover raster"),
            (self.OUTPUT_FRICTION, "Friction raster"),
            (self.OUTPUT_FRICTION_WEIGHTED, "Weighted friction raster"),
            (self.OUTPUT_SCENARIO_NO_FENCES, "Scenario: no fences"),
            (self.OUTPUT_SCENARIO_NO_LTI, "Scenario: no linear transport infrastructure"),
            (self.OUTPUT_SCENARIO_NO_BARRIERS, "Scenario: no barriers"),
        ):
            self.addOutput(QgsProcessingOutputRasterLayer(key, label))

    def processAlgorithm(self, parameters, context, feedback):
        epci_layer = self.parameterAsVectorLayer(parameters, self.EPCI_FILE, context)
        if epci_layer is None:
            raise QgsProcessingException("Invalid EPCI layer")

        epci_path = epci_layer.source().split("|")[0]  # Best effort to get path

        base_dir = self.parameterAsString(parameters, self.BASE_DIR, context)
        output_dir = self.parameterAsString(parameters, self.OUTPUT_DIR, context)
        area_name = self.parameterAsString(parameters, self.AREA_NAME, context)
        name_field = self.parameterAsString(parameters, self.NAME_FIELD, context)
        buffer_dist = self.parameterAsDouble(parameters, self.BUFFER_DIST, context)
        resolution = self.parameterAsDouble(parameters, self.RESOLUTION, context)
        table_csv = self.parameterAsString(parameters, self.TABLE_CSV, context)
        building_code = self.parameterAsInt(parameters, self.BUILDING_CODE, context)
        save_vectors = self.parameterAsBool(parameters, self.SAVE_VECTORS, context)
        only_vectors = self.parameterAsBool(parameters, self.ONLY_VECTORS, context)
        skip_vectors = self.parameterAsBool(parameters, self.SKIP_VECTORS, context)
        slope_weights = self.parameterAsString(parameters, self.SLOPE_WEIGHTS, context)
        building_weights = self.parameterAsString(parameters, self.BUILDING_WEIGHTS, context)
        custom_sources = self.parameterAsString(parameters, self.CUSTOM_SOURCES, context)
        weighting_rules = self.parameterAsString(parameters, self.WEIGHTING_RULES, context)
        verify_data = self.parameterAsBool(parameters, self.VERIFY_DATA, context)

        # Adapters for callbacks
        def progress_callback(percent, msg):
            feedback.setProgress(percent)
            feedback.setProgressText(msg)

        def log_callback(msg):
            feedback.pushInfo(msg)
            QgsMessageLog.logMessage(msg, "FricMaps", Qgis.Info)

        try:
            results = run_pipeline(
                epci_file=epci_path,
                base_dir=base_dir,
                output_dir=output_dir,
                area_name=area_name,
                name_field=name_field,
                buffer_dist=buffer_dist,
                resolution=resolution,
                table_csv=table_csv if table_csv else None,
                building_code=building_code,
                save_vector_outputs=save_vectors,
                only_vectors=only_vectors,
                skip_vectors=skip_vectors,
                slope_weights=slope_weights,
                building_weights=building_weights,
                custom_sources=custom_sources,
                weighting_rules=weighting_rules,
                verify_data=verify_data,
                progress_callback=progress_callback,
                log_callback=log_callback,
            )
        except Exception as e:
            raise QgsProcessingException(f"Pipeline failed: {e}")

        # Hand every produced raster back to the caller. Missing keys are simply
        # absent from the result (a vector-only run produces no raster at all).
        outputs = {self.OUTPUT_FOLDER: output_dir}
        for result_key, output_key in self._RESULT_TO_OUTPUT.items():
            path = (results or {}).get(result_key)
            if path:
                outputs[output_key] = path
        return outputs

    def name(self):
        return "build_surfaces"

    def displayName(self):
        return "Build land-cover and resistance surfaces"

    def group(self):
        """Returns the group name."""
        return self.tr("FricMaps")

    def groupId(self):
        """Returns the group ID."""
        return "fricmaps"

    def flags(self):
        """Returns the algorithm flags.

        The algorithm is deliberately NOT hidden from the Processing toolbox:
        exposing it is what makes a run reproducible from a script, without
        opening the interface.
        """
        return super().flags()

    def shortHelpString(self):
        return self.tr("Builds land-cover and resistance surfaces for connectivity modelling.")

    def createInstance(self):
        return FricMapsAlgorithm()
