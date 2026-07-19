"""
Build a z(N-1)..z1 pyramid from a single-zoom PMTiles seed via 2x2 box-average
downsampling, cascading one level at a time (mapterhorn-style).

Where the seed has no coverage for a quadrant (no real aerial photo *and* no
GSI satellite gap-fill at that finer zoom), fall back — in order — to:

  1. the ORIGINAL low-zoom GSI tile as already packaged in depot.optgeo.org's
     seamlessphoto512.pmtiles (cropped to the matching sub-region), then
  2. GSI's own live seamlessphoto XYZ endpoint, fetching the same tile fresh.

Tier 2 exists because depot's own z1-12 layer turns out to have a systemic
corruption problem — 7.4% of tiles in our Hokkaido sample decode to literal
all-zero bytes despite plausible-looking lengths — while GSI's live server
had valid data at every corrupt spot checked (see HANDOVER.md). Tier 1 is
tried first because it's already local/extracted and doesn't need network
round-trips; tier 2 only runs for the minority of quadrants where tier 1 is
itself missing or corrupt.

Separately, individual seed tiles can be *present* but still contain pure
black (0,0,0) nodata padding for sub-regions outside the real photo's actual
footprint (a coastline crossing the tile diagonally, or — in ~6% of tiles in
our sample — the whole tile being nodata despite decoding as a valid JPEG).
Because this is invisible to the "is this tile present at all" checks above,
it was previously baked into the output as literal black. `clean_seed_tile()`
detects black pixels within an otherwise-present tile and replaces them,
pixel-for-pixel, with GSI's own same-zoom live tile (which is satellite
imagery at this zoom range, not aerial — see HANDOVER.md's zoom/source
table) — the same idea as the quadrant-level fallback, just at finer
granularity so partial coastline tiles don't lose their black corner.

The seed zoom itself is never written to the output — it's redundant with
what the original archive already serves at that zoom and above, and the
deployment plan is to compose the two by (disjoint) zoom range in a style.json
rather than duplicate data (see README.md / examples/style.json).

Usage: python downsample.py <seed.pmtiles> <seed_zoom> <fallback.pmtiles> <output.pmtiles> [min_zoom]

min_zoom (default 2): the pyramid keeps cascading down to this zoom even
after it converges to a single tile (harmless — a single coarse tile is a
normal thing to have at low global zooms), rather than stopping as soon as
the tile count reaches 1.
"""
import functools
import io
import sys
from collections import defaultdict

import numpy as np
import requests
from PIL import Image, UnidentifiedImageError

from pmtiles.reader import Reader, MmapSource, all_tiles
from pmtiles.tile import TileType, Compression, zxy_to_tileid
from pmtiles.writer import Writer

TILE_SIZE = 512
JPEG_QUALITY = 85
GSI_URL_TEMPLATE = 'https://maps.gsi.go.jp/xyz/seamlessphoto/{z}/{x}/{y}.jpg'
GSI_TIMEOUT = 10

_http = requests.Session()


def load_level(path, zoom):
    """Return {(x, y): PIL.Image} for the given zoom level, with black nodata
    pixels within each tile replaced by GSI's own satellite imagery."""
    tiles = {}
    n_cleaned = 0
    with open(path, 'r+b') as f:
        reader = Reader(MmapSource(f))
        for (z, x, y), tile_bytes in all_tiles(reader.get_bytes):
            if z != zoom:
                continue
            img = safe_decode(tile_bytes)
            if img is None:
                continue
            img, cleaned = clean_seed_tile(img, zoom, x, y)
            n_cleaned += cleaned
            tiles[(x, y)] = img
    print(f'  cleaned black nodata pixels in {n_cleaned:_} tiles')
    return tiles


