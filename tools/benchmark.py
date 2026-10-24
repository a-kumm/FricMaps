#!/usr/bin/env python3
"""Repeatable performance benchmark of the FricMaps pipeline (headless).

Each run is executed in a fresh ``qgis_process`` process, so that the peak memory of one
run is not polluted by the previous one and every run starts from the same state. The
plugin writes its own report (stage timings, memory, environment, workload); this script
adds the external wall-clock time and the peak memory of the child process, repeats the
runs, and aggregates them (n, median, mean, SD, min-max).

Two sweeps are run around a reference configuration:

* resolution: ``--resolutions`` at the reference buffer;
* area: ``--buffers`` (buffer around the study area) at the reference resolution.

Typical use::

    qgis_process plugins enable fricmaps_plugin        # once
    python tools/benchmark.py --config my_gui_config.json \\
        --machine-label "Ubuntu 24.04, Core Ultra 7 155H, 16 GB" --storage "NVMe SSD"

``--config`` reads a configuration saved from the plugin interface ("Save Config").
Add ``--dry-run`` to print the planned runs and the estimated grid sizes without running.

Outputs (in ``--results-dir``): ``runs.jsonl`` (one record per run, written as it goes, so
an interrupted benchmark keeps its data), ``runs.csv``, ``summary.csv``, ``summary.tex``,
``stages_by_config.csv`` and ``environment.json``.

Only the standard library is required; ``osgeo`` is used, when available, to estimate
the grid size before a run.
"""

import argparse
import csv
import datetime
import glob
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

ALGORITHM = "fricmaps:build_surfaces"
GIB = 1024**3

Config = Tuple[float, float]  # (resolution in map units, buffer in map units)


# =====================================================================
# 1) Configuration
# =====================================================================


