# kitaphoto

An improved version of [GSI seamlessphoto512](https://depot.optgeo.org/seamlessphoto512.pmtiles)
(国土地理院シームレス空中写真, re-tiled to 512px) with a real aerial-photo pyramid at low zoom
levels, instead of GSI's own low-resolution satellite fallback.

## Problem

GSI's seamlessphoto tile source switches data source by zoom level (see
[タイル一覧](https://maps.gsi.go.jp/development/ichiran.html)), in 512px-tile terms:

| zoom (512px) | source |
|---|---|
| z13–17 | 全国最新写真 — real aerial photography, with satellite (Landsat-8/GRUS) used only where no aerial photo exists |
| z8–12 | 全国Landsatモザイク — low-resolution nationwide satellite mosaic |
| z1–7 | 世界衛星モザイク — low-resolution world satellite mosaic |

At z13–17 the blend is already ideal: real photo where it exists, satellite only in genuine
coverage gaps. But below z13, GSI substitutes an entirely different, much lower-quality
satellite mosaic — even in places that have excellent aerial photo coverage at z13–17.

## Approach

Instead of reproducing GSI's z1-12 satellite mosaic, build z1-12 by downsampling z13 (2×2 box
averaging, cascading down to z1 — the same pyramid technique used by
[hfu/mapterhorn](https://github.com/hfu/mapterhorn)'s `downsampling` stage). Since z13 already
contains GSI's own photo/satellite blend, no separate coverage mask is needed: areas with real
aerial photography stay photographic all the way down the pyramid; areas that only ever had
satellite coverage at z13 stay satellite, just coarser — never regress to a *different*,
lower-quality satellite source than what's actually visible at z13.

z13–17 tiles are left untouched (pass-through) — they're already correct.

## Scope

Piloted on the z=4/x=14/y=5 tile (Hokkaido, Sakhalin, and surrounding sea — the same region
used in [hfu/4145](https://github.com/hfu/4145)), then rolled out nationwide once the pilot
algorithm was validated (black-nodata-pixel cleaning, GSI-live fallback tier — see
HANDOVER.md). Current output: `dst/kitaphoto.pmtiles`, z2–12, 747MB, 12,324 tiles, built from a
2.3GB z13 seed + 1.4GB z1–12 fallback (both extracted from the 715GB source archive via
`pmtiles extract`, never downloaded in full). Small enough to build entirely on a single
machine — no need for the 3TB SSD on `slate.local` originally considered for this project.

Runs on `/Users/hfu/kitaphoto` (persistent checkout, not session scratch space).
`dst/kitaphoto.pmtiles` is `.gitignore`d (same pattern as `hfu/kitavolca`'s `dst/*.pmtiles`) —
regenerate it with `just extract && just downsample`, or `just upload` it to `stars.local` once
built.

## Pipeline

1. **Extract** — `pmtiles extract` the z13 seed tiles (and, as a fallback source, the existing
   z1–12 tiles) for the target bbox from `https://depot.optgeo.org/seamlessphoto512.pmtiles`.
2. **Clean the seed** — before downsampling, replace pure-black (0,0,0) nodata pixels *within*
   otherwise-present z13 tiles (coastlines crossing a tile diagonally, or tiles that are
   entirely nodata despite decoding as a valid JPEG — about 13% of tiles in our sample have
   some) with GSI's own live tile at the same z/x/y, pixel-for-pixel. Without this, those
   nodata pixels get baked into the output as literal black, even though the tile is otherwise
   "present" and would never trigger the quadrant-level fallback in step 3.
3. **Downsample** (`scripts/downsample.py`) — build the z12→z1(ish) pyramid via 2×2 box
   averaging + JPEG re-encode, cascading down from the cleaned z13 seed. Where the seed has no
   coverage for a whole quadrant, fall back — in order — to (a) the original low-zoom GSI tile
   as packaged in depot's archive, cropped to the matching sub-region, then (b) GSI's own live
   seamlessphoto endpoint, fetched fresh. Tier (b) exists because depot's z1-12 layer turns out
   to have a systemic corruption problem (7.4% of tiles in our sample decode to literal
   all-zero bytes) that GSI's live server doesn't have — see HANDOVER.md.
4. **Ship as a patch archive, composed by zoom range in style.json — not merged.** The output
   only covers the zoom range it actually changes (roughly z1/4–12). z13–17 are left exactly as
   published in the original archive; there's no need to duplicate 190GB of unchanged data.
   `docs/style.json` + `docs/index.html` show the pattern: two raster sources
   (`kitaphoto-low`, `seamlessphoto512-high`) with adjacent, non-overlapping zoom ranges, same
   idea as [hfu/japan-seamless-aerial-z18](https://github.com/hfu/japan-seamless-aerial-z18)'s
   viewer. `scripts/merge.py` (a `go-pmtiles merge`-panic-safe reimplementation) stays available
   if a single self-contained file is ever wanted instead, but is no longer the primary plan.
5. **Verify** — `pmtiles verify`, plus visual spot checks across the z12/z13 boundary and in
   satellite-only/nodata-edge areas.

See [HANDOVER.md](HANDOVER.md) for current status, size measurements, and validation findings.

## Requirements

`go-pmtiles` (`pmtiles` CLI, for `extract`/`show`/`tile`/`verify` — its `merge` subcommand is
known broken, see HANDOVER.md) and Python 3 with `pmtiles`, `Pillow`, `numpy`, and `requests`.

## Data source and attribution

- Source: `https://depot.optgeo.org/seamlessphoto512.pmtiles` (GSI seamlessphoto, re-tiled to
  512px, z1-17, from 256px z2-z18)
- Attribution: 国土地理院 シームレス空中写真 (GSI seamlessphoto) CC BY 4.0
