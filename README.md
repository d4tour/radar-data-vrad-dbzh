# Radar Image Generator

Generates a **side-by-side** radar image (reflectivity DBZH + velocity VRADH) centered on any place name, with dark basemap

Used on windows 11

## Required files

Copy these **2 files** to any device:

```
radar_server.py
generate_place_image.py
```

## Install dependencies

```powershell
pip install requests numpy pillow h5py pyproj rasterio fastapi uvicorn scipy websockets
```

## Basic usage

```powershell
python generate_place_image.py "Lubawa"
python generate_place_image.py "Berlin"
```

Output: `lubawa_z12_combined.png` in the script folder.

## Options

| `--zoom` | `12` | Map zoom level (higher = more detail) |
| `--grid` | `5` | Tile grid size (3=tighter, 7=wider) |
| `--scheme` | `1` | Reflectivity color scheme: `1`=green-white, `2`=gray-to-red |
| `--elev` | lowest | Elevation angle, e.g. `0.5`, `1.5`, `3.5` |
| `--label-intensity` | `1.8` | Map label brightness (higher = more visible) |
| `--index` | If multiple place matches, pick this index |
| `--output` / `-o` | script folder | Save destination |

## Examples


# Scheme 2 (gray-to-red reflectivity scale)
python generate_place_image.py "Warszawa" --scheme 2

# Specific elevation, tighter view
python generate_place_image.py "Gdansk" --elev 1.5 --grid 3

# Higher zoom, wider grid, custom output
python generate_place_image.py "Krakow" --zoom 13 --grid 5 -o C:\Users\Public

# Boost labels more for readability
python generate_place_image.py "Liberec" --label-intensity 2.5

# Disambiguate when multiple places match
python generate_place_image.py "Mokre" --index 1


Output

- 2564 x ~1390 px PNG
- Left panel: DBZH (reflectivity)
- Right panel: VRADH (velocity)
- Dark basemap (CartoDB) + labels always on top
- Legend bars at top matching tile colors
- Bottom bar: station, time (UTC+2), parameter, city name
