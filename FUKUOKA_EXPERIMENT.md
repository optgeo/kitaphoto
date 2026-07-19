# Fukuoka color-harmonization experiment — negative result (2026-07-19)

## Motivation

kitaphoto's pyramid already solves one seam problem: the boundary between real
aerial photography and GSI's satellite gap-fill (see [README.md](README.md) /
[HANDOVER.md](HANDOVER.md)). But looking at the output, there's a second, different
kind of seam — *within* the orthophoto layer itself. GSI's "全国最新写真" is a mosaic
of many separate source photos/flight lines, each with its own color cast (capture
date, sun angle, sensor, atmospheric correction). Those casts don't match at the
seams, so wide-area views look like a patchwork of visibly different-toned
rectangles rather than one continuous photograph — most visible in
[hfu/kitaphoto](https://github.com/hfu/kitaphoto)'s own low-zoom pyramid, since
box-averaging doesn't erase a source photo's characteristic tone, only its local
texture.

The idea explored here: could kitaphoto's own coarse-to-fine pyramid structure be
used to detect these seams and harmonize color across them — segment cheaply at
low zoom (where local texture is averaged out and a source photo's broad tone
survives as a flat region), then refine near detected boundaries at full
resolution, then correct color across each seam (histogram/moment matching,
feathered so there's no hard edge)?

Piloted small before considering it for anything bigger: a ~55km × 65km Fukuoka
extract (z13 seed, 299 tiles, bbox `130.078125,33.137551,130.78125,33.724340`),
not the whole country.

**Result: both attempts failed to produce a real improvement.** This is a writeup
of what was tried and why it didn't work, per the decision to record the negative
result honestly rather than force a success narrative.

## Attempt 1: color-cluster segmentation

**Approach** (`scripts/experiments/fukuoka_harmonize.py`):

1. Stitch all 299 z13 tiles into one mosaic (9216×8704px).
2. Block-mean pool it down by 32× to a coarse view (288×272px) — local texture
   averages out, source-photo tone should survive as flat regions.
3. K-means cluster the coarse view's pixel colors into 6 segments.
4. Upsample the coarse label map to full resolution (nearest neighbor), then
   refine it near label boundaries: reassign each pixel within a boundary band to
   its nearest cluster centroid, computed at full resolution.
5. Harmonize: pick the largest segment as reference, apply a per-channel
   affine (gain+bias) correction to every other segment to match the reference's
   mean/std, feathered near segment boundaries (a lightweight stand-in for full
   Laplacian-pyramid multi-band blending).

**Quantitative result looked like a win**: block-mean color std dropped from
`[39.4, 38.9, 38.9]` to `[20.1, 20.0, 20.6]` — roughly half.

**Visually, it made things worse:**

| Before | After |
|---|---|
| ![before](scripts/experiments/fukuoka/images/01_before.jpg) | ![v1 after](scripts/experiments/fukuoka/images/03_v1_after.jpg) |

A strong, wrong teal/cyan cast spread across most of the scene, plus scattered
magenta artifacts.

**Root cause**, visible directly in the label map:

![v1 labels](scripts/experiments/fukuoka/images/02_v1_labels.jpg)

The clusters aren't source-photo regions — they're **land cover** (black =
forest/mountain shadow, white = urban/bright, gray = water/farmland). Land-cover
identity is a far stronger color signal than the subtle tonal difference between
two source photos of similar terrain, so unsupervised color clustering finds land
cover every time, not photo provenance. Worse, once labels are land-cover-driven,
they change constantly at fine texture scale (a single city block mixes roofs,
roads, trees) — so the "refine near boundaries" step ends up treating almost the
whole scene as boundary, and the harmonization step applies wrong corrections
almost everywhere, which is what produced the cyan wash: forest-shadow pixels
(the reference segment, since it was the largest cluster) pulling everything else
toward their tone regardless of what the other pixels actually depicted.

**Lesson**: segmenting by absolute color cannot separate "same source photo" from
"same land cover type." A usable segmentation needs a different signal.

## Attempt 2: straight-line seam detection

**Approach** (`scripts/experiments/fukuoka_harmonize_v2.py`): real photo-mosaic
seams should be straight lines (flight-strip / source-tile boundaries), unlike
land cover's organic, gradual color changes. So instead of clustering colors:

1. Build the same coarse (16×) block-mean view.
2. For every row boundary, compute the median color jump between the row just
   above and just below across the *entire row's width* (same for every column
   boundary) — two 1D "seam strength" profiles.
3. Threshold (98th percentile) + non-max-suppress those profiles to get a
   handful of candidate seam lines.
4. Partition the mosaic into strips along detected row seams, then (on the
   row-corrected result) into strips along detected column seams.
5. Harmonize sequentially: walk strips in order, match each new strip's
   near-boundary band (median, robust to a few outlier pixels) to the previous
   (already-corrected) strip's band, apply that bias to the whole strip, ramped
   in over the first ~32px so there's no hard edge right at the seam.

**First run**: essentially no visible change (block-mean std `40.85` →
`40.85`). Root cause: a feathering-direction bug — the ramp was written backwards
(`1 - dist/FEATHER_RADIUS`, decaying *to* zero away from the seam) so the
correction was confined to a ~32px sliver at each seam and never applied to the
bulk of each strip. Fixed (`dist/FEATHER_RADIUS`, ramping *up* from the seam and
staying at full strength).

**After the fix**: a small, real change (std `40.85` → `39.12`), but still not a
fix:

![v2 after](scripts/experiments/fukuoka/images/05_v2_after.jpg)

The correction is visible as a faint overall tone shift, but the actual obvious
seams — the blue-tinted rectangle upper right, the green farmland block on the
left, the dark forest square left-of-center — are all still there, essentially
unchanged. Checking where the detector actually placed its seams:

![v2 seams](scripts/experiments/fukuoka/images/04_v2_seams.jpg)

The detected lines (red) don't track the visible block boundaries at all — several
column detections cluster right next to each other on the right edge, which looks
like it's responding to the mountain ridge's texture or the coastline, not a
mosaic seam.

**Root cause**: the row/column-median approach implicitly assumes a seam spans
the *entire* width or height of the test region — a single line cutting all the
way across. But the real seams visible in the image are **local rectangular
blocks** (a single flight's footprint covers part of the area, not a full
row/column), so averaging a candidate line's jump across its whole length dilutes
any real local seam into noise, while incidentally strong full-length features
(a coastline, a mountain ridge) can outscore it.

**Lesson**: seam detection needs to work in 2D, matching actual (roughly
rectangular) photo-tile footprints — not a 1D projection across the whole image.

## Conclusion

Neither attempt produced a usable color-harmonization result. Both failures are
informative, though:

- Attempt 1 confirms color-based segmentation alone can't distinguish "same
  photo source" from "same land cover" — any future approach needs a different
  signal than absolute pixel color, or needs to constrain the segmentation
  spatially (e.g., known tile/flight footprints) rather than clustering freely.
- Attempt 2 confirms that seams are local 2D rectangular regions, not lines
  spanning the whole scene — the right next step (not attempted here) would be
  genuine 2D block/rectangle detection, closer to what orthomosaic software does
  with actual camera footprint metadata, rather than inferring boundaries purely
  from pixel statistics after the fact.

**This idea is shelved for now, not abandoned** — color harmonization across
GSI's orthophoto mosaic seams remains a real, open problem worth revisiting with
a properly 2D, footprint-aware approach if there's appetite for a third attempt.
