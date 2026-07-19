# kitaphoto — improved low-zoom pyramid for GSI seamlessphoto512
# z13 seed (real aerial photo + GSI's own satellite gap-fill) downsampled to
# z2-12, replacing GSI's separately-sourced low-zoom satellite mosaic.
# See README.md / HANDOVER.md for the full story.

set shell := ["bash", "-c"]

source_url := "https://depot.optgeo.org/seamlessphoto512.pmtiles"
bbox := env_var_or_default("BBOX", "122.920532,20.406420,153.989868,45.541946")  # nationwide by default
seed_zoom := "13"
min_zoom := env_var_or_default("MIN_ZOOM", "2")
venv := "/Users/hfu/photosynthesis/mapterhorn/pipelines/.venv/bin/python"

default:
    @just --list

# extract: pull the z13 seed and the z1-12 fallback for BBOX from depot.optgeo.org (no download of z14+)
extract:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p work
    echo "=== extracting z{{seed_zoom}} seed (bbox: {{bbox}}) ==="
    pmtiles extract {{source_url}} work/seed_z{{seed_zoom}}.pmtiles --bbox={{bbox}} --minzoom={{seed_zoom}} --maxzoom={{seed_zoom}}
    echo "=== extracting z1-12 fallback (bbox: {{bbox}}) ==="
    pmtiles extract {{source_url}} work/fallback_z1-12.pmtiles --bbox={{bbox}} --minzoom=1 --maxzoom=12
    ls -lh work/seed_z{{seed_zoom}}.pmtiles work/fallback_z1-12.pmtiles

# downsample: build the z{min_zoom}-12 pyramid from the seed, with depot + GSI-live fallback
downsample:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p dst
    {{venv}} scripts/downsample.py work/seed_z{{seed_zoom}}.pmtiles {{seed_zoom}} work/fallback_z1-12.pmtiles dst/kitaphoto.pmtiles {{min_zoom}}
    ls -lh dst/kitaphoto.pmtiles

# verify: structural check + summary
verify:
    pmtiles verify dst/kitaphoto.pmtiles
    pmtiles show dst/kitaphoto.pmtiles

# upload: rsync dst/kitaphoto.pmtiles to stars.local (same pattern as hfu/kitavolca).
# martin does NOT auto-restart — ssh in and `systemctl --user restart martin`
# separately once you're ready for it to go live (shared production service).
upload:
    #!/usr/bin/env bash
    set -euo pipefail
    [ -f dst/kitaphoto.pmtiles ] || { echo "❌ dst/kitaphoto.pmtiles not found — run 'just downsample' first"; exit 1; }
    rsync --progress dst/kitaphoto.pmtiles stars@stars.local:/home/stars/data/
    echo "✓ uploaded. Still needed: ssh stars.local 'systemctl --user restart martin'"

# clean: remove intermediate + output files (re-fetchable/rebuildable)
clean:
    rm -rf work dst
