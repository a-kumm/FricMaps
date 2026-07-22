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

import os
import json
import time
from qgis.PyQt.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QFormLayout,
    QLabel,
    QDoubleSpinBox,
    QCheckBox,
    QMessageBox,
    QTabWidget,
    QTextEdit,
    QProgressBar,
    QPushButton,
    QWidget,
    QHBoxLayout,
    QTextBrowser,
    QFrame,
    QSplitter,
    QRadioButton,
    QButtonGroup,
    QGridLayout,
    QGroupBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QFileDialog,
    QItemDelegate,
    QLineEdit,
    QComboBox,
    QSpinBox,
    QSizePolicy,
    QApplication,
    QToolButton,
    QScrollArea,
)
from qgis.PyQt.QtCore import Qt, pyqtSignal, QObject, QRegExp
from qgis.PyQt.QtGui import QRegExpValidator, QPalette, QColor, QIcon, QPixmap
from qgis.gui import (
    QgsMapLayerComboBox,
    QgsFieldComboBox,
    QgsFileWidget,
    QgsExpressionBuilderDialog,
)
from qgis.core import (
    QgsProject,
    QgsVectorLayer,
    QgsApplication,
    QgsMapLayerProxyModel,
    QgsTask,
    QgsProcessingFeedback,
)
import processing

from . import PLUGIN_ROOT
from .core.table_schema import canonical_header

