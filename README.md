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

## Scope (pilot)

Limited to the z=4/x=14/y=5 tile (bbox `135.0,40.97989806962013,157.5,55.77657301866769` —
Hokkaido, Sakhalin, and surrounding sea), matching the region used in
[hfu/4145](https://github.com/hfu/4145). Measured with `pmtiles extract --dry-run` against the
source archive:

| data | size |
|---|---|
| z13 seed (this pipeline's input) | 636 MB |
| current z1–12 in this region (what we're replacing) | 375 MB |
| new z1–12 pyramid (estimated, ~4/3 × z13 size) | ~850 MB |

Small enough to build entirely on a single machine — no need for the 3TB SSD on `slate.local`
originally considered for this project.

## Pipeline

1. **Extract** — `pmtiles extract` the z13 seed tiles for the target bbox from
   `https://depot.optgeo.org/seamlessphoto512.pmtiles`.
2. **Downsample** — build the z12→z1 pyramid via 2×2 box averaging + JPEG re-encode.
3. **Merge** — combine the new z1–12 pyramid with the original z13–17 tiles (pass-through,
   unmodified) into one archive.
4. **Verify** — `pmtiles verify`, plus visual spot checks across the z12/z13 boundary and in
   satellite-only areas.

See [HANDOVER.md](HANDOVER.md) for current status and findings.

## Data source and attribution

- Source: `https://depot.optgeo.org/seamlessphoto512.pmtiles` (GSI seamlessphoto, re-tiled to
  512px, z1-17, from 256px z2-z18)
- Attribution: 国土地理院 シームレス空中写真 (GSI seamlessphoto) CC BY 4.0