def load_gui_config(path: str) -> Dict[str, Any]:
    """Map a configuration saved by the plugin interface onto the options of this script."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    mapped = {
        "study_area": raw.get("epci_file"),
        "name_field": raw.get("name_field"),
        "area_name": raw.get("area_name"),
        "base_dir": raw.get("base_dir"),
        "table_csv": raw.get("csv_path"),
        "building_code": raw.get("building_code"),
        "save_vectors": raw.get("save_vectors"),
        "verify_data": raw.get("verify_data"),
        "default_buffer": raw.get("buffer_dist"),
        "default_resolution": raw.get("resolution"),
    }
    for key in ("weighting_rules", "custom_sources"):
        if raw.get(key) is not None:
            mapped[key] = json.dumps(raw[key])
    return {k: v for k, v in mapped.items() if v not in (None, "")}


def build_matrix(
    resolutions: Iterable[float],
    buffers: Iterable[float],
    default_resolution: float,
    default_buffer: float,
) -> List[Config]:
    """Resolution sweep at the reference buffer, then buffer sweep at the reference resolution."""
    seen: List[Config] = []
    for res in resolutions:
        if (res, default_buffer) not in seen:
            seen.append((res, default_buffer))
    for buf in buffers:
        if (default_resolution, buf) not in seen:
            seen.append((default_resolution, buf))
    return seen


def estimate_grid(
    study_area: str, name_field: str, area_name: str, buffer_m: float, resolution: float
) -> Optional[Tuple[int, int]]:
    """Rows and columns of the grid the pipeline will build; ``None`` if it cannot be told.

    Mirrors the pipeline: union of the features whose name matches (case-insensitive,
    trimmed), buffered, then the bounding box divided by the resolution.
    """
    try:
        from osgeo import ogr

        ogr.UseExceptions()
        path, _, layer_name = study_area.partition("|layername=")
        ds = ogr.Open(path)
        layer = ds.GetLayerByName(layer_name) if layer_name else ds.GetLayer(0)
        wanted = str(area_name).strip().lower()
        parts = ogr.Geometry(ogr.wkbMultiPolygon)
        for feature in layer:
            value = feature.GetField(name_field)
            geom = feature.GetGeometryRef()
            if value is None or geom is None or str(value).strip().lower() != wanted:
                continue
            multi = ogr.ForceToMultiPolygon(geom.Clone())
            for i in range(multi.GetGeometryCount()):
                parts.AddGeometry(multi.GetGeometryRef(i))
        if parts.IsEmpty():
            return None
        union = parts.UnionCascaded()
        if buffer_m > 0:
            union = union.Buffer(buffer_m, 30)
        min_x, max_x, min_y, max_y = union.GetEnvelope()
        return int((max_y - min_y) / resolution), int((max_x - min_x) / resolution)
    except Exception:
        return None


def qgis_process_command(qgis_process: str, params: Dict[str, Any], run_dir: str) -> List[str]:
    """Command line of one run (values after ``--`` use the PARAMETER=VALUE syntax)."""
    values = dict(params)
    values["OUTPUT_DIR"] = run_dir
    cmd = [qgis_process, "run", ALGORITHM, "--"]
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        cmd.append(f"{key}={value}")
    return cmd


# =====================================================================
# 2) Running
# =====================================================================


def _wait(proc: "subprocess.Popen[bytes]", timeout: Optional[float]):
    """Wait for the child. Returns ``(return_code, peak_rss_bytes, timed_out)``.

    On Unix the peak RSS of the child comes from ``wait4``; elsewhere it is unknown (the
    plugin's own report still carries the process peak).
    """
    if not hasattr(os, "wait4"):
        try:
            return proc.wait(timeout=timeout), None, False
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return None, None, True
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
        if pid:
            code = os.waitstatus_to_exitcode(status)
            proc.returncode = code
            scale = 1 if sys.platform == "darwin" else 1024  # ru_maxrss: bytes vs KiB
            return code, int(usage.ru_maxrss) * scale, False
        if deadline is not None and time.monotonic() > deadline:
            proc.kill()
            _, status, _ = os.wait4(proc.pid, 0)
            proc.returncode = os.waitstatus_to_exitcode(status)
            return None, None, True
        time.sleep(0.25)


def find_report(run_dir: str) -> Optional[str]:
    hits = sorted(glob.glob(os.path.join(run_dir, "intermediate", "Performance_*.json")))
    return hits[0] if hits else None


def run_once(
    cmd: List[str], env: Dict[str, str], run_dir: str, log_path: str, timeout: Optional[float]
) -> Dict[str, Any]:
    os.makedirs(run_dir, exist_ok=True)
    started = time.perf_counter()
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env)
        code, child_peak, timed_out = _wait(proc, timeout)
    wall = time.perf_counter() - started
    record: Dict[str, Any] = {
        "return_code": code,
        "wall_seconds": round(wall, 3),
        "child_peak_rss_bytes": child_peak,
        "log": log_path,
    }
    report_path = find_report(run_dir)
    report = None
    if report_path:
        try:
            with open(report_path, encoding="utf-8") as fh:
                report = json.load(fh)
        except (OSError, ValueError):
            report = None
    if timed_out:
        record["status"] = "timeout"
    elif report is not None:
        record["status"] = report.get("status", "unknown")
    else:
        record["status"] = "failed" if code else "no_report"
    if report is not None:
        record["pipeline_seconds"] = report.get("total_seconds")
        record["peak_rss_bytes"] = (report.get("memory") or {}).get("peak_rss_bytes")
        record["workload"] = report.get("workload", {})
        record["stages"] = {s["name"]: s["seconds"] for s in report.get("stages", [])}
        record["environment"] = report.get("environment")
    return record


# =====================================================================
# 3) Aggregation and output
# =====================================================================


def _stats(values: List[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {"median": None, "mean": None, "sd": None, "min": None, "max": None}
    return {
        "median": statistics.median(values),
        "mean": statistics.fmean(values),
        "sd": statistics.stdev(values) if len(values) > 1 else None,
        "min": min(values),
        "max": max(values),
    }


def aggregate(runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One row per (resolution, buffer): counts, timing statistics and peak memory."""
    groups: Dict[Config, List[Dict[str, Any]]] = {}
    for run in runs:
        groups.setdefault((run["resolution_m"], run["buffer_m"]), []).append(run)
    rows = []
    for (res, buf), items in groups.items():
        items.sort(key=lambda r: r["repeat"])
        ok = [r for r in items if r["status"] == "completed" and r.get("pipeline_seconds")]
        times = [float(r["pipeline_seconds"]) for r in ok]
        # Peak memory and grid size also cover failed runs: they show where a limit was hit.
        peaks = [r["peak_rss_bytes"] for r in items if r.get("peak_rss_bytes")]
        child_peaks = [r["child_peak_rss_bytes"] for r in items if r.get("child_peak_rss_bytes")]
        pixels = next((r["workload"].get("grid_pixels") for r in items if r.get("workload")), None)
        valid = next((r["workload"].get("valid_pixels") for r in ok if r.get("workload")), None)
        row = {
            "resolution_m": res,
            "buffer_m": buf,
            "grid_pixels": pixels,
            "valid_pixels": valid,
            "n_requested": len(items),
            "n_completed": len(ok),
            "n_failed": len(items) - len(ok),
            "first_run_seconds": times[0] if times else None,
            "peak_rss_bytes_max": max(peaks) if peaks else None,
            "child_peak_rss_bytes_max": max(child_peaks) if child_peaks else None,
            "failure_statuses": sorted({r["status"] for r in items if r not in ok}),
        }
        row.update({f"pipeline_s_{k}": v for k, v in _stats(times).items()})
        rows.append(row)
    rows.sort(key=lambda r: (r["buffer_m"], r["resolution_m"]))
    return rows


def stage_medians(runs: List[Dict[str, Any]]) -> Dict[str, Dict[Config, float]]:
    """Median duration of each stage, per configuration (completed runs only)."""
    collected: Dict[str, Dict[Config, List[float]]] = {}
    for run in runs:
        if run["status"] != "completed":
            continue
        for name, seconds in (run.get("stages") or {}).items():
            collected.setdefault(name, {}).setdefault(
                (run["resolution_m"], run["buffer_m"]), []
            ).append(float(seconds))
    return {
        name: {cfg: statistics.median(vals) for cfg, vals in per_cfg.items()}
        for name, per_cfg in collected.items()
    }


def _fmt(value: Optional[float], digits: int = 1) -> str:
    return "--" if value is None else f"{value:.{digits}f}"


def latex_table(rows: List[Dict[str, Any]]) -> str:
    """A ``booktabs`` tabular: median [min--max] of the pipeline time, per configuration."""
    lines = [
        r"\begin{tabular}{rrrrrrr}",
        r"\toprule",
        r"Resolution (m) & Buffer (km) & Grid (M) & Valid (M) & $n$ & Time, median [min--max] (s) "
        r"& Peak RSS (GiB) \\",
        r"\midrule",
    ]
    for r in rows:
        pixels = None if r["grid_pixels"] is None else r["grid_pixels"] / 1e6
        valid = None if r.get("valid_pixels") is None else r["valid_pixels"] / 1e6
        peak = None if r["peak_rss_bytes_max"] is None else r["peak_rss_bytes_max"] / GIB
        n = f"{r['n_completed']}/{r['n_requested']}"
        if r["pipeline_s_median"] is None:
            timing = "--"
        else:
            timing = (
                f"{_fmt(r['pipeline_s_median'])} "
                f"[{_fmt(r['pipeline_s_min'])}--{_fmt(r['pipeline_s_max'])}]"
            )
        lines.append(
            f"{r['resolution_m']:g} & {r['buffer_m'] / 1000:g} & {_fmt(pixels)} & "
            f"{_fmt(valid)} & {n} & {timing} & {_fmt(peak, 2)} \\\\"
        )
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines)


