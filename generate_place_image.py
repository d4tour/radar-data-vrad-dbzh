"""
Generate a combined radar image (DBZH + VRADH, side-by-side) centered on a place name.
Legend bars at top, radar panels in middle, info bar at bottom.

Usage:
    python generate_place_image.py "Lubawa"
    python generate_place_image.py "London" --index 1
    python generate_place_image.py "Berlin" --scheme 2 --zoom 11
"""

import os, sys, io, time, re, argparse
from datetime import datetime, timezone, timedelta
import concurrent.futures

import requests
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import types
_cell_detect = types.ModuleType("cell_detect")
def _noop(*args, **kwargs):
    return [], [], []
_cell_detect.process_frames = _noop
sys.modules["cell_detect"] = _cell_detect

from radar_server import (
    STATIONS, _geod, latest_key, get_parsed, render_tile,
    _latlon_to_tile, _station_country, COUNTRY_CONFIG,
    _get_station_separate, elevations_for_station, VARIABLES,
    DBZH_LUT, DBZH_LUT_2, VRADH_LUT
)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "opencode-radar-image-generator/1.0"

CARTO_SUBDOMAINS = ["a", "b", "c"]
DARK_NOLABELS = "https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}.png"
DARK_LABELS   = "https://{s}.basemaps.cartocdn.com/dark_only_labels/{z}/{x}/{y}.png"


def _is_europe(lat, lon):
    return 34.0 <= lat <= 72.0 and -25.0 <= lon <= 45.0

def geocode_all(place_name):
    params = {"q": place_name, "format": "json", "limit": 5}
    europe_codes = ",".join([
        "al","ad","at","ba","be","bg","by","ch","cy","cz","de","dk","ee",
        "es","fi","fr","gb","gr","hr","hu","ie","is","it","lt","lu","lv",
        "md","me","mk","mt","nl","no","pl","pt","ro","rs","se","si","sk",
        "ua","xk"
    ])
    params["countrycodes"] = europe_codes
    for attempt in range(5):
        if attempt > 0:
            wait = 2 ** attempt
            print(f"  Waiting {wait}s before retry...")
            time.sleep(wait)
        time.sleep(1.0)
        resp = requests.get(NOMINATIM_URL, params=params,
                            headers={"User-Agent": USER_AGENT}, timeout=15)
        if resp.status_code == 429:
            print(f"  Nominatim rate limited (attempt {attempt+1}/5)")
            continue
        resp.raise_for_status()
        data = resp.json()
        break
    else:
        raise RuntimeError(f"Nominatim rate limit exceeded for '{place_name}', try again later")
    data = resp.json()
    if not data:
        raise ValueError(f"Place not found: {place_name}")
    results = []
    for item in data:
        display = item.get("display_name", "")
        lat = float(item["lat"])
        lon = float(item["lon"])
        if _is_europe(lat, lon):
            results.append((display, lat, lon))
    if not results:
        raise ValueError(f"No European results for: {place_name}")
    return results


def find_closest_station(lat, lon):
    best_id = None
    best_dist = float("inf")
    for sid, info in STATIONS.items():
        _, _, dist = _geod.inv(lon, lat, info["lon"], info["lat"])
        if dist < best_dist:
            best_dist = dist
            best_id = sid
    return best_id, best_dist


def fetch_tile(url_template, z, x, y, fallback_color=(30, 30, 35)):
    subdomain = CARTO_SUBDOMAINS[(x + y) % 3]
    url = (url_template.replace("{s}", subdomain)
                       .replace("{z}", str(z))
                       .replace("{x}", str(x))
                       .replace("{y}", str(y)))
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content)).convert("RGBA")
    except Exception:
        return Image.new("RGBA", (256, 256), (*fallback_color, 255))


def render_radar_tile(parsed, elev_idx, z, x, y, var, colorscheme="1"):
    try:
        png = render_tile(parsed, elev_idx, z, x, y, var, colorscheme=colorscheme)
        return Image.open(io.BytesIO(png)).convert("RGBA")
    except Exception:
        return Image.new("RGBA", (256, 256), (0, 0, 0, 0))


