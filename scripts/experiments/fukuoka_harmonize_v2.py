"""
Fukuoka color-harmonization prototype, v2.

v1 (fukuoka_harmonize.py) failed: k-means color clustering on the coarse
view segmented by LAND COVER (forest/urban/water), not by photo source —
land-cover identity is a far stronger color signal than the subtle tonal
difference between two source photos of similar terrain, so v1's
"boundary refinement" ended up treating almost the whole scene as a
boundary and applying wrong corrections everywhere. See FUKUOKA_EXPERIMENT.md
(if this version succeeds) for the full writeup.

v2 changes strategy: instead of segmenting by absolute color, look for
STRAIGHT-LINE discontinuities — real photo-mosaic seams follow flight
strip / source-tile boundaries, so they show up as a consistent tone
*step* across a long, mostly-straight row or column, as opposed to the
gradual, organic color changes land cover produces. Concretely:

1. Build a coarse (block-mean) view, as in v1.
2. For every row boundary, measure the mean color jump between the row
   just above and just below it (same for every column boundary) — this
   gives two 1D profiles ("row-seam strength", "col-seam strength").
3. Threshold + non-max-suppress those profiles to get a handful of
   candidate seam lines — real seams should show up as isolated peaks
   much stronger than the local noise floor.
4. Partition the mosaic into strips along the detected row seams, then
   (on the row-corrected result) into strips along the detected column
   seams — a sequence of 1D corrections rather than a full 2D partition,
   to keep this prototype tractable.
5. Harmonize sequentially: walk strips in order, matching each new
   strip's near-boundary band statistics (median, not mean — more robust
   to a few unusual pixels near the line) to the previous (already
   corrected) strip's band, and apply that gain/bias to the whole strip
   with feathering near the boundary.

Usage: python fukuoka_harmonize_v2.py <seed.pmtiles> <out_dir>
"""
import io
import sys
from pathlib import Path

import numpy as np
from PIL import Image

from pmtiles.reader import Reader, MmapSource, all_tiles

TILE_SIZE = 512
COARSE_FACTOR = 16
SEAM_PERCENTILE = 98  # candidate seams: row/col diff above this percentile
NMS_WINDOW = 6  # coarse pixels; merge nearby peaks within this window
BAND_COARSE = 4  # coarse pixels either side of a seam used for matching stats
FEATHER_RADIUS = 32  # full-res pixels the correction blends in over


def load_seed_grid(path):
    grid = {}
    with open(path, 'r+b') as f:
        reader = Reader(MmapSource(f))
        for (z, x, y), tile_bytes in all_tiles(reader.get_bytes):
            img = Image.open(io.BytesIO(tile_bytes)).convert('RGB')
            grid[(x, y)] = np.asarray(img)
    return grid


def stitch(grid):
    xs = [x for x, y in grid]
    ys = [y for x, y in grid]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    w_tiles = max_x - min_x + 1
    h_tiles = max_y - min_y + 1
    mosaic = np.zeros((h_tiles * TILE_SIZE, w_tiles * TILE_SIZE, 3), dtype=np.uint8)
    mask = np.zeros((h_tiles * TILE_SIZE, w_tiles * TILE_SIZE), dtype=bool)
    for (x, y), tile in grid.items():
        row = (y - min_y) * TILE_SIZE
        col = (x - min_x) * TILE_SIZE
        mosaic[row:row + TILE_SIZE, col:col + TILE_SIZE] = tile
        mask[row:row + TILE_SIZE, col:col + TILE_SIZE] = True
    return mosaic, mask