def write_csv(path: str, rows: List[Dict[str, Any]], fields: Optional[List[str]] = None) -> None:
    if not rows and not fields:
        return
    fields = fields or list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in row.items()}
            )


def write_outputs(results_dir: str, runs: List[Dict[str, Any]], declared: Dict[str, Any]) -> None:
    rows = aggregate(runs)
    write_csv(os.path.join(results_dir, "summary.csv"), rows)
    with open(os.path.join(results_dir, "summary.tex"), "w", encoding="utf-8") as fh:
        fh.write(latex_table(rows) + "\n")
    flat_fields = [
        "run_order",
        "resolution_m",
        "buffer_m",
        "repeat",
        "status",
        "return_code",
        "wall_seconds",
        "pipeline_seconds",
        "peak_rss_bytes",
        "child_peak_rss_bytes",
    ]
    write_csv(os.path.join(results_dir, "runs.csv"), runs, flat_fields)
    medians = stage_medians(runs)
    configs = sorted({cfg for per in medians.values() for cfg in per}, key=lambda c: (c[1], c[0]))
    header = ["stage"] + [f"res{c[0]:g}_buf{c[1]:g}" for c in configs]
    with open(
        os.path.join(results_dir, "stages_by_config.csv"), "w", newline="", encoding="utf-8"
    ) as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for name, per in medians.items():
            writer.writerow([name] + [_fmt(per.get(c), 2) for c in configs])
    env = next((r["environment"] for r in runs if r.get("environment")), None)
    with open(os.path.join(results_dir, "environment.json"), "w", encoding="utf-8") as fh:
        json.dump({"declared": declared, "reported_by_plugin": env}, fh, indent=2, default=str)


