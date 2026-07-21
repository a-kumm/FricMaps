# FricMaps <img src="fricmaps_plugin/resources/logo_info.png" alt="FricMaps Logo" width="150" align="right">

[![Code: GPL v3](https://img.shields.io/badge/code-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Docs: CC BY 4.0](https://img.shields.io/badge/docs-CC%20BY%204.0-lightgrey.svg)](https://creativecommons.org/licenses/by/4.0/)
[![QGIS](https://img.shields.io/badge/QGIS-%E2%89%A5%203.28-93b023.svg)](https://qgis.org)
[![Version](https://img.shields.io/badge/version-1.0.0-informational.svg)](CHANGELOG.md)

**FricMaps** is an open-source QGIS plugin for **modelling landscape connectivity and fence permeability**. It provides an automated pipeline that turns heterogeneous vector datasets into high-resolution land-cover and resistance surfaces (friction maps), ready for graph-based connectivity analysis in tools such as [Graphab](https://sourcesup.renater.fr/www/graphab/).

## Description

Building a resistance surface is usually a long, manual and poorly reproducible process: datasets come from different producers, with different schemas and vintages, and every thematic choice ends up buried in a chain of one-off geoprocessing steps.

FricMaps makes that chain explicit and reproducible. Source harmonisation is automated, and every ecological assumption — which class, which resistance value, which barrier effect — lives in a single editable classification table rather than in code. The plugin pays particular attention to anthropogenic barriers that are usually underrepresented in connectivity studies, such as fences and artificial lighting, and can restore local permeability at validated wildlife crossings using databases such as BD ORFeH.

## Features

- **Automated vector pipeline** — from raw national datasets to analysis-ready rasters, over any study area.
- **Dual-format compatibility** — legacy Shapefile deliveries and the BD TOPO 3.x GeoPackage model are both supported; field names are harmonised automatically, so the raw download tree can be used as-is.
- **Data-driven custom sources** — register any additional vector dataset from the interface, with its own detection rule, SQL filter and buffer. No Python required.
- **Unified weighting engine** — multiplicative rules based on slope or on distance to *any* class or custom source, with user-defined bands. This is how diffuse pressures such as light pollution or disturbance around buildings are modelled.
- **Resolution-aware buffering** — linear features are widened proportionally to the output resolution, so barriers stay continuous in the raster instead of breaking into disconnected pixels.
- **Scenario generation** — alternative resistance surfaces (no fences, no linear transport infrastructure, no barriers) are produced without re-running the vector stage.
- **Bilingual interface** — English and French, with light and dark themes and a context-sensitive guidance panel.

## Requirements

- QGIS **3.28 LTR** or newer (tested up to 3.3x)
- No external Python dependency: the plugin relies solely on PyQGIS, GDAL and the native Processing algorithms shipped with QGIS

## Installation

Only the `fricmaps_plugin/` folder is the plugin. It is self-contained, so it installs on its own.

**From a ZIP, inside QGIS.** Build the archive, then use *Plugins → Manage and Install Plugins → Install from ZIP*:

```bash
zip -r fricmaps_plugin.zip fricmaps_plugin -x "*__pycache__*" -x "*.DS_Store"
```

**By hand.** Copy `fricmaps_plugin/` into your QGIS plugins directory:

- **Linux**: `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/`
- **Windows**: `C:\Users\YourUser\AppData\Roaming\QGIS\QGIS3\profiles\default\python\plugins\`
- **macOS**: `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/`

Then restart QGIS and enable **FricMaps** in the Plugin Manager.

> `fricmaps_plugin/` must sit directly in the plugins directory: QGIS uses the folder name as the Python module name, so an intermediate folder produces an invalid name and the plugin fails to load. Zipping from the macOS Finder also adds a `__MACOSX` folder that QGIS tries to load as a plugin — build the archive from a terminal, as above.

The plugin is then available from the **FricMaps** toolbar button and from the *Plugins → FricMaps* menu.

## Input data

FricMaps is built around the French national datasets, in either Shapefile or GeoPackage delivery:

| Dataset | Used for |
|---------|----------|
| **OCS GE** | Land-cover base layer |
| **BD TOPO** | Buildings, roads, railways, hydrography, vegetation, hedgerows, built-up areas |
| **RPG** | Agricultural parcels |
| **RGE ALTI** | Digital elevation model, for slope weighting |
| **BD ORFeH** *(optional)* | Wildlife crossing structures |

Point the plugin at the directory holding these datasets: sub-folders are scanned recursively, so the delivery tree does not need to be reorganised. Any other dataset can be added through the custom-sources mechanism.

## Usage

The interface follows the four stages of the workflow.

**1 — Vector processing.** Select the study area (a project layer or a file), the name field and the value identifying your territory. Set the base data directory and the output directory. Optionally apply a buffer around the study area to avoid edge effects in the connectivity graph, and declare any custom source.

**2 — Rasterization.** Edit the classification table that drives the whole thematic content of the output. Each row maps a subset of a source to a class and a resistance value:

| Column | Meaning |
|--------|---------|
| `CLASS_NAME` | Class name |
| `COMPILATION_ORDER` | Burn priority — higher values are rasterised last and overwrite lower ones |
| `DESCRIPTION` | Free-text description |
| `SOURCE` | Dataset the class is drawn from |
| `SQL_FILTER` | Optional SQL expression restricting the class to a subset of features |
| `FRICTION_VALUE` | Resistance value; low means permeable, high means costly to cross |

The table is a semicolon-separated CSV and can be edited in place or loaded from disk. A default table of 39 classes is provided as `fricmaps_plugin/resources/Table_Raster.csv`. Tables authored with the earlier French column names are migrated automatically on load.

**3 — Weighting.** Add multiplicative rules on top of the base friction. Each rule targets either the slope (from the DEM) or the distance to any class or custom source, with user-defined bands. A factor of `1.0` is neutral; values below 1 make a zone more permeable. The processing is launched from the bottom of this tab.

**4 — Logs.** Follow the run step by step. Each stage reports its retained feature count, which is the fastest way to confirm that a dataset was read correctly.

## Outputs

The output directory holds only the deliverables, with `<area>` the cleaned study-area name:

| File | Content |
|------|---------|
| `Land_Cover_<area>.tif` | Land-cover raster, one integer code per class |
| `Friction_<area>.tif` | Base friction raster, straight from the classification table |
| `Friction_Weighted_<area>.tif` | Friction surface after weighting and bias correction |
| `Scenario_No_Fences_<area>.tif` | Scenario without fences |
| `Scenario_No_LTI_<area>.tif` | Scenario without linear transport infrastructure |
| `Scenario_No_Barriers_<area>.tif` | Scenario without either |

Everything else — the clipped vector layers, the digital elevation model, the per-step weighting rasters and the scratch folders — is written to an `intermediate/` sub-folder. Nothing is discarded, so a run remains fully auditable, but the deliverables stay easy to find.

Output filenames are in English regardless of the interface language, so that scripts, figures and published protocols remain valid across locales.

## Repository layout

```
fricmaps_plugin/     THE PLUGIN - zip this folder, or drop it into the QGIS
                     plugins directory. It is self-contained.
  __init__.py          classFactory, called by QGIS on load
  metadata.txt         plugin metadata read by QGIS
  icon.png             toolbar icon
  main_plugin.py       menu/toolbar registration, Processing provider
  dialog.py            graphical interface
  processing_*.py      scriptable algorithm and its provider
  core/                data preparation, rasterisation, weighting, helpers
  resources/           default classification table, interface images
docs/                user guide and design notes
README.md            this file
LICENSE              GPL-3.0-or-later (code) + CC BY 4.0 (docs and data)
CHANGELOG.md         release history
CITATION.cff         citation metadata
pyproject.toml       formatting and linting configuration
```

## Documentation

- [`docs/GENERIC_SOURCES_ARCHITECTURE.md`](docs/GENERIC_SOURCES_ARCHITECTURE.md) — design of the data-driven custom-source system
- [`docs/GENERIC_SOURCES_IMPLEMENTATION.md`](docs/GENERIC_SOURCES_IMPLEMENTATION.md) — delivered behaviour and a worked example
- [`CHANGELOG.md`](CHANGELOG.md) — release history

## Citation

If you use FricMaps in academic work, please cite it. Citation metadata is provided in [`CITATION.cff`](CITATION.cff), and GitHub can generate a formatted citation from the *Cite this repository* button.

## Contributing

Bug reports and feature requests are welcome through the [issue tracker](https://github.com/a-kumm/FricMaps/issues). When reporting a processing problem, please attach the content of the **Logs** tab: it contains the resolved paths and per-step feature counts needed to reproduce the case.

## License

FricMaps uses a split licence, which is standard practice for scientific software:

| Material | Licence |
|----------|---------|
| **Source code** | [GNU GPL v3.0 or later](LICENSE) |
| **Documentation, classification table, figures** | [CC BY 4.0](LICENSE) (PART 2) |

The code is released under the GPL because FricMaps builds on QGIS, which is itself distributed under the GPL v2 or later; this is also a requirement of the official QGIS plugin repository. The documentation and the default classification table are released under CC BY 4.0 so that they can be freely reused, adapted and cited in scientific work, provided attribution is given.

Partner logos in `fricmaps_plugin/resources/` remain the property of their respective organisations and are not covered by either licence.

---

## Partners

<div align="center">
  <img src="fricmaps_plugin/resources/logo_terroiko.png" alt="Terroiko" height="60"/>
  &nbsp;&nbsp;&nbsp;&nbsp;
  <img src="fricmaps_plugin/resources/logo_live.png" alt="LIVE" height="60"/>
  &nbsp;&nbsp;&nbsp;&nbsp;
  <img src="fricmaps_plugin/resources/logo_unistra.png" alt="Universite de Strasbourg" height="60"/>
  &nbsp;&nbsp;&nbsp;&nbsp;
  <img src="fricmaps_plugin/resources/logo_ademe.png" alt="ADEME" height="60"/>
</div>

<p align="center">
  <i>Developed as part of the Polymor-FENCE project (ADEME, 2024-2026).</i>
</p>