# --- Translations ---
TRANSLATIONS = {
    "EN": {
        "window_title": "FricMaps - Landscape Connectivity & Fence Permeability",
        "tab_vector": "1 - Vector Processing",
        "tab_raster": "2 - Rasterization",
        "tab_logs": "Logs",
        "lbl_epci": "Study Area Layer :",
        "lbl_source_type": "Source Type:",
        "rb_layer": "Project Layer",
        "rb_file": "File (SHP/GPKG)",
        "lbl_epci_file": "Study Area File:",
        "lbl_name_field": "Name Field:",
        "lbl_area_name": "Area Name (Value to filter):",
        "lbl_base_dir": "Base Data Directory:",
        "lbl_output_dir": "Output Directory:",
        "lbl_buffer": "Buffer Distance:",
        "lbl_resolution": "Resolution:",
        "lbl_csv": "Classification Table (CSV) [Optional]:",
        "lbl_build_code": "Building Class Code:",
        "chk_save_vectors": "Save Intermediate Vector Layers",
        "chk_verify_data": "Verify required datasets before processing",
        "btn_run_vector": "Run Vector Processing",
        "btn_run_raster": "▶  Run Rasterization & Weighting",
        "btn_close": "Close",
        "btn_cancel": "Cancel Process",
        "btn_save_config": "💾 Save Config",
        "btn_load_config": "📂 Load Config",
        "msg_config_saved": "Configuration saved successfully!",
        "msg_config_loaded": "Configuration loaded successfully!",
        "msg_config_error": "Error managing configuration: ",
        "info_header": """
        <table width="100%">
            <tr>
                <td valign="middle">
                    <h3 style="color:{title}; margin:0;">FricMaps Plugin</h3>
                </td>
                <td align="right" valign="middle">
                    <img src="{logo_path}" width="100" />
                </td>
            </tr>
        </table>
        <hr style="border:0; border-top:1px solid {border};" />
        """,
        "info_tab_vector": """
        <p style="color:{accent}; font-weight:bold; margin:0 0 2px 0;">STEP 1 &mdash; VECTOR PROCESSING</p>
        <p style="margin-top:0;">Queries, clips and harmonises every source dataset over your study area, and turns
        linear features (roads, railways, hedgerows, streams) into polygonal footprints so they are
        faithfully represented at the target resolution. These layers are what the rasteriser burns in.</p>
        <p style="font-weight:bold; margin-bottom:2px;">How to proceed</p>
        <ol style="margin-top:0;">
            <li>Define the <b>study area</b>: pick a project layer or a file (SHP/GPKG), then the
            <b>name field</b> and the value identifying your territory.</li>
            <li>Set the <b>base directory</b> holding the source data (BD TOPO, OCS GE, RPG,
            RGE ALTI&hellip;). Sub-folders are scanned recursively, so the raw delivery tree can be
            left as downloaded.</li>
            <li>Set the <b>output directory</b> where layers and rasters will be written.</li>
            <li>Optionally apply a <b>buffer</b> around the study area to avoid edge effects in the
            connectivity graph.</li>
            <li>Use <b>Custom Sources</b> to declare any dataset beyond the built-in ones, with its
            own detection rule, SQL filter and buffer.</li>
        </ol>
        <table width="100%" cellpadding="6" bgcolor="{surface_alt}">
            <tr><td>
                <b>Good to know.</b> Legacy Shapefile deliveries and the BD TOPO 3.x GeoPackage
                model are both supported: field names are harmonised automatically. Attribute-dependent
                buffering widens roads and railways into realistic rights-of-way, and wildlife crossings
                (BD ORFeH) locally restore permeability. Leave <i>Verify required datasets</i> ticked for a
                pre-flight completeness check.
            </td></tr>
        </table>
        """,
        "info_tab_raster": """
        <p style="color:{accent}; font-weight:bold; margin:0 0 2px 0;">STEP 2 &mdash; RASTERIZATION</p>
        <p style="margin-top:0;">Burns the harmonised layers into two rasters &mdash; a <b>land-cover map</b> and its baseline
        <b>resistance (friction) surface</b> &mdash; driven by the classification table below. Each row is a
        geospatial rule; a hierarchical stacking engine rasterises them by priority, so higher-priority
        elements (roads) overwrite lower ones (land cover).</p>
        <p style="font-weight:bold; margin-bottom:2px;">Reading the table</p>
        <ul style="margin-top:0;">
            <li><b>SOURCE</b> &mdash; the dataset the class is drawn from.</li>
            <li><b>COMPILATION_ORDER</b> &mdash; burn priority. Higher values are rasterised last and
            therefore overwrite lower ones; use it to place roads above land cover.</li>
            <li><b>FRICTION</b> &mdash; the resistance value passed to Graphab. Low means permeable,
            high means costly to cross.</li>
            <li><b>SQL_FILTER</b> &mdash; an optional SQL expression restricting the class to a subset of
            features. Click the cell to open a full expression builder on the right source.</li>
        </ul>
        <table width="100%" cellpadding="6" bgcolor="{surface_alt}">
            <tr><td>
                <b>Good to know.</b> The table is a semicolon-separated CSV, editable in place or loaded
                from disk; a default 39-class table ships in <i>resources/</i>. The <i>resolution</i> also drives
                the buffering of linear features, so barriers stay continuous. Any row added here becomes a
                target in the Weighting tab.
            </td></tr>
        </table>
        """,
        "info_tab_weights": """
        <p style="color:{accent}; font-weight:bold; margin:0 0 2px 0;">STEP 3 &mdash; WEIGHTING</p>
        <p style="margin-top:0;">Applies a <b>multiplicative modulation</b> on top of the base
        friction, to represent gradual effects that a discrete class cannot capture.</p>
        <p style="font-weight:bold; margin-bottom:2px;">How to proceed</p>
        <ol style="margin-top:0;">
            <li>Click <b>Add a weighting</b> to create a rule card.</li>
            <li>Choose the rule <b>type</b>: <i>Slope</i>, computed from the DEM, or
            <i>Distance to a layer/class</i>.</li>
            <li>Select the <b>target</b>: any class of the table or any custom source.</li>
            <li>Fill the <b>bands</b>: for each interval, the factor multiplying the friction.</li>
            <li>Launch the full run &mdash; including the <i>no fences</i> / <i>no linear transport
            infrastructure</i> / <i>no barriers</i> scenarios &mdash; from the button at the bottom of this tab.</li>
        </ol>
        <table width="100%" cellpadding="6" bgcolor="{surface_alt}">
            <tr><td>
                <b>Good to know.</b> Factors multiply, so <b>1.0 is neutral</b> and values below 1
                make a zone <i>more</i> permeable. Distance weighting is the way to model diffuse
                pressures &mdash; light pollution around streetlights, disturbance around buildings
                &mdash; whose influence fades with distance.
            </td></tr>
        </table>
        """,
        "info_tab_logs": """
        <p style="color:{accent}; font-weight:bold; margin:0 0 2px 0;">STEP 4 &mdash; LOGS</p>
        <p style="margin-top:0;">Follows the run step by step. Each stage reports the number of
        features it retained, which is the quickest way to check that a dataset was read correctly.</p>
        <p style="font-weight:bold; margin-bottom:2px;">Reading the log</p>
        <ul style="margin-top:0;">
            <li>A step returning an unexpectedly <b>low feature count</b> usually points to a field
            naming or value-domain mismatch between two vintages of the same dataset.</li>
            <li>A skipped optional source is reported as a warning and never aborts the run.</li>
            <li>Messages are mirrored in the QGIS <i>Log Messages</i> panel.</li>
        </ul>
        <table width="100%" cellpadding="6" bgcolor="{surface_alt}">
            <tr><td>
                <b>Good to know.</b> When reporting an issue, copy the log: it contains the resolved
                paths and the per-step counts needed to reproduce the problem.
            </td></tr>
        </table>
        <p style="color:{text_subtle}; font-size:11pt;">FricMaps &mdash; friction and land-cover maps
        for ecological connectivity modelling.</p>
        """,
        "msg_error_epci": "Please select a Study Area layer.",
        "msg_error_base": "Please specify a valid Base Data Directory.",
        "msg_error_out": "Please specify an Output Directory.",
        "msg_error_epci_file": "Please select a valid Study Area file.",
        "msg_error_memory_layer": "Memory layers are not supported directly. Please save to disk first.",
        "msg_error_base_output": "Please specify Base and Output directories.",
        "msg_error_name_area": "Please select a Name Field and an Area Name.",
        "msg_success": "Process completed successfully!",
        "msg_failed": "Processing failed. Check logs.",
        "log_start": "🚀 Starting process...",
        "log_success": "✅ Process completed successfully!",
        "log_fail": "\n❌ Process failed.",
        "log_error": "❌ Error during process: ",
        "grp_input": "Input Data",
        "grp_params": "Processing Parameters",
        "grp_output": "Output",
        "tab_config": "Configuration",
        "btn_load_csv": "Load CSV",
        "btn_save_csv": "Save CSV",
        "btn_add_row": "Add Row",
        "btn_remove_row": "Remove Row",
        "msg_confirm_save": "Do you want to overwrite the existing file?",
        "msg_save_success": "File saved successfully!",
        "msg_load_error": "Error loading CSV file.",
        "tab_weights": "3 - Weighting Options",
        "tab_custom_new": "Custom Sources",
        "tab_raster_new": "2 - Rasterization",
        "tab_logs_new": "4 - Logs",
        "btn_open_custom": "🧩  Custom Sources…",
        "custom_win_title": "Custom Data Sources",
        "grp_slope_weights": "Slope Weighting (Degrees)",
        "grp_dist_weights": "Distance to Buildings Weighting (Meters)",
        "col_min": "Min",
        "col_max": "Max",
        "col_weight": "Weight Factor",
        "weights_intro": (
            "<b>Weighting</b> multiplies the friction of the map. Add as many "
            "rules as you like: each rule targets a factor (slope) or the distance "
            "to <b>any class or custom source</b>, with your own distance/×weight bands."
        ),
        "btn_add_weight": "➕  Add a weighting",
        "wtype_slope": "Slope (DEM)",
        "wtype_distance": "Distance to a layer/class",
        "lbl_target": "Target:",
        "enable_rule": "Active",
        "btn_remove_rule": "Remove this weighting",
        "target_building": "Buildings (class code)",
        "grp_custom_list": "Custom Data Sources",
        "grp_custom_bands": "Distance / Light Weighting — Custom Sources",
        "weight_help": (
            "Apply a decreasing multiplier around a declared custom source "
            "(e.g. light halo around street lights). Pick the target Layer, then "
            "add distance bands (metres) → weight. Declare sources first via "
            "‘Custom Sources…’ in the Vector Processing tab."
        ),
        "btn_add_source": "Add Source",
        "btn_remove_source": "Remove Source",
        "btn_add_band": "Add Band",
        "btn_remove_band": "Remove Band",
        "col_c_key": "Key (SOURCE)",
        "col_c_label": "Label",
        "col_c_type": "Type",
        "col_c_source": "File / Layer",
        "col_c_buffer": "Buffer (m)",
        "col_c_filter": "SQL Filter",
        "col_w_source": "Layer",
        "type_file": "File",
        "type_layer": "QGIS Layer",
        "tt_key": "Unique ID — reuse it in the SOURCE column of the CSV table.",
        "tt_label": "Free description (display only).",
        "tt_type": "‘File’: a dataset on disk. ‘QGIS Layer’: a layer already loaded in the project.",
        "tt_source": "Browse a file with the … button, or pick a project layer.",
        "tt_buffer": "Buffer radius (m). MANDATORY for point data, otherwise invisible in the raster.",
        "tt_filter": "Optional QGIS expression to keep only some features (e.g. \"etat\" = 'active').",
        "custom_help": (
            "<b>Custom sources</b> let you add ANY vector dataset (e.g. street "
            "lights) without coding.<br>"
            "<b>1.</b> Declare a source below: a <b>Key</b>, its <b>Type</b> "
            "(a file on disk, or a layer loaded in QGIS), the <b>file/layer</b> "
            "itself, a <b>buffer</b> (mandatory for points), and an optional SQL filter "
            "(double-click the filter cell for the expression console).<br>"
            "<b>2.</b> In the CSV table (Rasterization tab), add row(s) with the same "
            "<b>SOURCE</b> key to set its class &amp; friction.<br>"
            "<b>3.</b> (Optional) Configure a distance/light multiplier in the "
            "<b>Weighting</b> tab."
        ),
    },
    "FR": {
        "window_title": "FricMaps - Connectivité Paysagère & Perméabilité des Clôtures",
        "tab_vector": "1 - Traitement Vecteur",
        "tab_raster": "2 - Rasterisation",
        "tab_logs": "Logs / Journaux",
        "lbl_epci": "Couche Zone d'Étude :",
        "lbl_source_type": "Type de source :",
        "rb_layer": "Couche du projet",
        "rb_file": "Fichier (SHP/GPKG)",
        "lbl_epci_file": "Fichier Zone d'Étude :",
        "lbl_name_field": "Champ Nom :",
        "lbl_area_name": "Nom de la Zone (Filtre) :",
        "lbl_base_dir": "Dossier Données Base :",
        "lbl_output_dir": "Dossier Sortie :",
        "lbl_buffer": "Distance Tampon :",
        "lbl_resolution": "Résolution :",
        "lbl_csv": "Table Classification (CSV) [Optionnel] :",
        "lbl_build_code": "Code Classe Bâti :",
        "chk_save_vectors": "Sauvegarder Couches Vecteurs Intermédiaires",
        "chk_verify_data": "Vérifier les données requises avant traitement",
        "btn_run_vector": "Lancer le traitement vectoriel",
        "btn_run_raster": "▶  Lancer la rasterisation & pondérations",
        "btn_close": "Fermer",
        "btn_cancel": "Annuler le traitement",
        "btn_save_config": "💾 Sauvegarder Config",
        "btn_load_config": "📂 Charger Config",
        "msg_config_saved": "Configuration sauvegardée avec succès !",
        "msg_config_loaded": "Configuration chargée avec succès !",
        "msg_config_error": "Erreur lors de la gestion de la configuration : ",
        "info_header": """
        <table width="100%">
            <tr>
                <td valign="middle">
                    <h3 style="color:{title}; margin:0;">Plugin FricMaps</h3>
                </td>
                <td align="right" valign="middle">
                    <img src="{logo_path}" width="100" />
                </td>
            </tr>
        </table>
        <hr style="border:0; border-top:1px solid {border};" />
        """,
        "info_tab_vector": """
        <p style="color:{accent}; font-weight:bold; margin:0 0 2px 0;">&Eacute;TAPE 1 &mdash; TRAITEMENT VECTEUR</p>
        <p style="margin-top:0;">Interroge, d&eacute;coupe et harmonise l'ensemble des donn&eacute;es sources sur votre zone
        d'&eacute;tude, et convertit les entit&eacute;s lin&eacute;aires (routes, voies ferr&eacute;es, haies,
        cours d'eau) en emprises polygonales pour qu'elles soient fid&egrave;lement repr&eacute;sent&eacute;es
        &agrave; la r&eacute;solution cible. Ce sont ces couches que le rasteriseur grave ensuite.</p>
        <p style="font-weight:bold; margin-bottom:2px;">Marche &agrave; suivre</p>
        <ol style="margin-top:0;">
            <li>D&eacute;finissez la <b>zone d'&eacute;tude</b> : une couche du projet ou un fichier
            (SHP/GPKG), puis le <b>champ nom</b> et la valeur identifiant votre territoire.</li>
            <li>Indiquez le <b>dossier de donn&eacute;es</b> contenant les sources (BD TOPO, OCS GE,
            RPG, RGE ALTI&hellip;). Les sous-dossiers sont parcourus r&eacute;cursivement : vous
            pouvez laisser l'arborescence de livraison telle quelle.</li>
            <li>Choisissez le <b>dossier de sortie</b> o&ugrave; seront &eacute;crits les couches et
            les rasters.</li>
            <li>Appliquez si besoin une <b>zone tampon</b> autour de la zone d'&eacute;tude pour
            &eacute;viter les effets de bord dans le graphe de connectivit&eacute;.</li>
            <li>Utilisez les <b>Sources personnalis&eacute;es</b> pour d&eacute;clarer toute donn&eacute;e
            suppl&eacute;mentaire, avec sa r&egrave;gle de d&eacute;tection, son filtre SQL et son tampon.</li>
        </ol>
        <table width="100%" cellpadding="6" bgcolor="{surface_alt}">
            <tr><td>
                <b>&Agrave; savoir.</b> Les anciennes livraisons Shapefile et le mod&egrave;le
                GeoPackage BD TOPO 3.x sont tous deux pris en charge : les noms de champs sont
                harmonis&eacute;s automatiquement. La bufferisation
                attributaire &eacute;largit routes et voies ferr&eacute;es en emprises r&eacute;alistes, et les
                passages faune (BD ORFeH) restaurent localement la perm&eacute;abilit&eacute;. Laissez
                <i>V&eacute;rifier les donn&eacute;es requises</i> coch&eacute; pour un contr&ocirc;le
                pr&eacute;alable de compl&eacute;tude.
            </td></tr>
        </table>
        """,
        "info_tab_raster": """
        <p style="color:{accent}; font-weight:bold; margin:0 0 2px 0;">&Eacute;TAPE 2 &mdash; RASTERISATION</p>
        <p style="margin-top:0;">Grave les couches harmonis&eacute;es en deux rasters &mdash; une <b>carte d'occupation du sol</b>
        et sa <b>surface de r&eacute;sistance (friction)</b> de base &mdash; pilot&eacute;s par la table de
        classification ci-dessous. Chaque ligne est une r&egrave;gle g&eacute;ospatiale ; un moteur
        d'empilement hi&eacute;rarchique les rasterise par priorit&eacute;, si bien que les &eacute;l&eacute;ments
        prioritaires (routes) &eacute;crasent les autres (occupation du sol).</p>
        <p style="font-weight:bold; margin-bottom:2px;">Lire la table</p>
        <ul style="margin-top:0;">
            <li><b>SOURCE</b> &mdash; la donn&eacute;e dont la classe est issue.</li>
            <li><b>COMPILATION_ORDER</b> &mdash; priorit&eacute; de gravure. Les valeurs les plus
            hautes sont rasteris&eacute;es en dernier et &eacute;crasent donc les plus basses :
            c'est ainsi qu'on place les routes au-dessus de l'occupation du sol.</li>
            <li><b>FRICTION</b> &mdash; la valeur de r&eacute;sistance transmise &agrave; Graphab.
            Faible = perm&eacute;able, &eacute;lev&eacute;e = co&ucirc;teux &agrave; traverser.</li>
            <li><b>SQL_FILTER</b> &mdash; expression SQL facultative restreignant la classe &agrave; un
            sous-ensemble d'entit&eacute;s. Cliquez sur la cellule pour ouvrir un v&eacute;ritable
            constructeur d'expression, sur la bonne source.</li>
        </ul>
        <table width="100%" cellpadding="6" bgcolor="{surface_alt}">
            <tr><td>
                <b>&Agrave; savoir.</b> La table est un CSV s&eacute;par&eacute; par points-virgules,
                &eacute;ditable sur place ou charg&eacute; depuis le disque ; une table par d&eacute;faut de
                39 classes est fournie dans <i>resources/</i>. La <i>r&eacute;solution</i> pilote aussi la
                bufferisation des entit&eacute;s lin&eacute;aires, pour que les barri&egrave;res restent
                continues. Toute ligne ajout&eacute;e ici devient une cible dans l'onglet Pond&eacute;rations.
            </td></tr>
        </table>
        """,
        "info_tab_weights": """
        <p style="color:{accent}; font-weight:bold; margin:0 0 2px 0;">&Eacute;TAPE 3 &mdash; POND&Eacute;RATIONS</p>
        <p style="margin-top:0;">Applique une <b>modulation multiplicative</b> par-dessus la friction
        de base, pour repr&eacute;senter les effets graduels qu'une classe discr&egrave;te ne peut
        pas restituer.</p>
        <p style="font-weight:bold; margin-bottom:2px;">Marche &agrave; suivre</p>
        <ol style="margin-top:0;">
            <li>Cliquez sur <b>Ajouter une pond&eacute;ration</b> pour cr&eacute;er une carte de r&egrave;gle.</li>
            <li>Choisissez le <b>type</b> : <i>Pente</i>, calcul&eacute;e depuis le MNT, ou
            <i>Distance &agrave; une couche/classe</i>.</li>
            <li>S&eacute;lectionnez la <b>cible</b> : n'importe quelle classe de la table ou source
            personnalis&eacute;e.</li>
            <li>Renseignez les <b>bandes</b> : pour chaque intervalle, le facteur multipliant la friction.</li>
            <li>Lancez le traitement complet &mdash; incluant les sc&eacute;narios <i>sans cl&ocirc;tures</i> /
            <i>sans infrastructures de transport lin&eacute;aires</i> / <i>sans barri&egrave;res</i> &mdash;
            depuis le bouton en bas de cet onglet.</li>
        </ol>
        <table width="100%" cellpadding="6" bgcolor="{surface_alt}">
            <tr><td>
                <b>&Agrave; savoir.</b> Les facteurs se multiplient : <b>1,0 est neutre</b> et une
                valeur inf&eacute;rieure &agrave; 1 rend la zone <i>plus</i> perm&eacute;able. La
                pond&eacute;ration par distance est le moyen de mod&eacute;liser les pressions
                diffuses &mdash; pollution lumineuse autour des lampadaires, d&eacute;rangement
                autour du b&acirc;ti &mdash; dont l'influence d&eacute;cro&icirc;t avec la distance.
            </td></tr>
        </table>
        """,
        "info_tab_logs": """
        <p style="color:{accent}; font-weight:bold; margin:0 0 2px 0;">&Eacute;TAPE 4 &mdash; JOURNAUX</p>
        <p style="margin-top:0;">Suit le d&eacute;roul&eacute; du traitement. Chaque &eacute;tape
        indique le nombre d'entit&eacute;s retenues : c'est le moyen le plus rapide de v&eacute;rifier
        qu'une donn&eacute;e a bien &eacute;t&eacute; lue.</p>
        <p style="font-weight:bold; margin-bottom:2px;">Lire le journal</p>
        <ul style="margin-top:0;">
            <li>Une &eacute;tape renvoyant un <b>nombre d'entit&eacute;s anormalement faible</b>
            traduit g&eacute;n&eacute;ralement un &eacute;cart de nom de champ ou de domaine de
            valeurs entre deux mill&eacute;simes d'une m&ecirc;me donn&eacute;e.</li>
            <li>Une source optionnelle absente est signal&eacute;e en avertissement et n'interrompt
            jamais le traitement.</li>
            <li>Les messages sont repris dans le panneau <i>Messages de journal</i> de QGIS.</li>
        </ul>
        <table width="100%" cellpadding="6" bgcolor="{surface_alt}">
            <tr><td>
                <b>&Agrave; savoir.</b> Pour signaler un probl&egrave;me, copiez le journal : il
                contient les chemins r&eacute;solus et les comptages par &eacute;tape
                n&eacute;cessaires pour reproduire le cas.
            </td></tr>
        </table>
        <p style="color:{text_subtle}; font-size:11pt;">FricMaps &mdash; cartes de friction et
        d'occupation du sol pour la mod&eacute;lisation de la connectivit&eacute; &eacute;cologique.</p>
        """,
        "msg_error_epci": "Veuillez sélectionner une couche Zone d'Étude.",
        "msg_error_epci_file": "Veuillez sélectionner un fichier Zone d'Étude valide.",
        "msg_error_memory_layer": "Les couches mémoire ne sont pas supportées. Veuillez sauvegarder sur disque.",
        "msg_error_base": "Veuillez spécifier un dossier de données de base valide.",
        "msg_error_out": "Veuillez spécifier un dossier de sortie.",
        "msg_error_base_output": "Veuillez spécifier les dossiers de base et de sortie.",
        "msg_error_name_area": "Veuillez sélectionner un champ Nom et un Nom de Zone.",
        "msg_success": "Traitement terminé avec succès !",
        "msg_failed": "Échec du traitement. Vérifiez les journaux.",
        "log_start": "🚀 Démarrage du traitement...",
        "log_success": "\n✅ Traitement terminé avec succès !",
        "log_fail": "\n❌ Échec du traitement.",
        "grp_input": "Données d'Entrée",
        "grp_params": "Paramètres de Traitement",
        "grp_output": "Sortie",
        "tab_config": "Configuration",
        "btn_load_csv": "Charger CSV",
        "btn_save_csv": "Sauvegarder CSV",
        "btn_add_row": "Ajouter Ligne",
        "btn_remove_row": "Supprimer Ligne",
        "msg_confirm_save": "Voulez-vous écraser le fichier existant ?",
        "msg_save_success": "Fichier sauvegardé avec succès !",
        "msg_load_error": "Erreur lors du chargement du fichier CSV.",
        "tab_weights": "3 - Pondérations",
        "tab_custom_new": "Sources Personnalisées",
        "tab_raster_new": "2 - Rasterisation",
        "tab_logs_new": "4 - Logs / Journaux",
        "btn_open_custom": "🧩  Sources personnalisées…",
        "custom_win_title": "Sources de Données Personnalisées",
        "grp_slope_weights": "Pondération de la Pente (Degrés)",
        "grp_dist_weights": "Pondération de la Distance au Bâti (Mètres)",
        "col_min": "Min",
        "col_max": "Max",
        "col_weight": "Poids (Multiplicateur)",
        "weights_intro": (
            "La <b>pondération</b> multiplie la friction de la carte. Ajoutez "
            "autant de règles que voulu : chaque règle cible un facteur (pente) ou "
            "la distance à <b>n'importe quelle classe ou source personnalisée</b>, "
            "avec vos propres bandes distance/×poids."
        ),
        "btn_add_weight": "➕  Ajouter une pondération",
        "wtype_slope": "Pente (MNT)",
        "wtype_distance": "Distance à une couche/classe",
        "lbl_target": "Cible :",
        "enable_rule": "Active",
        "btn_remove_rule": "Supprimer cette pondération",
        "target_building": "Bâti (code de classe)",
        "grp_custom_list": "Sources de Données Personnalisées",
        "grp_custom_bands": "Pondération Distance / Lumière — Sources Personnalisées",
        "weight_help": (
            "Applique un multiplicateur dégressif autour d'une source personnalisée "
            "déclarée (ex. halo lumineux autour des lampadaires). Choisissez la "
            "Couche cible, puis ajoutez des bandes de distance (mètres) → poids. "
            "Déclarez d'abord vos sources via « Sources personnalisées… » dans "
            "l'onglet Traitement Vecteur."
        ),
        "btn_add_source": "Ajouter une Source",
        "btn_remove_source": "Supprimer la Source",
        "btn_add_band": "Ajouter une Bande",
        "btn_remove_band": "Supprimer la Bande",
        "col_c_key": "Clé (SOURCE)",
        "col_c_label": "Libellé",
        "col_c_type": "Type",
        "col_c_source": "Fichier / Couche",
        "col_c_buffer": "Buffer (m)",
        "col_c_filter": "Filtre SQL",
        "col_w_source": "Couche",
        "type_file": "Fichier",
        "type_layer": "Couche QGIS",
        "tt_key": "Identifiant unique — à réutiliser dans la colonne SOURCE du tableau CSV.",
        "tt_label": "Description libre (affichage uniquement).",
        "tt_type": "« Fichier » : une donnée sur le disque. « Couche QGIS » : une couche déjà chargée dans le projet.",
        "tt_source": "Parcourir un fichier avec le bouton …, ou choisir une couche du projet.",
        "tt_buffer": "Rayon de buffer (m). INDISPENSABLE pour les données ponctuelles, sinon invisibles dans le raster.",
        "tt_filter": "Expression QGIS optionnelle pour ne garder que certains objets (ex. \"etat\" = 'actif').",
        "custom_help": (
            "<b>Les sources personnalisées</b> permettent d'ajouter N'IMPORTE "
            "quelle donnée vecteur (ex. lampadaires) sans coder.<br>"
            "<b>1.</b> Déclarez une source ci-dessous : une <b>Clé</b>, son "
            "<b>Type</b> (un fichier sur le disque, ou une couche chargée dans "
            "QGIS), le <b>fichier/couche</b> lui-même, un <b>buffer</b> "
            "(indispensable pour les points), et un filtre SQL optionnel "
            "(double-cliquez la cellule filtre pour la console d'expression).<br>"
            "<b>2.</b> Dans le tableau CSV (onglet Rasterisation), ajoutez une ou "
            "des lignes avec la même clé <b>SOURCE</b> pour définir sa classe et sa friction.<br>"
            "<b>3.</b> (Optionnel) Configurez un multiplicateur distance/lumière "
            "dans l'onglet <b>Pondérations</b>."
        ),
    },
}


