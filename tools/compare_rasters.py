#!/usr/bin/env python3
"""Compare raster outputs pixel by pixel.

Two uses:

* determinism: two runs of the plugin on the same inputs must be identical;
* equivalence: the outputs of another implementation (for instance the standalone
  Python workflow of the paper) compared with the plugin's, on the same inputs.

Usage::

    python tools/compare_rasters.py DIR_A DIR_B
    python tools/compare_rasters.py --pair a/Land_Cover_x.tif b/Raster_Classe_x.tif
    python tools/compare_rasters.py DIR_A DIR_B --json report.json

With two folders, the ``*.tif`` files that carry the same name in both are compared.
Use ``--pair`` (repeatable) when the two implementations name their outputs differently.

The exit status is 0 when every pair is identical, 1 when at least one differs or cannot
be compared, 2 on usage errors. Rasters are read in strips, so memory use stays bounded
whatever their size. Requires GDAL (``osgeo``) and NumPy, as available in QGIS.
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

STRIP_ROWS = 1024
MAX_DISTINCT = 2000
TOP_TRANSITIONS = 8
#: Integer types for which "value in A -> value in B" transitions are tabulated.
_TRANSITION_TYPES = ("Byte", "UInt16", "Int16")
#: Encoding of a (value in A, value in B) pair as one integer: values lie in
#: [-32768, 65535], so shifting by 32768 keeps them below 2**20.
_SHIFT = 32768
_BASE = 1 << 20


def _band_info(ds: Any) -> Dict[str, Any]:
    from osgeo import gdal

    band = ds.GetRasterBand(1)
    return {
        "cols": ds.RasterXSize,
        "rows": ds.RasterYSize,
        "dtype": gdal.GetDataTypeName(band.DataType),
        "nodata": band.GetNoDataValue(),
        "geotransform": list(ds.GetGeoTransform()),
        "projection": ds.GetProjection(),
    }


def _same_projection(wkt_a: str, wkt_b: str) -> bool:
    if wkt_a == wkt_b:
        return True
    try:
        from osgeo import osr

        a, b = osr.SpatialReference(wkt=wkt_a), osr.SpatialReference(wkt=wkt_b)
        return bool(a.IsSame(b))
    except Exception:
        return False


def compare_pair(
    path_a: str,
    path_b: str,
    strip_rows: int = STRIP_ROWS,
    crop: bool = False,
    ignore_dtype: bool = False,
) -> Dict[str, Any]:
    """Compare band 1 of two rasters and return a JSON-serialisable summary.

    ``crop`` compares the window common to both grids (same origin and pixel size, one
    possibly a row or a column larger, as happens when a grid size is rounded up instead
    of down). ``ignore_dtype`` does not count a different pixel type as a difference.
    """
    import numpy as np
    from osgeo import gdal

    gdal.UseExceptions()
    out: Dict[str, Any] = {"a": path_a, "b": path_b, "comparable": False}
    ds_a, ds_b = gdal.Open(path_a), gdal.Open(path_b)
    info_a, info_b = _band_info(ds_a), _band_info(ds_b)
    out["a_info"] = {k: info_a[k] for k in ("cols", "rows", "dtype", "nodata")}
    out["b_info"] = {k: info_b[k] for k in ("cols", "rows", "dtype", "nodata")}
    out["same_shape"] = (info_a["cols"], info_a["rows"]) == (info_b["cols"], info_b["rows"])
    out["same_dtype"] = info_a["dtype"] == info_b["dtype"]
    out["geotransform_max_abs_diff"] = max(
        abs(x - y) for x, y in zip(info_a["geotransform"], info_b["geotransform"])
    )
    out["same_projection"] = _same_projection(info_a["projection"], info_b["projection"])
    cols, rows = info_a["cols"], info_a["rows"]
    if not out["same_shape"]:
        aligned = out["geotransform_max_abs_diff"] < 1e-9
        if not (crop and aligned):
            out["note"] = "different grid sizes: pixels cannot be compared"
            if crop and not aligned:
                out["note"] += " (and the grids do not share origin and pixel size)"
            return out
        cols, rows = min(info_a["cols"], info_b["cols"]), min(info_a["rows"], info_b["rows"])
        out["compared_window"] = {"cols": cols, "rows": rows}
    band_a, band_b = ds_a.GetRasterBand(1), ds_b.GetRasterBand(1)
    tabulate = info_a["dtype"] in _TRANSITION_TYPES and info_b["dtype"] in _TRANSITION_TYPES
    n_diff, max_abs = 0, 0.0
    values_a, values_b = set(), set()
    transitions: Dict[Tuple[int, int], int] = {}
    for y in range(0, rows, strip_rows):
        h = min(strip_rows, rows - y)
        a = band_a.ReadAsArray(0, y, cols, h)
        b = band_b.ReadAsArray(0, y, cols, h)
        differ = a != b
        if a.dtype.kind == "f" or b.dtype.kind == "f":
            differ &= ~(np.isnan(a) & np.isnan(b))
        count = int(differ.sum())
        if len(values_a) < MAX_DISTINCT:
            values_a.update(np.unique(a).tolist())
        if len(values_b) < MAX_DISTINCT:
            values_b.update(np.unique(b).tolist())
        if count:
            n_diff += count
            da, db = a[differ].astype(np.float64), b[differ].astype(np.float64)
            max_abs = max(max_abs, float(np.abs(da - db).max()))
            if tabulate:
                pairs = (a[differ].astype(np.int64) + _SHIFT) * _BASE + (
                    b[differ].astype(np.int64) + _SHIFT
                )
                keys, freq = np.unique(pairs, return_counts=True)
                for key, n in zip(keys.tolist(), freq.tolist()):
                    pair = (key // _BASE - _SHIFT, key % _BASE - _SHIFT)
                    transitions[pair] = transitions.get(pair, 0) + n
    total = cols * rows
    out.update(
        {
            "comparable": True,
            "pixels": total,
            "pixels_different": n_diff,
            "percent_identical": round(100.0 * (total - n_diff) / total, 6) if total else None,
            "max_abs_diff": max_abs,
            "identical_values": n_diff == 0,
            "identical": n_diff == 0
            and out["same_shape"]
            and (out["same_dtype"] or ignore_dtype)
            and out["same_projection"]
            and out["geotransform_max_abs_diff"] < 1e-9,
        }
    )
    if len(values_a) < MAX_DISTINCT and len(values_b) < MAX_DISTINCT:
        out["values_only_in_a"] = sorted(values_a - values_b)
        out["values_only_in_b"] = sorted(values_b - values_a)
    if transitions:
        top = sorted(transitions.items(), key=lambda kv: -kv[1])[:TOP_TRANSITIONS]
        out["top_transitions_a_to_b"] = [
            {"a": pair[0], "b": pair[1], "pixels": n} for pair, n in top
        ]
    return out


def pairs_from_folders(dir_a: str, dir_b: str) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Match ``*.tif`` files by name; return the pairs and the unmatched file names."""

    def tifs(folder: str) -> Dict[str, str]:
        return {
            name: os.path.join(folder, name)
            for name in sorted(os.listdir(folder))
            if name.lower().endswith((".tif", ".tiff"))
        }

    files_a, files_b = tifs(dir_a), tifs(dir_b)
    pairs = [(files_a[n], files_b[n]) for n in files_a if n in files_b]
    unmatched = [f"only in A: {n}" for n in files_a if n not in files_b]
    unmatched += [f"only in B: {n}" for n in files_b if n not in files_a]
    return pairs, unmatched


