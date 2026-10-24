# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.2.0] - 2026-10-24

### Added
- Performance report: every run writes `intermediate/Performance_<area>.json` with
  per-stage timings, memory (RSS), workload descriptors (grid size, valid pixels,
  features per source) and the software and hardware environment. It is also
  returned as the `OUTPUT_PERFORMANCE` output of the Processing algorithm, and is
  written even when the run fails. It runs no external commands, as required by
  the security scan of the QGIS plugin repository: on macOS, memory, CPU model
  and RAM are read from the system library through `ctypes` (`proc_pidinfo`,
  `sysctlbyname`) instead of `ps` and `sysctl`, and exceptions deliberately
  ignored while recording performance are logged at debug level.
- `tools/benchmark.py` (repeated headless runs with aggregation: median, spread,
  peak memory, LaTeX table) and `tools/compare_rasters.py` (pixel-wise comparison
  of two sets of outputs); see `docs/BENCHMARKING.md`.
- Unit tests under `tests/` (`python -m unittest discover -s tests`).

### Changed
- `qgisMaximumVersion` raised to 4.99 so that the plugin can be installed on
  QGIS 4. Following the Qt 6 check of the QGIS plugin repository, Qt and QGIS
  enums are written in their fully qualified form (`Qt.ItemDataRole.EditRole`
  instead of `Qt.EditRole`), and `exec_()` and `QRegExp`, removed in Qt 6, are
  replaced by `exec()` and `QRegularExpression`. These forms also work with
  QGIS 3. The plugin has not yet been tested on QGIS 4.

### Fixed
- Results no longer depend on the operating system when the BD TOPO departments
  of a study area mix Shapefile and GeoPackage deliveries. The same attribute is
  defined differently in the two formats (`SALINITE`, `FICTIF`: 3-character text
  in a Shapefile, boolean in a GeoPackage), and merging the layers failed or
  succeeded depending on their order, which came from the file system: with the
  same data, hydrography and linear transport infrastructure were lost on macOS
  and kept on Linux. Fields are now given compatible definitions before every
  merge (booleans become `Oui`/`Non`), and input files are found in sorted order.
- Classes were silently dropped when a layer could not be written to disk for
  rasterisation: a `fid` attribute inherited from a GeoPackage source collided
  with the keys of features from other sources ("UNIQUE constraint failed:
  ...fid", error 7). With mixed BD TOPO deliveries this removed the hydrography
  classes on Linux. The attribute is no longer written.
- A class that cannot be rasterised (layer not written, mask not produced or not
  readable) now stops the run with an explicit message instead of being left out
  of a land-cover raster reported as complete.
- Rasterisation no longer needs temporary space proportional to the number of
  classes. The per-class masks are written as bytes (they were Float32, four
  times larger) and deleted as soon as they have been read: a 124 M-pixel grid
  previously needed about 20 GB of temporary files and could fill a RAM-backed
  `/tmp`.
- On macOS, the log of the plugin window showed an empty box instead of most
  lines. Qt 5 on macOS cannot draw a symbol followed by the emoji variation
  selector (U+FE0F, as in the `ℹ️` that prefixes every pipeline message) and drops
  the rest of the line; the selector is now removed from the text shown on macOS.
  Other platforms are unchanged.
- An optional source that fails is still skipped so that the run can finish, but
  it is now listed in the performance report (`workload.failed_sources`) and in a
  summary line of the log, instead of only in a warning among the messages.

## [1.0.0] - 2026-07-22

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
