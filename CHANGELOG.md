# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.0.0] - Unreleased

### Licensing
- Source code released under GPL-3.0-or-later, as required for a QGIS
  plugin building on QGIS (GPL-2.0-or-later) and by the official plugin
  repository.
- Documentation, the default classification table and figures released
  under CC BY 4.0 (see `LICENSE`), so they can be reused, adapted and
  cited in scientific work.

### Changed
- The Processing algorithm is no longer hidden from the toolbox, and the plugin
  now declares its Processing provider. The complete workflow can be run from
  Python or `qgis_process` as `fricmaps:build_surfaces`, and returns the path of
  every deliverable.
- Legacy development names removed throughout. The Processing algorithm is now
  addressed as `fricmaps:build_surfaces` (previously
  `autoecomap:ecofriction_pipeline`), and the plugin classes are named
  `FricMapsPlugin`, `FricMapsDialog`, `FricMapsTask`, `FricMapsAlgorithm` and
  `FricMapsProvider`. Any script or Processing model referring to the old
  identifier must be updated.
- Plugin code grouped under a single `FricMaps/` package, with `fricmaps_plugin/core/`
  holding the data preparation, rasterisation and weighting modules.
- Classification-table columns renamed to a standardised, language-independent
  schema: `CLASS_NAME`, `COMPILATION_ORDER`, `DESCRIPTION`, `SOURCE`,
  `SQL_FILTER`, `FRICTION_VALUE`. Tables using the previous French column names
  are migrated transparently on load and rewritten with the new names on save.
- Output directory now holds only the six deliverables; every intermediate
  product is written to an `intermediate/` sub-folder.
- Output filenames are language-independent (`Land_Cover_`, `Friction_`,
  `Friction_Weighted_`, `Scenario_No_*`).
- Interface now defaults to English; the French toggle is unchanged.

### Added
- Custom (user-defined) data sources: any vector dataset can be plugged into the
  pipeline from the GUI, with file or project-layer input, optional buffering
  and an optional SQL filter (expression console).
- Unified weighting engine: user-defined rules combining a factor (slope, or
  distance to any class/source) with min/max/multiplier bands.
- Support for both Shapefile and GeoPackage deliveries of the French national
  datasets, including layers stored as GeoPackage sublayers.
- Field-name harmonisation between the legacy and the current BD TOPO data
  models (upper-casing plus explicit renamings).
- Resolution-scalable continuity buffer for linear features, guaranteeing that
  linear barriers remain continuous at any raster resolution.
- Optional pre-flight verification of the required datasets, which can be
  disabled to force a run.
- Theme-aware user interface (light/dark), following the host QGIS theme.

### Fixed
- SQL filter parsing no longer corrupts multi-character operators (`>=`, `<=`,
  `!=`) nor `=` characters inside string literals.
- CSV reading and writing now use proper quoting/escaping.
- GeoPackage outputs no longer fail with `UNIQUE constraint failed: ...fid`.
- The expression builder now opens on the layer actually being filtered.

### Changed
- Comments, docstrings and log messages translated to English.
- Project renamed consistently to FricMaps in the documentation.
