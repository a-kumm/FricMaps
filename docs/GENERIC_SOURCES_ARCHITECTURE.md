# Architecture — Generic data sources in FricMaps

**Goal.** Let any user register an additional data source (street lights, for
example) from the graphical interface, with buffering and decreasing
multiplicative weighting, without writing a single line of Python.

**Status.** Implemented. See `GENERIC_SOURCES_IMPLEMENTATION.md` for the
delivered behaviour and the current user workflow.

---

## 1. Diagnosis: where was the data model hard-coded?

The pipeline was already largely data-driven. All class and friction logic lives
in the classification table (`SOURCE`, `COMPILATION_ORDER`, `SQL_FILTER`,
`FRICTION_VALUE` columns), and the rasterisation step
(`rasterize_classes_and_friction`) is **source-agnostic**: it iterates over the
table rows, looks the `SOURCE` key up in a dictionary of layers, applies the SQL
filter, then burns the class and friction values into the rasters.

Hard coupling was therefore confined to **three locks**:

| # | Lock | File | Role |
|---|------|------|------|
| 1 | Specialised `process_*` functions | `fricmaps_plugin/core/data_processing.py` | Locate and filter each dataset (OCS GE, BD TOPO, RPG…) with hard-coded fields and filters |
| 2 | Assembly of the `vector_sources` dict | `fricmaps_plugin/core/full_pipeline.py` | Hard-coded mapping of each `SOURCE` key to a layer |
| 3 | `SourceDelegate.items` drop-down | `fricmaps_plugin/dialog.py` | Restricted the `SOURCE` values selectable in the table editor |

A secondary lock existed in `check_required_datasets` (`fricmaps_plugin/core/utils_check.py`), which
imposed a fixed list of mandatory datasets (BD TOPO, OCS GE, RPG).

**Consequence for the street-light use case.** The user would have had to write a
`process_streetlights` function, import it into `full_pipeline`, add it to the
dictionary, extend the delegate list, then edit the table — four separate Python
edits. Removing that requirement is the purpose of this architecture.

---

## 2. Guiding principle

Change **nothing** in the behaviour of the existing sources. Add a fully
data-driven "custom sources" layer, declared in the interface, that plugs into
the three locks through parameters whose defaults are neutral (`None` / `[]`). An
existing project runs identically; every addition is strictly additive.

A generic source does not need the bespoke treatment of the `process_*`
functions. It only needs to be **located → loaded → reprojected → clipped →
optionally buffered → registered** under a `SOURCE` key. Class assignment and
base friction are already handled by the classification table, and distance
weighting reuses the mechanism originally written for distance to buildings.

---

## 3. Data model: the definition of a custom source

Each custom source is an object serialised as JSON, stored in the existing
configuration alongside the weighting rules:

```json
{
  "source_key": "LAMPADAIRES",
  "label": "Street lights (public lighting)",
  "enabled": true,
  "detection": {
    "mode": "token",
    "token": "LAMPADAIRE",
    "path": "",
    "extensions": [".shp", ".gpkg", ".geojson"]
  },
  "geometry": {
    "buffer_m": 15,
    "dissolve": false
  },
  "field_filter": "",
  "distance_weighting": {
    "enabled": true,
    "bands": [
      {"min": 0,  "max": 10, "weight": 3.0},
      {"min": 10, "max": 30, "weight": 2.0},
      {"min": 30, "max": 60, "weight": 1.3}
    ]
  },
  "required": false
}
```

Key fields:

- **`source_key`** — unique identifier, matching exactly the `SOURCE` value of
  the classification-table rows for this source. This is the bridge to the
  existing table.
- **`detection.mode`** — `token` (fuzzy search in `base_dir` through
  `find_files_fuzzy`), `path` (explicit file or directory) or `layer` (a layer
  already loaded in the QGIS project).
- **`geometry.buffer_m`** — buffer radius in metres. **Mandatory for point
  datasets**: an unbuffered point produces almost no pixel at rasterisation. A
  street light becomes a disc of influence.
- **`field_filter`** — optional SQL expression applied at load time to reduce the
  layer (for example `"etat" = 'En service'`).
- **`distance_weighting.bands`** — `min` / `max` / `weight` bands, in the same
  format as the slope and building weightings. Applies a decreasing
  multiplicative factor in rings around the footprint.
- **`required`** — when `false`, a missing dataset never blocks the run; it only
  raises a warning.

**Separation of concerns.** The *base* friction of the footprint comes from the
classification table (`FRICTION_VALUE`); the *decay around it* comes from
`distance_weighting`. The object and its halo are modelled independently.

---

## 4. Components created or modified

### 4.1 New module — `fricmaps_plugin/core/custom_sources.py`

The core of the system, exposing two entry points:

- `load_custom_sources_config(raw)` — parses and validates the list of
  definitions (types, defaults, unique keys).
- `process_generic_source(...)` — the generic loader, chaining:
  1. **Location** — `find_files_fuzzy(base_dir, token)`, explicit path, or QGIS layer
  2. **Merge and reprojection** to the study CRS — `merge_vector_layers_from_paths`
  3. **Clipping** to the extent — `clip_layer`
  4. **Optional SQL filter** — `native:extractbyexpression`
  5. **Buffering** when `buffer_m > 0` — `native:buffer`
  6. **Optional dissolve**
  7. Returns a ready `QgsVectorLayer`, tagged with `source_key`