class CustomFeedback(QgsProcessingFeedback):
    """Custom feedback to emit signals for the GUI."""

    def __init__(self, progress_callback, log_callback):
        super().__init__()
        self.progress_callback = progress_callback
        self.log_callback = log_callback

    def setProgress(self, progress):
        self.progress_callback(progress)

    def setProgressText(self, text):
        self.log_callback(f"⏳ {text}")

    def pushInfo(self, info):
        self.log_callback(f"ℹ️ {info}")

    def pushCommandInfo(self, info):
        self.log_callback(f"🔧 {info}")

    def pushDebugInfo(self, info):
        self.log_callback(f"🐛 {info}")

    def pushConsoleInfo(self, info):
        self.log_callback(f"💻 {info}")

    def reportError(self, error, fatalError=False):
        self.log_callback(f"❌ {error}")


class SourceDelegate(QItemDelegate):
    """Delegate for SOURCE column (editable ComboBox).

    The list of allowed sources is the built-in socle PLUS any custom source
    keys currently declared in the "Custom Sources" tab. The custom keys are
    fetched live at edit time via ``key_provider`` so the dropdown always
    reflects the latest declarations.
    """

    BASE_ITEMS = [
        "OCS",
        "VEGETATION",
        "RPG",
        "HEDGES",
        "HYDRO",
        "BUILT_AREA",
        "TECH_INFRA",
        "LTI",
        "SOLAR_FENCES",
        "OTHER",
    ]

    def __init__(self, parent=None, key_provider=None):
        super().__init__(parent)
        self.key_provider = key_provider  # callable → list[str] of custom keys

    def createEditor(self, parent, option, index):
        editor = QComboBox(parent)
        editor.setEditable(True)  # allow free-typing of any source key
        items = list(self.BASE_ITEMS)
        if callable(self.key_provider):
            try:
                for k in self.key_provider():
                    if k and k not in items:
                        items.append(k)
            except Exception:
                pass
        editor.addItems(items)
        return editor

    def setEditorData(self, editor, index):
        value = index.model().data(index, Qt.EditRole)
        if value:
            idx = editor.findText(value)
            if idx >= 0:
                editor.setCurrentIndex(idx)

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText(), Qt.EditRole)


class UppercaseDelegate(QItemDelegate):
    """Delegate for CLASS_NAME (Uppercase, Alphanumeric)."""

    def createEditor(self, parent, option, index):
        editor = QLineEdit(parent)
        # Regex: Uppercase letters, numbers, underscores, no spaces
        regex = QRegExp("[A-Z0-9_]+")
        validator = QRegExpValidator(regex, editor)
        editor.setValidator(validator)
        return editor

    def setModelData(self, editor, model, index):
        model.setData(index, editor.text().upper(), Qt.EditRole)


class IntegerDelegate(QItemDelegate):
    """Delegate for FRICTION_VALUE (integer friction coefficient)."""

    def createEditor(self, parent, option, index):
        editor = QSpinBox(parent)
        editor.setRange(0, 1000000)  # friction can reach 10000+ (obstacles)
        return editor

    def setEditorData(self, editor, index):
        value = index.model().data(index, Qt.EditRole)
        try:
            editor.setValue(int(value))
        except:
            editor.setValue(0)

    def setModelData(self, editor, model, index):
        model.setData(index, str(editor.value()), Qt.EditRole)


class SqlDelegate(QItemDelegate):
    """Delegate for SQL_FILTER (SQL Builder)."""

    def __init__(self, parent=None, layer=None):
        super().__init__(parent)
        self.layer = layer  # We might need a reference layer, but for now generic

    def createEditor(self, parent, option, index):
        # We don't want a standard editor, we want a dialog.
        # But QItemDelegate expects a widget.
        # Strategy: Use a LineEdit with a tool button or just double click event on table.
        # Simpler: Just use LineEdit, but we'll handle the double-click in the main dialog to open the builder.
        editor = QLineEdit(parent)
        return editor


class FloatDelegate(QItemDelegate):
    """Delegate for Float values in Weighting tables."""

    def createEditor(self, parent, option, index):
        editor = QDoubleSpinBox(parent)
        editor.setRange(0, 100000)
        editor.setDecimals(2)
        return editor

    def setEditorData(self, editor, index):
        value = index.model().data(index, Qt.EditRole)
        try:
            editor.setValue(float(value))
        except:
            editor.setValue(0.0)

    def setModelData(self, editor, model, index):
        model.setData(index, str(editor.value()), Qt.EditRole)


class CollapsibleSection(QWidget):
    """A simple collapsible card: a clickable header that shows/hides its body."""

    def __init__(self, title="", parent=None, expanded=True):
        super().__init__(parent)
        self.setObjectName("accordion_card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.header = QToolButton()
        self.header.setObjectName("accordion_header")
        self.header.setText(title)
        self.header.setCheckable(True)
        self.header.setChecked(expanded)
        self.header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.header.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.header.setCursor(Qt.PointingHandCursor)
        self.header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.header.clicked.connect(self._on_toggle)
        outer.addWidget(self.header)

        self.body = QWidget()
        self.body.setObjectName("accordion_body")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(10, 8, 10, 10)
        self.body.setVisible(expanded)
        outer.addWidget(self.body)

    def _on_toggle(self, checked):
        self.body.setVisible(checked)
        self.header.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)

    def setTitle(self, title):
        self.header.setText(title)

    def addWidget(self, w):
        self.body_layout.addWidget(w)

    def addLayout(self, lay):
        self.body_layout.addLayout(lay)


