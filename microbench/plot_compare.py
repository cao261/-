"""
Plot digitization + reproduction-alignment for MicroBench.

V1.2 features:
- extract_curve_from_image(image_bytes): detect the dominant colored curve in
  a chart screenshot, return (x_pixel, y_pixel) tuples.
  Uses Pillow only (no OpenCV dep). Algorithm:
    1. Convert to grayscale + compute per-pixel "saturation" (max-min RGB)
    2. Threshold: keep pixels where saturation > sat_thresh AND value < val_thresh
       (filters white background and black axes/text)
    3. Cluster by hue-quantized RGB (round to 32-step buckets)
    4. Largest cluster (by count) is the "main curve"
    5. For each x column with cluster pixels, take the median y as the curve point
- calibrate(pixel_points, ref_a, ref_b): affine pixel→real transform from
  two reference points the user clicks on the image.
- compute_metrics(paper_xy, repro_xy): RMSE / MAE / max abs / max rel /
  Pearson R / R^2 / verdict (pass/fail based on tolerance thresholds).
  Paper points are linearly interpolated onto the repro x-grid before
  comparison (assumes monotonic x, otherwise we sort first).
- parse_csv_points(text): minimal CSV / TSV parser, two-column (x, y).
"""

import io
import math
import re
from collections import Counter, defaultdict
from typing import Optional


# ---------------------------------------------------------------------------
# Image → pixel curve extraction
# ---------------------------------------------------------------------------

# Curve-color "saturation" floor (0-255). Most chart curves use saturated
# colors (red/blue/green), background is white/near-white (sat near 0).
DEFAULT_SAT_THRESHOLD = 60
# Curve-color "value" ceiling. Filters near-black axes/labels.
DEFAULT_VAL_THRESHOLD = 230
# Hue quantization bucket size (RGB each quantized to 32 levels = 32^3 = 32768 buckets).
# Smaller bucket → stricter clustering (separates close colors); larger → merges.
DEFAULT_HUE_BUCKET = 32

# Hard ceiling on the image area we are willing to digitise. Pillow only
# raises DecompressionBombError above ~2x its own limit, so a 4K screenshot
# (4000x3000 = 12 MP) sails through while the pure-Python pixel loop needs
# tens of seconds and a 700MB boolean mask. 40 MP keeps the worst case
# bounded while covering any realistic chart screenshot.
MAX_IMAGE_PIXELS = 40_000_000


def _threshold_mask(img, sat_thresh: int, val_thresh: int):
    """Return a binary mask (mode "L", 0/255) of "data" pixels.

    The threshold test is `saturation >= sat_thresh AND value <= val_thresh`
    where saturation = max(R,G,B) - min(R,G,B) and value = max(R,G,B).

    Implemented with Pillow's C-level channel ops (split / lighter / darker /
    difference / point) instead of a Python loop over every pixel.
    """
    from PIL import ImageChops

    r, g, b = img.split()
    lighter = ImageChops.lighter(ImageChops.lighter(r, g), b)   # per-pixel max
    darker = ImageChops.darker(ImageChops.darker(r, g), b)       # per-pixel min
    sat = ImageChops.difference(lighter, darker)                 # max - min

    sat_mask = sat.point(lambda v: 255 if v >= sat_thresh else 0)
    val_mask = lighter.point(lambda v: 255 if v <= val_thresh else 0)
    # multiply() on 0/255 values is a logical AND.
    return ImageChops.multiply(sat_mask, val_mask)