The module reuses the helpers already present in `utils.py`, which guarantees
consistency with the built-in sources. A guard warns explicitly when the geometry
is a point and `buffer_m` is zero, since the object would otherwise be invisible
in the raster.

### 4.2 Generalised distance weighting — `fricmaps_plugin/core/raster_processing.py`

`apply_building_distance_weighting` was already generic in its mechanics: it
takes a class code, computes pixel-to-pixel distance through `gdal:proximity`,
then applies weights per band. It was generalised to:

```python
def apply_class_distance_weighting(path_friction, path_classes, target_class_code,
                                   weight_bands, path_output, max_friction=10000):
    ...
```

`apply_building_distance_weighting` remains as a thin backward-compatibility
wrapper, so no existing call site is broken. The generic function is then called
in a loop for every rule, chaining its outputs exactly as slope and building
weightings already chained.

The `target_class_code` is **resolved automatically** from the loaded table: the
rows whose `SOURCE` or `CLASS_NAME` matches the rule target are looked up and
their `COMPILATION_ORDER` is used. The user never types a class code by hand.

### 4.3 Wiring the custom sources — `fricmaps_plugin/core/full_pipeline.py`

- New `custom_sources` parameter (default `None`, preserving current behaviour).
- After the core `vector_sources` dictionary is assembled, iterate over
  `custom_sources` → `process_generic_source` → add to the dictionary. Each
  failure is non-blocking, logged in the same way as the built-in sources.
- Rasterisation step: **unchanged**, since it is already source-agnostic.
- Weighting stage: iterate over the weighting rules and chain
  `apply_class_distance_weighting`.

### 4.4 Configurable dataset verification — `fricmaps_plugin/core/utils_check.py`

Custom sources must not abort the run when absent. With `required: false` they
are excluded from blocking checks and only produce a warning. The core datasets
(BD TOPO, OCS GE, RPG) remain mandatory.

### 4.5 User interface — `fricmaps_plugin/dialog.py`

Two mechanisms, deliberately separated:

- **Custom sources** are declared in a dedicated pop-up opened from tab 1
  (*Vector Processing*), keeping source declaration next to the data-input
  settings it belongs to.
- **All weighting** is centralised in tab 3, where each rule is an accordion
  card able to target any class or custom source.
- **Key unlock**: `SourceDelegate.items` became **dynamic**. The list is the
  built-in sources plus the declared custom keys, so the `SOURCE` drop-down of
  the table editor updates as soon as a custom source is added.
- `save_config` / `load_config` serialise and restore `custom_sources` in the
  same JSON as the other settings.

### 4.6 Parameter transit — `fricmaps_plugin/processing_algorithm.py`

`CUSTOM_SOURCES` and `WEIGHTING_RULES` are added to the parameters forwarded to
`run_pipeline`.

---

## 5. End-to-end flow — the street-light case

1. The user places the data in `base_dir` (for example `Lampadaires_EMS.gpkg`).
2. Tab 1 → **Custom Sources…** → *Add*: key `LAMPADAIRES`, detection by file or
   token, buffer `15 m`.
3. Tab 2 (*Rasterization*) → *Add row*: `CLASS_NAME=LAMPADAIRE`,
   `SOURCE=LAMPADAIRES` (now present in the drop-down), `SQL_FILTER` empty or a valid
   SQL expression, `FRICTION_VALUE=200`.
4. Tab 3 (*Weighting*) → **Add a weighting**: type *Distance to a layer/class*,
   target `LAMPADAIRE`, bands `0–10: ×3`, `10–30: ×2`, `30–60: ×1.3`.
5. **Run**:
   - `process_generic_source` loads, reprojects, clips and buffers by 15 m
   - `rasterize_classes_and_friction` burns the class and friction 200
   - `apply_class_distance_weighting` applies the decreasing rings
6. **Result**: street lights integrated natively, with a decreasing light halo,
   without a single line of user code.

---

## 6. Backward compatibility

- All new parameters have neutral defaults (`None` / `[]`), so an existing
  project runs unchanged.
- No existing signature is broken; backward-compatibility wrappers are kept.
- Existing classification tables remain valid without modification.
- Earlier JSON configurations load without error: the `custom_sources` key is
  simply absent.

---

## 7. Design notes and open questions

- **Point rasterisation.** Without a buffer, `gdal:rasterize` produces almost
  nothing, hence the mandatory buffer for point geometries.
- **Compilation order.** Custom sources overwrite according to
  `COMPILATION_ORDER` like every other class. A barrier must be given a high
  order to remain visible.
- **Performance.** Buffering and `gdal:proximity` on very large point datasets is
  costly, so the loader clips **before** buffering to limit the volume processed.
- **CRS.** Systematic reprojection to the study CRS inside the loader.
- **Scenarios.** The scenario class codes remain fixed. Including a custom source
  in the *no barriers* scenario would require a per-source `is_barrier` flag;
  this is a candidate for a future release.
