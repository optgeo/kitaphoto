"""
Fukuoka color-harmonization prototype.

Idea (per user discussion): the seams visible in seamlessphoto512 aren't
between aerial-photo and satellite (that's a different, already-solved
problem in downsample.py) — they're *within* the orthophoto itself, between
adjacent source photos/flight-lines that GSI mosaicked together with visibly
different color casts. This script tests a coarse-to-fine approach to
finding and correcting those seams, prototyped on a small Fukuoka-area
extract before considering it for anything bigger:

1. Stitch all z13 seed tiles for the test region into one big mosaic array.
2. Downsample it heavily (block-mean pooling) to get a coarse view where
   local texture is averaged out but each source photo's characteristic
   color cast survives as a broad, flat region — segmentation is much
   easier here than at full resolution.
3. Cluster the coarse view's colors (k-means) into a handful of segments —
   an approximation of "which source photo is this."
4. Upsample the coarse label map back to full resolution (nearest
   neighbor), then refine it near label boundaries by reassigning each
   pixel to whichever cluster centroid it's closest to, within a local
   window — the "coarse segmentation, refine only near boundaries at full
   resolution" idea, avoiding a full-resolution clustering pass over the
   whole mosaic.
5. Harmonize: pick the largest segment as the reference, and apply a
   per-channel affine (gain+bias) correction to every other segment so its
   mean/std matches the reference's — classic histogram/moment matching.
   Blend the correction in smoothly near segment boundaries (feathering,
   via a distance-to-boundary weight) instead of applying it as a hard cut,
   as a lightweight stand-in for full Laplacian-pyramid multi-band blending.

Usage: python fukuoka_harmonize.py <seed.pmtiles> <out_dir>
"""
import io
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.cluster.vq import kmeans2
from scipy import ndimage

from pmtiles.reader import Reader, MmapSource, all_tiles

TILE_SIZE = 512
COARSE_FACTOR = 32  # block-pooling factor for the coarse segmentation view
N_CLUSTERS = 6
BOUNDARY_BAND = 3  # coarse pixels either side of a label boundary to refine
FEATHER_RADIUS = 24  # full-res pixels over which correction blends in near a boundary


def load_seed_grid(path):
    """Return {(x, y): np.ndarray[512,512,3] uint8} for every tile in `path`."""
    grid = {}
    with open(path, 'r+b') as f:
        reader = Reader(MmapSource(f))
        for (z, x, y), tile_bytes in all_tiles(reader.get_bytes):
            img = Image.open(io.BytesIO(tile_bytes)).convert('RGB')
            grid[(x, y)] = np.asarray(img)
    return grid


def stitch(grid):
    """Assemble the tile grid into one big array. Returns (mosaic, mask, origin_x, origin_y)."""
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
    return mosaic, mask, min_x, min_y