def clean_seed_tile(img, zoom, x, y):
    """Replace pure-black (0,0,0) pixels in `img` with the corresponding
    pixels from GSI's live tile at the same (zoom, x, y) — same index space,
    but satellite content rather than aerial photo at this zoom range (see
    module docstring). Returns (img, was_cleaned: bool)."""
    arr = np.asarray(img)
    black = np.all(arr == 0, axis=-1)
    if not black.any():
        return img, False
    reference = fetch_gsi_tile(zoom, x, y)
    if reference is None:
        return img, False
    if reference.size != img.size:
        reference = reference.resize(img.size, Image.LANCZOS)
    mask = Image.fromarray((black * 255).astype(np.uint8))
    return Image.composite(reference, img, mask), True


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
    depot_used = 0
    gsi_used = 0
    still_missing = 0
    for (px, py) in all_px:
        quads = parents[(px, py)]
        canvas = Image.new('RGB', (TILE_SIZE, TILE_SIZE))
        for qx in (0, 1):
            for qy in (0, 1):
                if (qx, qy) in quads:
                    small = quads[(qx, qy)].resize((half, half), Image.LANCZOS)
                    canvas.paste(small, (qx * half, qy * half))
                    continue
                small = fallback_quadrant(fallback_index, zoom - 1, px, py, qx, qy, half)
                if small is not None:
                    depot_used += 1
                else:
                    small = gsi_live_quadrant(zoom - 1, px, py, qx, qy, half)
                    if small is not None:
                        gsi_used += 1
                    else:
                        still_missing += 1
                        small = Image.new('RGB', (half, half), (0, 0, 0))
                canvas.paste(small, (qx * half, qy * half))
        out[(px, py)] = canvas
    if depot_used or gsi_used or still_missing:
        print(f'    z{zoom - 1}: {depot_used} quadrants backfilled from depot z1-12, '
              f'{gsi_used} from GSI live, {still_missing} still missing (no data anywhere)')
    return out


def fallback_quadrant(fallback_index, parent_zoom, px, py, qx, qy, half):
    """Crop the (qx, qy) quadrant out of the ORIGINAL depot tile at (parent_zoom, px, py)."""
    tile_bytes = fallback_index.get((parent_zoom, px, py))
    if tile_bytes is None:
        return None
    img = safe_decode(tile_bytes)
    if img is None:
        return None
    return crop_quadrant(img, qx, qy, half)


def gsi_live_quadrant(parent_zoom, px, py, qx, qy, half):
    """Crop the (qx, qy) quadrant out of a live GSI tile at (parent_zoom, px, py)."""
    img = fetch_gsi_tile(parent_zoom, px, py)
    if img is None:
        return None
    return crop_quadrant(img, qx, qy, half)


def crop_quadrant(img, qx, qy, half):
    w, h = img.size
    box = (qx * w // 2, qy * h // 2, (qx + 1) * w // 2, (qy + 1) * h // 2)
    return img.crop(box).resize((half, half), Image.LANCZOS)


@functools.lru_cache(maxsize=None)
def fetch_gsi_tile(zoom, x, y):
    """Fetch GSI's native 256px tile at (zoom, x, y) directly (single request).

    Same z/x/y index as depot's 512px tile at that zoom — verified directly:
    both known-corrupt depot tiles (z12/3655/1497, z12/3641/1520) return a
    valid 256px JPEG from this exact z/x/y on GSI's live server.
    """
    url = GSI_URL_TEMPLATE.format(z=zoom, x=x, y=y)
    try:
        resp = _http.get(url, timeout=GSI_TIMEOUT)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    return safe_decode(resp.content)


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
    target_min_zoom = int(sys.argv[5]) if len(sys.argv) > 5 else 2

    print(f'loading seed level z{seed_zoom} from {seed_path}...')
    level = load_level(seed_path, seed_zoom)
    print(f'  {len(level):_} tiles at z{seed_zoom}')

    print(f'indexing fallback archive {fallback_path}...')
    fallback_index = load_fallback_index(fallback_path)
    print(f'  {len(fallback_index):_} fallback tiles indexed')

    levels = {seed_zoom: level}
    z = seed_zoom
    while z > target_min_zoom:
        prev = levels[z]
        nxt = downsample_level(prev, z, fallback_index)
        z -= 1
        levels[z] = nxt
        print(f'  z{z}: {len(nxt):_} tiles')

    # The seed zoom itself is never written — it stays served straight from
    # the original archive (see module docstring).
    written_zooms = [z for z in levels if z != seed_zoom]
    min_zoom = min(written_zooms)
    max_zoom = max(written_zooms)

    min_lon, min_lat, max_lon, max_lat = 180.0, 90.0, -180.0, -90.0
    total = 0
    with open(output_path, 'wb') as out_f:
        writer = Writer(out_f)
        for z in sorted(written_zooms):
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
                    f'kitaphoto: z{seed_zoom} GSI seamlessphoto512 (with black nodata '
                    f'pixels cleaned via GSI live satellite tiles) downsampled to '
                    f'z{min_zoom}-{max_zoom} via 2x2 box averaging, with depot low-zoom '
                    f'satellite mosaic and GSI live tiles as fallback for gaps. '
                    f'z{seed_zoom}+ intentionally not included here — served from the '
                    f'original seamlessphoto512.pmtiles instead, see examples/style.json'
                ),
            },
        )
    print(f'wrote {output_path}: {total:_} tiles, z{min_zoom}-{max_zoom}')


if __name__ == '__main__':
    main()
