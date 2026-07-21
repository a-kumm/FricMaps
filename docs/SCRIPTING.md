# Running FricMaps from a script

The whole workflow is exposed as a QGIS Processing algorithm,
`fricmaps:build_surfaces`. Anything the graphical interface does can therefore
be run headlessly, which is what makes a published analysis reproducible: the
snippet below is the complete specification of a run, and re-executing it on the
same input data reproduces the same rasters.

The algorithm is also listed in the Processing toolbox under **FricMaps**, so it
can be chained inside a Processing model or a batch run.

## Minimal example

```python
import processing

results = processing.run(
    "fricmaps:build_surfaces",
    {
        # --- study area -----------------------------------------------------
        "EPCI_FILE": "/data/study_area.gpkg",
        "NAME_FIELD": "nom",
        "AREA_NAME": "Eurometropole de Strasbourg",
        # --- input and output directories -----------------------------------
        "BASE_DIR": "/data/sources",       # BD TOPO, OCS GE, RPG, RGE ALTI...
        "OUTPUT_DIR": "/data/outputs",
        # --- processing parameters ------------------------------------------
        "BUFFER_DIST": 5000.0,             # metres around the study area
        "RESOLUTION": 5.0,                 # output pixel size, metres
        "TABLE_CSV": "/data/Table_Raster.csv",
        "BUILDING_CODE": 29,
        "VERIFY_DATA": True,
        "SAVE_VECTORS": True,
        "ONLY_VECTORS": False,
        "SKIP_VECTORS": False,
        # --- weighting rules (JSON) -----------------------------------------
        "WEIGHTING_RULES": "[]",
        "CUSTOM_SOURCES": "[]",
        "SLOPE_WEIGHTS": "[]",
        "BUILDING_WEIGHTS": "[]",
    },
)

print(results["OUTPUT_FRICTION_WEIGHTED"])
```

## Returned values

The algorithm returns the path of every deliverable, so a script never has to
rebuild filenames:

| Key | Content |
|-----|---------|
| `OUTPUT_FOLDER_PATH` | Output directory |
| `OUTPUT_LAND_COVER` | Land-cover raster |
| `OUTPUT_FRICTION` | Base friction raster |
| `OUTPUT_FRICTION_WEIGHTED` | Friction surface after weighting |
| `OUTPUT_SCENARIO_NO_FENCES` | Scenario without fences |
| `OUTPUT_SCENARIO_NO_LTI` | Scenario without linear transport infrastructure |
| `OUTPUT_SCENARIO_NO_BARRIERS` | Scenario without either |

A vector-only run (`ONLY_VECTORS: True`) produces no raster, so those keys are
simply absent from the result.

## Weighting rules and custom sources

Both parameters take JSON, in the same form the interface writes them, which
means a configuration built interactively can be pasted straight into a script.

```python
"WEIGHTING_RULES": json.dumps([
    {
        "type": "distance",
        "target": "LAMPADAIRE",
        "bands": [
            {"min": 0,  "max": 10, "weight": 3.0},
            {"min": 10, "max": 30, "weight": 2.0},
            {"min": 30, "max": 60, "weight": 1.3},
        ],
    },
    {
        "type": "slope",
        "bands": [
            {"min": 0,  "max": 30, "weight": 1.0},
            {"min": 30, "max": 40, "weight": 10.0},
            {"min": 40, "max": 90, "weight": 1000.0},
        ],
    },
])
```

See [`GENERIC_SOURCES_ARCHITECTURE.md`](GENERIC_SOURCES_ARCHITECTURE.md) for the
`CUSTOM_SOURCES` schema.

## Running outside the QGIS interface

The same call works from `qgis_process` or from a standalone PyQGIS script, so a
run can be scheduled or executed on a server without any graphical session.

```bash
qgis_process run fricmaps:build_surfaces \
    --EPCI_FILE=/data/study_area.gpkg \
    --NAME_FIELD=nom \
    --AREA_NAME="Eurometropole de Strasbourg" \
    --BASE_DIR=/data/sources \
    --OUTPUT_DIR=/data/outputs \
    --RESOLUTION=5.0
```