def format_summary(rows: List[Dict[str, Any]]) -> str:
    head = (
        f"{'res (m)':>8}{'buffer (km)':>12}{'grid (M)':>10}{'valid (M)':>11}{'ok/n':>7}"
        f"{'median (s)':>12}{'min':>9}{'max':>9}{'SD':>8}{'peak (GiB)':>12}"
    )
    lines = [head]
    for r in rows:
        pixels = None if r["grid_pixels"] is None else r["grid_pixels"] / 1e6
        valid = None if r.get("valid_pixels") is None else r["valid_pixels"] / 1e6
        peak = None if r["peak_rss_bytes_max"] is None else r["peak_rss_bytes_max"] / GIB
        lines.append(
            f"{r['resolution_m']:>8g}{r['buffer_m'] / 1000:>12g}{_fmt(pixels):>10}"
            f"{_fmt(valid):>11}{str(r['n_completed']) + '/' + str(r['n_requested']):>7}"
            f"{_fmt(r['pipeline_s_median']):>12}{_fmt(r['pipeline_s_min']):>9}"
            f"{_fmt(r['pipeline_s_max']):>9}{_fmt(r['pipeline_s_sd']):>8}{_fmt(peak, 2):>12}"
        )
    return "\n".join(lines)


# =====================================================================
# 4) Command line
# =====================================================================


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--config", help="configuration saved from the plugin interface (JSON)")
    p.add_argument("--study-area", help="study-area layer (file, or path|layername=name)")
    p.add_argument("--name-field", help="attribute holding the area names")
    p.add_argument("--area-name", help="value of the area to process")
    p.add_argument("--base-dir", help="folder holding the input datasets")
    p.add_argument("--table-csv", help="classification table (default: the plugin's)")
    p.add_argument("--weighting-rules", help="weighting rules as JSON text, or a .json file")
    p.add_argument("--custom-sources", help="custom sources as JSON text, or a .json file")
    p.add_argument("--building-code", type=int)
    p.add_argument("--save-vectors", dest="save_vectors", action="store_true", default=None)
    p.add_argument("--no-save-vectors", dest="save_vectors", action="store_false")
    p.add_argument("--no-verify-data", dest="verify_data", action="store_false", default=None)
    p.add_argument("--resolutions", type=float, nargs="+", default=[2, 5, 10, 25])
    p.add_argument("--buffers", type=float, nargs="+", default=[0, 5000, 10000, 20000])
    p.add_argument("--default-resolution", type=float, default=None)
    p.add_argument("--default-buffer", type=float, default=None)
    p.add_argument("--repeats", type=int, default=5)
    p.add_argument("--heavy-repeats", type=int, default=3)
    p.add_argument("--heavy-pixels", type=float, default=1e8)
    p.add_argument("--max-pixels", type=float, default=None, help="skip larger grids")
    p.add_argument("--timeout", type=float, default=None, help="seconds, per run")
    p.add_argument("--qgis-process", default="qgis_process")
    p.add_argument("--plugin-path", help="folder holding fricmaps_plugin/ (QGIS_PLUGINPATH)")
    p.add_argument("--results-dir", default="benchmark_results")
    p.add_argument("--work-dir", help="where runs write their outputs (default: results-dir)")
    p.add_argument("--keep-outputs", action="store_true", help="do not delete run folders")
    p.add_argument("--machine-label", default="", help="free text: machine and OS")
    p.add_argument("--storage", default="", help="free text: SSD, NVMe, HDD, network...")
    p.add_argument("--note", default="")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    if args.config:
        if not os.path.isfile(args.config):
            p.error(f"configuration file not found: {args.config}")
        try:
            gui_config = load_gui_config(args.config)
        except ValueError as exc:
            p.error(f"{args.config} is not a valid configuration file: {exc}")
        for key, value in gui_config.items():
            if getattr(args, key, None) is None:
                setattr(args, key, value)
    for key in ("weighting_rules", "custom_sources"):
        value = getattr(args, key, None)
        if value and os.path.isfile(value):
            with open(value, encoding="utf-8") as fh:
                setattr(args, key, json.dumps(json.load(fh)))
    missing = [
        k
        for k in ("study_area", "name_field", "area_name", "base_dir")
        if not getattr(args, k, None)
    ]
    if missing:
        p.error("missing: " + ", ".join(missing) + " (give them, or use --config)")
    study_area_file = args.study_area.partition("|layername=")[0]
    if not os.path.isfile(study_area_file):
        p.error(f"study-area file not found: {study_area_file}")
    if args.default_resolution is None:
        args.default_resolution = 5.0
    if args.default_buffer is None:
        args.default_buffer = 5000.0
    return args