# --- Main Dialog Class ---
class FricMapsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_lang = "EN"  # Default language (toggle available in the header)
        # Auto-detect QGIS dark mode so the plugin matches the host theme.
        self.dark_mode = self._detect_dark_theme()
        self.setWindowTitle(TRANSLATIONS[self.current_lang]["window_title"])
        # Preferred opening size, clamped to what the screen actually offers so
        # the dialog never opens larger than the available desktop area.
        _pref_w, _pref_h = 1240, 800
        _screen = QApplication.primaryScreen()
        if _screen is not None:
            _avail = _screen.availableGeometry()
            _pref_w = min(_pref_w, int(_avail.width() * 0.9))
            _pref_h = min(_pref_h, int(_avail.height() * 0.9))
        # A low floor keeps the dialog freely resizable on laptop screens; the
        # tab contents scroll rather than imposing a large minimum size.
        self.setMinimumSize(760, 520)
        self.resize(_pref_w, _pref_h)

        # Declare the window as a utility panel (Qt.Tool). On macOS this makes it
        # follow the active app and float over a FULLSCREEN QGIS in the SAME Space,
        # instead of opening on another desktop and bouncing between Spaces.
        self.setWindowFlags(
            Qt.Tool
            | Qt.WindowTitleHint
            | Qt.WindowSystemMenuHint
            | Qt.WindowCloseButtonHint
            | Qt.WindowMinMaxButtonsHint
        )

        # Set Window Icon
        icon_path = os.path.join(PLUGIN_ROOT, "icon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.temp_layer = None  # Keep reference to temporary layer for file import

        # Main Layout (Vertical: Splitter on top, Progress/Logos on bottom)
        self.main_layout = QVBoxLayout(self)

        # Splitter for resizable panels
        self.splitter = QSplitter(Qt.Horizontal)
        self.main_layout.addWidget(self.splitter, stretch=1)

        # --- Left Panel (Controls & Logs) ---
        self.left_panel = QWidget()
        self.left_layout = QVBoxLayout(self.left_panel)
        self.left_layout.setContentsMargins(0, 0, 0, 0)

        # Header with Language Switch
        # Header with Config Buttons and Language Switch
        self.header_layout = QHBoxLayout()

        self.btn_save_config = QPushButton()
        self.btn_load_config = QPushButton()

        self.header_layout.addWidget(self.btn_save_config)
        self.header_layout.addWidget(self.btn_load_config)

        self.header_layout.addStretch()

        # Theme toggle (auto-detected, but user can force light/dark)
        self.btn_theme = QPushButton()
        self.btn_theme.setFixedWidth(44)
        self.btn_theme.setToolTip("Toggle light / dark theme")
        self.btn_theme.clicked.connect(self.toggle_theme)
        self.header_layout.addWidget(self.btn_theme)

        self.btn_lang = QPushButton("EN 🇬🇧")
        self.btn_lang.setFixedWidth(60)
        self.btn_lang.clicked.connect(self.toggle_language)

        self.header_layout.addWidget(self.btn_lang)
        self.left_layout.addLayout(self.header_layout)

        # Connections for config
        self.btn_save_config.clicked.connect(self.save_config)
        self.btn_load_config.clicked.connect(self.load_config)

        # Tabs
        self.tabs = QTabWidget()
        self.left_layout.addWidget(self.tabs)

        # Tab 1: Parameters
        self.tab_params = QWidget()
        self.params_layout = QVBoxLayout(self.tab_params)

        # --- Group 1: Input Data ---
        self.grp_input = QGroupBox()
        self.layout_input = QFormLayout(self.grp_input)
        self.layout_input.setVerticalSpacing(9)
        self.layout_input.setHorizontalSpacing(12)

        # Source Type
        self.lbl_source_type = QLabel(TRANSLATIONS[self.current_lang]["lbl_source_type"])
        self.layout_input.addRow(self.lbl_source_type)

        self.source_type_layout = QHBoxLayout()
        self.rb_layer = QRadioButton(TRANSLATIONS[self.current_lang]["rb_layer"])
        self.rb_file = QRadioButton(TRANSLATIONS[self.current_lang]["rb_file"])
        self.rb_layer.setChecked(True)
        self.source_type_group = QButtonGroup()
        self.source_type_group.addButton(self.rb_layer)
        self.source_type_group.addButton(self.rb_file)
        self.source_type_layout.addWidget(self.rb_layer)
        self.source_type_layout.addWidget(self.rb_file)
        self.layout_input.addRow(self.source_type_layout)

        # Layer Selection
        self.lbl_epci = QLabel(TRANSLATIONS[self.current_lang]["lbl_epci"])
        self.epci_layer_cb = QgsMapLayerComboBox()
        self.epci_layer_cb.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.layout_input.addRow(self.lbl_epci, self.epci_layer_cb)

        # File Selection
        self.lbl_epci_file = QLabel(TRANSLATIONS[self.current_lang]["lbl_epci_file"])
        self.epci_file_widget = QgsFileWidget()
        self.epci_file_widget.setFilter("Vector files (*.shp *.gpkg *.geojson)")
        self.epci_file_widget.setStorageMode(QgsFileWidget.GetFile)
        self.layout_input.addRow(self.lbl_epci_file, self.epci_file_widget)

        self.lbl_epci_file.setVisible(False)
        self.epci_file_widget.setVisible(False)

        self.source_type_group.buttonClicked.connect(self.toggle_source_type)

        # Field Selection
        self.lbl_name_field = QLabel(TRANSLATIONS[self.current_lang]["lbl_name_field"])
        self.name_field_cb = QgsFieldComboBox()
        self.name_field_cb.setLayer(self.epci_layer_cb.currentLayer())
        self.epci_layer_cb.layerChanged.connect(self.name_field_cb.setLayer)
        self.layout_input.addRow(self.lbl_name_field, self.name_field_cb)

        self.epci_file_widget.fileChanged.connect(self.load_fields_from_file)

        # Area Name
        self.lbl_area_name = QLabel()
        self.area_name_cb = QComboBox(self.tab_params)
        self.area_name_cb.setEditable(True)
        self.area_name_cb.setInsertPolicy(QComboBox.InsertAtTop)
        self.area_name_cb.setPlaceholderText("Select or type area name...")
        self.layout_input.addRow(self.lbl_area_name, self.area_name_cb)

        # Base Data Directory
        self.lbl_base_dir = QLabel()
        self.base_dir_widget = QgsFileWidget(self.tab_params)
        self.base_dir_widget.setStorageMode(QgsFileWidget.GetDirectory)
        self.layout_input.addRow(self.lbl_base_dir, self.base_dir_widget)

        # Custom sources manager (opens a pop-up)
        self.btn_open_custom = QPushButton()
        self.btn_open_custom.clicked.connect(self.open_custom_sources_dialog)
        self.layout_input.addRow("", self.btn_open_custom)

        self.params_layout.addWidget(self.grp_input)

        # --- Group 2: Processing Parameters ---
        self.grp_params = QGroupBox()
        self.layout_params = QFormLayout(self.grp_params)

        # Buffer Distance
        self.lbl_buffer = QLabel()
        self.buffer_dist_sb = QDoubleSpinBox(self.tab_params)
        self.buffer_dist_sb.setRange(0, 100000)
        self.buffer_dist_sb.setValue(5000.0)
        self.buffer_dist_sb.setSuffix(" m")
        self.layout_params.addRow(self.lbl_buffer, self.buffer_dist_sb)

        # Resolution
        self.lbl_resolution = QLabel()
        self.resolution_sb = QDoubleSpinBox(self.tab_params)
        self.resolution_sb.setRange(0.1, 1000)
        self.resolution_sb.setValue(5.0)
        self.resolution_sb.setSuffix(" m")
        self.layout_params.addRow(self.lbl_resolution, self.resolution_sb)

        # Classification Table
        self.lbl_csv = QLabel()
        self.table_csv_widget = QgsFileWidget(self.tab_params)
        self.table_csv_widget.setFilter("CSV Files (*.csv)")
        self.table_csv_widget.setStorageMode(QgsFileWidget.GetFile)

        # Set default CSV path
        default_csv = os.path.join(PLUGIN_ROOT, "resources", "Table_Raster.csv")
        if os.path.exists(default_csv):
            self.table_csv_widget.setFilePath(default_csv)

        self.layout_params.addRow(self.lbl_csv, self.table_csv_widget)

        # Building Class Code
        self.lbl_build_code = QLabel()
        self.building_code_sb = QSpinBox(self.tab_params)
        self.building_code_sb.setRange(0, 9999)
        self.building_code_sb.setValue(29)
        self.layout_params.addRow(self.lbl_build_code, self.building_code_sb)

        # Vector Only Mode and Skip Vector checkboxes removed for Split Workflow

        self.params_layout.addWidget(self.grp_params)

        # --- Group 3: Output ---
        self.grp_output = QGroupBox()
        self.layout_output = QFormLayout(self.grp_output)

        # Output Directory
        self.lbl_output_dir = QLabel()
        self.output_dir_widget = QgsFileWidget(self.tab_params)
        self.output_dir_widget.setStorageMode(QgsFileWidget.GetDirectory)
        self.layout_output.addRow(self.lbl_output_dir, self.output_dir_widget)

        # Save Intermediate Vectors
        self.save_vectors_cb = QCheckBox(self.tab_params)
        self.save_vectors_cb.setChecked(True)
        self.layout_output.addRow("", self.save_vectors_cb)

        # Verify required datasets before running (can be disabled to force a run)
        self.verify_data_cb = QCheckBox(self.tab_params)
        self.verify_data_cb.setChecked(True)
        self.layout_output.addRow("", self.verify_data_cb)

        self.params_layout.addWidget(self.grp_output)

        self.params_layout.addStretch()  # Push button to bottom

        # Run Vector Button
        self.btn_run_vector = QPushButton()
        self.btn_run_vector.clicked.connect(lambda: self.run_process(only_vectors=True))
        self.params_layout.addWidget(self.btn_run_vector)

        # Wrap the (tall) Vector tab in a scroll area so the whole dialog can
        # be shrunk vertically without clipping the parameter groups.
        self.params_scroll = QScrollArea()
        self.params_scroll.setWidgetResizable(True)
        self.params_scroll.setFrameShape(QFrame.NoFrame)
        self.params_scroll.setWidget(self.tab_params)
        self.tabs.addTab(self.params_scroll, "")  # Title set in translate_ui

        # Tab 2: Weighting options - dynamic rules engine
        self.tab_weights = QWidget()
        self.weights_layout = QVBoxLayout(self.tab_weights)

        self.lbl_weights_help = QLabel()
        self.lbl_weights_help.setObjectName("lbl_custom_help")
        self.lbl_weights_help.setWordWrap(True)
        self.weights_layout.addWidget(self.lbl_weights_help)

        # Top bar: add a weighting rule
        wtop = QHBoxLayout()
        self.btn_add_weight = QPushButton()
        self.btn_add_weight.setObjectName("btn_run_vector")  # accent style
        self.btn_add_weight.clicked.connect(lambda: self.add_weight_rule())
        wtop.addWidget(self.btn_add_weight)
        wtop.addStretch()
        self.weights_layout.addLayout(wtop)

        # Scroll area hosting the dynamic weighting cards
        self.weights_scroll = QScrollArea()
        self.weights_scroll.setWidgetResizable(True)
        self.weights_scroll.setFrameShape(QFrame.NoFrame)
        self.weights_host = QWidget()
        self.weight_cards_layout = QVBoxLayout(self.weights_host)
        self.weight_cards_layout.setContentsMargins(0, 0, 0, 0)
        self.weight_cards_layout.setSpacing(10)
        self.weight_cards_layout.addStretch()  # cards are inserted before this stretch
        self.weights_scroll.setWidget(self.weights_host)
        self.weights_layout.addWidget(self.weights_scroll)

        self.weight_cards = []  # list of per-card widget dicts

        # Two default rules (slope + distance to buildings), matching legacy defaults.
        self.add_weight_rule(
            preset={
                "type": "slope",
                "bands": [
                    {"min": 0, "max": 30, "weight": 1},
                    {"min": 30, "max": 40, "weight": 10},
                    {"min": 40, "max": 90, "weight": 1000},
                ],
            }
        )
        self.add_weight_rule(
            preset={
                "type": "distance",
                "target": "__BUILDING__",
                "bands": [
                    {"min": 0, "max": 50, "weight": 2.5},
                    {"min": 50, "max": 100, "weight": 2.0},
                    {"min": 100, "max": 200, "weight": 1.5},
                ],
            }
        )

        # Run button at the BOTTOM of this tab: it launches rasterisation AND the
        # weighting rules above — so it belongs here, after everything is set.
        self.btn_run_raster = QPushButton()
        self.btn_run_raster.clicked.connect(lambda: self.run_process(skip_vectors=True))
        self.weights_layout.addWidget(self.btn_run_raster)

        self.tabs.addTab(self.tab_weights, "")

        # Custom sources: built as a pop-up dialog (opened from tab 1), not a tab.
        self._build_custom_sources_ui()

        # Tab 3: Configuration (CSV Editor)
        self.tab_config = QWidget()
        self.config_layout = QVBoxLayout(self.tab_config)

        # Main Content Layout (Buttons Left + Table Right)
        self.content_layout = QHBoxLayout()

        # Reorder Buttons (Left)
        self.reorder_layout = QVBoxLayout()
        self.reorder_layout.addStretch()
        self.btn_up = QPushButton("▲")
        self.btn_up.setFixedWidth(40)
        self.btn_up.setFixedHeight(60)
        self.btn_down = QPushButton("▼")
        self.btn_down.setFixedWidth(40)
        self.btn_down.setFixedHeight(60)
        self.reorder_layout.addWidget(self.btn_up)
        self.reorder_layout.addSpacing(20)
        self.reorder_layout.addWidget(self.btn_down)
        self.reorder_layout.addStretch()

        self.content_layout.addLayout(self.reorder_layout)

        # Table Widget
        self.table_widget = QTableWidget()
        self.content_layout.addWidget(self.table_widget)

        self.config_layout.addLayout(self.content_layout)

        # Bottom Buttons Layout
        self.config_buttons_layout = QHBoxLayout()

        self.btn_load_csv = QPushButton()
        self.btn_save_csv = QPushButton()
        self.btn_add_row = QPushButton()
        self.btn_remove_row = QPushButton()

        self.config_buttons_layout.addWidget(self.btn_load_csv)
        self.config_buttons_layout.addWidget(self.btn_save_csv)
        self.config_buttons_layout.addStretch()
        self.config_buttons_layout.addWidget(self.btn_add_row)
        self.config_buttons_layout.addWidget(self.btn_remove_row)

        self.config_layout.addLayout(self.config_buttons_layout)

        # NB: the "Run Rasterization" button lives at the bottom of the
        # Weighting tab (it launches rasterisation AND weighting).

        # Table Expansion
        self.table_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # Column delegates are assigned by HEADER NAME in load_csv_table()
        # (the CSV column order is not fixed), so nothing hardcoded here.
        self.table_widget.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        self.table_widget.cellDoubleClicked.connect(self.open_sql_builder)
        # Editing the raster table (e.g. a new CLASS_NAME) must refresh the
        # weighting target dropdowns so the new class becomes selectable.
        self.table_widget.itemChanged.connect(self._on_raster_table_changed)

        self.tabs.addTab(self.tab_config, "")  # Title set in translate_ui

        # Tab 3: Logs
        self.tab_logs = QWidget()
        self.logs_layout = QVBoxLayout(self.tab_logs)
        self.log_text = QTextEdit(self.tab_logs)
        self.log_text.setReadOnly(True)
        self.logs_layout.addWidget(self.log_text)
        self.tabs.addTab(self.tab_logs, "")  # Title set in translate_ui

        # Reorder tabs so Weighting comes AFTER Rasterisation:
        # 1-Vector, 2-Rasterisation, 3-Weighting, 4-Logs.
        self.tabs.tabBar().moveTab(1, 2)

        # Buttons (Keep in Left Panel)
        # Buttons moved to bottom

        # --- Right Panel (Info) ---
        self.right_panel = QWidget()
        self.right_layout = QVBoxLayout(self.right_panel)
        self.right_layout.setContentsMargins(0, 0, 0, 0)

        # No heading above the panel: its content is self-evidently informational,
        # so the browser takes the full height of the right column.
        self.info_browser = QTextBrowser()
        self.info_browser.setOpenExternalLinks(True)
        # Colours are applied theme-aware in apply_styles().

        self.right_layout.addWidget(self.info_browser)

        # Add panels to splitter
        self.splitter.addWidget(self.left_panel)
        self.splitter.addWidget(self.right_panel)
        self.splitter.setStretchFactor(0, 2)  # controls grow faster on resize
        self.splitter.setStretchFactor(1, 1)
        # Stretch factors alone left the info panel too narrow to read at start.
        # Give it an explicit initial width and a floor it cannot collapse below.
        self.right_panel.setMinimumWidth(300)
        self.splitter.setSizes([1180, 330])

        # --- Bottom Row (Full Width) ---
        # --- Bottom Section ---

        # Progress Bar (Full Width of Left Panel)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setMinimumHeight(30)
        self.left_layout.addWidget(self.progress_bar)

        # Bottom Buttons & Logos
        self.bottom_layout = QHBoxLayout()

        # Buttons (Cancel & Close)
        self.btn_cancel = QPushButton()
        self.btn_cancel.clicked.connect(self.cancel_process)
        self.btn_cancel.setEnabled(False)  # Disabled by default

        self.btn_close = QPushButton()
        self.btn_close.clicked.connect(self.close)

        self.bottom_layout.addWidget(self.btn_cancel)
        self.bottom_layout.addWidget(self.btn_close)

        self.bottom_layout.addStretch()
        self.bottom_layout.setContentsMargins(0, 20, 0, 20)  # Increase height to match logos

        self.left_layout.addLayout(self.bottom_layout)

        # Partner Logos (Moved to Right Panel)
        self.logos_layout = QGridLayout()
        self.logos_layout.setSpacing(5)
        self.logos_layout.setContentsMargins(0, 5, 0, 0)  # Top margin for spacing
        self.right_layout.addLayout(self.logos_layout)

        # Load Partner Logos
        self.load_partner_logos()

        # Connections
        # Swap the guidance panel whenever the user changes tab.
        self.tabs.currentChanged.connect(self.update_info_panel)
        self.epci_layer_cb.layerChanged.connect(self.on_layer_changed)
        self.name_field_cb.fieldChanged.connect(self.populate_area_names)
        self.btn_close.clicked.connect(self.close)

        # CSV Editor Connections
        # "Load CSV" ALWAYS opens a file chooser (None -> dialog).
        self.btn_load_csv.clicked.connect(lambda: self.load_csv_table(None))
        self.btn_save_csv.clicked.connect(self.save_csv_table)
        self.btn_add_row.clicked.connect(self.add_row)
        self.btn_remove_row.clicked.connect(self.remove_row)
        self.btn_up.clicked.connect(self.move_row_up)
        self.btn_down.clicked.connect(self.move_row_down)

        # Auto-load when file selected in params
        self.table_csv_widget.fileChanged.connect(self.load_csv_table)

        # Initial population & Translation
        self.on_layer_changed(self.epci_layer_cb.currentLayer())
        self.translate_ui()

        # Load default CSV
        default_csv = os.path.join(PLUGIN_ROOT, "resources", "Table_Raster.csv")
        self.load_csv_table(default_csv)

        # Now that the CSV class names are known, populate weighting target lists.
        self._refresh_weight_targets()

        # Apply Styles
        self.btn_run_vector.setObjectName("btn_run_vector")
        self.btn_run_raster.setObjectName("btn_run_raster")
        self.apply_styles()

    def toggle_language(self):
        if self.current_lang == "FR":
            self.current_lang = "EN"
            self.btn_lang.setText("FR 🇫🇷")
        else:
            self.current_lang = "FR"
            self.btn_lang.setText("EN 🇬🇧")
        self.translate_ui()

    def translate_ui(self):
        tr = TRANSLATIONS[self.current_lang]

        self.setWindowTitle(tr["window_title"])
        # Tab order (after moveTab): Vector, Rasterisation, Weighting, Logs.
        self.tabs.setTabText(0, tr["tab_vector"])
        self.tabs.setTabText(1, tr["tab_raster_new"])
        self.tabs.setTabText(2, tr["tab_weights"])
        self.tabs.setTabText(3, tr["tab_logs_new"])
        # Custom sources pop-up
        self.btn_open_custom.setText(tr["btn_open_custom"])
        self.custom_dialog.setWindowTitle(tr["custom_win_title"])
        self.btn_custom_close.setText(tr["btn_close"])

        # Weighting tab (dynamic rules)
        self.lbl_weights_help.setText(tr["weights_intro"])
        self.btn_add_weight.setText(tr["btn_add_weight"])
        self._translate_weight_cards()

        # Custom sources pop-up
        self.lbl_custom_help.setText(tr["custom_help"])
        self.grp_custom.setTitle(tr["grp_custom_list"])
        self.btn_add_source.setText(tr["btn_add_source"])
        self.btn_remove_source.setText(tr["btn_remove_source"])
        self.table_custom.setHorizontalHeaderLabels([tr[k] for k in self.CUSTOM_COLS])
        # Header tooltips to clarify what each column expects.
        header_tt = {
            0: "tt_key",
            1: "tt_label",
            2: "tt_type",
            3: "tt_source",
            4: "tt_buffer",
            5: "tt_filter",
        }
        for col, tt_key in header_tt.items():
            hitem = self.table_custom.horizontalHeaderItem(col)
            if hitem is not None:
                hitem.setToolTip(tr.get(tt_key, ""))
        # Re-translate per-row Type combos, preserving the selected index.
        for r in range(self.table_custom.rowCount()):
            type_w = self.table_custom.cellWidget(r, 2)
            if isinstance(type_w, QComboBox):
                idx = type_w.currentIndex()
                type_w.blockSignals(True)
                type_w.clear()
                type_w.addItems([tr["type_file"], tr["type_layer"]])
                type_w.setCurrentIndex(idx if idx >= 0 else 0)
                type_w.blockSignals(False)

        self.grp_input.setTitle(tr["grp_input"])
        self.grp_params.setTitle(tr["grp_params"])
        self.grp_output.setTitle(tr["grp_output"])

        self.lbl_epci.setText(tr["lbl_epci"])
        self.lbl_source_type.setText(tr["lbl_source_type"])
        self.rb_layer.setText(tr["rb_layer"])
        self.rb_file.setText(tr["rb_file"])
        self.lbl_epci_file.setText(tr["lbl_epci_file"])
        self.lbl_name_field.setText(tr["lbl_name_field"])
        self.lbl_area_name.setText(tr["lbl_area_name"])
        self.lbl_base_dir.setText(tr["lbl_base_dir"])
        self.lbl_output_dir.setText(tr["lbl_output_dir"])
        self.lbl_buffer.setText(tr["lbl_buffer"])
        self.lbl_resolution.setText(tr["lbl_resolution"])
        self.lbl_csv.setText(tr["lbl_csv"])
        self.lbl_build_code.setText(tr["lbl_build_code"])
        self.save_vectors_cb.setText(tr["chk_save_vectors"])
        self.verify_data_cb.setText(tr["chk_verify_data"])

        self.btn_run_vector.setText(tr["btn_run_vector"])
        self.btn_run_raster.setText(tr["btn_run_raster"])
        self.btn_close.setText(tr["btn_close"])
        self.btn_cancel.setText(tr["btn_cancel"])

        self.btn_save_config.setText(tr["btn_save_config"])
        self.btn_load_config.setText(tr["btn_load_config"])

        self._apply_raster_header_labels()
        self.btn_load_csv.setText(tr["btn_load_csv"])
        self.btn_save_csv.setText(tr["btn_save_csv"])
        self.btn_add_row.setText(tr["btn_add_row"])
        self.btn_remove_row.setText(tr["btn_remove_row"])

        # The info panel is context-sensitive: it is rebuilt from the active tab.
        self.update_info_panel()

    # Info-panel body shown for each tab, in tab-bar order (after moveTab):
    # Vector processing, Rasterization, Weighting, Logs.
    INFO_TAB_KEYS = (
        "info_tab_vector",
        "info_tab_raster",
        "info_tab_weights",
        "info_tab_logs",
    )

    def update_info_panel(self, index=None):
        """Render the context-sensitive guidance panel for the active tab.

        The header (plugin name and logo) is constant, while the body is swapped
        according to the tab currently in front, so the guidance always matches
        what the user is looking at. Colours are taken from the active theme
        palette, keeping the panel readable under both light and dark themes.

        Args:
            index: Tab index to render. Defaults to the current tab. The slot is
                connected to ``QTabWidget.currentChanged``, which supplies it.
        """
        tr = TRANSLATIONS[self.current_lang]
        c = self._theme_palette()

        if index is None:
            index = self.tabs.currentIndex()
        try:
            body_key = self.INFO_TAB_KEYS[int(index)]
        except (IndexError, TypeError, ValueError):
            body_key = self.INFO_TAB_KEYS[0]

        logo_path = os.path.join(PLUGIN_ROOT, "resources", "logo_info.png")
        # Forward slashes keep the <img> source valid on Windows as well.
        logo_path = logo_path.replace("\\", "/")

        fmt = {
            "logo_path": logo_path,
            "title": c["title"],
            "accent": c["accent"],
            "border": c["border"],
            "surface_alt": c["surface_alt"],
            "text_subtle": c["text_subtle"],
        }
        html = tr["info_header"].format(**fmt) + tr.get(body_key, "").format(**fmt)

        # Pin the typography on the document itself. Qt resolves the rich-text
        # font when the HTML is parsed, so relying on the widget stylesheet alone
        # would make the rendered font depend on when apply_styles() last ran -
        # which is what made the panel appear to change font on theme switch.
        self.info_browser.document().setDefaultStyleSheet(
            "body, p, li, td, h3 { font-family:%(font)s; }"
            " body { font-size:12pt; }"
            " h3 { font-size:16pt; }"
            " p { margin-top:8px; margin-bottom:8px; }"
            " li { margin-bottom:7px; }"
            " ol, ul { margin-top:4px; }" % {"font": c["font"]}
        )
        self.info_browser.setHtml(html)

    def load_partner_logos(self):
        """Load partner logos into the bottom layout."""
        # Clear existing items if any (though usually called once)
        while self.logos_layout.count():
            item = self.logos_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        resources_dir = os.path.join(PLUGIN_ROOT, "resources")
        if os.path.exists(resources_dir):
            # Only partner logos here. logo_info.png is the plugin's own logo,
            # already shown in the info-panel header, so it must be excluded.
            logo_files = [
                f
                for f in os.listdir(resources_dir)
                if f.lower().startswith("logo_")
                and f.lower() != "logo_info.png"
                and f.lower().endswith((".png", ".jpg", ".jpeg"))
            ]
            # Sort to ensure consistent order
            logo_files.sort()

            for i, logo_file in enumerate(logo_files):
                full_path = os.path.join(resources_dir, logo_file)

                # Create label for logo
                lbl_logo = QLabel()
                pixmap = QPixmap(full_path)

                # Scale pixmap to fit height (e.g., 45px for slightly larger logos)
                if not pixmap.isNull():
                    scaled_pixmap = pixmap.scaledToHeight(45, Qt.SmoothTransformation)
                    lbl_logo.setPixmap(scaled_pixmap)
                    lbl_logo.setToolTip(logo_file)

                    # Grid positioning: 2 columns
                    row = i // 2
                    col = i % 2
                    self.logos_layout.addWidget(lbl_logo, row, col, alignment=Qt.AlignCenter)

    # --- Custom Sources Tab ---
    # Source columns: 0=Key 1=Label 2=Type 3=File/Layer 4=Buffer 5=SQL filter
    CUSTOM_COLS = [
        "col_c_key",
        "col_c_label",
        "col_c_type",
        "col_c_source",
        "col_c_buffer",
        "col_c_filter",
    ]
    # Weighting columns: 0=target layer 1=min 2=max 3=weight
    WEIGHT_COLS = ["col_w_source", "col_min", "col_max", "col_weight"]

    def open_custom_sources_dialog(self):
        """Show the custom-sources pop-up, themed to match the main window."""
        try:
            self.custom_dialog.setStyleSheet(self.styleSheet())
        except Exception:
            pass
        self.custom_dialog.exec_()

    def _build_custom_sources_ui(self):
        """Build the custom-sources manager as a pop-up dialog (opened from tab 1)."""
        self.custom_dialog = QDialog(self)
        self.custom_dialog.setWindowTitle("Custom Data Sources")
        # Utility panel + modal to the plugin window only → stays in the same
        # macOS Space as the (fullscreen) QGIS, no desktop switching.
        self.custom_dialog.setWindowFlags(
            Qt.Tool | Qt.WindowTitleHint | Qt.WindowSystemMenuHint | Qt.WindowCloseButtonHint
        )
        self.custom_dialog.setWindowModality(Qt.WindowModal)
        self.custom_dialog.resize(1020, 660)
        icon_path = os.path.join(PLUGIN_ROOT, "icon.png")
        if os.path.exists(icon_path):
            self.custom_dialog.setWindowIcon(QIcon(icon_path))
        self.custom_layout = QVBoxLayout(self.custom_dialog)

        # Help text
        self.lbl_custom_help = QLabel()
        self.lbl_custom_help.setObjectName("lbl_custom_help")
        self.lbl_custom_help.setWordWrap(True)
        self.custom_layout.addWidget(self.lbl_custom_help)

        # Group 1: list of custom sources
        self.grp_custom = QGroupBox()
        self.grp_custom_v = QVBoxLayout(self.grp_custom)

        self.table_custom = QTableWidget(0, len(self.CUSTOM_COLS))
        hh = self.table_custom.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setStretchLastSection(True)
        self.table_custom.setColumnWidth(0, 150)  # key
        self.table_custom.setColumnWidth(1, 150)  # label
        self.table_custom.setColumnWidth(2, 110)  # type
        self.table_custom.setColumnWidth(3, 260)  # file/layer
        self.table_custom.setColumnWidth(4, 90)  # buffer
        self.table_custom.setSelectionBehavior(QTableWidget.SelectRows)
        self.table_custom.setSelectionMode(QTableWidget.SingleSelection)
        self.table_custom.verticalHeader().setDefaultSectionSize(36)
        self.table_custom.verticalHeader().setVisible(False)
        self.grp_custom_v.addWidget(self.table_custom)

        self.custom_btns = QHBoxLayout()
        self.btn_add_source = QPushButton()
        self.btn_remove_source = QPushButton()
        self.custom_btns.addStretch()
        self.custom_btns.addWidget(self.btn_add_source)
        self.custom_btns.addWidget(self.btn_remove_source)
        self.grp_custom_v.addLayout(self.custom_btns)
        self.custom_layout.addWidget(self.grp_custom)

        # NB: the distance/light weighting of custom sources is configured in the
        # centralised Weighting tab (card "Distance / light"), NOT here.

        self.custom_layout.addStretch()

        # Close button for the pop-up
        close_row = QHBoxLayout()
        close_row.addStretch()
        self.btn_custom_close = QPushButton()
        self.btn_custom_close.setObjectName("btn_run_vector")  # accent style
        self.btn_custom_close.clicked.connect(self.custom_dialog.accept)
        close_row.addWidget(self.btn_custom_close)
        self.custom_layout.addLayout(close_row)

        # Connections
        self.btn_add_source.clicked.connect(self.add_custom_source)
        self.btn_remove_source.clicked.connect(self.remove_custom_source)
        # Refresh the weighting target dropdowns (in the Weighting tab) whenever a
        # source key is edited / added / removed.
        self.table_custom.itemChanged.connect(self._refresh_weight_targets)
        # Double-click the SQL filter column → open the real expression console.
        self.table_custom.cellDoubleClicked.connect(self._open_custom_sql_console)

    def _resolve_custom_row_layer(self, row):
        """Build a QgsVectorLayer for a custom-source row (file or project layer)."""
        type_w = self.table_custom.cellWidget(row, 2)
        source_cell = self.table_custom.cellWidget(row, 3)
        if source_cell is None:
            return None
        is_layer = bool(type_w) and type_w.currentIndex() == 1
        if is_layer:
            return source_cell.layer_combo.currentLayer()
        path = source_cell.file_widget.filePath().strip()
        if path and os.path.exists(path):
            lyr = QgsVectorLayer(path, os.path.basename(path), "ogr")
            return lyr if lyr.isValid() else None
        return None

    def _open_custom_sql_console(self, row, column):
        """Open the QGIS expression builder on the source's real fields (SQL_FILTER col)."""
        if column != 5:
            return
        layer = self._resolve_custom_row_layer(row)
        item = self.table_custom.item(row, 5)
        current = item.text() if item else ""
        dlg = (
            QgsExpressionBuilderDialog(layer, current, self.custom_dialog)
            if layer is not None
            else QgsExpressionBuilderDialog(None, current, self.custom_dialog)
        )
        dlg.setWindowFlags(dlg.windowFlags() | Qt.Tool)
        dlg.setWindowTitle("SQL filter - " + (layer.name() if layer is not None else "source"))
        dlg.expressionBuilder().setExpressionText(current)
        if dlg.exec_():
            self.table_custom.setItem(
                row, 5, QTableWidgetItem(dlg.expressionBuilder().expressionText())
            )

    def get_custom_source_keys(self):
        """Return the list of custom source keys currently declared (upper-case)."""
        keys = []
        if not hasattr(self, "table_custom"):
            return keys
        for r in range(self.table_custom.rowCount()):
            it = self.table_custom.item(r, 0)
            if it and it.text().strip():
                keys.append(it.text().strip().upper())
        return keys

    def _make_source_cell(self, preset=None):
        """Build the composite File/Layer widget for the 'Source' column.

        Contains a QgsFileWidget (with a built-in "..." browse button) and a
        QgsMapLayerComboBox; only one is visible depending on the row Type.
        """
        preset = preset or {}
        container = QWidget()
        lay = QHBoxLayout(container)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        file_w = QgsFileWidget()
        file_w.setStorageMode(QgsFileWidget.GetFile)
        file_w.setFilter("Couches vecteur (*.shp *.gpkg *.geojson *.kml *.tab *.mif)")
        if preset.get("path"):
            file_w.setFilePath(str(preset.get("path")))

        layer_w = QgsMapLayerComboBox()
        layer_w.setFilters(QgsMapLayerProxyModel.VectorLayer)
        layer_w.setAllowEmptyLayer(True)
        # Restore a previously chosen project layer (by id then name).
        ident = preset.get("layer_id", "") or preset.get("token", "")
        if ident:
            lyr = QgsProject.instance().mapLayer(ident)
            if lyr is None:
                for cand in QgsProject.instance().mapLayers().values():
                    if cand.name().strip().lower() == str(ident).strip().lower():
                        lyr = cand
                        break
            if lyr is not None:
                layer_w.setLayer(lyr)

        lay.addWidget(file_w)
        lay.addWidget(layer_w)
        # Store references for later access.
        container.file_widget = file_w
        container.layer_combo = layer_w
        return container

    def _apply_source_type(self, type_combo, source_container):
        """Show the file picker or the layer combo depending on the Type value.

        Index 0 = File, index 1 = QGIS Layer (language-agnostic).
        """
        is_layer = type_combo.currentIndex() == 1
        source_container.file_widget.setVisible(not is_layer)
        source_container.layer_combo.setVisible(is_layer)

    def add_custom_source(self, *_, preset=None):
        """Add a custom-source row (optionally pre-filled from a config dict)."""
        tr = TRANSLATIONS[self.current_lang]
        preset = preset or {}
        r = self.table_custom.rowCount()
        self.table_custom.insertRow(r)

        self.table_custom.setItem(
            r, 0, QTableWidgetItem(str(preset.get("source_key", "NOUVELLE_SOURCE")).upper())
        )
        self.table_custom.setItem(r, 1, QTableWidgetItem(str(preset.get("label", ""))))

        # Type combo (File / QGIS Layer)
        type_cb = QComboBox()
        type_cb.addItems([tr["type_file"], tr["type_layer"]])
        is_layer_mode = str(preset.get("detection_mode", "path")).lower() == "layer"
        type_cb.setCurrentIndex(1 if is_layer_mode else 0)
        self.table_custom.setCellWidget(r, 2, type_cb)

        # Source cell (file picker + layer combo)
        source_cell = self._make_source_cell(preset)
        self.table_custom.setCellWidget(r, 3, source_cell)
        type_cb.currentIndexChanged.connect(
            lambda _=None, tc=type_cb, sc=source_cell: self._apply_source_type(tc, sc)
        )
        self._apply_source_type(type_cb, source_cell)

        # Buffer
        buf = QDoubleSpinBox()
        buf.setRange(0, 100000)
        buf.setDecimals(1)
        buf.setSuffix(" m")
        try:
            buf.setValue(float(preset.get("buffer_m", 0.0)))
        except (TypeError, ValueError):
            buf.setValue(0.0)
        self.table_custom.setCellWidget(r, 4, buf)

        # SQL filter
        self.table_custom.setItem(r, 5, QTableWidgetItem(str(preset.get("field_filter", ""))))

        self.table_custom.selectRow(r)
        self._refresh_weight_targets()

    def remove_custom_source(self):
        """Remove the selected custom source."""
        r = self.table_custom.currentRow()
        if r < 0:
            return
        self.table_custom.removeRow(r)
        self._refresh_weight_targets()

    # ===================== Dynamic weighting rules =====================
    def _weight_target_entries(self):
        """Return [(label, value)] targets for a distance rule.

        Value is a class name (CLASS_NAME), a custom SOURCE key, or the special
        "__BUILDING__" token (resolved to the Building Class Code at run time).
        """
        tr = TRANSLATIONS[self.current_lang]
        entries = [(tr["target_building"], "__BUILDING__")]
        seen = {"__BUILDING__"}
        for k in self.get_custom_source_keys():
            if k not in seen:
                entries.append(("⊕ " + k, k))
                seen.add(k)
        if hasattr(self, "table_widget"):
            name_col = self._raster_col_index("CLASS_NAME")
            if name_col >= 0:
                for r in range(self.table_widget.rowCount()):
                    it = self.table_widget.item(r, name_col)
                    if it and it.text().strip():
                        val = it.text().strip().upper()
                        if val not in seen:
                            entries.append((val, val))
                            seen.add(val)
        return entries

    def _populate_card_targets(self, rec, selected_value=""):
        cb = rec["target_cb"]
        cb.blockSignals(True)
        cb.clear()
        for label, value in self._weight_target_entries():
            cb.addItem(label, value)
        sel = str(selected_value).strip().upper()
        if sel:
            idx = cb.findData(sel)
            if idx < 0:
                cb.addItem(sel, sel)
                idx = cb.count() - 1
            cb.setCurrentIndex(idx)
        cb.blockSignals(False)

    def _refresh_weight_targets(self, *_):
        for rec in getattr(self, "weight_cards", []):
            self._populate_card_targets(rec, rec["target_cb"].currentData() or "")

    def _on_weight_type_changed(self, rec):
        is_slope = rec["type_cb"].currentIndex() == 0
        rec["lbl_target"].setVisible(not is_slope)
        rec["target_cb"].setVisible(not is_slope)
        self._update_weight_card_header(rec)

    def _update_weight_card_header(self, rec):
        tr = TRANSLATIONS[self.current_lang]
        if rec["type_cb"].currentIndex() == 0:
            title = "⛰️  " + tr["wtype_slope"]
        else:
            title = "🎯  " + tr["wtype_distance"] + " · " + (rec["target_cb"].currentText() or "—")
        if not rec["enable_cb"].isChecked():
            title += "   (off)"
        rec["card"].setTitle(title)

    def add_weight_rule(self, *_, preset=None):
        """Add a weighting-rule card (slope or distance-to-target)."""
        tr = TRANSLATIONS[self.current_lang]
        preset = preset or {}
        # Parent the card to the host from the start (never a top-level window),
        # and freeze repaints during construction. This avoids the macOS
        # window-manager "flash / Space switch" when inserting composite widgets.
        self.weights_host.setUpdatesEnabled(False)
        card = CollapsibleSection("", parent=self.weights_host, expanded=True)

        ctrl = QHBoxLayout()
        type_cb = QComboBox()
        type_cb.addItems([tr["wtype_slope"], tr["wtype_distance"]])
        type_cb.setCurrentIndex(0 if str(preset.get("type", "distance")).lower() == "slope" else 1)
        lbl_target = QLabel(tr["lbl_target"])
        target_cb = QComboBox()
        target_cb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        enable_cb = QCheckBox(tr["enable_rule"])
        enable_cb.setChecked(preset.get("enabled", True) is not False)
        btn_del = QToolButton()
        btn_del.setText("🗑")
        btn_del.setToolTip(tr["btn_remove_rule"])
        ctrl.addWidget(type_cb)
        ctrl.addSpacing(6)
        ctrl.addWidget(lbl_target)
        ctrl.addWidget(target_cb, 1)
        ctrl.addSpacing(6)
        ctrl.addWidget(enable_cb)
        ctrl.addWidget(btn_del)
        card.addLayout(ctrl)

        table = QTableWidget(0, 3)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.verticalHeader().setVisible(False)
        table.setItemDelegate(FloatDelegate(table))
        table.setHorizontalHeaderLabels([tr["col_min"], tr["col_max"], tr["col_weight"]])
        card.addWidget(table)
        bt = QHBoxLayout()
        bt.addStretch()
        b_add = QPushButton(tr["btn_add_row"])
        b_rem = QPushButton(tr["btn_remove_row"])
        bt.addWidget(b_add)
        bt.addWidget(b_rem)
        card.addLayout(bt)

        rec = {
            "card": card,
            "type_cb": type_cb,
            "target_cb": target_cb,
            "lbl_target": lbl_target,
            "enable_cb": enable_cb,
            "table": table,
            "b_add": b_add,
            "b_rem": b_rem,
            "btn_del": btn_del,
        }

        b_add.clicked.connect(lambda: table.insertRow(table.rowCount()))
        b_rem.clicked.connect(
            lambda: table.removeRow(table.currentRow()) if table.currentRow() >= 0 else None
        )
        btn_del.clicked.connect(lambda: self.remove_weight_rule(rec))
        type_cb.currentIndexChanged.connect(lambda _=None, rc=rec: self._on_weight_type_changed(rc))
        target_cb.currentIndexChanged.connect(
            lambda _=None, rc=rec: self._update_weight_card_header(rc)
        )
        enable_cb.toggled.connect(lambda _=None, rc=rec: self._update_weight_card_header(rc))

        self._populate_card_targets(rec, preset.get("target", ""))
        for band in preset.get("bands", []):
            r = table.rowCount()
            table.insertRow(r)
            table.setItem(r, 0, QTableWidgetItem(str(band.get("min", 0))))
            table.setItem(r, 1, QTableWidgetItem(str(band.get("max", 0))))
            table.setItem(r, 2, QTableWidgetItem(str(band.get("weight", 1.0))))

        self._on_weight_type_changed(rec)

        self.weight_cards_layout.insertWidget(self.weight_cards_layout.count() - 1, card)
        self.weight_cards.append(rec)
        # Re-enable repaints now that the whole card subtree is parented & in place.
        self.weights_host.setUpdatesEnabled(True)

    def remove_weight_rule(self, rec):
        try:
            self.weight_cards.remove(rec)
        except ValueError:
            pass
        rec["card"].setParent(None)
        rec["card"].deleteLater()

    def _card_read_bands(self, table):
        bands = []
        for i in range(table.rowCount()):
            it0 = table.item(i, 0)
            it1 = table.item(i, 1)
            it2 = table.item(i, 2)
            try:
                bands.append(
                    {
                        "min": float(it0.text()) if it0 and it0.text() else 0.0,
                        "max": float(it1.text()) if it1 and it1.text() else 0.0,
                        "weight": float(it2.text()) if it2 and it2.text() else 1.0,
                    }
                )
            except ValueError:
                continue
        return bands

    def collect_weighting_rules(self):
        """Serialise the weighting cards into a list of rule dicts."""
        rules = []
        for rec in getattr(self, "weight_cards", []):
            bands = self._card_read_bands(rec["table"])
            if not bands:
                continue
            is_slope = rec["type_cb"].currentIndex() == 0
            rule = {
                "type": "slope" if is_slope else "distance",
                "enabled": rec["enable_cb"].isChecked(),
                "bands": bands,
            }
            if not is_slope:
                rule["target"] = rec["target_cb"].currentData() or rec["target_cb"].currentText()
            rules.append(rule)
        return rules

    def _translate_weight_cards(self):
        tr = TRANSLATIONS[self.current_lang]
        for rec in getattr(self, "weight_cards", []):
            cur = rec["type_cb"].currentIndex()
            rec["type_cb"].blockSignals(True)
            rec["type_cb"].clear()
            rec["type_cb"].addItems([tr["wtype_slope"], tr["wtype_distance"]])
            rec["type_cb"].setCurrentIndex(cur if cur >= 0 else 0)
            rec["type_cb"].blockSignals(False)
            rec["lbl_target"].setText(tr["lbl_target"])
            rec["enable_cb"].setText(tr["enable_rule"])
            rec["btn_del"].setToolTip(tr["btn_remove_rule"])
            rec["b_add"].setText(tr["btn_add_row"])
            rec["b_rem"].setText(tr["btn_remove_row"])
            rec["table"].setHorizontalHeaderLabels([tr["col_min"], tr["col_max"], tr["col_weight"]])
            self._populate_card_targets(rec, rec["target_cb"].currentData() or "")
            self._update_weight_card_header(rec)

    def collect_custom_sources(self):
        """Serialise the custom sources tab into a list of definition dicts.

        Weighting is NOT set here — it is configured centrally in the
        Weighting tab (distance rules targeting the source key).
        """
        out = []
        for r in range(self.table_custom.rowCount()):
            key_item = self.table_custom.item(r, 0)
            key = key_item.text().strip().upper() if key_item else ""
            if not key:
                continue
            label_item = self.table_custom.item(r, 1)
            type_w = self.table_custom.cellWidget(r, 2)
            source_cell = self.table_custom.cellWidget(r, 3)
            buf_w = self.table_custom.cellWidget(r, 4)
            filter_item = self.table_custom.item(r, 5)

            is_layer = bool(type_w) and type_w.currentIndex() == 1

            path_val = ""
            layer_id = ""
            layer_name = ""
            if source_cell is not None:
                if is_layer:
                    lyr = source_cell.layer_combo.currentLayer()
                    if lyr is not None:
                        layer_id = lyr.id()
                        layer_name = lyr.name()
                else:
                    path_val = source_cell.file_widget.filePath().strip()

            out.append(
                {
                    "source_key": key,
                    "label": label_item.text() if label_item else key,
                    "enabled": True,
                    "detection_mode": "layer" if is_layer else "path",
                    "token": layer_name,  # name fallback for cross-session layer match
                    "path": path_val,
                    "layer_id": layer_id,
                    "buffer_m": buf_w.value() if buf_w else 0.0,
                    "dissolve": False,
                    "field_filter": filter_item.text().strip() if filter_item else "",
                    "weighting_enabled": False,
                    "weighting_bands": [],
                    "required": False,
                }
            )
        return out

    def _on_raster_table_changed(self, *_):
        """Refresh weighting targets when the raster table is edited (skip bulk load)."""
        if getattr(self, "_loading_raster", False):
            return
        if hasattr(self, "weight_cards"):
            self._refresh_weight_targets()

    def on_layer_changed(self, layer):
        """Update field combo box when layer changes."""
        self.name_field_cb.setLayer(layer)
        self.populate_area_names()

    def toggle_source_type(self):
        """Toggle visibility of layer/file widgets based on radio button state."""
        is_file = self.rb_file.isChecked()

        self.lbl_epci.setVisible(not is_file)
        self.epci_layer_cb.setVisible(not is_file)

        self.lbl_epci_file.setVisible(is_file)
        self.epci_file_widget.setVisible(is_file)

        # Trigger field update
        if is_file:
            self.load_fields_from_file(self.epci_file_widget.filePath())
        else:
            self.name_field_cb.setLayer(self.epci_layer_cb.currentLayer())
            self.populate_area_names()  # Changed from update_area_names to populate_area_names to match existing method name

    def load_fields_from_file(self, file_path):
        """Load fields from the selected file into the combo box."""
        if not file_path or not os.path.exists(file_path):
            return

        try:
            layer = QgsVectorLayer(file_path, "temp", "ogr")
            if layer.isValid():
                self.name_field_cb.setLayer(layer)
                self.temp_layer = layer  # Keep reference
                self.populate_area_names()
            else:
                QMessageBox.warning(self, "Error", f"Invalid vector file: {file_path}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error loading file: {str(e)}")

    def populate_area_names(self):
        """Update the Area Name combo box based on the selected layer and field."""
        layer = self.name_field_cb.layer()
        field_name = self.name_field_cb.currentField()

        self.area_name_cb.clear()

        if not layer or not layer.isValid() or not field_name:
            return

        try:
            # Get unique values from the field
            idx = layer.fields().indexOf(field_name)
            if idx != -1:
                values = layer.uniqueValues(idx)
                str_values = sorted([str(v) for v in values if v is not None])
                self.area_name_cb.addItems(str_values)
        except Exception as e:
            # Assuming log_message exists and takes level argument, otherwise remove level
            self.log_message(f"⚠️ Error loading area names: {e}")  # Removed level=Qgis.Warning

    def log_message(self, message):
        self.log_text.append(message)
        # Auto-scroll
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())

    def update_progress(self, percent):
        self.progress_bar.setValue(int(percent))

    # --- CSV Editor Methods ---
    def load_csv_table(self, file_path=None):
        """Load CSV content into the table widget."""
        if not file_path:
            # Prompt user to select file
            file_path, _ = QFileDialog.getOpenFileName(
                self, "Open a CSV table", "", "CSV Files (*.csv)"
            )
            if not file_path:
                return
            # Update the file widget WITHOUT re-triggering a second load.
            try:
                self.table_csv_widget.blockSignals(True)
                self.table_csv_widget.setFilePath(file_path)
            finally:
                self.table_csv_widget.blockSignals(False)

        if not file_path or not os.path.exists(file_path):
            return

        try:
            import csv

            with open(file_path, "r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f, delimiter=";"))

            if not rows:
                return

            # Parse Header (strip a possible BOM on the first cell)
            # Legacy column names are mapped onto the canonical schema, so a
            # table authored with an earlier version still loads; it is written
            # back with canonical names on the next save.
            header = canonical_header(rows[0])
            self.table_widget.setColumnCount(len(header))
            self.table_widget.setHorizontalHeaderLabels(header)
            # Keep the canonical CSV names; the visible labels are translated.
            self._raster_col_keys = list(header)
            self._apply_raster_header_labels()
            self.table_widget.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

            # Set Delegates based on column names
            for col in range(len(header)):
                col_name = header[col]
                if col_name == "SOURCE":
                    self.table_widget.setItemDelegateForColumn(
                        col,
                        SourceDelegate(self.table_widget, key_provider=self.get_custom_source_keys),
                    )
                elif col_name == "CLASS_NAME":
                    self.table_widget.setItemDelegateForColumn(
                        col, UppercaseDelegate(self.table_widget)
                    )
                elif col_name == "FRICTION_VALUE":
                    self.table_widget.setItemDelegateForColumn(
                        col, IntegerDelegate(self.table_widget)
                    )
                # SQL_FILTER handled by double click
                # COMPILATION_ORDER is read-only logic

            # Parse Data (csv.reader already handles quoting/escaping cleanly)
            self._loading_raster = True  # avoid a target refresh per cell
            self.table_widget.setRowCount(0)
            for row_data in rows[1:]:
                if not row_data or all(not c.strip() for c in row_data):
                    continue
                row_idx = self.table_widget.rowCount()
                self.table_widget.insertRow(row_idx)
                for col_idx, data in enumerate(row_data):
                    if col_idx >= self.table_widget.columnCount():
                        break
                    self.table_widget.setItem(row_idx, col_idx, QTableWidgetItem(data))

            self.update_compilation_order()
            self._loading_raster = False
            # Weighting target dropdowns depend on the class names → refresh them.
            if hasattr(self, "weight_cards"):
                self._refresh_weight_targets()
        except Exception as e:
            self._loading_raster = False
            QMessageBox.warning(self, "Error", f"Failed to load CSV: {str(e)}")

    def save_csv_table(self):
        """Save table content to CSV."""
        # Always prompt for save location (Save As)
        csv_path, _ = QFileDialog.getSaveFileName(
            self, "Save the CSV table", "", "CSV Files (*.csv)"
        )

        if not csv_path:
            return

        if not csv_path.lower().endswith(".csv"):
            csv_path += ".csv"

        # Update the file widget to reflect the saved file
        self.table_csv_widget.setFilePath(csv_path)

        try:
            import csv

            with open(csv_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f, delimiter=";", quoting=csv.QUOTE_MINIMAL)
                # Header
                # Write canonical CSV names, never the translated labels.
                headers = [self._col_key(c) for c in range(self.table_widget.columnCount())]
                writer.writerow(headers)
                # Data — csv.writer quotes/escapes fields containing ; or " safely
                for row in range(self.table_widget.rowCount()):
                    row_data = []
                    for col in range(self.table_widget.columnCount()):
                        item = self.table_widget.item(row, col)
                        row_data.append(item.text() if item else "")
                    writer.writerow(row_data)

            QMessageBox.information(
                self, "Success", TRANSLATIONS[self.current_lang]["msg_save_success"]
            )
            # Update the widget in params tab
            self.table_csv_widget.setFilePath(csv_path)

        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save CSV: {str(e)}")

    def add_row(self):
        """Add a new empty row to the table."""
        row_idx = self.table_widget.rowCount()
        self.table_widget.insertRow(row_idx)
        self.update_compilation_order()  # give the new row a valid COMPILATION_ORDER

    def remove_row(self):
        """Remove the selected row."""
        current_row = self.table_widget.currentRow()
        if current_row >= 0:
            self.table_widget.removeRow(current_row)
            self.update_compilation_order()
            self._on_raster_table_changed()

    def move_row_up(self):
        row = self.table_widget.currentRow()
        if row > 0:
            self.swap_rows(row, row - 1)
            self.table_widget.selectRow(row - 1)
            self.update_compilation_order()

    def move_row_down(self):
        row = self.table_widget.currentRow()
        if row < self.table_widget.rowCount() - 1 and row != -1:
            self.swap_rows(row, row + 1)
            self.table_widget.selectRow(row + 1)
            self.update_compilation_order()

    def swap_rows(self, row1, row2):
        for col in range(self.table_widget.columnCount()):
            item1 = self.table_widget.takeItem(row1, col)
            item2 = self.table_widget.takeItem(row2, col)
            self.table_widget.setItem(row2, col, item1)
            self.table_widget.setItem(row1, col, item2)

    def _col_key(self, col):
        """Canonical CSV name of a raster-table column.

        The visible header is translated, so it must never be used to identify
        a column. The canonical key is stored on the header item under
        ``Qt.UserRole`` when the table is built; the text is only a fallback
        for tables created before the header was translated.
        """
        item = self.table_widget.horizontalHeaderItem(col)
        if item is None:
            return ""
        key = item.data(Qt.UserRole)
        return str(key) if key else item.text()

    def _raster_col_index(self, key):
        """Index of the raster-table column carrying ``key``, or -1."""
        target = str(key).strip().upper()
        for c in range(self.table_widget.columnCount()):
            if self._col_key(c).strip().upper() == target:
                return c
        return -1

    def _apply_raster_header_labels(self):
        """Display the canonical column names, stored on the header items.

        Column names are part of the file format, so they are shown verbatim and
        never translated: a classification table must read identically whatever
        the interface language. The name is also kept under ``Qt.UserRole`` so
        that column lookups never depend on the displayed text.
        """
        if not hasattr(self, "table_widget"):
            return
        keys = getattr(self, "_raster_col_keys", [])
        for col in range(self.table_widget.columnCount()):
            key = keys[col] if col < len(keys) else ""
            item = self.table_widget.horizontalHeaderItem(col)
            if item is None:
                item = QTableWidgetItem()
                self.table_widget.setHorizontalHeaderItem(col, item)
            item.setData(Qt.UserRole, key)
            item.setText(key)

    def update_compilation_order(self):
        """Update the COMPILATION_ORDER column based on row index."""
        col_idx = self._raster_col_index("COMPILATION_ORDER")

        if col_idx == -1:
            return

        # Avoid a target-refresh burst while renumbering (guard restored after).
        prev = getattr(self, "_loading_raster", False)
        self._loading_raster = True
        for row in range(self.table_widget.rowCount()):
            item = QTableWidgetItem(str(row + 1))
            item.setFlags(item.flags() ^ Qt.ItemIsEditable)  # Make read-only
            self.table_widget.setItem(row, col_idx, item)
        self._loading_raster = prev

    # Processed-output filename patterns per built-in SOURCE (glob in output_dir).
    SOURCE_OUTPUT_PATTERNS = {
        "OCS": "OCS_GE_*.gpkg",
        "VEGETATION": "Vegetation_*.gpkg",
        "HEDGES": "Hedges_*.gpkg",
        "RPG": "RPG_*.gpkg",
        "HYDRO": "Hydro_*.gpkg",
        "TECH_INFRA": "Technical_infrastructures_*.gpkg",
        "LTI": "ILT_*.gpkg",
        "SOLAR_FENCES": "Solar_Fences_*.gpkg",
        "BUILT_AREA": "dense_built_zones_*.gpkg",
    }

    def _processed_glob(self, output_dir, pattern):
        """Find a processed vector layer produced by a previous run.

        Intermediate products now live in ``output_dir/intermediate``; the root
        is kept as a fallback so that output folders produced by earlier
        versions still resolve.
        """
        import glob

        for folder in (os.path.join(output_dir, "intermediate"), output_dir):
            matches = glob.glob(os.path.join(folder, pattern))
            if matches:
                return matches
        return []

    def _reference_layer_for_source(self, source_name):
        """Return the layer whose fields should populate the SQL builder for a
        given SOURCE — the actual data being filtered, not a random project layer.
        """

        src = (source_name or "").strip().upper()
        if not src:
            return self.epci_layer_cb.currentLayer()
        output_dir = self.output_dir_widget.filePath()

        # 1. Custom source → its declared file or project layer.
        try:
            for cfg in self.collect_custom_sources():
                if cfg.get("source_key", "").upper() != src:
                    continue
                if cfg.get("detection_mode") == "layer":
                    ident = cfg.get("layer_id", "") or cfg.get("token", "")
                    lyr = QgsProject.instance().mapLayer(ident)
                    if lyr is None:
                        for c in QgsProject.instance().mapLayers().values():
                            if c.name().strip().lower() == cfg.get("token", "").strip().lower():
                                lyr = c
                                break
                    if lyr is not None:
                        return lyr
                p = cfg.get("path", "")
                if p and os.path.exists(p):
                    lyr = QgsVectorLayer(p, source_name, "ogr")
                    if lyr.isValid():
                        return lyr
                # custom processed output fallback
                if output_dir:
                    m = self._processed_glob(output_dir, f"Custom_*{src.lower()}*_*.gpkg")
                    if m:
                        lyr = QgsVectorLayer(m[0], source_name, "ogr")
                        if lyr.isValid():
                            return lyr
                break
        except Exception:
            pass

        # 2. Built-in → processed output file in the output directory.
        if output_dir and src in self.SOURCE_OUTPUT_PATTERNS:
            m = self._processed_glob(output_dir, self.SOURCE_OUTPUT_PATTERNS[src])
            if m:
                lyr = QgsVectorLayer(m[0], source_name, "ogr")
                if lyr.isValid():
                    return lyr

        # 3. Project layer with an EXACT name match (no loose substring).
        for lyr in QgsProject.instance().mapLayers().values():
            if lyr.name().strip().upper() == src:
                return lyr

        # 4. Fallback: study-area layer (fields won't match, but avoids a crash).
        return self.epci_layer_cb.currentLayer()

    def open_sql_builder(self, row, column):
        """Open SQL Builder for SQL_FILTER column."""
        if not self.table_widget.horizontalHeaderItem(column):
            return

        if self._col_key(column).strip().upper() == "SQL_FILTER":
            # 1. Identify Source — find the SOURCE column by header name
            #    (column order in the CSV is not fixed).
            source_col = self._raster_col_index("SOURCE")
            source_item = self.table_widget.item(row, source_col) if source_col >= 0 else None
            source_name = source_item.text() if source_item else ""

            # Resolve the RIGHT reference layer for this SOURCE (processed output
            # for built-ins, declared file/layer for custom sources).
            layer = self._reference_layer_for_source(source_name)

            # Open Dialog (parented to this panel + Tool flag → same macOS Space)
            filtre_item = self.table_widget.item(row, column)
            current_expr = filtre_item.text() if filtre_item else ""
            dlg = QgsExpressionBuilderDialog(layer, current_expr, self)
            dlg.setWindowFlags(dlg.windowFlags() | Qt.Tool)
            dlg.expressionBuilder().setExpressionText(current_expr)

            if dlg.exec_():
                new_expr = dlg.expressionBuilder().expressionText()
                self.table_widget.setItem(row, column, QTableWidgetItem(new_expr))

    def save_config(self):
        """Save current configuration to JSON."""
        tr = TRANSLATIONS[self.current_lang]
        path, _ = QFileDialog.getSaveFileName(
            self, tr["btn_save_config"], "", "JSON Files (*.json)"
        )
        if not path:
            return

        # Ensure extension
        if not path.lower().endswith(".json"):
            path += ".json"

        data = {
            "source_type": "file" if self.rb_file.isChecked() else "layer",
            "epci_file": self.epci_file_widget.filePath(),
            "name_field": self.name_field_cb.currentField(),
            "area_name": self.area_name_cb.currentText(),
            "base_dir": self.base_dir_widget.filePath(),
            "output_dir": self.output_dir_widget.filePath(),
            "buffer_dist": self.buffer_dist_sb.value(),
            "resolution": self.resolution_sb.value(),
            "csv_path": self.table_csv_widget.filePath(),
            "building_code": self.building_code_sb.value(),
            "save_vectors": self.save_vectors_cb.isChecked(),
            "verify_data": self.verify_data_cb.isChecked(),
            "weighting_rules": self.collect_weighting_rules(),
            "custom_sources": self.collect_custom_sources(),
        }

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
            QMessageBox.information(self, "Success", tr["msg_config_saved"])
        except Exception as e:
            QMessageBox.critical(self, "Error", tr["msg_config_error"] + str(e))

    def load_config(self):
        """Load configuration from JSON."""
        tr = TRANSLATIONS[self.current_lang]
        path, _ = QFileDialog.getOpenFileName(
            self, tr["btn_load_config"], "", "JSON Files (*.json)"
        )
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if data.get("source_type") == "file":
                self.rb_file.setChecked(True)
                self.toggle_source_type()
                self.epci_file_widget.setFilePath(data.get("epci_file", ""))
            else:
                self.rb_layer.setChecked(True)
                self.toggle_source_type()
                # We cannot easily restore the layer selection by object, but we can try by name if needed.
                # For now, user might need to re-select layer if it's not active.

            self.name_field_cb.setField(data.get("name_field", ""))
            # Force update of area names
            self.populate_area_names()
            self.area_name_cb.setCurrentText(data.get("area_name", ""))

            self.base_dir_widget.setFilePath(data.get("base_dir", ""))
            self.output_dir_widget.setFilePath(data.get("output_dir", ""))
            self.buffer_dist_sb.setValue(data.get("buffer_dist", 5000.0))
            self.resolution_sb.setValue(data.get("resolution", 5.0))
            self.table_csv_widget.setFilePath(data.get("csv_path", ""))
            self.building_code_sb.setValue(data.get("building_code", 29))
            self.save_vectors_cb.setChecked(data.get("save_vectors", True))
            self.verify_data_cb.setChecked(data.get("verify_data", True))

            # Custom sources (restore first so weighting targets can resolve them)
            custom_sources = data.get("custom_sources", [])
            self.table_custom.setRowCount(0)
            for cfg in custom_sources:
                self.add_custom_source(preset=cfg)

            # Weighting rules (new unified engine). Fall back to legacy keys if present.
            rules = data.get("weighting_rules")
            if rules is None:
                # Backward compatibility with older config files.
                rules = []
                if data.get("slope_weights"):
                    rules.append({"type": "slope", "enabled": True, "bands": data["slope_weights"]})
                if data.get("dist_weights"):
                    rules.append(
                        {
                            "type": "distance",
                            "target": "__BUILDING__",
                            "enabled": True,
                            "bands": data["dist_weights"],
                        }
                    )
            # Rebuild the weighting cards from the rules.
            for rec in list(getattr(self, "weight_cards", [])):
                self.remove_weight_rule(rec)
            for rule in rules:
                self.add_weight_rule(preset=rule)
            self._refresh_weight_targets()

            QMessageBox.information(self, "Success", tr["msg_config_loaded"])
        except Exception as e:
            QMessageBox.critical(self, "Error", tr["msg_config_error"] + str(e))

    def cancel_process(self):
        """Cancel the running task."""
        if hasattr(self, "task") and self.task and self.task.status() == QgsTask.Running:
            self.task.cancel()
            self.log_message(TRANSLATIONS[self.current_lang]["log_fail"] + " (Cancelled)")
            self.btn_cancel.setEnabled(False)
            self.btn_close.setEnabled(True)

    def _persist_current_table(self):
        """Write the current CSV-editor table to a temp file; return its path.

        Guarantees that rows added in the UI (e.g. a custom-source class) are
        used at run time even if the user never clicked "Save CSV". Returns
        None if the table is empty.
        """
        try:
            if self.table_widget.rowCount() == 0 or self.table_widget.columnCount() == 0:
                return None
            import csv as _csv
            import tempfile

            fd, path = tempfile.mkstemp(prefix="FricMaps_table_", suffix=".csv")
            os.close(fd)
            with open(path, "w", encoding="utf-8", newline="") as f:
                writer = _csv.writer(f, delimiter=";", quoting=_csv.QUOTE_MINIMAL)
                # Canonical names: this file is read back by the pipeline.
                headers = [self._col_key(c) for c in range(self.table_widget.columnCount())]
                writer.writerow(headers)
                for r in range(self.table_widget.rowCount()):
                    row = []
                    for c in range(self.table_widget.columnCount()):
                        it = self.table_widget.item(r, c)
                        row.append(it.text() if it else "")
                    # skip fully-empty rows
                    if any(cell.strip() for cell in row):
                        writer.writerow(row)
            return path
        except Exception:
            return None

    def run_process(self, only_vectors=False, skip_vectors=False):
        """Run the processing algorithm."""
        tr = TRANSLATIONS[self.current_lang]

        # Collect inputs
        if self.rb_file.isChecked():
            epci_source = self.epci_file_widget.filePath()
            if not epci_source or not os.path.exists(epci_source):
                QMessageBox.warning(self, "Error", tr["msg_error_epci_file"])
                return
        else:
            layer = self.epci_layer_cb.currentLayer()
            if not layer:
                QMessageBox.warning(self, "Error", tr["msg_error_epci"])
                return
            epci_source = layer.source()
            if layer.providerType() == "memory":
                QMessageBox.warning(self, "Error", tr["msg_error_memory_layer"])
                return

        name_field = self.name_field_cb.currentField()
        area_name = self.area_name_cb.currentText()

        base_dir = self.base_dir_widget.filePath()
        output_dir = self.output_dir_widget.filePath()

        buffer_dist = self.buffer_dist_sb.value()
        resolution = self.resolution_sb.value()

        # Always run against the CURRENT editor table (so rows added for custom
        # sources are used even if the user didn't click "Save CSV"). Falls back
        # to the CSV file path if the table is empty.
        csv_path = self._persist_current_table()
        if not csv_path:
            csv_path = self.table_csv_widget.filePath() or None
            if csv_path and not os.path.exists(csv_path):
                QMessageBox.warning(
                    self,
                    "Error",
                    f"CSV file not found: {csv_path}\nPlease check the path or save the table again.",
                )
                return

        build_code = self.building_code_sb.value()
        save_vectors = self.save_vectors_cb.isChecked()
        # vector_only and skip_vectors are passed as arguments

        # Unified weighting rules (slope + distance to any class/source)
        weighting_rules = self.collect_weighting_rules()

        # Validation
        if not base_dir or not output_dir:
            QMessageBox.warning(self, "Error", tr["msg_error_base_output"])
            return

        if not name_field or not area_name:
            QMessageBox.warning(self, "Error", tr["msg_error_name_area"])
            return

        # Switch to the log tab (index 3: Vector, Rasterisation, Weighting, Logs)
        self.tabs.setCurrentIndex(3)
        self.log_text.clear()
        self.log_message(tr["log_start"])

        # Disable buttons
        self.btn_run_vector.setEnabled(False)
        self.btn_run_raster.setEnabled(False)
        self.btn_close.setEnabled(False)
        self.btn_cancel.setEnabled(True)

        self.progress_bar.setValue(0)

        # Prepare parameters for the task
        params = {
            "EPCI_FILE": epci_source,
            "NAME_FIELD": name_field,
            "AREA_NAME": area_name,
            "BASE_DIR": base_dir,
            "OUTPUT_DIR": output_dir,
            "BUFFER_DIST": buffer_dist,
            "RESOLUTION": resolution,
            "TABLE_CSV": csv_path,
            "BUILDING_CODE": build_code,
            "SAVE_VECTORS": save_vectors,
            "ONLY_VECTORS": only_vectors,
            "SKIP_VECTORS": skip_vectors,
            "SLOPE_WEIGHTS": "[]",
            "BUILDING_WEIGHTS": "[]",
            "WEIGHTING_RULES": json.dumps(weighting_rules),
            "CUSTOM_SOURCES": json.dumps(self.collect_custom_sources()),
            "VERIFY_DATA": self.verify_data_cb.isChecked(),
            "OUTPUT_FOLDER_PATH": "memory:",
        }

        self.last_run_vector_only = only_vectors

        # Define task function
        self.task = FricMapsTask(
            params, self.log_message, self.update_progress, self.process_finished
        )
        QgsApplication.taskManager().addTask(self.task)

    def process_finished(self, success):
        tr = TRANSLATIONS[self.current_lang]
        self.btn_run_vector.setEnabled(True)
        self.btn_run_raster.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.btn_close.setEnabled(True)

        if success:
            self.log_message(tr["log_success"])
            QMessageBox.information(self, "Success", tr["msg_success"])

            # If vector only run, switch to the Rasterization tab (index 1 now)
            if getattr(self, "last_run_vector_only", False):
                self.tabs.setCurrentIndex(1)

        else:
            self.log_message(tr["log_fail"])
            QMessageBox.critical(self, "Error", tr["msg_failed"])

    @staticmethod
    def _detect_dark_theme():
        """Return True if the host (QGIS) is using a dark theme."""
        try:
            win = QApplication.palette().color(QPalette.Window)
            # Perceived lightness; < ~128 → dark theme.
            return win.lightness() < 128
        except Exception:
            return False

    def _theme_palette(self):
        """Return the colour palette dict for the current theme."""
        if self.dark_mode:
            return {
                "font": '"Segoe UI", "Helvetica Neue", Arial, sans-serif',
                "window": "#232629",
                "surface": "#2b2e31",
                "surface_alt": "#34373b",
                "text": "#e3e5e8",
                "text_strong": "#ffffff",
                "text_subtle": "#a7abb0",
                "border": "#43474c",
                "border_strong": "#4a4e52",
                "title": "#8ab4f8",
                "accent": "#8ab4f8",
                "tab_inactive": "#2f3336",
                "progress_track": "#3b3f43",
                "progress_chunk": "#34a853",
                "sel_bg": "#33445f",
                "sel_text": "#ffffff",
                "hover_bg": "#34373b",
                "pressed_bg": "#3b3f43",
                "disabled_text": "#6a6e72",
                "disabled_bg": "#2b2e31",
                "gridline": "#3b3f43",
                "sb_track": "#2b2e31",
                "sb_handle": "#55595d",
                "sb_handle_hover": "#6a6e72",
                "run_bg": "#1a73e8",
                "run_hover": "#2b7cf0",
                "run_pressed": "#1765cc",
                "run_disabled": "#3a4757",
            }
        return {
            "font": '"Segoe UI", "Helvetica Neue", Arial, sans-serif',
            "window": "#f0f2f5",
            "surface": "#ffffff",
            "surface_alt": "#f8f9fa",
            "text": "#3c4043",
            "text_strong": "#202124",
            "text_subtle": "#5f6368",
            "border": "#ced4db",
            "border_strong": "#bcc1c9",
            "title": "#2c3e50",
            "accent": "#1a73e8",
            "tab_inactive": "#e8eaed",
            "progress_track": "#e8eaed",
            "progress_chunk": "#34a853",
            "sel_bg": "#d2e3fc",
            "sel_text": "#202124",
            "hover_bg": "#f8f9fa",
            "pressed_bg": "#f1f3f4",
            "disabled_text": "#bdc1c6",
            "disabled_bg": "#f1f3f4",
            "gridline": "#f1f3f4",
            "sb_track": "#f1f3f4",
            "sb_handle": "#c1c5ca",
            "sb_handle_hover": "#a8adb3",
            "run_bg": "#1a73e8",
            "run_hover": "#1765cc",
            "run_pressed": "#1659b1",
            "run_disabled": "#8ab4f8",
        }

    def toggle_theme(self):
        """Manually flip between light and dark theme."""
        self.dark_mode = not self.dark_mode
        self.apply_styles()

    def apply_styles(self):
        """Apply a theme-aware, self-consistent QSS to the dialog.

        Every rule that sets a background also sets a text colour, so nothing
        inherits the host palette (which caused white-on-white in dark mode).
        """
        c = self._theme_palette()

        # Theme toggle button glyph (shows the theme you can switch TO).
        self.btn_theme.setText("☀️" if self.dark_mode else "🌙")

        # Apply a real QPalette, not just a stylesheet. Sub-controls left to the
        # native style - the checkbox tick and the radio dot - are painted from
        # the widget PALETTE, which is inherited from QGIS and does not follow
        # our theme toggle. Without this, switching the plugin to light while
        # QGIS stays dark paints dark glyphs on a light background.
        qpal = QPalette()
        qpal.setColor(QPalette.Window, QColor(c["window"]))
        qpal.setColor(QPalette.WindowText, QColor(c["text"]))
        qpal.setColor(QPalette.Base, QColor(c["surface"]))
        qpal.setColor(QPalette.AlternateBase, QColor(c["surface_alt"]))
        qpal.setColor(QPalette.Text, QColor(c["text"]))
        qpal.setColor(QPalette.Button, QColor(c["surface"]))
        qpal.setColor(QPalette.ButtonText, QColor(c["text"]))
        qpal.setColor(QPalette.Highlight, QColor(c["sel_bg"]))
        qpal.setColor(QPalette.HighlightedText, QColor(c["sel_text"]))
        qpal.setColor(QPalette.ToolTipBase, QColor(c["surface"]))
        qpal.setColor(QPalette.ToolTipText, QColor(c["text"]))
        for _role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
            qpal.setColor(QPalette.Disabled, _role, QColor(c["disabled_text"]))
        self.setPalette(qpal)
        # The custom-sources pop-up is a separate top-level window, so the
        # palette does not propagate to it automatically.
        if getattr(self, "custom_dialog", None) is not None:
            self.custom_dialog.setPalette(qpal)

        # Object-specific widgets that previously had hardcoded inline colours.
        self.info_browser.setStyleSheet(
            "QTextBrowser{background-color:%s; color:%s; border:1px solid %s; "
            "border-radius:6px; padding:14px; font-family:%s; font-size:12pt;}"
            % (c["surface"], c["text"], c["border"], c["font"])
        )
        self.lbl_custom_help.setStyleSheet("color:%s; padding:4px 2px;" % c["text_subtle"])

        self.setStyleSheet("""
            QDialog, QWidget {
                background-color: %(window)s;
                font-family: %(font)s;
                font-size: 12pt;
                color: %(text)s;
            }

            /* Group Boxes */
            QGroupBox {
                background-color: %(surface)s;
                border: 1px solid %(border)s;
                border-radius: 6px;
                margin-top: 1.2em;
                padding-top: 15px;
                padding-bottom: 10px;
                font-weight: bold;
                color: %(text)s;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 5px;
                left: 10px;
                color: %(title)s;
                background-color: transparent;
            }

            /* Tabs */
            QTabWidget::pane {
                border: 1px solid %(border)s;
                background-color: %(surface)s;
                border-radius: 6px;
                top: -1px;
            }
            QTabBar::tab {
                background-color: %(tab_inactive)s;
                border: 1px solid %(border)s;
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                padding: 8px 16px;
                margin-right: 2px;
                color: %(text_subtle)s;
            }
            QTabBar::tab:selected {
                background-color: %(surface)s;
                border-bottom: 1px solid %(surface)s;
                color: %(accent)s;
                font-weight: bold;
            }
            QTabBar::tab:hover {
                background-color: %(hover_bg)s;
            }

            /* Buttons */
            QPushButton {
                background-color: %(surface)s;
                border: 1px solid %(border_strong)s;
                border-radius: 4px;
                padding: 6px 16px;
                color: %(text)s;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: %(hover_bg)s;
                border-color: %(border_strong)s;
                color: %(text_strong)s;
            }
            QPushButton:pressed {
                background-color: %(pressed_bg)s;
            }
            QPushButton:disabled {
                background-color: %(disabled_bg)s;
                color: %(disabled_text)s;
                border-color: %(disabled_bg)s;
            }

            /* Primary Action Buttons (Run) */
            QPushButton[objectName="btn_run_vector"], QPushButton[objectName="btn_run_raster"] {
                background-color: %(run_bg)s;
                color: #ffffff;
                border: none;
                font-weight: bold;
                padding: 8px 20px;
            }
            QPushButton[objectName="btn_run_vector"]:hover, QPushButton[objectName="btn_run_raster"]:hover {
                background-color: %(run_hover)s;
            }
            QPushButton[objectName="btn_run_vector"]:pressed, QPushButton[objectName="btn_run_raster"]:pressed {
                background-color: %(run_pressed)s;
            }
            QPushButton[objectName="btn_run_vector"]:disabled, QPushButton[objectName="btn_run_raster"]:disabled {
                background-color: %(run_disabled)s;
                color: #ffffff;
            }

            /* Progress Bar */
            QProgressBar {
                border: none;
                background-color: %(progress_track)s;
                border-radius: 4px;
                text-align: center;
                color: %(text)s;
            }
            QProgressBar::chunk {
                background-color: %(progress_chunk)s;
                border-radius: 4px;
            }

            /* Input Fields */
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit, QPlainTextEdit {
                border: 1px solid %(border_strong)s;
                border-radius: 4px;
                padding: 4px 8px;
                background-color: %(surface)s;
                color: %(text)s;
                selection-background-color: %(sel_bg)s;
                selection-color: %(sel_text)s;
            }
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTextEdit:focus {
                border: 2px solid %(accent)s;
                padding: 3px 7px;
            }
            QComboBox::drop-down {
                border: none;
                width: 18px;
            }
            QComboBox QAbstractItemView {
                border: 1px solid %(border_strong)s;
                background-color: %(surface)s;
                color: %(text)s;
                selection-background-color: %(sel_bg)s;
                selection-color: %(sel_text)s;
            }

            /* Tool buttons (e.g. file-picker "..." in QgsFileWidget) */
            QToolButton {
                background-color: %(surface)s;
                border: 1px solid %(border_strong)s;
                border-radius: 4px;
                color: %(text)s;
                padding: 2px 6px;
            }
            QToolButton:hover { background-color: %(hover_bg)s; }
            QToolButton:pressed { background-color: %(pressed_bg)s; }

            /* Accordion cards */
            QToolButton#accordion_header {
                background-color: %(surface_alt)s;
                border: 1px solid %(border)s;
                border-radius: 6px;
                padding: 9px 12px;
                color: %(title)s;
                font-weight: bold;
                text-align: left;
            }
            QToolButton#accordion_header:hover { background-color: %(hover_bg)s; }
            QWidget#accordion_body {
                background-color: %(surface)s;
                border: 1px solid %(border)s;
                border-top: none;
                border-bottom-left-radius: 6px;
                border-bottom-right-radius: 6px;
            }

            /* Tables */
            QTableWidget, QTableView {
                border: 1px solid %(border)s;
                gridline-color: %(gridline)s;
                background-color: %(surface)s;
                alternate-background-color: %(surface_alt)s;
                color: %(text)s;
            }
            QTableWidget::item { padding: 2px 4px; }
            QTableWidget::item:selected, QTableView::item:selected {
                background-color: %(sel_bg)s;
                color: %(sel_text)s;
            }
            QHeaderView::section {
                background-color: %(surface_alt)s;
                padding: 4px;
                border: none;
                border-bottom: 2px solid %(border)s;
                font-weight: bold;
                color: %(text_subtle)s;
            }
            QTableCornerButton::section {
                background-color: %(surface_alt)s;
                border: none;
            }

            /* Text browser (info panel) */
            QTextBrowser {
                background-color: %(surface)s;
                color: %(text)s;
                border: 1px solid %(border)s;
                border-radius: 6px;
                padding: 14px;
                font-family: %(font)s;
                font-size: 12pt;
            }

            /* Labels, checkboxes, radios */
            QLabel { color: %(text)s; background: transparent; }
            QCheckBox, QRadioButton {
                spacing: 8px;
                color: %(text)s;
                background: transparent;
                padding: 2px 0;
            }
            /* No ::indicator rule on purpose. Qt only takes over the painting of
               a sub-control that is explicitly styled, so leaving it alone keeps
               the native tick mark and radio dot - and keeps them consistent
               across themes. Styling the size alone would silently suppress that
               painting and shift the glyph. */

            /* Scrollbars */
            QScrollBar:vertical {
                background: %(sb_track)s; width: 10px; margin: 0; border-radius: 5px;
            }
            QScrollBar::handle:vertical {
                background: %(sb_handle)s; border-radius: 5px; min-height: 24px;
            }
            QScrollBar::handle:vertical:hover { background: %(sb_handle_hover)s; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar:horizontal {
                background: %(sb_track)s; height: 10px; margin: 0; border-radius: 5px;
            }
            QScrollBar::handle:horizontal {
                background: %(sb_handle)s; border-radius: 5px; min-width: 24px;
            }
            QScrollBar::handle:horizontal:hover { background: %(sb_handle_hover)s; }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
        """ % c)

        # Re-render the info panel LAST. Its HTML embeds palette colours, and Qt's
        # rich-text importer resolves the document font at parse time: rendering it
        # before setStyleSheet() would bake in the outgoing theme's font.
        if hasattr(self, "info_browser") and hasattr(self, "tabs"):
            self.update_info_panel()


class FricMapsTask(QgsTask):
    """Task to run the processing algorithm in background."""

    def __init__(self, params, log_callback, progress_callback, finished_callback):
        super().__init__("FricMaps", QgsTask.CanCancel)
        self.params = params
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        self.finished_callback = finished_callback
        self.exception = None

        self.signal_emitter = WorkerSignals()
        self.signal_emitter.log.connect(self.log_callback)
        self.signal_emitter.progress.connect(self.progress_callback)
        self.start_time = None

    def run(self):
        """Run the algorithm."""
        try:
            feedback = CustomFeedback(
                progress_callback=self.signal_emitter.progress.emit,
                log_callback=self.signal_emitter.log.emit,
            )
            self.start_time = time.time()
            processing.run("fricmaps:build_surfaces", self.params, feedback=feedback)
            return True
        except Exception as e:
            self.exception = e
            self.signal_emitter.log.emit(f"❌ ERROR: {e}")
            return False

    def finished(self, result):
        """Called on main thread when task finishes."""
        if self.start_time:
            end_time = time.time()
            duration = end_time - self.start_time
            hours, rem = divmod(duration, 3600)
            minutes, seconds = divmod(rem, 60)
            time_str = "{:0>2}:{:0>2}:{:05.2f}".format(int(hours), int(minutes), seconds)
            self.log_callback(f"\n⏱️ Execution Time: {time_str}")
        self.finished_callback(result)


class WorkerSignals(QObject):
    log = pyqtSignal(str)
    progress = pyqtSignal(float)
