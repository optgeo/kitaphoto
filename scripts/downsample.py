"""
Build a z(N-1)..z1 pyramid from a single-zoom PMTiles seed via 2x2 box-average
downsampling, cascading one level at a time (mapterhorn-style).

Where the seed has no coverage for a quadrant (no real aerial photo *and* no
GSI satellite gap-fill at that finer zoom), fall back to the ORIGINAL
low-zoom GSI tile (Landsat/world-satellite mosaic) for that quadrant, cropped
to the right sub-region, instead of leaving a black hole. This is what makes
"areas without high-quality aerial photos keep using satellite imagery" true
even where the seed zoom itself has a genuine gap (observed in practice: some
locations have literally zero-byte-pattern corrupt tiles in the source
archive, not just low quality ones — see HANDOVER.md).

Usage: python downsample.py <seed.pmtiles> <seed_zoom> <fallback.pmtiles> <output.pmtiles>
"""
import io
import sys
from collections import defaultdict

from PIL import Image, UnidentifiedImageError

from pmtiles.reader import Reader, MmapSource, all_tiles
from pmtiles.tile import TileType, Compression, zxy_to_tileid
from pmtiles.writer import Writer

TILE_SIZE = 512
JPEG_QUALITY = 85


def load_level(path, zoom):
    """Return {(x, y): PIL.Image} for the given zoom level."""
    tiles = {}
    with open(path, 'r+b') as f:
        reader = Reader(MmapSource(f))
        for (z, x, y), tile_bytes in all_tiles(reader.get_bytes):
            if z != zoom:
                continue
            img = safe_decode(tile_bytes)
            if img is not None:
                tiles[(x, y)] = img
    return tiles


def safe_decode(tile_bytes):
    try:
        return Image.open(io.BytesIO(tile_bytes)).convert('RGB')
    except UnidentifiedImageError:
        return None


def load_fallback_index(path):
    """Return {(z, x, y): raw tile bytes} for every tile in `path`.

    Kept as compressed bytes (not decoded) — most fallback tiles are never
    used, so decoding lazily avoids holding the whole level in raw pixels.
    """
    index = {}
    with open(path, 'r+b') as f:
        reader = Reader(MmapSource(f))
        for (z, x, y), tile_bytes in all_tiles(reader.get_bytes):
            index[(z, x, y)] = tile_bytes
    return index


def downsample_level(tiles, zoom, fallback_index):
    """2x2 box-average tiles at `zoom` into tiles at `zoom - 1`.

    Missing quadrants are backfilled from the original low-zoom archive
    (cropped to the matching sub-region) when available; left black only if
    the fallback also has no usable data there.
    """
    parents = defaultdict(dict)  # (px, py) -> {quadrant: img}
    all_px = set()
    for (x, y), img in tiles.items():
        px, py = x // 2, y // 2
        qx, qy = x % 2, y % 2
        parents[(px, py)][(qx, qy)] = img
        all_px.add((px, py))

    half = TILE_SIZE // 2
    out = {}
    fallback_used = 0
    still_missing = 0
    for (px, py) in all_px:
        quads = parents[(px, py)]
        canvas = Image.new('RGB', (TILE_SIZE, TILE_SIZE))
        for qx in (0, 1):
            for qy in (0, 1):
                if (qx, qy) in quads:
                    small = quads[(qx, qy)].resize((half, half), Image.LANCZOS)
                else:
                    small = fallback_quadrant(fallback_index, zoom - 1, px, py, qx, qy, half)
                    if small is not None:
                        fallback_used += 1
                    else:
                        still_missing += 1
                        small = Image.new('RGB', (half, half), (0, 0, 0))
                canvas.paste(small, (qx * half, qy * half))
        out[(px, py)] = canvas
    if fallback_used or still_missing:
        print(f'    z{zoom - 1}: {fallback_used} quadrants backfilled from original, '
              f'{still_missing} still missing (no fallback data either)')
    return out


def fallback_quadrant(fallback_index, parent_zoom, px, py, qx, qy, half):
    """Crop the (qx, qy) quadrant out of the ORIGINAL tile at (parent_zoom, px, py)."""
    tile_bytes = fallback_index.get((parent_zoom, px, py))
    if tile_bytes is None:
        return None
    img = safe_decode(tile_bytes)
    if img is None:
        return None
    w, h = img.size
    box = (qx * w // 2, qy * h // 2, (qx + 1) * w // 2, (qy + 1) * h // 2)
    return img.crop(box).resize((half, half), Image.LANCZOS)


def encode_jpeg(img):
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=JPEG_QUALITY)
    return buf.getvalue()


def tile_bounds(z, x, y):
    import math
    n = 2 ** z
    lon1, lon2 = x / n * 360 - 180, (x + 1) / n * 360 - 180

    def lat(yy):
        t = math.pi * (1 - 2 * yy / n)
        return math.degrees(math.atan(math.sinh(t)))

    return lon1, lat(y + 1), lon2, lat(y)


def main():
    seed_path, seed_zoom_s, fallback_path, output_path = sys.argv[1:5]
    seed_zoom = int(seed_zoom_s)

    print(f'loading seed level z{seed_zoom} from {seed_path}...')
    level = load_level(seed_path, seed_zoom)
    print(f'  {len(level):_} tiles at z{seed_zoom}')

    print(f'indexing fallback archive {fallback_path}...')
    fallback_index = load_fallback_index(fallback_path)
    print(f'  {len(fallback_index):_} fallback tiles indexed')

    levels = {seed_zoom: level}
    z = seed_zoom
    while z > 1 and len(levels[z]) > 1:
        prev = levels[z]
        nxt = downsample_level(prev, z, fallback_index)
        z -= 1
        levels[z] = nxt
        print(f'  z{z}: {len(nxt):_} tiles')

    min_zoom = min(levels)
    max_zoom = max(levels)

    min_lon, min_lat, max_lon, max_lat = 180.0, 90.0, -180.0, -90.0
    total = 0
    with open(output_path, 'wb') as out_f:
        writer = Writer(out_f)
        for z in sorted(levels):
            for (x, y), img in sorted(levels[z].items()):
                tile_bytes = encode_jpeg(img)
                tile_id = zxy_to_tileid(z, x, y)
                writer.write_tile(tile_id, tile_bytes)
                lon1, lat1, lon2, lat2 = tile_bounds(z, x, y)
                min_lon, max_lon = min(min_lon, lon1), max(max_lon, lon2)
                min_lat, max_lat = min(min_lat, lat1), max(max_lat, lat2)
                total += 1

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
                'description': (
                    f'kitaphoto: z{seed_zoom} GSI seamlessphoto512 downsampled '
                    f'to z{min_zoom}-{max_zoom - 1} via 2x2 box averaging, with '
                    f'original low-zoom satellite mosaic as fallback for gaps '
                    f'(z{max_zoom} is the untouched seed, included for reference)'
                ),
            },
        )
    print(f'wrote {output_path}: {total:_} tiles, z{min_zoom}-{max_zoom}')


if __name__ == '__main__':
    main()
