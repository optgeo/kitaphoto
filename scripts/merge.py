"""
Stream-merge PMTiles archives with disjoint zoom ranges into one archive.

`go-pmtiles merge` (v1.28.0) panics on invocation (confirmed against both a
WebP archive in hfu/photosynthesis and a jpg archive here — see
HANDOVER.md) — this reimplements the merge directly against the tiles
themselves, following the same approach as
hfu/mapterhorn/pipelines/merge_bundles.py.

Zoom ranges across inputs must not overlap; this errors out on collision
(via `--fail-on-duplicate`, on by default) rather than silently
overwriting.

Usage: python merge.py <output.pmtiles> <input1.pmtiles> <input2.pmtiles> ...
"""
import sys

from pmtiles.reader import Reader, MmapSource, all_tiles
from pmtiles.tile import TileType, Compression, zxy_to_tileid
from pmtiles.writer import Writer

import math


def tile_bounds(z, x, y):
    n = 2 ** z
    lon1, lon2 = x / n * 360 - 180, (x + 1) / n * 360 - 180

    def lat(yy):
        t = math.pi * (1 - 2 * yy / n)
        return math.degrees(math.atan(math.sinh(t)))

    return lon1, lat(y + 1), lon2, lat(y)


def main():
    output_path, inputs = sys.argv[1], sys.argv[2:]
    if not inputs:
        print(__doc__)
        sys.exit(1)

    min_lon, min_lat, max_lon, max_lat = 180.0, 90.0, -180.0, -90.0
    min_zoom, max_zoom = 99, -1
    total = 0
    seen = set()

    with open(output_path, 'wb') as out_f:
        writer = Writer(out_f)
        for path in inputs:
            with open(path, 'r+b') as in_f:
                reader = Reader(MmapSource(in_f))
                count = 0
                for (z, x, y), tile_bytes in all_tiles(reader.get_bytes):
                    tile_id = zxy_to_tileid(z, x, y)
                    if tile_id in seen:
                        raise SystemExit(
                            f'duplicate tile_id {tile_id} ({z}/{x}/{y}) from {path} — '
                            f'inputs must have disjoint zoom ranges'
                        )
                    seen.add(tile_id)
                    writer.write_tile(tile_id, tile_bytes)
                    lon1, lat1, lon2, lat2 = tile_bounds(z, x, y)
                    min_lon, max_lon = min(min_lon, lon1), max(max_lon, lon2)
                    min_lat, max_lat = min(min_lat, lat1), max(max_lat, lat2)
                    min_zoom, max_zoom = min(min_zoom, z), max(max_zoom, z)
                    total += 1
                    count += 1
                print(f'done with {path}: {count:_} tiles (running total {total:_})')

        writer.finalize(
            {
                'tile_type': TileType.JPEG,
                'tile_compression': Compression.NONE,
                'min_lon_e7': int(min_lon * 1e7),
                'min_lat_e7': int(min_lat * 1e7),
                'max_lon_e7': int(max_lon * 1e7),
                'max_lat_e7': int(max_lat * 1e7),
                'min_zoom': min_zoom,
                'max_zoom': max_zoom,
                'center_zoom': min_zoom,
                'center_lon_e7': int((min_lon + max_lon) / 2 * 1e7),
                'center_lat_e7': int((min_lat + max_lat) / 2 * 1e7),
            },
            {
                'attribution': '国土地理院 シームレス空中写真 (GSI seamlessphoto) CC BY 4.0',
                'description': f'kitaphoto: merged from {", ".join(inputs)}',
            },
        )
    print(f'wrote {output_path}: {total:_} tiles, z{min_zoom}-{max_zoom}, {len(seen):_} unique tile_ids')


if __name__ == '__main__':
    main()