def _boost_labels(img, factor=1.8):
    arr = np.array(img, dtype=np.float32)
    arr[:, :, :3] = np.clip(arr[:, :, :3] * factor, 0, 255)
    arr[:, :, 3] = np.clip(arr[:, :, 3] * factor, 0, 255)
    return Image.fromarray(arr.astype(np.uint8), "RGBA")


_small_font = None
_font = None
_large_font = None


def _init_fonts():
    global _small_font, _font, _large_font
    try:
        _small_font = ImageFont.truetype("arial.ttf", 11)
        _font = ImageFont.truetype("arial.ttf", 14)
        _large_font = ImageFont.truetype("arial.ttf", 18)
    except Exception:
        _small_font = ImageFont.load_default()
        _font = ImageFont.load_default()
        _large_font = ImageFont.load_default()


def _draw_legend_bar(canvas, draw, x, y, width, height, var, colorscheme="1"):
    vmin = VARIABLES[var]["vmin"]
    vmax = VARIABLES[var]["vmax"]
    is_refl = var in ("DBZH", "TH")
    if is_refl:
        lut = DBZH_LUT_2 if colorscheme == "2" else DBZH_LUT
    else:
        lut = VRADH_LUT

    bar = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    bar_d = bar.load()
    for i in range(width):
        idx = int(i / width * 255)
        idx = min(idx, 255)
        c = tuple(int(v) for v in lut[idx])
        for j in range(height):
            bar_d[i, j] = c

    canvas.paste(bar, (x, y), bar)

    start_label = f"{vmin}"
    end_label = f"{vmax}"
    dy = height + 2
    draw.text((x, y + dy), start_label, fill=(180, 180, 180), font=_small_font)
    end_w = draw.textbbox((0, 0), end_label, font=_small_font)[2]
    draw.text((x + width - end_w, y + dy), end_label, fill=(180, 180, 180), font=_small_font)


def _parse_ts_to_utc2(ts_str):
    if ts_str == "N/A" or not ts_str:
        return "N/A"
    dt_utc = _parse_ts_dt(ts_str)
    tz_utc2 = timezone(timedelta(hours=2))
    dt_local = dt_utc.astimezone(tz_utc2)
    return dt_local.strftime("%Y-%m-%d %H:%M")

def _parse_ts_dt(ts_str):
    return datetime(
        int(ts_str[0:4]), int(ts_str[4:6]), int(ts_str[6:8]),
        int(ts_str[9:11]), int(ts_str[11:13]), tzinfo=timezone.utc
    )

def _is_stale(ts_str, max_minutes=30):
    if ts_str == "N/A" or not ts_str:
        return True
    dt = _parse_ts_dt(ts_str)
    age = datetime.now(timezone.utc) - dt
    return age.total_seconds() > max_minutes * 60