def block_mean(arr, factor):
    h, w = arr.shape[:2]
    h2, w2 = (h // factor) * factor, (w // factor) * factor
    cropped = arr[:h2, :w2].astype(np.float64)
    if cropped.ndim == 3:
        reshaped = cropped.reshape(h2 // factor, factor, w2 // factor, factor, -1)
        return reshaped.mean(axis=(1, 3))
    reshaped = cropped.reshape(h2 // factor, factor, w2 // factor, factor)
    return reshaped.mean(axis=(1, 3))


def find_seams(coarse, mask_coarse, axis):
    """axis=0 -> row seams (horizontal lines), axis=1 -> col seams (vertical lines)."""
    diff_full = np.abs(np.diff(coarse, axis=axis, prepend=coarse[:1] if axis == 0 else coarse[:, :1]))
    both_valid = mask_coarse & np.roll(mask_coarse, 1, axis=axis)
    diff_full = diff_full.mean(axis=2)  # average over channels
    diff_full[~both_valid] = np.nan

    profile = np.nanmedian(diff_full, axis=1 - axis)  # median across the line's length
    valid_profile = profile[~np.isnan(profile)]
    if len(valid_profile) == 0:
        return []
    threshold = np.percentile(valid_profile, SEAM_PERCENTILE)

    candidates = [i for i, v in enumerate(profile) if not np.isnan(v) and v >= threshold and v > 0]
    # non-max suppression: merge candidates within NMS_WINDOW, keep the strongest
    candidates.sort(key=lambda i: -profile[i])
    kept = []
    for c in candidates:
        if all(abs(c - k) > NMS_WINDOW for k in kept):
            kept.append(c)
    kept.sort()
    return kept


def harmonize_along_axis(mosaic, mask, seams_coarse, factor, axis):
    """Sequentially match strip tone across detected seam lines (in coarse
    pixel coordinates, scaled up to full-res here) along `axis`."""
    if not seams_coarse:
        return mosaic
    seams_full = [s * factor for s in seams_coarse]
    length = mosaic.shape[axis]
    boundaries = [0] + seams_full + [length]

    corrected = mosaic.astype(np.float64).copy()
    band = BAND_COARSE * factor

    def strip_slice(lo, hi):
        idx = [slice(None), slice(None), slice(None)]
        idx[axis] = slice(lo, hi)
        return tuple(idx)

    def band_stats(lo, hi, side):
        # `side`: 'end' = band just before hi, 'start' = band just after lo
        if side == 'end':
            b_lo, b_hi = max(lo, hi - band), hi
        else:
            b_lo, b_hi = lo, min(hi, lo + band)
        idx = strip_slice(b_lo, b_hi)
        slab = corrected[idx]
        return np.median(slab.reshape(-1, 3), axis=0)

    # first strip stays as-is (reference); walk forward matching each next
    # strip's near-boundary band to the previous (already-corrected) strip's band
    for i in range(len(boundaries) - 2):
        lo, hi, nxt = boundaries[i], boundaries[i + 1], boundaries[i + 2]
        prev_band = band_stats(lo, hi, 'end')
        next_band = band_stats(hi, nxt, 'start')
        bias = prev_band - next_band  # additive match (bias-only: safer than gain on a narrow band)
        idx = strip_slice(hi, nxt)
        # feather: ramp UP from 0 at the seam to full correction over FEATHER_RADIUS px,
        # then stay fully corrected for the rest of the strip (not the other way around —
        # v2's first attempt at this had the ramp backwards, applying the correction only
        # in a thin band at the seam and nothing across the bulk of the strip).
        coord = np.arange(hi, nxt)
        dist = coord - hi
        w = np.clip(dist / FEATHER_RADIUS, 0, 1)
        w_shape = [1, 1, 1]
        w_shape[axis] = len(w)
        w = w.reshape(w_shape)
        corrected[idx] = corrected[idx] + bias.reshape(1, 1, 3) * w

    return np.clip(corrected, 0, 255).astype(np.uint8)


def save(arr, path):
    Image.fromarray(arr).save(path, format='PNG')


def main():
    seed_path, out_dir = sys.argv[1], Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f'loading seed tiles from {seed_path}...')
    grid = load_seed_grid(seed_path)
    print(f'  {len(grid):_} tiles')

    mosaic, mask = stitch(grid)
    print(f'  stitched mosaic: {mosaic.shape}')

    coarse = block_mean(mosaic, COARSE_FACTOR)
    mask_coarse = block_mean(mask, COARSE_FACTOR) > 0.5
    print(f'  coarse view: {coarse.shape}')

    row_seams = find_seams(coarse, mask_coarse, axis=0)
    col_seams = find_seams(coarse, mask_coarse, axis=1)
    print(f'  detected {len(row_seams)} row seams at coarse y={row_seams}')
    print(f'  detected {len(col_seams)} col seams at coarse x={col_seams}')

    # visualize detected seams on the coarse view
    seam_vis = np.clip(coarse, 0, 255).astype(np.uint8).copy()
    for s in row_seams:
        seam_vis[max(0, s - 1):s + 1, :] = [255, 0, 0]
    for s in col_seams:
        seam_vis[:, max(0, s - 1):s + 1] = [255, 0, 0]
    save(seam_vis, out_dir / '02_seams_on_coarse.png')

    print('harmonizing along rows, then columns...')
    step1 = harmonize_along_axis(mosaic, mask, row_seams, COARSE_FACTOR, axis=0)
    step2 = harmonize_along_axis(step1, mask, col_seams, COARSE_FACTOR, axis=1)

    save(mosaic, out_dir / '01_mosaic_before.png')
    save(step2, out_dir / '05_mosaic_after_v2.png')

    before_blocks = block_mean(mosaic, COARSE_FACTOR)
    after_blocks = block_mean(step2, COARSE_FACTOR)
    print(f'  block-mean color std before: {before_blocks.reshape(-1, 3).std(axis=0)}')
    print(f'  block-mean color std after:  {after_blocks.reshape(-1, 3).std(axis=0)}')
    print(f'wrote outputs to {out_dir}')


if __name__ == '__main__':
    main()
