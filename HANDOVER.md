# Project Handover — kitaphoto

## Status: planning complete, build not yet started (2026-07-19)

## Decisions made

- **Scope**: Hokkaido pilot only, tile z=4/x=14/y=5 (bbox
  `135.0,40.97989806962013,157.5,55.77657301866769`). Full-Japan expansion later, once the
  pilot is validated — see README.md's size table, which suggests full-Japan is also cheap
  (~2.3GB z13 seed nationwide) if the pilot works out.
- **No coverage mask needed**: GSI's own z13-17 layer (512px terms) already blends real aerial
  photo with satellite-fallback for genuine gaps (see README.md "Problem"). Downsampling from
  z13 downward inherits that blend automatically. Confirmed via
  [maps.gsi.go.jp/development/ichiran.html](https://maps.gsi.go.jp/development/ichiran.html).
- **Host**: everything fits on a single machine given real data volumes (~1GB new output for
  the pilot) — `slate.local`'s 3TB SSD (actually 2TB, 225GB free — see below) is not needed for
  this scope.
- **Repo hygiene**: new repo (`optgeo/kitaphoto`), not built inside `hfu/photosynthesis`
  (Freetown-specific), `hfu/mapterhorn` (upstream fork, different purpose), or
  `hfu/faceless-cartographer` (unrelated project).

## Corrections made during planning (things the user's initial phrasing got approximately right
but needed verifying before acting on)

- User said tile "4/15/5" for the Hokkaido region; the actual tile containing Hokkaido is
  **4/14/5** (verified by computing tile bounds: 4/15/5 = 157.5°–180°E, open Pacific, no land；
  4/14/5 = 135°–157.5°E, 41°–55.8°N, which does contain Hokkaido). User confirmed 4/14/5 is
  correct.
- User said "講座室の航空写真" (kōza-shitsu — "seminar room['s aerial photos]"), almost
  certainly a speech-to-text misrecognition of "高画質の航空写真" (kōgashitsu — "high-quality
  aerial photos"). User confirmed this reading; there is no separate external aerial-photo
  source involved, just GSI's own z13-17 layer.
- User believed `slate.local`'s SSD was 3TB with ample free space; `diskutil list` on
  `slate.local` showed the actual external volume (`/Volumes/Migrate-2025-04`, mounted at
  `/Users/hfu/github`) is 2TB, with only 225GB free (1.6TB already used by other data). Turned
  out not to matter — see "Host" above.

## Size measurements (via `pmtiles extract --dry-run`, no actual download)

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
| z1-12, Hokkaido bbox (current, to be replaced) | 88,415 | 33,934 | 375 MB |
| z13 only, nationwide bbox | 492,755 | 31,842 | 2.3 GB |
| z1-17, nationwide bbox | 167,733,123 | 9,524,865 | 766 GB |

Key point: we only need the z13 seed (636 MB), never the z17 (140 GB) or full z13-17 (190 GB) —
z13 is already GSI's own photo/satellite blend, and downsampling from it is equivalent (for our
purposes) to downsampling from finer zooms, since z13 was itself built by GSI as an overview of
finer aerial imagery.

## Real (non-dry-run) extracts done so far

- `low_z1-12.pmtiles` — real extract, z1-12, Hokkaido bbox, from depot.optgeo.org. 375,333,500
  bytes on disk, matches the dry-run estimate. `pmtiles show` confirms: 33,934 tile entries,
  clustered, jpg, matches source metadata. This is the "current, to be replaced" baseline and
  also a same-format test file for merge experiments.
- z13 (and z13-17) extract attempts **failed with HTTP 530** (Cloudflare origin error) —
  depot.optgeo.org appears to be having an outage as of 2026-07-19 09:35 UTC. Confirmed via
  direct `curl -I` against the .pmtiles URL, and via repeated polling (6 attempts, 15s apart,
  all 530). Not something on our end — retry later.

## Next steps (blocked on depot.optgeo.org recovering)

1. Once depot.optgeo.org is back: extract real z13 tiles for the Hokkaido bbox, and test
   `pmtiles merge` (or the `mapterhorn/pipelines/merge_bundles.py` Python fallback, which was
   needed for `hfu/photosynthesis` because `go-pmtiles merge` v1.28.0 panicked on WebP bundle
   files — unclear yet whether the same panic hits jpg/gzip-internal-compression archives like
   this one) merging `low_z1-12.pmtiles`-shaped data with z13+ data. This was the user's
   explicit ask: verify the merge step works before building the full pipeline.
2. Write the downsampling script (2×2 box average + JPEG re-encode, z12→z1), scoped small since
   we don't need mapterhorn's aggregation/reprojection stages (source is already tiled Web
   Mercator).
3. Build the real z1-12 pyramid for the pilot region, merge with pass-through z13-17, verify
   (`pmtiles verify` + visual spot checks at the z12/z13 boundary and in known satellite-only
   areas), and report back before considering nationwide expansion or deployment to
   `stars.optgeo.org`.
