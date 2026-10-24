# Measuring performance

FricMaps records how a run behaves, and ships two small tools to repeat runs and to
check that two sets of outputs are the same. Everything needed to interpret a timing
(machine, software versions, amount of work) is stored with it.

## What every run records

Each run writes `intermediate/Performance_<area>.json` and prints a one-screen summary
in the log. The report is also returned by the Processing algorithm as
`OUTPUT_PERFORMANCE` (see [`SCRIPTING.md`](SCRIPTING.md)). It is written whether the run
completes or fails, so that the cost of a run that ran out of memory is not lost.

| Section | Content |
|---------|---------|
| `status`, `total_seconds` | `completed` or `failed`; wall-clock time of the pipeline |
| `stages` | duration, share of the total, and memory at the end of each stage |
| `memory` | resident memory (RSS) at start, peak, and peak above the start |
| `workload` | resolution, grid rows × columns, `grid_pixels`, grid and processing area (km²), `valid_pixels` and `nodata_pixels`, classification-table rows, features per source, pixels of the barrier classes |
| `parameters` | study area, buffer, resolution, switches, number of weighting rules and custom sources |
| `environment` | plugin, QGIS, GDAL, Qt, Python and NumPy versions; OS; CPU model; logical cores; RAM; `GDAL_NUM_THREADS` and other thread variables; GDAL cache size; file system, free space and, on Linux, whether the disk is rotational |

The grid is the bounding box of the buffered study area, so part of it (the corners, and
anything outside the buffered polygon) has no land-cover class: those pixels are NoData.
`grid_pixels` counts the whole grid, `valid_pixels` only the pixels that carry a class.
Report which of the two a quoted pixel count refers to.

A stage starts at one progress message and ends at the next: the stage called
*Processing Land Cover (OCS GE)...* is the work done after that message.

### How memory is measured

The figure is the resident set size of the whole process (Linux `/proc`, macOS
`ps`/`getrusage`, Windows `psapi`). It therefore includes GDAL and GEOS allocations, and,
when the plugin runs inside QGIS, QGIS itself. The peak is cumulative: it is the highest
value reached since the process started. For a figure that describes FricMaps alone, run
headless (below) and report the peak, not a difference.

The macOS and Windows probes are implemented but have so far only been exercised on
Linux.

## Repeating runs: `tools/benchmark.py`

Each run is a separate `qgis_process` process, so that one run's memory does not leak
into the next and every run starts from the same state.

```bash
# once: make the plugin available to qgis_process
qgis_process plugins enable fricmaps_plugin

# --config is a configuration saved with "Save Config" in the plugin interface
python tools/benchmark.py \
    --config my_gui_config.json \
    --machine-label "Ubuntu 24.04, Core Ultra 7 155H, 16 GB" \
    --storage "NVMe SSD" \
    --repeats 5 --results-dir benchmark_results
```

`--config` takes a configuration saved from the interface, so the benchmark runs the
analysis you actually care about, weighting rules and custom sources included. Any
option given on the command line overrides it. Use `--dry-run` first: it prints the grid
size of every planned run without executing anything.

Two sweeps are made around the reference configuration (`--default-resolution`,
`--default-buffer`; 5 m and 5 km by default):

* **resolution**: `--resolutions 2 5 10 25`;
* **area**: `--buffers 0 5000 10000 20000`, the buffer drawn around the study area.

Useful options: `--max-pixels` skips grids that would not fit in memory; `--heavy-pixels`
and `--heavy-repeats` lower the number of repetitions of the largest grids; `--timeout`
bounds a run; `--plugin-path` points `qgis_process` at a checkout; `--keep-outputs`
keeps the rasters of each run (they are deleted by default).

Failed runs are kept, with their peak memory, not discarded: they are the information
about where the limits are.

| File | Content |
|------|---------|
| `runs.jsonl`, `runs.csv` | one record per run, written as the benchmark proceeds |
| `summary.csv` | per configuration: n, median, mean, SD, min, max, first run, peak memory |
| `summary.tex` | the same as a `booktabs` table, ready for a paper |
| `stages_by_config.csv` | median duration of each stage, per configuration |
| `environment.json` | what you declared (machine, storage, note) and what the plugin detected |

### Reporting guidelines

* Say how many runs a figure rests on and give a spread (min–max or SD), not a single
  value. The **first** run of a series is often slower (cold file cache); it is reported
  separately as `first_run_seconds`.
* Storage type cannot be detected reliably on every OS: declare it with `--storage`.
* QGIS Processing writes its temporary files under the temporary folder, i.e. `$TMPDIR` or
  `/tmp` (`%TEMP%` on Windows). The per-class masks of the rasterisation are deleted as soon
  as they have been read, so the space needed no longer depends on the number of classes;
  other intermediate products can still be large. Where `/tmp` is a `tmpfs` (the default on
  recent Ubuntu releases) these files live in RAM and compete with the run for memory. For
  comparable figures, point `TMPDIR` at a folder on disk and declare it. The report records
  the volume of the temporary folder (`environment.temp_volume`).
* Timings from machines with different hardware must not be read as a comparison of
  operating systems.
* Close other applications and use mains power; note anything unusual with `--note`.
* The pipeline is single-process (`num_workers` is not used); native libraries may use
  threads, which the report records.

## Checking that two outputs are the same: `tools/compare_rasters.py`

```bash
python tools/compare_rasters.py outputs_run1 outputs_run2          # same file names
python tools/compare_rasters.py --pair a/Land_Cover_x.tif b/Raster_Classe_x.tif
```

It compares dimensions, georeferencing, type, and every pixel, in strips so that large
rasters fit in memory. For differing pixels it lists the most frequent *value in A → value
in B* transitions, which usually shows at once which class or rule differs. The exit
status is 0 only when everything is identical.

Two uses: two runs of the plugin must be identical (determinism), and the outputs of
another implementation can be checked against the plugin's on the same inputs.

## Tests

```bash
python -m unittest discover -s tests -v
```

The recorder tests need only the standard library. The tool tests need GDAL and NumPy
(any QGIS Python) and are skipped without them.
