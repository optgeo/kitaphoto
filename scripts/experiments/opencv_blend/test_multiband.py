"""
Small test: can OpenCV's stitching-detail seam finder / blender do anything
useful on two ADJACENT (not overlapping) GSI orthophoto tiles?

This is a scoping test, not a full solution attempt — see the caveat at the
top of the session notes: seam-FINDING algorithms (DP/GraphCut) and
MicMac's Tawny are designed for images with a known OVERLAP region (the
classic photogrammetry/panorama case). GSI's seamlessphoto512 tiles are
already hard-cut with no overlap between adjacent tiles, so there is no
"search region" for DP/GraphCut to find an optimal path within. This
script checks what happens when you feed OpenCV's seam finder abutting
(zero-overlap) tiles anyway, and separately tests whether MultiBandBlender
alone (skipping seam-finding, since the seam here is trivially "the tile
boundary") produces a better result than the naive gain/bias correction
from FUKUOKA_EXPERIMENT.md's attempts.

Usage: python test_multiband.py <seed.pmtiles> <x1> <y1> <x2> <y2> <out_dir>
"""
import io
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from pmtiles.reader import Reader, MmapSource, all_tiles


def load_tile(path, tz, tx, ty):
    with open(path, 'r+b') as f:
        reader = Reader(MmapSource(f))
        for (z, x, y), tile_bytes in all_tiles(reader.get_bytes):
            if (x, y) == (tx, ty):
                return np.asarray(Image.open(io.BytesIO(tile_bytes)).convert('RGB'))
    raise ValueError(f'{tx},{ty} not found')


def save(arr, path):
    Image.fromarray(arr).save(path)


def main():
    seed_path = sys.argv[1]
    x1, y1, x2, y2 = map(int, sys.argv[2:6])
    out_dir = Path(sys.argv[6])
    out_dir.mkdir(parents=True, exist_ok=True)

    img1 = load_tile(seed_path, 13, x1, y1)
    img2 = load_tile(seed_path, 13, x2, y2)
    horizontal = (x2 != x1)  # side-by-side vs stacked

    if horizontal:
        combined_before = np.hstack([img1, img2])
        corner1, corner2 = (0, 0), (img1.shape[1], 0)
    else:
        combined_before = np.vstack([img1, img2])
        corner1, corner2 = (0, 0), (0, img1.shape[0])
    save(combined_before, out_dir / '01_before.png')
    print(f'tiles ({x1},{y1}) and ({x2},{y2}), combined shape {combined_before.shape}')

    # --- Attempt 1: seam finder on the two full tiles treated as "images
    # placed with zero overlap" (this is the scoping test — DP/GraphCut
    # need overlap to search within; here there effectively isn't any) ---
    images_f32 = [img1.astype(np.float32), img2.astype(np.float32)]
    masks = [np.full(img1.shape[:2], 255, np.uint8), np.full(img2.shape[:2], 255, np.uint8)]
    corners = [corner1, corner2]

    for name, finder in [
        ('dp', cv2.detail.DpSeamFinder('COLOR')),
        ('graphcut', cv2.detail.GraphCutSeamFinder('COST_COLOR')),
    ]:
        try:
            result_masks = [m.copy() for m in masks]
            finder.find(images_f32, corners, result_masks)
            changed = int((result_masks[0] != masks[0]).sum())
            print(f'  seam finder [{name}]: mask pixels changed from input = {changed} '
                  f'(0 changed would mean it found no overlap to search, as expected here)')
        except cv2.error as e:
            print(f'  seam finder [{name}] raised cv2.error: {e}')

    # --- Attempt 2: skip seam-finding (the seam here is just the known
    # tile boundary), test MultiBandBlender directly across it ---
    blender = cv2.detail.MultiBandBlender()
    canvas_size = (combined_before.shape[1], combined_before.shape[0])
    blender.prepare((0, 0, canvas_size[0], canvas_size[1]))

    # feed each image at its known corner with a hard-split mask (the true
    # boundary), so blending has to smooth across a real hard edge
    mask1 = np.full(img1.shape[:2], 255, np.uint8)
    mask2 = np.full(img2.shape[:2], 255, np.uint8)
    blender.feed(img1.astype(np.int16), mask1, corner1)
    blender.feed(img2.astype(np.int16), mask2, corner2)
    result, result_mask = blender.blend(None, None)
    result = np.clip(result, 0, 255).astype(np.uint8)
    save(result, out_dir / '02_multiband_blend.png')
    print('  wrote multi-band blend result')

    # crop a strip straight across the boundary for close inspection
    h, w = combined_before.shape[:2]
    if horizontal:
        strip_before = combined_before[:, w // 2 - 300:w // 2 + 300]
        strip_after = result[:, w // 2 - 300:w // 2 + 300]
    else:
        strip_before = combined_before[h // 2 - 300:h // 2 + 300, :]
        strip_after = result[h // 2 - 300:h // 2 + 300, :]
    save(strip_before, out_dir / '03_boundary_strip_before.png')
    save(strip_after, out_dir / '04_boundary_strip_after.png')
    print(f'wrote outputs to {out_dir}')


if __name__ == '__main__':
    main()
