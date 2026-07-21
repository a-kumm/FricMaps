# Implementation — Generic data sources

This document records what was implemented to allow **any** additional data
source to be registered from the interface without writing Python, and the
procedure used to validate it on a street-light (`lampadaires`) dataset.

## What was added or modified

**New module — `fricmaps_plugin/core/custom_sources.py`**
Data-driven generic loader. `load_custom_sources_config()` parses the JSON
configuration; `process_generic_source()` chains location (fuzzy token, explicit
path, or QGIS layer) → merge and reprojection to the study CRS → clipping to the
extent → optional SQL filter → buffering (mandatory for point geometries) →
optional dissolve. It reuses the helpers in `utils.py` and never raises: a
failing source is logged and skipped without aborting the run.

**`fricmaps_plugin/core/raster_processing.py`**
`apply_building_distance_weighting` was generalised into
`apply_class_distance_weighting(path_permeability, path_raster_classes,
target_class_code, weight_bands, path_output, max_friction)`. The former function
is kept as a backward-compatibility wrapper, so no existing call site is broken.
Temporary distance rasters now embed the class code in their filename, which
avoids collisions when several weightings are chained.

**`fricmaps_plugin/core/full_pipeline.py`**
New `custom_sources` and `weighting_rules` parameters. Custom sources are loaded
after the core vector processing and merged into `vector_sources`, so they take
part in rasterisation exactly like native sources through their `SOURCE` key. A
unified weighting engine then resolves each rule's target class code from the
classification table, which means no class code is ever entered by hand.

**`fricmaps_plugin/processing_algorithm.py`**
New `CUSTOM_SOURCES` and `WEIGHTING_RULES` parameters (JSON), forwarded to
`run_pipeline`.

**`fricmaps_plugin/dialog.py`**
Custom sources are declared in a **pop-up opened from tab 1**, keeping source
declaration alongside the data-input settings. All weighting is centralised in
**tab 3**, where each rule is an accordion card that can target any class or
custom source. The `SOURCE` drop-down of the table editor is **dynamic**: it
automatically includes the declared custom keys. Configuration save and load
serialise both the custom sources and the weighting rules.

## Bugs found and fixed in the existing code

1. **Friction capped at 1000.** The table editor (`IntegerDelegate`) limited
   `FRICTION_VALUE` to 1000, while the reference table contains values up to
   10000 for obstacles. Any edit above 1000 was silently truncated. The range was
   raised to 1 000 000.
2. **SQL filter builder reading the wrong column.** `open_sql_builder` assumed
   `SOURCE` was in column 0, whereas it is in column 3. The reference layer was
   therefore mis-resolved. The column is now located by header name.
3. **SQL builder crash on an empty SQL_FILTER cell.** `item(row, col).text()` raised
   an `AttributeError` on a newly added row. Guarded.
4. **Layout added twice.** `config_buttons_layout` was added twice to the
   configuration tab layout. The duplicate was removed.
5. **Wrong tab after a vector-only run.** The code switched to the weighting tab
   instead of the rasterisation tab, despite a comment stating otherwise.
6. **Hard-coded column delegates.** Static assignments mapped the wrong columns;
   they were removed in favour of assignment by header name.
7. **SQL operators corrupted by the filter parser.** `apply_SQL_filter` inserted
   spaces inside comparison operators, turning `>=` into `> =` and breaking every
   non-trivial filter. It now only unescapes the legacy CSV quoting.

## Validation procedure — street lights

1. **Tab 1 → Custom Sources…→ Add a source**
   - Key (`SOURCE`): `LAMPADAIRES`
   - Label: `Lampadaires`
   - Type: *File* → select the dataset, or *QGIS layer* if it is already loaded
     in the project.
   - Buffer (m): `15` — mandatory, the dataset is a point layer.
   - SQL filter: empty, or a valid expression on an existing field.

2. **Tab 2 (Rasterization) → Add row**
   - `CLASS_NAME`: `LAMPADAIRE`
   - `SOURCE`: `LAMPADAIRES`, available in the drop-down
   - `SQL_FILTER`: empty
   - `FRICTION_VALUE`: for example `200`, the base friction of the footprint

3. **Tab 3 (Weighting) → Add a weighting**
   - Type: *Distance to a layer/class*
   - Target: `LAMPADAIRE`
   - Bands: `0 → 10: 3`, `10 → 30: 2`, `30 → 60: 1.3`

4. **Run** the processing from the button at the bottom of tab 3.

Expected result: street lights appear as 15 m discs with friction 200, surrounded
by a decreasing multiplicative halo (×3, then ×2, then ×1.3) representing the
effect of artificial light on species movement.

The live editor table is persisted automatically at run time, so a target added
in tab 2 is immediately resolvable by a weighting rule in tab 3 without saving
the CSV manually.

## Known limitations and possible follow-ups

- The scenario class codes remain fixed. Integrating a custom source into a
  scenario would require a per-source `is_barrier` flag.
- A source's class code is its `COMPILATION_ORDER` in the classification table;
  adding or removing rows shifts the codes. Scenarios should be re-checked after
  reordering the table.
- Value-domain differences between dataset vintages are handled case by case; a
  few hydrography and road attribute domains still differ between the legacy
  Shapefile model and the BD TOPO 3.x GeoPackage model.