def extract_curve_from_image(
    image_bytes: bytes,
    sat_thresh: int = DEFAULT_SAT_THRESHOLD,
    val_thresh: int = DEFAULT_VAL_THRESHOLD,
    hue_bucket: int = DEFAULT_HUE_BUCKET,
    min_pixels_per_col: int = 1,
    max_cols: int = 2000,
) -> dict:
    """
    Extract the dominant curve from a chart image.

    Returns dict with keys:
      - points: list of [x_pixel, y_pixel] (sorted left-to-right)
      - total_pixels: how many "data pixels" were detected
      - cluster_color: approximate RGB of the dominant curve (for debug)
      - width / height: image dimensions

    Implementation note: this used to walk every pixel twice in pure Python
    and keep a `[[False] * width] * height` mask alive, i.e. 89M Python list
    slots (~700MB) for a 4000x3000 screenshot, and 10-20 seconds of CPU.
    The threshold now runs in Pillow's C code, the mask is a flat bytes
    buffer, and — when numpy is importable — the clustering and the
    per-column median are vectorised. numpy stays an *optional* accelerator;
    the pure-Pillow fallback keeps the same numbers without it.
    """
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    width, height = img.size

    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError(
            f"图片过大 ({width}x{height} = {width * height / 1e6:.1f} MP), "
            f"请裁剪到 {MAX_IMAGE_PIXELS / 1e6:.0f} MP 以内再上传"
        )

    mask = _threshold_mask(img, sat_thresh, val_thresh)
    bbox = mask.getbbox()
    if bbox is None:
        return {
            "points": [],
            "total_pixels": 0,
            "cluster_color": [0, 0, 0],
            "width": width,
            "height": height,
            "warning": "未检测到饱和度足够的数据像素。请尝试调整阈值或确认图表是否含彩色曲线。",
        }

    # Crop to the bounding box of the candidate pixels. For a real chart
    # this typically removes the margins and the legend area, and it keeps
    # the coordinates we emit in the original image space by adding the
    # crop offset back at the end.
    off_x, off_y = bbox[0], bbox[1]
    mask_c = mask.crop(bbox)
    img_c = img.crop(bbox)
    cw, ch = mask_c.size

    cluster_pixels: dict = {}
    n_clusters = 0
    main_cluster: list = []
    main_cluster_key = (0, 0, 0)
    cluster_color = [0, 0, 0]

    try:
        import numpy as np
    except ImportError:
        np = None

    if np is not None:
        arr = np.asarray(img_c)                      # (h, w, 3) uint8
        m = np.asarray(mask_c) > 0                   # (h, w) bool
        ys, xs = np.nonzero(m)
        if xs.size == 0:
            rgb = np.empty((0, 3), dtype=np.uint8)
        else:
            rgb = arr[ys, xs]
        if rgb.shape[0]:
            keys = (rgb.astype(np.int32) // hue_bucket)
            packed = (keys[:, 0].astype(np.int64) << 40) | \
                     (keys[:, 1].astype(np.int64) << 20) | \
                     keys[:, 2].astype(np.int64)
            uniq, inverse, counts = np.unique(
                packed, return_inverse=True, return_counts=True
            )
            n_clusters = int(uniq.size)
            best = int(np.argmax(counts))
            main_cluster_key = (
                int((int(uniq[best]) >> 40) & 0xFFFFF),
                int((int(uniq[best]) >> 20) & 0xFFFFF),
                int(int(uniq[best]) & 0xFFFFF),
            )
            sel = inverse == best
            mx = xs[sel]
            my = ys[sel]
            main_cluster = list(zip(mx.tolist(), my.tolist()))
        cluster_color = [
            main_cluster_key[0] * hue_bucket + hue_bucket // 2,
            main_cluster_key[1] * hue_bucket + hue_bucket // 2,
            main_cluster_key[2] * hue_bucket + hue_bucket // 2,
        ]
    else:
        # Pure-Pillow fallback: one flat pass over the mask bytes.
        md = mask_c.tobytes()
        pd = img_c.tobytes()
        buckets: dict = {}
        for i, m in enumerate(md):
            if not m:
                continue
            j = i * 3
            r = pd[j]
            g = pd[j + 1]
            b = pd[j + 2]
            key = (r // hue_bucket, g // hue_bucket, b // hue_bucket)
            y, x = divmod(i, cw)
            buckets.setdefault(key, []).append((x, y))
        n_clusters = len(buckets)
        if buckets:
            main_cluster_key, main_cluster = max(
                buckets.items(), key=lambda kv: len(kv[1])
            )
        cluster_color = [
            main_cluster_key[0] * hue_bucket + hue_bucket // 2,
            main_cluster_key[1] * hue_bucket + hue_bucket // 2,
            main_cluster_key[2] * hue_bucket + hue_bucket // 2,
        ]

    # Phase 3: per-column median y
    by_x = defaultdict(list)
    for x, y in main_cluster:
        by_x[x].append(y)

    sorted_xs = sorted(by_x.keys())
    if len(sorted_xs) > max_cols:
        # Subsample: take every Nth column to keep payload small
        stride = max(1, len(sorted_xs) // max_cols)
        sorted_xs = sorted_xs[::stride]

    points = []
    for x in sorted_xs:
        ys = by_x[x]
        if len(ys) < min_pixels_per_col:
            continue
        ys_sorted = sorted(ys)
        mid = len(ys_sorted) // 2
        median_y = ys_sorted[mid] if len(ys_sorted) % 2 else (ys_sorted[mid - 1] + ys_sorted[mid]) / 2
        # Re-add the crop offset so the points are in original image space.
        points.append([x + off_x, median_y + off_y])

    return {
        "points": points,
        "total_pixels": len(main_cluster),
        "cluster_color": cluster_color,
        "width": width,
        "height": height,
        "n_clusters": n_clusters,
    }


# ---------------------------------------------------------------------------
# Pixel → real coordinate calibration (affine from 2 reference points)
# ---------------------------------------------------------------------------

def calibrate(
    pixel_points: list,
    ref_a: dict,
    ref_b: dict,
) -> list:
    """
    Apply an affine transform from pixel space to "real" data space,
    derived from two reference points the user clicks on the chart.

    ref_a = {"px": x_pixel, "py": y_pixel, "rx": x_real, "ry": y_real}
    ref_b = {"px": x_pixel, "py": y_pixel, "rx": x_real, "ry": y_real}

    Returns: list of [x_real, y_real]

    Math: solve for a, b, c, d such that
        x_real = a * x_pixel + b
        y_real = c * y_pixel + d
      using the 2 reference points (4 equations, 4 unknowns — over-determined
      so we use least-squares, but with 2 points it gives an exact affine).

    Note: y axis may be inverted in pixel space (top-left origin vs
    bottom-left origin in chart), which is handled automatically.
    """
    if not pixel_points:
        return []

    a1, b1, c1, d1 = ref_a["px"], ref_a["rx"], ref_a["py"], ref_a["ry"]
    a2, b2, c2, d2 = ref_b["px"], ref_b["rx"], ref_b["py"], ref_b["ry"]

    # Linear interpolation in x
    if a1 == a2:
        raise ValueError("两个参考点的 x 像素坐标不能相同")
    x_slope = (b2 - b1) / (a2 - a1)
    x_intercept = b1 - x_slope * a1

    # Linear interpolation in y
    if c1 == c2:
        raise ValueError("两个参考点的 y 像素坐标不能相同")
    y_slope = (d2 - d1) / (c2 - c1)
    y_intercept = d1 - y_slope * c1

    out = []
    for pt in pixel_points:
        px, py = pt[0], pt[1]
        rx = x_slope * px + x_intercept
        ry = y_slope * py + y_intercept
        out.append([rx, ry])
    return out


# ---------------------------------------------------------------------------
# Reproduction alignment metrics
# ---------------------------------------------------------------------------

# Default tolerance bands (matching the user's复现标准Checklist):
#   - TCAD/EDA physical sim: <5% acceptable, >20% = recheck contact resistance
#   - ML/heuristic: 3-5% acceptable
DEFAULT_TOLERANCE = {
    "pass_pct": 5.0,      # ≤ 5% max relative error → pass
    "warn_pct": 20.0,     # ≤ 20% → needs review
    # > 20% → fail
}


def _parse_csv_points(text: str) -> list:
    """
    Minimal CSV/TSV parser for two-column (x, y) data.
    Tolerates header lines, comment lines (#, //), blank lines,
    comma/tab/semicolon separators, and trailing whitespace.
    Returns list of [x, y] (floats). Pairs with non-numeric cells are skipped.
    """
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        # Replace tabs/semicolons with comma
        s = re.sub(r"[\t;]", ",", s)
        # If still no comma, try whitespace split
        if "," not in s:
            parts = s.split()
        else:
            parts = s.split(",")
        if len(parts) < 2:
            continue
        try:
            x = float(parts[0].strip())
            y = float(parts[1].strip())
            out.append([x, y])
        except ValueError:
            # Likely a header row (e.g. "V_G, I_D"); skip silently
            continue
    return out


def compute_metrics(
    paper_xy: list,
    repro_xy: list,
    tolerance: Optional[dict] = None,
) -> dict:
    """
    Compare paper-reported data against reproduction data.

    Pipeline:
      1. Sort both by x
      2. Linear-interpolate paper_xy onto repro_xy's x grid
         (assumes both are dense enough that interpolation is meaningful)
      3. Compute per-point differences
      4. Aggregate: RMSE, MAE, max abs err, max rel err %, Pearson R, R^2
      5. Verdict based on tolerance bands

    Returns dict with all metrics + verdict + per-point table.
    """
    if not paper_xy or not repro_xy:
        raise ValueError("paper_xy 与 repro_xy 均不能为空")

    tol = tolerance or DEFAULT_TOLERANCE

    # Sort both by x
    paper_sorted = sorted(paper_xy, key=lambda p: p[0])
    repro_sorted = sorted(repro_xy, key=lambda p: p[0])

    px = [p[0] for p in paper_sorted]
    py = [p[1] for p in paper_sorted]
    rx = [r[0] for r in repro_sorted]
    ry = [r[1] for r in repro_sorted]

    # Check x overlap
    x_lo = max(px[0], rx[0])
    x_hi = min(px[-1], rx[-1])
    if x_hi <= x_lo:
        raise ValueError(
            f"paper 与 repro 的 x 范围无重叠 (paper {px[0]:.4g}~{px[-1]:.4g}, "
            f"repro {rx[0]:.4g}~{rx[-1]:.4g})"
        )

    # Linearly interpolate paper onto repro's x grid
    interpolated = _linear_interp(px, py, rx)

    # Per-point diff
    diffs = []
    abs_errs = []
    rel_errs = []
    for i, (xi, yi) in enumerate(zip(rx, ry)):
        paper_yi = interpolated[i]
        abs_err = yi - paper_yi
        abs_errs.append(abs(abs_err))
        diffs.append(abs_err)
        # Relative error vs paper value (skip if paper ≈ 0)
        if abs(paper_yi) > 1e-12:
            rel_errs.append(abs(abs_err / paper_yi) * 100.0)
        else:
            rel_errs.append(None)

    # Aggregate metrics
    n = len(diffs)
    sum_sq = sum(d * d for d in diffs)
    rmse = math.sqrt(sum_sq / n) if n > 0 else 0.0
    mae = sum(abs_errs) / n if n > 0 else 0.0
    max_abs = max(abs_errs) if abs_errs else 0.0
    valid_rel = [r for r in rel_errs if r is not None]
    max_rel_pct = max(valid_rel) if valid_rel else 0.0
    avg_rel_pct = sum(valid_rel) / len(valid_rel) if valid_rel else 0.0

    # Pearson correlation (between paper_yi interpolated and repro y)
    if n > 1:
        paper_mean = sum(interpolated) / n
        repro_mean = sum(ry) / n
        cov = sum((interpolated[i] - paper_mean) * (ry[i] - repro_mean) for i in range(n))
        var_p = sum((p - paper_mean) ** 2 for p in interpolated)
        var_r = sum((y - repro_mean) ** 2 for y in ry)
        denom = math.sqrt(var_p * var_r)
        pearson = cov / denom if denom > 1e-12 else 0.0
        # R^2 = 1 - SS_res / SS_tot
        ss_res = sum((ry[i] - interpolated[i]) ** 2 for i in range(n))
        ss_tot = sum((y - repro_mean) ** 2 for y in ry)
        r2 = 1 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0
    else:
        pearson = 0.0
        r2 = 0.0

    # Verdict
    #
    # A relative error is only computable where |paper_y| is non-zero. If the
    # extracted paper curve is flat-zero (digitisiser failed / wrong column
    # picked / empty panel), EVERY relative error is None, valid_rel is empty,
    # and the old code fell through to max_rel_pct = 0.0 <= pass_pct -> "pass".
    # That reported a total extraction failure as a successful reproduction,
    # which is the single most misleading thing this module can do. Declare it
    # inconclusive instead, and make it loud in the response.
    if not valid_rel:
        verdict = "inconclusive"
        verdict_msg = (
            "⚠️ 无法判定: 论文曲线在重叠区间内全为 0, 相对误差无从计算。"
            "这通常意味着图表数据提取失败(选错坐标轴/列), 而不是复现通过。"
            f" 绝对 RMSE={rmse:.4g}, MAE={mae:.4g} 仅作参考。"
        )
    elif max_rel_pct <= tol["pass_pct"]:
        verdict = "pass"
        verdict_msg = f"✅ 通过: 最大相对误差 {max_rel_pct:.2f}% ≤ {tol['pass_pct']}%"
    elif max_rel_pct <= tol["warn_pct"]:
        verdict = "warn"
        verdict_msg = f"⚠️ 需复核: 最大相对误差 {max_rel_pct:.2f}% (容许区间 {tol['pass_pct']}%-{tol['warn_pct']}%)"
    else:
        verdict = "fail"
        verdict_msg = f"❌ 未通过: 最大相对误差 {max_rel_pct:.2f}% > {tol['warn_pct']}%, 重点检查接触电阻/迁移率退化/边界条件"

    return {
        "n_points": n,
        "x_overlap": [x_lo, x_hi],
        "rmse": rmse,
        "mae": mae,
        "max_abs_err": max_abs,
        "max_rel_err_pct": max_rel_pct,
        "avg_rel_err_pct": avg_rel_pct,
        "pearson_r": pearson,
        "r_squared": r2,
        "verdict": verdict,
        "verdict_msg": verdict_msg,
        "tolerance": tol,
        # Per-point table (capped at 100 to avoid huge payloads)
        "per_point": [
            {
                "x": rx[i],
                "paper_y": interpolated[i],
                "repro_y": ry[i],
                "abs_err": diffs[i],
                "rel_err_pct": rel_errs[i],
            }
            for i in range(n)
        ][:100],
    }


def _linear_interp(xs: list, ys: list, query_xs: list) -> list:
    """Standard linear interpolation. Assumes xs is sorted ascending."""
    out = []
    n = len(xs)
    for q in query_xs:
        # Binary search for the bracketing segment
        if q <= xs[0]:
            out.append(ys[0])
            continue
        if q >= xs[-1]:
            out.append(ys[-1])
            continue
        lo, hi = 0, n - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if xs[mid] <= q:
                lo = mid
            else:
                hi = mid
        # Linear interp between (xs[lo], ys[lo]) and (xs[hi], ys[hi])
        t = (q - xs[lo]) / (xs[hi] - xs[lo])
        out.append(ys[lo] * (1 - t) + ys[hi] * t)
    return out


__all__ = [
    "extract_curve_from_image",
    "calibrate",
    "compute_metrics",
    "parse_csv_points" if False else "_parse_csv_points",  # exposed via compute_metrics too
]