def algorithm_parameters(
    args: argparse.Namespace, resolution: float, buffer_m: float
) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "EPCI_FILE": args.study_area,
        "NAME_FIELD": args.name_field,
        "AREA_NAME": args.area_name,
        "BASE_DIR": args.base_dir,
        "BUFFER_DIST": buffer_m,
        "RESOLUTION": resolution,
        "TABLE_CSV": args.table_csv,
        "BUILDING_CODE": getattr(args, "building_code", None),
        "SAVE_VECTORS": args.save_vectors,
        "VERIFY_DATA": args.verify_data,
        "WEIGHTING_RULES": getattr(args, "weighting_rules", None),
        "CUSTOM_SOURCES": getattr(args, "custom_sources", None),
    }
    return {k: v for k, v in params.items() if v is not None}


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    matrix = build_matrix(
        args.resolutions, args.buffers, args.default_resolution, args.default_buffer
    )
    plan = []
    for res, buf in matrix:
        grid = estimate_grid(args.study_area, args.name_field, args.area_name, buf, res)
        pixels = None if grid is None else grid[0] * grid[1]
        skip = args.max_pixels is not None and pixels is not None and pixels > args.max_pixels
        heavy = pixels is not None and pixels > args.heavy_pixels
        plan.append(
            {
                "resolution": res,
                "buffer": buf,
                "pixels": pixels,
                "skip": skip,
                "repeats": args.heavy_repeats if heavy else args.repeats,
            }
        )

    print(f"{'res (m)':>8}{'buffer (m)':>12}{'est. pixels (M)':>17}{'repeats':>9}  action")
    for item in plan:
        est = "unknown" if item["pixels"] is None else f"{item['pixels'] / 1e6:.1f}"
        action = "SKIP (> --max-pixels)" if item["skip"] else "run"
        print(
            f"{item['resolution']:>8g}{item['buffer']:>12g}{est:>17}{item['repeats']:>9}  {action}"
        )
    if args.dry_run:
        return 0
    if shutil.which(args.qgis_process) is None and not os.path.isfile(args.qgis_process):
        print(f"qgis_process not found: {args.qgis_process} (use --qgis-process)", file=sys.stderr)
        return 2

    results_dir = os.path.abspath(args.results_dir)
    work_root = os.path.abspath(args.work_dir or results_dir)
    os.makedirs(results_dir, exist_ok=True)
    env = dict(os.environ)
    if args.plugin_path:
        env["QGIS_PLUGINPATH"] = os.path.abspath(args.plugin_path)
    if sys.platform.startswith("linux") and not env.get("DISPLAY"):
        env.setdefault("QT_QPA_PLATFORM", "offscreen")

    declared = {
        "machine_label": args.machine_label,
        "storage": args.storage,
        "note": args.note,
        "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "benchmark_host": platform.platform(),
        "repeats": args.repeats,
        "heavy_repeats": args.heavy_repeats,
        "qgis_process": args.qgis_process,
    }
    jsonl_path = os.path.join(results_dir, "runs.jsonl")
    runs: List[Dict[str, Any]] = []
    order = 0
    with open(jsonl_path, "a", encoding="utf-8") as jsonl:
        for item in plan:
            if item["skip"]:
                continue
            params = algorithm_parameters(args, item["resolution"], item["buffer"])
            for repeat in range(item["repeats"]):
                order += 1
                tag = f"res{item['resolution']:g}_buf{item['buffer']:g}_r{repeat}"
                run_dir = os.path.join(work_root, "run_" + tag)
                cmd = qgis_process_command(args.qgis_process, params, run_dir)
                print(f"[{order}] {tag} ...", flush=True)
                record = run_once(
                    cmd, env, run_dir, os.path.join(results_dir, f"{tag}.log"), args.timeout
                )
                record.update(
                    {
                        "run_order": order,
                        "resolution_m": item["resolution"],
                        "buffer_m": item["buffer"],
                        "repeat": repeat,
                    }
                )
                jsonl.write(json.dumps(record, default=str) + "\n")
                jsonl.flush()
                runs.append(record)
                print(
                    f"    {record['status']}: pipeline {_fmt(record.get('pipeline_seconds'))} s, "
                    f"wall {record['wall_seconds']} s",
                    flush=True,
                )
                if not args.keep_outputs:
                    shutil.rmtree(run_dir, ignore_errors=True)
    write_outputs(results_dir, runs, declared)
    print()
    print(format_summary(aggregate(runs)))
    print(f"\nResults written to {results_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