def block_mean(arr, factor):
    """Block-mean pool `arr` (H,W,3) by `factor`, cropping to a multiple of factor first."""
    h, w = arr.shape[:2]
    h2, w2 = (h // factor) * factor, (w // factor) * factor
    cropped = arr[:h2, :w2].astype(np.float64)
    reshaped = cropped.reshape(h2 // factor, factor, w2 // factor, factor, 3)
    return reshaped.mean(axis=(1, 3))


def coarse_segment(coarse, mask_coarse, n_clusters):
    """K-means on coarse RGB pixels (only where masked in) -> label image (same H,W as coarse)."""
    h, w = coarse.shape[:2]
    flat = coarse.reshape(-1, 3)
    flat_mask = mask_coarse.reshape(-1)
    valid = flat[flat_mask]
    centroids, valid_labels = kmeans2(valid, n_clusters, seed=0, minit='++')
    labels = np.full(h * w, -1, dtype=np.int32)
    labels[flat_mask] = valid_labels
    return labels.reshape(h, w), centroids


def refine_near_boundaries(labels_full, centroids, mosaic_ds, band_px):
    """Reassign pixels near coarse-label boundaries to their nearest centroid
    in a downsampled-resolution working copy (`mosaic_ds`, same shape as
    labels_full) — cheap full-resolution refinement limited to a boundary
    band, rather than clustering the whole mosaic at full res."""
    boundary = labels_full != ndimage.grey_erosion(labels_full, size=3)
    boundary |= labels_full != ndimage.grey_dilation(labels_full, size=3)
    band = ndimage.binary_dilation(boundary, iterations=band_px)
    refined = labels_full.copy()
    ys, xs = np.nonzero(band)
    if len(ys):
        pts = mosaic_ds[ys, xs].astype(np.float64)
        dists = ((pts[:, None, :] - centroids[None, :, :]) ** 2).sum(axis=2)
        refined[ys, xs] = dists.argmin(axis=1)
    return refined


def harmonize(mosaic, mask, labels_full, reference_label):
    """Per-channel affine (gain+bias) correction of every non-reference
    segment to match the reference segment's mean/std, feathered near
    segment boundaries via a distance-to-boundary weight."""
    corrected = mosaic.astype(np.float64).copy()
    ref_pixels = mosaic[(labels_full == reference_label) & mask].astype(np.float64)
    ref_mean, ref_std = ref_pixels.mean(axis=0), ref_pixels.std(axis=0) + 1e-6

    dist_to_ref = ndimage.distance_transform_edt(labels_full != reference_label)
    weight = np.clip(dist_to_ref / FEATHER_RADIUS, 0, 1)  # 0 at the boundary, 1 deep inside a segment

    for label in np.unique(labels_full):
        if label == reference_label or label < 0:
            continue
        seg_mask = (labels_full == label) & mask
        seg_pixels = mosaic[seg_mask].astype(np.float64)
        if len(seg_pixels) < 100:
            continue
        seg_mean, seg_std = seg_pixels.mean(axis=0), seg_pixels.std(axis=0) + 1e-6
        gain = ref_std / seg_std
        bias = ref_mean - seg_mean * gain
        fully_corrected = mosaic[seg_mask].astype(np.float64) * gain + bias
        w = weight[seg_mask][:, None]
        corrected[seg_mask] = w * fully_corrected + (1 - w) * mosaic[seg_mask]

    return np.clip(corrected, 0, 255).astype(np.uint8)


def save(arr, path):
    Image.fromarray(arr).save(path, format='PNG')


def main():
    seed_path, out_dir = sys.argv[1], Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f'loading seed tiles from {seed_path}...')
    grid = load_seed_grid(seed_path)
    print(f'  {len(grid):_} tiles')

    mosaic, mask, ox, oy = stitch(grid)
    print(f'  stitched mosaic: {mosaic.shape}')
    save(mosaic, out_dir / '01_mosaic_before.png')

    coarse = block_mean(mosaic, COARSE_FACTOR)
    mask_coarse = block_mean(mask.astype(np.float64)[..., None].repeat(3, -1), COARSE_FACTOR)[..., 0] > 0.5
    print(f'  coarse view: {coarse.shape}')
    save(np.clip(coarse, 0, 255).astype(np.uint8), out_dir / '02_coarse_view.png')

    print(f'segmenting coarse view into {N_CLUSTERS} clusters...')
    labels_coarse, centroids = coarse_segment(coarse, mask_coarse, N_CLUSTERS)
    label_vis = (labels_coarse.astype(np.float64) / max(1, N_CLUSTERS - 1) * 255).astype(np.uint8)
    save(label_vis, out_dir / '03_coarse_labels.png')

    print('upsampling labels + refining near boundaries...')
    labels_full = np.array(
        Image.fromarray(labels_coarse.astype(np.int32), mode='I')
        .resize((mosaic.shape[1], mosaic.shape[0]), Image.NEAREST)
    )
    mosaic_ds = np.array(
        Image.fromarray(np.clip(coarse, 0, 255).astype(np.uint8))
        .resize((mosaic.shape[1], mosaic.shape[0]), Image.BILINEAR)
    )
    labels_full = refine_near_boundaries(labels_full, centroids, mosaic_ds, BOUNDARY_BAND * COARSE_FACTOR)
    label_vis_full = (labels_full.astype(np.float64) / max(1, N_CLUSTERS - 1) * 255).astype(np.uint8)
    save(label_vis_full, out_dir / '04_full_labels.png')

    counts = [(labels_full == lbl).sum() for lbl in range(N_CLUSTERS)]
    reference_label = int(np.argmax(counts))
    print(f'  segment pixel counts: {counts}, reference segment: {reference_label}')

    print('harmonizing colors...')
    corrected = harmonize(mosaic, mask, labels_full, reference_label)
    save(corrected, out_dir / '05_mosaic_after.png')

    # simple quantitative check: block-mean color variance before/after,
    # over a coarse grid (lower = more uniform / less patchwork-looking)
    before_blocks = block_mean(mosaic, COARSE_FACTOR)
    after_blocks = block_mean(corrected, COARSE_FACTOR)
    print(f'  block-mean color std before: {before_blocks.reshape(-1, 3).std(axis=0)}')
    print(f'  block-mean color std after:  {after_blocks.reshape(-1, 3).std(axis=0)}')

    print(f'wrote outputs to {out_dir}')


if __name__ == '__main__':
    main()
