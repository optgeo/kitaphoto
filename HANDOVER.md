# Project Handover — kitaphoto

## Status: pilot pipeline proven end-to-end on real data (2026-07-19)

`scripts/downsample.py` + `scripts/merge.py` successfully built and validated a real z4-13
pyramid for the Hokkaido bbox from live `depot.optgeo.org/seamlessphoto512.pmtiles` data.
Visual spot checks confirm the output is a large quality improvement over GSI's own low-zoom
satellite mosaic, including correct fallback behavior for genuine coverage gaps. Not yet run
at full scope (this session only processed the z13 seed down to z4 — a single z4 tile's worth
— not multiple z4 tiles or all of Hokkaido's full zoom range 1-13) or deployed.

## Decisions made

- **Scope**: Hokkaido pilot only, tile z=4/x=14/y=5 (bbox
  `135.0,40.97989806962013,157.5,55.77657301866769`). Full-Japan expansion later.
- **No separate coverage mask needed**: GSI's own z13-17 layer (512px terms) already blends
  real aerial photo with satellite-fallback for genuine gaps. Downsampling from z13 downward
  inherits that blend automatically. Confirmed via
  [maps.gsi.go.jp/development/ichiran.html](https://maps.gsi.go.jp/development/ichiran.html).
- **Deliverable shape: a z1-12 "patch" archive, not a full merged z1-17 file.** z13-17 stay
  exactly as published in the original `seamlessphoto512.pmtiles` (2.33M tiles / 190GB for the
  Hokkaido bbox alone — no reason to duplicate that). `kitaphoto`'s output only needs to cover
  the zoom range it actually changes (z1/4-12, ~1GB). At serving time, combine as two raster
  sources by zoom range (same pattern already used in
  [hfu/japan-seamless-aerial-z18](https://github.com/hfu/japan-seamless-aerial-z18)'s viewer),
  rather than shipping one giant merged file.
- **Host**: everything fits on a single machine given real data volumes — `slate.local`'s SSD
  (2TB, 225GB free, not 3TB as originally assumed) is not needed for this scope. This session's
  work was all done directly on `aalto`.
- **Repo hygiene**: new repo (`optgeo/kitaphoto`), not built inside `hfu/photosynthesis`
  (Freetown-specific), `hfu/mapterhorn` (upstream fork, different purpose), or
  `hfu/faceless-cartographer` (unrelated project).

## Corrections made during planning

- User said tile "4/15/5" for the Hokkaido region; the actual tile containing Hokkaido is
  **4/14/5** (4/15/5 = 157.5°-180°E, open Pacific, no land). User confirmed 4/14/5.
- User said "講座室の航空写真" — almost certainly a speech-to-text misrecognition of
  "高画質の航空写真". User confirmed: use GSI's own z13-17 layer, no separate external source.
- User believed `slate.local`'s SSD was 3TB with ample free space; actual external volume
  (`/Volumes/Migrate-2025-04`, mounted at `/Users/hfu/github`) is 2TB with 225GB free. Turned
  out not to matter — real data volumes are far smaller than originally estimated (see below).

## Size measurements (`pmtiles extract --dry-run`, no download)

Against `https://depot.optgeo.org/seamlessphoto512.pmtiles` (spec v3, jpg tiles, gzip internal
compression, clustered, z1-17, 715 GiB / 767,579,254,057 bytes total, 9,772,357 addressed
tiles nationwide):

| query | region tiles scanned | result entries | size |
|---|---|---|---|
| z1-17, Hokkaido bbox | 89,531,751 | 2,363,851 | 190 GB |
| z17 only, Hokkaido bbox | 67,141,635 | 1,739,829 | 140 GB |
| z13-17, Hokkaido bbox | 89,443,336 | 2,329,917 | 190 GB |
| **z13 only, Hokkaido bbox (our actual seed)** | 263,169 | 8,874 | **636 MB** |
| z12 only, Hokkaido bbox | 66,049 | 22,469 | 249 MB |
| z1-12, Hokkaido bbox (current, being replaced) | 88,415 | 33,934 | 375 MB |
| z13 only, nationwide bbox | 492,755 | 31,842 | 2.3 GB |
| z1-17, nationwide bbox | 167,733,123 | 9,524,865 | 766 GB |

We only ever need the z13 seed (636 MB) and the existing z1-12 (375 MB, used only as fallback
source, see below) — never z17 or the full z13-17 range.

## Merge mechanics — verified working (this was the user's specific ask)

**`pmtiles merge` (go-pmtiles v1.28.0) panics on invocation**, confirmed again in this project
(previously seen in `hfu/photosynthesis` against WebP bundle files):

```
$ pmtiles merge merged_test.pmtiles low_z1-12.pmtiles high_z13.pmtiles
panic: merge <output> <input>
goroutine 1 [running]:
main.main()
        github.com/protomaps/go-pmtiles/main.go:248 +0xfd0
```

Same panic regardless of tile format (jpg here vs. webp in photosynthesis) — this is a bug in
the CLI's argument handling, not format-specific.

**Fix: `scripts/merge.py`**, a direct reimplementation using the `pmtiles` Python library
(`Reader`/`Writer`, same approach as `mapterhorn/pipelines/merge_bundles.py`), which streams
tiles from each input in order and writes them to the output. Tested end-to-end against real
extracted data:

- Input: `low_z1-12.pmtiles` (real z1-12 extract, 76,506 tiles) + `high_z13.pmtiles` (real z13
  extract, 8,887 tiles) — disjoint zoom ranges, as our actual use case will be.
- Result: 85,393 tiles written, **zero duplicate tile_ids**, completed in ~4.5s.
- `pmtiles verify merged_test.pmtiles` → passes (`Completed verify in 8.376417ms`).
- `pmtiles show merged_test.pmtiles` → correct tile/content counts (42,805 entries / 34,158
  contents after gzip dedup — consistent with realistic imagery, not corruption).
- Fetched real tiles from the merged output at both zoom levels (`pmtiles tile ... 13 7312
  3008` and `... 12 3656 1504`, a Sapporo location) and visually confirmed: z13 shows sharp
  real aerial photo (roads, rivers, buildings); z12 (pass-through, unmodified original) shows
  the washed-out blue Landsat mosaic — exactly the quality gap this project exists to fix, and
  proof the merge doesn't corrupt either side's content.

**Conclusion: merging is not a risk.** `scripts/merge.py` handles it cleanly; the only
requirement is that input zoom ranges stay disjoint (it now raises an error on any duplicate
tile_id rather than silently overwriting).

Note: the final deliverable design (see "Deliverable shape" above) means the merge step may
not even be needed for the actual release — the patch archive only needs to contain z1-12,
served as a separate raster source layered under the original z13-17. `scripts/merge.py` stays
useful if a single self-contained file is ever wanted instead.

## Downsampling — built and validated on real data

`scripts/downsample.py`: loads a single-zoom PMTiles seed (z13 here), cascades 2×2 box-average
downsampling from z13 down to z1 (mapterhorn-style), re-encoding each output tile as JPEG
(quality 85).

First run (no fallback): z13 (8,887 tiles) → z12 (2,412) → z11 (694) → z10 (206) → z9 (66) →
z8 (24) → z7 (9) → z6 (5) → z5 (2) → **z4 (1 tile)** — correctly terminates at exactly the
single z4/14/5 tile, confirming the region math end-to-end. Total runtime ~2m40s on this
machine for the whole cascade.

Visual result (Sapporo, z12): the new downsampled tile shows real aerial-photo detail (roads,
buildings, river) — a dramatic improvement over the original z12 Landsat mosaic tile at the
same location (washed-out blue/purple, no usable detail). Screenshots compared during this
session; not committed to the repo (binary, regenerate via the scripts to inspect).

### Found during validation: a real gap-handling bug, and a real source-data anomaly

Inspecting partial-coverage tiles (a z12 tile whose 4 z13 children aren't all present) surfaced
two things:

1. **First version of the script left missing quadrants pure black.** This actively regresses
   on the "preserve satellite imagery where no aerial photo exists" requirement — the original
   archive has real (if low-quality) content there, and turning it into a black hole is worse
   than doing nothing.
2. **The original `seamlessphoto512.pmtiles` itself has at least one corrupt tile**: z12 tile
   (3655, 1497) contains 38,085 bytes of literal `\x00` padding — not a valid JPEG (no FFD8
   marker), confirmed via both `pmtiles tile` CLI and direct Python `pmtiles.reader` access.
   This is a genuine anomaly in GSI's/depot's source data, not something introduced by this
   pipeline.

**Fix**: `scripts/downsample.py` now takes a `<fallback.pmtiles>` argument (the original
low-zoom archive). For any quadrant missing from the seed at a given cascade step, it looks up
the corresponding tile in the fallback archive, crops out the matching quadrant sub-region, and
pastes that instead of leaving it black — only falling back to black if the fallback tile is
also missing or fails to decode (`PIL.UnidentifiedImageError`, handled explicitly — this is how
the corrupt tile above was made to degrade safely instead of crashing the whole run).

Re-run with fallback enabled, same Hokkaido bbox:

| level | quadrants backfilled from original | still missing (no fallback either) |
|---|---|---|
| z12 | 345 | 416 |
| z11 | 364 | 0 |
| z10 | 130 | 0 |
| z9 | 58 | 0 |
| z8 | 30 | 0 |
| z7 | 12 | 0 |
| z6 | 11 | 0 |
| z5 | 3 | 0 |
| z4 | 2 | 0 |

Only z12 has any unrecoverable gaps (416 quadrants, out of 2,412 tiles × 4 quadrants = 9,648
total — about 4.3%) — these are locations where *both* the z13 seed and the original z12
archive lack usable data (the corrupt-tile case above, and presumably similar isolated gaps
elsewhere). At every coarser level the fallback closes 100% of gaps, because a coarser tile's
4 children average out any single bad sub-area against its (mostly good) neighbors. Visually
confirmed: a previously-all-black quadrant (z12 tile 3650/1528, open water near a coastline)
now shows the original dark-blue satellite ocean tone instead of a black hole; the real-photo
coastline in the same tile is unaffected.

## Real (non-dry-run) extracts / builds done so far (test artifacts, not committed — see
`.gitignore`)

- `low_z1-12.pmtiles` — real extract, z1-12, Hokkaido bbox. 375,333,500 bytes, 33,934 tile
  entries. Used both as the "current, to be replaced" baseline and as the fallback source.
- `high_z13.pmtiles` — real extract, z13, Hokkaido bbox. 636,556,325 bytes, 8,874 tile entries.
  The downsampling seed.
- `pyramid_v2.pmtiles` — real output of `scripts/downsample.py high_z13.pmtiles 13
  low_z1-12.pmtiles pyramid_v2.pmtiles`. 12,306 tiles, z4-13 (z13 = untouched seed passthrough,
  z4-12 = new pyramid with fallback).
- `merged_test.pmtiles` — real output of `scripts/merge.py`, proving the merge mechanics (see
  above). Superseded by the "ship z1-12 as its own patch archive" decision, but the script and
  the verification remain valid/available if a single merged file is wanted later.
- One transient depot.optgeo.org outage encountered mid-session (HTTP 530, Cloudflare origin
  error, ~2 minutes, resolved on its own) — not something on our end, just noting in case it
  recurs.

## Next steps

1. Decide: is the z12 gap rate (~4.3% of quadrants, concentrated in what looks like open-water/
   remote areas) acceptable to ship as-is, or worth investigating further (e.g. is there a
   pattern — all near a specific coastline, all corrupt-tile artifacts, etc.)?
2. Run the full pipeline for **all** z4 tiles needed to cover Hokkaido properly (this session
   only processed the single z4/14/5 tile — confirm whether that one tile's coverage is
   actually sufficient, or whether Hokkaido's true extent needs neighboring z4 tiles too).
3. Decide on final min zoom — this run went down to z4 (limited by the seed data's actual
   extent within the bbox); confirm whether the real deliverable should extend further down
   (z1-3) using the original low-zoom tiles unmodified (they're already appropriately coarse
   world/nationwide mosaics at that level) or stop at z4.
4. Write a small end-user viewer (à la `hfu/japan-seamless-aerial-z18`'s `index.html`) to
   preview the patch archive layered under the original z13-17, for a final visual sign-off
   before considering deployment to `stars.optgeo.org`'s martin catalog.