def generate_place_image(place_name, zoom=12, grid_size=5, elevation=None,
                         label_intensity=1.8, colorscheme="1", output_dir=None,
                         place_lat=None, place_lon=None):
    if output_dir is None:
        output_dir = os.path.dirname(os.path.abspath(__file__))
    _init_fonts()
    t_start = time.time()

    print(f"[1/7] Resolving place '{place_name}'...")
    if place_lat is None or place_lon is None:
        all_results = geocode_all(place_name)
        if len(all_results) > 1:
            print(f"       Multiple matches ({len(all_results)} found):")
            for i, (display, lat, lon) in enumerate(all_results):
                short = display[:80] + "..." if len(display) > 80 else display
                print(f"         [{i}] {short}  ({lat:.4f}, {lon:.4f})")
            print(f"       Using [0] by default. Use --index N to pick another.")
        _, place_lat, place_lon = all_results[0]
    print(f"       => {place_lat:.4f}, {place_lon:.4f}")

    print(f"[2/7] Finding closest radar station...")
    station_id, dist = find_closest_station(place_lat, place_lon)
    info = STATIONS[station_id]
    print(f"       => {info['name']} ({info['country'].upper()}), {dist/1000:.1f} km away")

    print(f"[3/7] Fetching latest radar data...")
    country = _station_country(station_id)
    cfg = COUNTRY_CONFIG[country]
    sep = _get_station_separate(station_id)
    if sep is None:
        sep = cfg["separate_elevations"]

    elevs = elevations_for_station(station_id)
    default_elev = elevation if elevation else (elevs[0] if elevs else "0.5")
    print(f"       Available elevations: {elevs}")
    print(f"       Using elevation: {default_elev}")

    dbzh_key = latest_key(station_id, "DBZH", default_elev if sep else None)

    vel_var = "VRADH"
    vel_key = latest_key(station_id, vel_var, default_elev if sep else None)
    if not vel_key:
        vel_var = "VRAD"
        vel_key = latest_key(station_id, vel_var, default_elev if sep else None)

    print(f"       DBZH key: {dbzh_key or 'N/A'}")
    print(f"       {vel_var} key: {vel_key or 'N/A'}")

    if not dbzh_key and not vel_key:
        raise RuntimeError("No radar data available for closest station")

    dbzh_parsed = get_parsed(dbzh_key) if dbzh_key else None
    vel_parsed = get_parsed(vel_key) if vel_key else None

    def extract_ts(key):
        m = re.search(r'@(\d{8}T\d{4})@', key or "")
        return m.group(1) if m else "N/A"

    dbzh_ts = extract_ts(dbzh_key)
    vel_ts = extract_ts(vel_key)
    latest_ts = dbzh_ts if dbzh_ts != "N/A" else vel_ts
    if vel_ts != "N/A" and vel_ts > latest_ts:
        latest_ts = vel_ts

    time_str = _parse_ts_to_utc2(latest_ts)

    no_data_dbzh = False
    no_data_vel = False
    if dbzh_key and _is_stale(dbzh_ts, 30):
        print(f"       WARNING: DBZH data is older than 30 min ({dbzh_ts})")
        dbzh_parsed = None
        no_data_dbzh = True
    if vel_key and _is_stale(vel_ts, 30):
        print(f"       WARNING: {vel_var} data is older than 30 min ({vel_ts})")
        vel_parsed = None
        no_data_vel = True

    def find_elev_idx(parsed, elev_str):
        if sep or not parsed:
            return 0
        el = float(elev_str)
        for i, ds in enumerate(parsed["datasets"]):
            if abs(float(ds["elevation"]) - el) < 0.01:
                return i
        return 0

    dbzh_elev_idx = find_elev_idx(dbzh_parsed, default_elev)
    vel_elev_idx = find_elev_idx(vel_parsed, default_elev)

    print(f"[4/7] Calculating tile grid at zoom {zoom}...")
    cx, cy = _latlon_to_tile(place_lat, place_lon, zoom)
    half = grid_size // 2
    tile_xs = list(range(cx - half, cx - half + grid_size))
    tile_ys = list(range(cy - half, cy - half + grid_size))
    tile_positions = [(x, y) for x in tile_xs for y in tile_ys]
    print(f"       Center tile: ({cx}, {cy}), Grid: {grid_size}x{grid_size}")

    print(f"[5/7] Rendering {len(tile_positions)} tiles...")
    cache = {}

    def worker(x, y):
        base = fetch_tile(DARK_NOLABELS, zoom, x, y)
        labels = _boost_labels(fetch_tile(DARK_LABELS, zoom, x, y), label_intensity)
        dbzh_t = render_radar_tile(dbzh_parsed, dbzh_elev_idx, zoom, x, y, "DBZH", colorscheme) if dbzh_parsed else None
        vel_t = render_radar_tile(vel_parsed, vel_elev_idx, zoom, x, y, vel_var) if vel_parsed else None
        return x, y, base, labels, dbzh_t, vel_t

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        fs = {pool.submit(worker, x, y): (x, y) for x, y in tile_positions}
        for f in concurrent.futures.as_completed(fs):
            x, y, base, labels, dbzh_t, vel_t = f.result()
            cache[(x, y)] = (base, labels, dbzh_t, vel_t)

    print(f"[6/7] Compositing panels...")
    T = 256
    panel_w = grid_size * T
    panel_h = grid_size * T

    dbzh_canvas = Image.new("RGBA", (panel_w, panel_h), (0, 0, 0, 0))
    vel_canvas = Image.new("RGBA", (panel_w, panel_h), (0, 0, 0, 0))

    for x in tile_xs:
        for y in tile_ys:
            px = (x - (cx - half)) * T
            py = (y - (cy - half)) * T
            base, labels, dbzh_t, vel_t = cache.get((x, y), (None, None, None, None))

            base = base or Image.new("RGBA", (T, T), (30, 30, 35, 255))
            labels = labels or Image.new("RGBA", (T, T), (0, 0, 0, 0))
            dbzh_t = dbzh_t or Image.new("RGBA", (T, T), (0, 0, 0, 0))
            vel_t = vel_t or Image.new("RGBA", (T, T), (0, 0, 0, 0))

            for canvas, radar_tile in [(dbzh_canvas, dbzh_t), (vel_canvas, vel_t)]:
                panel = Image.new("RGBA", (T, T), (0, 0, 0, 0))
                panel.paste(base, (0, 0), base)
                panel = Image.alpha_composite(panel, radar_tile)
                panel = Image.alpha_composite(panel, labels)
                canvas.paste(panel, (px, py), panel)

    print(f"[7/7] Assembling final image...")
    dbzh_rgb = Image.new("RGB", dbzh_canvas.size, (0, 0, 0))
    dbzh_rgb.paste(dbzh_canvas, (0, 0), dbzh_canvas)
    vel_rgb = Image.new("RGB", vel_canvas.size, (0, 0, 0))
    vel_rgb.paste(vel_canvas, (0, 0), vel_canvas)

    gap = 4
    header_h = 58
    bottom_h = 50
    total_w = panel_w * 2 + gap
    total_h = header_h + panel_h + bottom_h

    final = Image.new("RGB", (total_w, total_h), (0, 0, 0))
    draw = ImageDraw.Draw(final)

    final.paste(dbzh_rgb, (0, header_h))
    final.paste(vel_rgb, (panel_w + gap, header_h))

    no_data_font = ImageFont.truetype("arial.ttf", 32)
    no_data_color = (180, 180, 180)
    for panel_name, panel_nodata, panel_x in [
        ("DBZH", no_data_dbzh, 0), (vel_var, no_data_vel, panel_w + gap)
    ]:
        if panel_nodata:
            nd_text = f"No data"
            nd_bbox = draw.textbbox((0, 0), nd_text, font=no_data_font)
            nd_w = nd_bbox[2] - nd_bbox[0]
            nd_h = nd_bbox[3] - nd_bbox[1]
            nd_x = panel_x + (panel_w - nd_w) // 2
            nd_y = header_h + (panel_h - nd_h) // 2
            draw.text((nd_x, nd_y), nd_text, fill=no_data_color, font=no_data_font)

    dbg = (22, 22, 28)
    draw.rectangle([(0, 0), (panel_w, header_h)], fill=dbg)
    draw.rectangle([(panel_w + gap, 0), (total_w, header_h)], fill=dbg)

    l_margin = 16
    l_bar_h = 26
    l_bar_y = (header_h - l_bar_h) // 2 - 4
    l_bar_w = panel_w - l_margin * 2

    _draw_legend_bar(final, draw, l_margin, l_bar_y, l_bar_w, l_bar_h, "DBZH", colorscheme)
    _draw_legend_bar(final, draw, panel_w + gap + l_margin, l_bar_y, l_bar_w, l_bar_h, vel_var)

    bar_y = header_h + panel_h
    draw.rectangle([(0, bar_y), (total_w, total_h)], fill=dbg)

    station_str = f"{info['name']} ({info['country'].upper()})"
    left_line = f"{station_str}  {time_str}  DBZH"
    right_line = f"{station_str}  {time_str}  {vel_var}"

    one_line_font = ImageFont.truetype("arial.ttf", 26)
    text_y = bar_y + (bottom_h - 30) // 2

    margin = 14
    mid_gap_start = panel_w + gap

    lbbox = draw.textbbox((0, 0), left_line, font=one_line_font)
    lw = lbbox[2] - lbbox[0]
    rbbox = draw.textbbox((0, 0), right_line, font=one_line_font)
    rw = rbbox[2] - rbbox[0]
    cbbox = draw.textbbox((0, 0), place_name, font=one_line_font)
    cw = cbbox[2] - cbbox[0]

    max_left_x = margin + lw
    min_right_x = total_w - margin - rw
    city_center_x = (total_w - cw) // 2
    city_left_edge = city_center_x
    city_right_edge = city_center_x + cw

    if max_left_x > city_left_edge or min_right_x < city_right_edge:
        max_station = 12
        short_station = info['name'][:max_station] + ".." if len(info['name']) > max_station else info['name']
        station_str = f"{short_station} ({info['country'].upper()})"
        left_line = f"{station_str}  {time_str}  DBZH"
        right_line = f"{station_str}  {time_str}  {vel_var}"

    draw.text((margin, text_y), left_line, fill=(210, 210, 215), font=one_line_font)

    rbbox2 = draw.textbbox((0, 0), right_line, font=one_line_font)
    rw2 = rbbox2[2] - rbbox2[0]
    draw.text((total_w - margin - rw2, text_y), right_line, fill=(210, 210, 215), font=one_line_font)

    cbbox2 = draw.textbbox((0, 0), place_name, font=one_line_font)
    cw2 = cbbox2[2] - cbbox2[0]
    draw.text(((total_w - cw2) // 2, text_y), place_name, fill=(255, 240, 160), font=one_line_font)

    elapsed = time.time() - t_start
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', place_name.lower())
    output_path = os.path.join(output_dir, f"{safe_name}_z{zoom}_combined.png")
    n = 1
    while os.path.exists(output_path):
        output_path = os.path.join(output_dir, f"{safe_name}_z{zoom}_combined_{n}.png")
        n += 1
    final.save(output_path, "PNG")
    print(f"\n{'='*60}")
    print(f"  Done in {elapsed:.1f}s")
    print(f"  Output: {output_path}")
    print(f"  Size: {final.width} x {final.height} px")
    print(f"  Color scheme: {colorscheme}")
    print(f"{'='*60}")
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Generate combined radar image centered on a place")
    parser.add_argument("place", help="Place name (e.g., 'Lubawa', 'Berlin')")
    parser.add_argument("--index", type=int, default=None, help="If multiple place matches, pick this index")
    parser.add_argument("--scheme", type=str, default="1", choices=["1", "2"], help="Reflectivity color scheme: 1=colorful (default), 2=gray-to-red")
    parser.add_argument("--zoom", type=int, default=12, help="Zoom level (default: 12)")
    parser.add_argument("--grid", type=int, default=5, help="Tile grid size (default: 5)")
    parser.add_argument("--elev", type=str, default=None, help="Elevation angle in degrees, e.g. 0.5, 1.5 (default: lowest available)")
    parser.add_argument("--label-intensity", type=float, default=1.8, help="Label brightness boost factor (default: 1.8)")
    parser.add_argument("--output", "-o", default=None, help="Output directory (default: script folder)")
    args = parser.parse_args()

    all_results = geocode_all(args.place)
    if args.index is None:
        if len(all_results) > 1:
            print(f"Multiple matches for '{args.place}':")
            for i, (disp, lat, lon) in enumerate(all_results):
                short = disp[:90] + "..." if len(disp) > 90 else disp
                print(f"  [{i}] {short}  ({lat:.4f}, {lon:.4f})")
            print(f"Rerun with --index N (0-{len(all_results)-1}) to pick one.")
            sys.exit(0)
        args.index = 0
    if args.index >= len(all_results):
        print(f"Index {args.index} out of range (max {len(all_results)-1}).")
        sys.exit(1)
    display_name, place_lat, place_lon = all_results[args.index]

    display_place = display_name.split(",")[0].strip() if args.index > 0 else args.place

    generate_place_image(display_place,
                         zoom=args.zoom, grid_size=args.grid, elevation=args.elev,
                         label_intensity=args.label_intensity, colorscheme=args.scheme,
                         output_dir=args.output,
                         place_lat=place_lat, place_lon=place_lon)


if __name__ == "__main__":
    main()
