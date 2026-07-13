"""
Generate a combined radar image (DBZH + VRADH, side-by-side) centered on a place name.
Legend bars at top, radar panels in middle, info bar at bottom.

Usage:
    python generate_place_image.py "Lubawa"
    python generate_place_image.py "London" --index 1
    python generate_place_image.py "Berlin" --scheme 2 --zoom 11
    python generate_place_image.py "Czestochowa" --station plram --time 202606301005
    python generate_place_image.py "Warsaw" --light
    python generate_place_image.py "Krakow" --osm
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
    DBZH_LUT, DBZH_LUT_2, VRADH_LUT,
    list_s3, _extract_timestamp, _ensure_elevation
)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "opencode-radar-image-generator/1.0"

CARTO_SUBDOMAINS = ["a", "b", "c"]
DARK_NOLABELS = "https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}.png"
DARK_LABELS   = "https://{s}.basemaps.cartocdn.com/dark_only_labels/{z}/{x}/{y}.png"
LIGHT_NOLABELS = "https://{s}.basemaps.cartocdn.com/light_nolabels/{z}/{x}/{y}.png"
LIGHT_LABELS   = "https://{s}.basemaps.cartocdn.com/light_only_labels/{z}/{x}/{y}.png"
OSM_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"


def _basemap_config(basemap):
    if basemap == "osm":
        return {
            "tile_url": OSM_URL,
            "labels_url": None,
            "fallback": (235, 235, 235),
            "bar_bg": (230, 230, 230),
            "bar_text": (50, 50, 50),
            "accent": (30, 80, 160),
        }
    elif basemap == "light":
        return {
            "tile_url": LIGHT_NOLABELS,
            "labels_url": LIGHT_LABELS,
            "fallback": (235, 235, 235),
            "bar_bg": (220, 220, 220),
            "bar_text": (50, 50, 50),
            "accent": (30, 80, 160),
        }
    else:
        return {
            "tile_url": DARK_NOLABELS,
            "labels_url": DARK_LABELS,
            "fallback": (30, 30, 35),
            "bar_bg": (22, 22, 28),
            "bar_text": (210, 210, 215),
            "accent": (255, 240, 160),
        }


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


def fetch_tile(url_template, z, x, y, fallback_color=(30, 30, 35), headers=None):
    subdomain = CARTO_SUBDOMAINS[(x + y) % 3]
    url = (url_template.replace("{s}", subdomain)
                       .replace("{z}", str(z))
                       .replace("{x}", str(x))
                       .replace("{y}", str(y)))
    try:
        kwargs = {"timeout": 15}
        if headers:
            kwargs["headers"] = headers
        resp = requests.get(url, **kwargs)
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


def _apply_opacity(img, factor):
    if factor is None or factor >= 1.0:
        return img
    arr = np.array(img, dtype=np.float32)
    arr[:, :, 3] = arr[:, :, 3] * factor
    return Image.fromarray(arr.astype(np.uint8), "RGBA")


def _boost_labels(img, factor=1.8):
    arr = np.array(img, dtype=np.float32)
    arr[:, :, :3] = np.clip(arr[:, :, :3] * factor, 0, 255)
    arr[:, :, 3] = np.clip(arr[:, :, 3] * factor, 0, 255)
    return Image.fromarray(arr.astype(np.uint8), "RGBA")


_small_font = None
_font = None
_large_font = None


_FONT_PATHS = [
    "arial.ttf",
    "/usr/share/fonts/liberation-sans-fonts/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]

def _open_font(size):
    for path in _FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()

def _init_fonts():
    global _small_font, _font, _large_font
    _small_font = _open_font(11)
    _font = _open_font(14)
    _large_font = _open_font(18)


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


def get_keys_for_time(station, variable, dt, elevation=None):
    from radar_server import list_s3_archive, ARCHIVE_BASE
    country = _station_country(station)
    cfg = COUNTRY_CONFIG[country]
    cc_upper = country.upper()
    sep = _get_station_separate(station)
    if sep is None:
        sep = cfg["separate_elevations"]
    prefix = f"{dt.strftime('%Y/%m/%d')}/{cc_upper}/{station}/{cfg['dir']}/"
    for s3_fn in [list_s3, list_s3_archive]:
        try:
            keys = s3_fn(prefix)
        except Exception:
            continue
        ts_str = dt.strftime("%Y%m%dT%H%M")
        if sep and elevation is not None:
            elev_str = _ensure_elevation(elevation)
            var_keys = [k for k in keys if f"@{ts_str}@" in k and f"@{elev_str}@" in k and f"@{variable}" in k and k.endswith(".h5")]
        else:
            var_keys = [k for k in keys if f"@{ts_str}@" in k and f"@{variable}" in k and k.endswith(".h5")]
        if not var_keys:
            def ts_diff(key):
                m = re.search(r'@(\d{8}T\d{4})@', key)
                if m:
                    try:
                        kt = datetime.strptime(m.group(1), "%Y%m%dT%H%M").replace(tzinfo=timezone.utc)
                        return abs((kt - dt).total_seconds())
                    except:
                        return float('inf')
                return float('inf')
            var_keys = [k for k in keys if f"@{variable}" in k and k.endswith(".h5")]
            var_keys.sort(key=ts_diff)
            if var_keys and ts_diff(var_keys[0]) <= 900:
                print(f"       Exact time not found, using closest ({_extract_timestamp(var_keys[0])})")
                return var_keys[0]
        if var_keys:
            return var_keys[0]
    return None
    return var_keys[0]


def generate_place_image(place_name, zoom=12, grid_size=5, elevation=None,
                         label_intensity=1.8, colorscheme="1", output_dir=None,
                         place_lat=None, place_lon=None, station_override=None,
                         target_time=None, basemap="dark", opacity=None,
                         variable="both"):
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

    print(f"[2/7] Finding radar station...")
    if station_override:
        if station_override not in STATIONS:
            valid = ", ".join(sorted(STATIONS.keys()))
            raise ValueError(f"Unknown station '{station_override}'. Valid: {valid}")
        station_id = station_override
        info = STATIONS[station_id]
        _, _, dist = _geod.inv(place_lon, place_lat, info["lon"], info["lat"])
        print(f"       => Override: {info['name']} ({info['country'].upper()}), {dist/1000:.1f} km from place")
    else:
        station_id, dist = find_closest_station(place_lat, place_lon)
        info = STATIONS[station_id]
        print(f"       => {info['name']} ({info['country'].upper()}), {dist/1000:.1f} km away")

    if target_time:
        print(f"[3/7] Fetching radar data for {target_time.strftime('%Y-%m-%d %H:%M')} UTC...")
    else:
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

    from radar_server import _extract_ts, _ts_to_dt

    want_dbzh = variable in ("DBZH", "both")
    want_vel = variable in ("VRADH", "both")

    if want_dbzh:
        if target_time:
            dbzh_key = get_keys_for_time(station_id, "DBZH", target_time, default_elev if sep else None)
        else:
            dbzh_key = latest_key(station_id, "DBZH", default_elev if sep else None)
        dbzh_ts = _extract_ts(dbzh_key)
    else:
        dbzh_key = None
        dbzh_ts = None

    sub_target = _ts_to_dt(dbzh_ts) if dbzh_ts else target_time

    if want_vel:
        if target_time:
            vel_var = "VRADH"
            vel_key = get_keys_for_time(station_id, vel_var, sub_target or target_time, default_elev if sep else None)
            if not vel_key:
                vel_var = "VRAD"
                vel_key = get_keys_for_time(station_id, vel_var, sub_target or target_time, default_elev if sep else None)
        else:
            vel_var = "VRADH"
            vel_key = latest_key(station_id, vel_var, default_elev if sep else None, target_time=sub_target)
            if not vel_key:
                vel_var = "VRAD"
                vel_key = latest_key(station_id, vel_var, default_elev if sep else None, target_time=sub_target)
        vel_ts = _extract_ts(vel_key)
    else:
        vel_key = None
        vel_ts = None
        vel_var = "VRADH"

    print(f"       DBZH key: {dbzh_key or 'N/A'}")
    print(f"       {vel_var} key: {vel_key or 'N/A'}")

    if not dbzh_key and not vel_key:
        raise RuntimeError("No radar data available for closest station")

    dbzh_parsed = get_parsed(dbzh_key) if dbzh_key else None
    vel_parsed = get_parsed(vel_key) if vel_key else None

    latest_ts = dbzh_ts if dbzh_ts and dbzh_ts != "N/A" else vel_ts
    if vel_ts and vel_ts != "N/A" and (latest_ts is None or vel_ts > latest_ts):
        latest_ts = vel_ts

    time_str = _parse_ts_to_utc2(latest_ts)

    no_data_dbzh = False
    no_data_vel = False
    if not target_time:
        if want_dbzh and dbzh_key and _is_stale(dbzh_ts, 30):
            print(f"       WARNING: DBZH data is older than 30 min ({dbzh_ts})")
            dbzh_parsed = None
            no_data_dbzh = True
        if want_vel and vel_key and _is_stale(vel_ts, 30):
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

    bm = _basemap_config(basemap)
    osm_headers = {"User-Agent": "opencode-radar-composite/1.0 (radar visualization; educational)"}

    print(f"[5/7] Rendering {len(tile_positions)} tiles...")
    cache = {}

    def worker(x, y):
        hdrs = osm_headers if basemap == "osm" else None
        base = fetch_tile(bm["tile_url"], zoom, x, y, bm["fallback"], hdrs)
        labels = None
        if bm["labels_url"]:
            labels = _boost_labels(fetch_tile(bm["labels_url"], zoom, x, y, (0, 0, 0, 0)), label_intensity)
        dbzh_t = render_radar_tile(dbzh_parsed, dbzh_elev_idx, zoom, x, y, "DBZH", colorscheme) if dbzh_parsed and want_dbzh else None
        vel_t = render_radar_tile(vel_parsed, vel_elev_idx, zoom, x, y, vel_var) if vel_parsed and want_vel else None
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

    num_panels = (1 if want_dbzh else 0) + (1 if want_vel else 0)
    gap = 4
    header_h = 58
    bottom_h = 50
    total_w = panel_w * num_panels + (gap if num_panels > 1 else 0)
    total_h = header_h + panel_h + bottom_h

    dbzh_canvas = Image.new("RGBA", (panel_w, panel_h), (0, 0, 0, 0)) if want_dbzh else None
    vel_canvas = Image.new("RGBA", (panel_w, panel_h), (0, 0, 0, 0)) if want_vel else None

    for x in tile_xs:
        for y in tile_ys:
            px = (x - (cx - half)) * T
            py = (y - (cy - half)) * T
            base, labels, dbzh_t, vel_t = cache.get((x, y), (None, None, None, None))

            base = base or Image.new("RGBA", (T, T), (*bm["fallback"], 255))
            labels = labels or Image.new("RGBA", (T, T), (0, 0, 0, 0))
            dbzh_t = dbzh_t or Image.new("RGBA", (T, T), (0, 0, 0, 0))
            vel_t = vel_t or Image.new("RGBA", (T, T), (0, 0, 0, 0))

            for canvas, radar_tile in (
                [(dbzh_canvas, dbzh_t)] if want_dbzh else []
            ) + (
                [(vel_canvas, vel_t)] if want_vel else []
            ):
                panel_img = Image.new("RGBA", (T, T), (0, 0, 0, 0))
                panel_img.paste(base, (0, 0), base)
                panel_img = Image.alpha_composite(panel_img, _apply_opacity(radar_tile, opacity))
                if labels:
                    panel_img = Image.alpha_composite(panel_img, labels)
                canvas.paste(panel_img, (px, py), panel_img)

    print(f"[7/7] Assembling final image...")
    final = Image.new("RGB", (total_w, total_h), (0, 0, 0))
    draw = ImageDraw.Draw(final)

    panel_offset = 0
    for pname, pcanvas, p_nodata in [
        ("DBZH", dbzh_canvas, no_data_dbzh),
        (vel_var, vel_canvas, no_data_vel)
    ]:
        if pcanvas is None:
            continue
        prgb = Image.new("RGB", pcanvas.size, (0, 0, 0))
        prgb.paste(pcanvas, (0, 0), pcanvas)
        final.paste(prgb, (panel_offset, header_h))

        no_data_font = _open_font(32)
        no_data_color = (180, 180, 180)
        if p_nodata:
            nd_text = "No data"
            nd_bbox = draw.textbbox((0, 0), nd_text, font=no_data_font)
            nd_w = nd_bbox[2] - nd_bbox[0]
            nd_h = nd_bbox[3] - nd_bbox[1]
            nd_x = panel_offset + (panel_w - nd_w) // 2
            nd_y = header_h + (panel_h - nd_h) // 2
            draw.text((nd_x, nd_y), nd_text, fill=no_data_color, font=no_data_font)

        draw.rectangle([(panel_offset, 0), (panel_offset + panel_w, header_h)], fill=bm["bar_bg"])

        l_margin = 16
        l_bar_h = 26
        l_bar_y = (header_h - l_bar_h) // 2 - 4
        l_bar_w = panel_w - l_margin * 2
        scheme = colorscheme if pname == "DBZH" else "1"
        _draw_legend_bar(final, draw, panel_offset + l_margin, l_bar_y, l_bar_w, l_bar_h, pname, scheme)

        panel_offset += panel_w + gap

    bar_y = header_h + panel_h
    draw.rectangle([(0, bar_y), (total_w, total_h)], fill=bm["bar_bg"])

    one_line_font = _open_font(26)
    text_y = bar_y + (bottom_h - 30) // 2
    margin = 14

    lines = []
    if want_dbzh:
        lines.append(f"{info['name']} ({info['country'].upper()})  {time_str}  DBZH")
    if want_vel:
        lines.append(f"{info['name']} ({info['country'].upper()})  {time_str}  {vel_var}")

    if num_panels == 1:
        line = lines[0]
        lbbox = draw.textbbox((0, 0), line, font=one_line_font)
        lw = lbbox[2] - lbbox[0]
        cbbox = draw.textbbox((0, 0), place_name, font=one_line_font)
        cw = cbbox[2] - cbbox[0]
        max_left_allowed = margin + lw
        if max_left_allowed > total_w - margin - cw - margin:
            max_station = 12
            short_station = info['name'][:max_station] + ".." if len(info['name']) > max_station else info['name']
            line = f"{short_station} ({info['country'].upper()})  {time_str}  {'DBZH' if want_dbzh else vel_var}"
            lbbox = draw.textbbox((0, 0), line, font=one_line_font)
            lw = lbbox[2] - lbbox[0]
        draw.text((margin, text_y), line, fill=bm["bar_text"], font=one_line_font)
        cbbox2 = draw.textbbox((0, 0), place_name, font=one_line_font)
        cw2 = cbbox2[2] - cbbox2[0]
        draw.text(((total_w - cw2) // 2, text_y), place_name, fill=bm["accent"], font=one_line_font)
    else:
        left_line, right_line = lines
        lbbox = draw.textbbox((0, 0), left_line, font=one_line_font)
        lw = lbbox[2] - lbbox[0]
        rbbox = draw.textbbox((0, 0), right_line, font=one_line_font)
        rw = rbbox[2] - rbbox[0]
        cbbox = draw.textbbox((0, 0), place_name, font=one_line_font)
        cw = cbbox[2] - cbbox[0]
        max_left_x = margin + lw
        min_right_x = total_w - margin - rw
        city_center_x = (total_w - cw) // 2
        if max_left_x > city_center_x or (city_center_x + cw) > min_right_x:
            max_station = 12
            short_station = info['name'][:max_station] + ".." if len(info['name']) > max_station else info['name']
            station_str = f"{short_station} ({info['country'].upper()})"
            left_line = f"{station_str}  {time_str}  DBZH"
            right_line = f"{station_str}  {time_str}  {vel_var}"
        draw.text((margin, text_y), left_line, fill=bm["bar_text"], font=one_line_font)
        rbbox2 = draw.textbbox((0, 0), right_line, font=one_line_font)
        rw2 = rbbox2[2] - rbbox2[0]
        draw.text((total_w - margin - rw2, text_y), right_line, fill=bm["bar_text"], font=one_line_font)
        cbbox2 = draw.textbbox((0, 0), place_name, font=one_line_font)
        cw2 = cbbox2[2] - cbbox2[0]
        draw.text(((total_w - cw2) // 2, text_y), place_name, fill=bm["accent"], font=one_line_font)

    station_short = station_id[2:].lower()
    elapsed = time.time() - t_start
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', place_name.lower())
    var_tag = variable.lower()
    output_path = os.path.join(output_dir, f"{safe_name}_z{zoom}_{var_tag}_{station_short}.png")
    n = 1
    while os.path.exists(output_path):
        output_path = os.path.join(output_dir, f"{safe_name}_z{zoom}_{var_tag}_{n}_{station_short}.png")
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
    parser.add_argument("--station", default=None, help="Radar station ID (e.g. plrze, plleg). Overrides closest-station logic.")
    parser.add_argument("--opacity", type=float, default=None,
                        help="Radar overlay opacity 0-1 (e.g. 0.5 for 50%% transparency, default: 1.0)")
    parser.add_argument("--var", type=str, default="both", choices=["DBZH", "VRADH", "both"],
                        help="Which variable to show: DBZH, VRADH, or both (default: both)")
    parser.add_argument("--time", type=str, default=None,
                        help="Specific timestamp in UTC: YYYYMMDDHHMM (e.g. 202606301005). Renders historical frame instead of latest.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--light", action="store_true", help="Light basemap (CartoDB light)")
    group.add_argument("--osm", action="store_true", help="OSM standard basemap")
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

    target_time = None
    if args.time:
        if len(args.time) != 12:
            print("Error: --time must be 12 digits: YYYYMMDDHHMM")
            sys.exit(1)
        try:
            target_time = datetime.strptime(args.time, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
        except ValueError:
            print("Error: --time must be in YYYYMMDDHHMM format (e.g. 202606301005)")
            sys.exit(1)

    basemap = "osm" if args.osm else ("light" if args.light else "dark")

    generate_place_image(display_place,
                         zoom=args.zoom, grid_size=args.grid, elevation=args.elev,
                         label_intensity=args.label_intensity, colorscheme=args.scheme,
                         output_dir=args.output,
                         place_lat=place_lat, place_lon=place_lon,
                         station_override=args.station,
                         target_time=target_time,
                         basemap=basemap, opacity=args.opacity,
                         variable=args.var)


if __name__ == "__main__":
    main()