def format_report(results: List[Dict[str, Any]], unmatched: List[str]) -> str:
    lines: List[str] = []
    for r in results:
        name = os.path.basename(r["a"])
        if os.path.basename(r["b"]) != name:
            name += " vs " + os.path.basename(r["b"])
        if not r["comparable"]:
            lines.append(f"[CANNOT COMPARE] {name}: {r.get('note', '')}")
            continue
        if r["identical"]:
            status = "IDENTICAL"
        elif r.get("identical_values"):
            status = "SAME VALUES"  # grid size or pixel type differs
        else:
            status = "DIFFERENT"
        lines.append(
            f"[{status}] {name}: {r['percent_identical']:.4f} % of {r['pixels']:,} pixels "
            f"identical, max |diff| = {r['max_abs_diff']:g}"
        )
        flags = []
        if "compared_window" in r:
            w = r["compared_window"]
            flags.append(
                f"grids differ ({r['a_info']['cols']}x{r['a_info']['rows']} vs "
                f"{r['b_info']['cols']}x{r['b_info']['rows']}): compared the common "
                f"{w['cols']}x{w['rows']} window"
            )
        if not r["same_dtype"]:
            flags.append(f"dtype {r['a_info']['dtype']} vs {r['b_info']['dtype']}")
        if not r["same_projection"]:
            flags.append("projections differ")
        if r["geotransform_max_abs_diff"] >= 1e-9:
            flags.append(f"geotransform differs by {r['geotransform_max_abs_diff']:g}")
        if flags:
            lines.append("    " + "; ".join(flags))
        if r.get("values_only_in_a") or r.get("values_only_in_b"):
            lines.append(
                f"    values only in A: {r.get('values_only_in_a')}, "
                f"only in B: {r.get('values_only_in_b')}"
            )
        for t in r.get("top_transitions_a_to_b", []):
            lines.append(f"    {t['a']} -> {t['b']}: {t['pixels']:,} pixels")
    lines.extend(unmatched)
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("dir_a", nargs="?", help="folder of the first set of outputs")
    parser.add_argument("dir_b", nargs="?", help="folder of the second set of outputs")
    parser.add_argument(
        "--pair", nargs=2, action="append", metavar=("A", "B"), help="explicit pair of rasters"
    )
    parser.add_argument(
        "--crop",
        action="store_true",
        help="when grid sizes differ by rows/columns, compare the common top-left window",
    )
    parser.add_argument(
        "--ignore-dtype",
        action="store_true",
        help="do not count a different pixel type (e.g. UInt16 vs Float32) as a difference",
    )
    parser.add_argument("--json", metavar="FILE", help="also write the full report as JSON")
    args = parser.parse_args(argv)

    pairs: List[Tuple[str, str]] = [tuple(p) for p in (args.pair or [])]
    unmatched: List[str] = []
    if args.dir_a and args.dir_b:
        found, unmatched = pairs_from_folders(args.dir_a, args.dir_b)
        pairs.extend(found)
    elif args.dir_a or args.dir_b:
        parser.error("give both folders, or use --pair")
    if not pairs:
        parser.error("nothing to compare: no common .tif names and no --pair")

    results = [compare_pair(a, b, crop=args.crop, ignore_dtype=args.ignore_dtype) for a, b in pairs]
    print(format_report(results, unmatched))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"pairs": results, "unmatched": unmatched}, fh, indent=2)
    ok = all(r.get("identical") for r in results) and not unmatched
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
