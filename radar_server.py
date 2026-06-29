"""
Radar data fetcher and tile renderer for image generation.
"""

import os, io, re, time, math, shutil
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET

import numpy as np
import h5py
import requests
from PIL import Image
from pyproj import Geod

S3_BASE = "https://s3.waw3-1.cloudferro.com/openradar-24h"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)

STATIONS = {
    "plleg": {"name": "Legionowo", "short": "LEG", "country": "pl", "lat": 52.40525, "lon": 20.96111},
    "plbrz": {"name": "Brzuchania", "short": "BRZ", "country": "pl", "lat": 50.394169, "lon": 20.083228},
    "plgdy": {"name": "Gdynia", "short": "GDY", "country": "pl", "lat": 54.500917, "lon": 18.271842},
    "plgsa": {"name": "Gorzow Wlkp.", "short": "GOR", "country": "pl", "lat": 50.463864, "lon": 18.153211},
    "plpas": {"name": "Pastewnik", "short": "PAS", "country": "pl", "lat": 50.89246, "lon": 16.039494},
    "plpoz": {"name": "Poznan", "short": "POZ", "country": "pl", "lat": 52.413253, "lon": 16.796986},
    "plram": {"name": "Ramza", "short": "RAM", "country": "pl", "lat": 50.151328, "lon": 18.725094},
    "plrze": {"name": "Rzeszow", "short": "RZE", "country": "pl", "lat": 50.11406, "lon": 22.037},
    "plswi": {"name": "Swidwin", "short": "SWI", "country": "pl", "lat": 53.795786, "lon": 15.836828},
    "pluzr": {"name": "Uzranki", "short": "UZR", "country": "pl", "lat": 53.855733, "lon": 21.412331},
    "deasb": {"name": "Asbach", "short": "ASB", "country": "de", "lat": 53.564129, "lon": 6.748317},
    "deboo": {"name": "Boostedt", "short": "BOO", "country": "de", "lat": 54.004381, "lon": 10.046899},
    "dedrs": {"name": "Dresden", "short": "DRS", "country": "de", "lat": 51.124639, "lon": 13.768639},
    "deeis": {"name": "Eisenach", "short": "EIS", "country": "de", "lat": 49.540667, "lon": 12.402788},
    "deess": {"name": "Essen", "short": "ESS", "country": "de", "lat": 51.405649, "lon": 6.967111},
    "defbg": {"name": "Feldberg", "short": "FBG", "country": "de", "lat": 47.873611, "lon": 8.003611},
    "defld": {"name": "Flechterberg", "short": "FLD", "country": "de", "lat": 51.311197, "lon": 8.801998},
    "dehnr": {"name": "Hannover", "short": "HNR", "country": "de", "lat": 52.460083, "lon": 9.694533},
    "deisn": {"name": "Isen", "short": "ISN", "country": "de", "lat": 48.174705, "lon": 12.101779},
    "demem": {"name": "Memmingen", "short": "MEM", "country": "de", "lat": 48.042145, "lon": 10.219222},
    "deneu": {"name": "Neuhaus", "short": "NEU", "country": "de", "lat": 50.500114, "lon": 11.135034},
    "denhb": {"name": "Neuhaus (NRW)", "short": "NHB", "country": "de", "lat": 50.109656, "lon": 6.548328},
    "deoft": {"name": "Offenthal", "short": "OFT", "country": "de", "lat": 49.984745, "lon": 8.712933},
    "depro": {"name": "Proetzel", "short": "PRO", "country": "de", "lat": 52.648667, "lon": 13.858212},
    "deros": {"name": "Rostock", "short": "ROS", "country": "de", "lat": 54.17566, "lon": 12.058076},
    "detur": {"name": "Tuerkheim", "short": "TUR", "country": "de", "lat": 48.585379, "lon": 9.782675},
    "deumd": {"name": "Ummendorf", "short": "UMD", "country": "de", "lat": 52.160096, "lon": 11.176091},
    "czbrd": {"name": "Brdicky", "short": "BRD", "country": "cz", "lat": 49.6583, "lon": 13.8178},
    "czska": {"name": "Skalky", "short": "SKA", "country": "cz", "lat": 49.5011, "lon": 16.7885},
    "behel": {"name": "Helchteren", "short": "HEL", "country": "be", "lat": 51.0702, "lon": 5.4054},
    "bejab": {"name": "Jabbeke", "short": "JAB", "country": "be", "lat": 51.1917, "lon": 3.0642},
    "bewid": {"name": "Wideumont", "short": "WID", "country": "be", "lat": 49.9136, "lon": 5.5044},
    "chalb": {"name": "Albis", "short": "ALB", "country": "ch", "lat": 47.2843, "lon": 8.5120},
    "chdol": {"name": "Dole", "short": "DOL", "country": "ch", "lat": 46.4251, "lon": 6.0994},
    "chlem": {"name": "Lema", "short": "LEM", "country": "ch", "lat": 46.0408, "lon": 8.8332},
    "chppm": {"name": "Piz Martegnas", "short": "PPM", "country": "ch", "lat": 46.3706, "lon": 7.4866},
    "chwei": {"name": "Weissfluh", "short": "WEI", "country": "ch", "lat": 46.8350, "lon": 9.7945},
    "dkbor": {"name": "Bornholm", "short": "BOR", "country": "dk", "lat": 55.1127, "lon": 14.8875},
    "dkrom": {"name": "Romoe", "short": "ROM", "country": "dk", "lat": 55.1731, "lon": 8.5520},
    "dksam": {"name": "Samsoe", "short": "SAM", "country": "dk", "lat": 55.8119, "lon": 10.5853},
    "dksin": {"name": "Sindal", "short": "SIN", "country": "dk", "lat": 57.4893, "lon": 10.1365},
    "dkste": {"name": "Stevns", "short": "STE", "country": "dk", "lat": 55.3262, "lon": 12.4493},
    "eesur": {"name": "Surju", "short": "SUR", "country": "ee", "lat": 58.4823, "lon": 25.5187},
    "fianj": {"name": "Anjalankoski", "short": "ANJ", "country": "fi", "lat": 60.9039, "lon": 27.1081},
    "fikan": {"name": "Kankaanpaa", "short": "KAN", "country": "fi", "lat": 61.8108, "lon": 22.5020},
    "fikau": {"name": "Kauhava", "short": "KAU", "country": "fi", "lat": 68.4344, "lon": 27.4440},
    "fikes": {"name": "Kesalahti", "short": "KES", "country": "fi", "lat": 61.9070, "lon": 29.7977},
    "fikor": {"name": "Korppoo", "short": "KOR", "country": "fi", "lat": 60.1285, "lon": 21.6434},
    "fikuo": {"name": "Kuopio", "short": "KUO", "country": "fi", "lat": 62.8626, "lon": 27.3815},
    "filuo": {"name": "Luosto", "short": "LUO", "country": "fi", "lat": 67.1391, "lon": 26.8969},
    "finur": {"name": "Nurmijarvi", "short": "NUR", "country": "fi", "lat": 63.8379, "lon": 29.4489},
    "fipet": {"name": "Petajavesi", "short": "PET", "country": "fi", "lat": 62.3045, "lon": 25.4401},
    "fiuta": {"name": "Utajarvi", "short": "UTA", "country": "fi", "lat": 64.7749, "lon": 26.3189},
    "fivih": {"name": "Vihriala", "short": "VIH", "country": "fi", "lat": 60.5562, "lon": 24.4956},
    "fivim": {"name": "Vimpeli", "short": "VIM", "country": "fi", "lat": 63.1048, "lon": 23.8209},
    "frabb": {"name": "Abbeville", "short": "ABB", "country": "fr", "lat": 50.1360, "lon": 1.8347},
    "fraja": {"name": "Ajaccio", "short": "AJA", "country": "fr", "lat": 41.9531, "lon": 8.7005},
    "frale": {"name": "Aleria", "short": "ALE", "country": "fr", "lat": 42.1297, "lon": 9.4964},
    "frave": {"name": "Avesnes", "short": "AVE", "country": "fr", "lat": 50.1283, "lon": 3.8118},
    "frbla": {"name": "Blaisy", "short": "BLA", "country": "fr", "lat": 47.3552, "lon": 4.7759},
    "frbol": {"name": "Bollene", "short": "BOL", "country": "fr", "lat": 44.3231, "lon": 4.7622},
    "frbor": {"name": "Bordeaux", "short": "BOR", "country": "fr", "lat": 44.8315, "lon": -0.6919},
    "frbou": {"name": "Bourges", "short": "BOU", "country": "fr", "lat": 47.0586, "lon": 2.3596},
    "frcae": {"name": "Caen", "short": "CAE", "country": "fr", "lat": 48.9272, "lon": -0.1495},
    "frcol": {"name": "Collobrieres", "short": "COL", "country": "fr", "lat": 43.2166, "lon": 6.3729},
    "frgre": {"name": "Grezet", "short": "GRE", "country": "fr", "lat": 45.1044, "lon": 1.3697},
    "frmom": {"name": "Momuy", "short": "MOM", "country": "fr", "lat": 43.6245, "lon": -0.6094},
    "frmtc": {"name": "Montancy", "short": "MTC", "country": "fr", "lat": 47.3686, "lon": 7.0190},
    "frnan": {"name": "Nancy", "short": "NAN", "country": "fr", "lat": 48.7158, "lon": 6.5816},
    "frnim": {"name": "Nimes", "short": "NIM", "country": "fr", "lat": 43.8061, "lon": 4.5027},
    "frniz": {"name": "Nizas", "short": "NIZ", "country": "fr", "lat": 46.0678, "lon": 4.4454},
    "fropo": {"name": "Opoul", "short": "OPO", "country": "fr", "lat": 42.9184, "lon": 2.8650},
    "frpla": {"name": "Plabennec", "short": "PLA", "country": "fr", "lat": 48.4609, "lon": -4.4298},
    "frtou": {"name": "Toulouse", "short": "TOU", "country": "fr", "lat": 43.5743, "lon": 1.3763},
    "frtre": {"name": "Trevarez", "short": "TRE", "country": "fr", "lat": 47.3374, "lon": -1.6563},
    "frtro": {"name": "Troyes", "short": "TRO", "country": "fr", "lat": 48.4621, "lon": 4.3093},
    "hrbil": {"name": "Bilogora", "short": "BIL", "country": "hr", "lat": 45.8835, "lon": 17.2005},
    "hrdeb": {"name": "Debela", "short": "DEB", "country": "hr", "lat": 44.0452, "lon": 15.3764},
    "hrgra": {"name": "Granic", "short": "GRA", "country": "hr", "lat": 45.1592, "lon": 18.7033},
    "hrpun": {"name": "Puntijarka", "short": "PUN", "country": "hr", "lat": 45.9078, "lon": 15.9684},
    "hrulj": {"name": "Uljenje", "short": "ULJ", "country": "hr", "lat": 42.8944, "lon": 17.4783},
    "iedub": {"name": "Dublin", "short": "DUB", "country": "ie", "lat": 53.4299, "lon": -6.2443},
    "iesha": {"name": "Shannon", "short": "SHA", "country": "ie", "lat": 52.6928, "lon": -8.9200},
    "isbjo": {"name": "Bjorg", "short": "BJO", "country": "is", "lat": 65.2659, "lon": -14.0618},
    "iskef": {"name": "Keflavik", "short": "KEF", "country": "is", "lat": 64.0257, "lon": -22.6354},
    "isska": {"name": "Skagafjordur", "short": "SKA", "country": "is", "lat": 66.0557, "lon": -20.2680},
    "ltlau": {"name": "Laudyne", "short": "LAU", "country": "lt", "lat": 55.6090, "lon": 22.2395},
    "ltvil": {"name": "Vilnius", "short": "VIL", "country": "lt", "lat": 54.6262, "lon": 25.1068},
    "mtgud": {"name": "Gudja", "short": "GUD", "country": "mt", "lat": 35.8528, "lon": 14.4747},
    "nldhl": {"name": "Den Helder", "short": "DHL", "country": "nl", "lat": 52.9528, "lon": 4.7906},
    "nlhrw": {"name": "Herwijnen", "short": "HRW", "country": "nl", "lat": 51.8369, "lon": 5.1381},
    "noand": {"name": "Andoya", "short": "AND", "country": "no", "lat": 69.2414, "lon": 16.0030},
    "nober": {"name": "Berlevag", "short": "BER", "country": "no", "lat": 70.5107, "lon": 29.0184},
    "nobml": {"name": "Bremnes", "short": "BML", "country": "no", "lat": 59.8540, "lon": 5.0900},
    "nohas": {"name": "Hasvik", "short": "HAS", "country": "no", "lat": 70.6052, "lon": 22.4430},
    "nohfj": {"name": "Hogfjell", "short": "HFJ", "country": "no", "lat": 61.2318, "lon": 10.5273},
    "nohgb": {"name": "Hauge", "short": "HGB", "country": "no", "lat": 58.3601, "lon": 7.1648},
    "nohur": {"name": "Hurum", "short": "HUR", "country": "no", "lat": 59.6271, "lon": 10.5645},
    "norsa": {"name": "Rissa", "short": "RSA", "country": "no", "lat": 63.6900, "lon": 10.2040},
    "norsg": {"name": "Rost", "short": "RSG", "country": "no", "lat": 69.2186, "lon": 23.4398},
    "norst": {"name": "Rostad", "short": "RST", "country": "no", "lat": 67.5307, "lon": 12.0986},
    "nosmn": {"name": "Smola", "short": "SMN", "country": "no", "lat": 65.2199, "lon": 11.9926},
    "nosta": {"name": "Starheim", "short": "STA", "country": "no", "lat": 62.1871, "lon": 5.1275},
    "robar": {"name": "Barlad", "short": "BAR", "country": "ro", "lat": 47.0118, "lon": 27.5825},
    "robob": {"name": "Bobohalma", "short": "BOB", "country": "ro", "lat": 46.3602, "lon": 24.2252},
    "robuc": {"name": "Bucuresti", "short": "BUC", "country": "ro", "lat": 44.5127, "lon": 26.0773},
    "rocra": {"name": "Craiu", "short": "CRA", "country": "ro", "lat": 44.3103, "lon": 23.8674},
    "romed": {"name": "Medgidia", "short": "MED", "country": "ro", "lat": 44.2434, "lon": 28.2506},
    "roora": {"name": "Oradea", "short": "ORA", "country": "ro", "lat": 47.0500, "lon": 21.9500},
    "rotim": {"name": "Timisoara", "short": "TIM", "country": "ro", "lat": 45.7717, "lon": 21.2577},
    "seang": {"name": "Angelholm", "short": "ANG", "country": "se", "lat": 56.3675, "lon": 12.8517},
    "seatv": {"name": "Atvidaberg", "short": "ATV", "country": "se", "lat": 58.1059, "lon": 15.9365},
    "sebaa": {"name": "Balsta", "short": "BAA", "country": "se", "lat": 59.6110, "lon": 17.5833},
    "sehem": {"name": "Hemse", "short": "HEM", "country": "se", "lat": 57.3035, "lon": 18.4001},
    "sehuv": {"name": "Hudiksvall", "short": "HUV", "country": "se", "lat": 61.5771, "lon": 16.7144},
    "sekaa": {"name": "Karlskrona", "short": "KAA", "country": "se", "lat": 56.2955, "lon": 15.6102},
    "sekrn": {"name": "Kiruna", "short": "KRN", "country": "se", "lat": 67.7088, "lon": 20.6178},
    "sella": {"name": "Lulea", "short": "LLA", "country": "se", "lat": 65.4309, "lon": 21.8650},
    "seoer": {"name": "Ornskoldsvik", "short": "OER", "country": "se", "lat": 63.6395, "lon": 18.4019},
    "seosd": {"name": "Ostersund", "short": "OSD", "country": "se", "lat": 63.2951, "lon": 14.7591},
    "sevax": {"name": "Vaxjo", "short": "VAX", "country": "se", "lat": 58.2556, "lon": 12.8260},
    "silis": {"name": "Lisca", "short": "LIS", "country": "si", "lat": 46.0678, "lon": 15.2849},
    "sipas": {"name": "Pasja Ravan", "short": "PAS", "country": "si", "lat": 46.0980, "lon": 14.2282},
}

COUNTRY_CONFIG = {
    "pl": {"dir": "PVOL", "separate_elevations": False},
    "de": {"dir": "SCAN", "separate_elevations": True},
    "cz": {"dir": "PVOL", "separate_elevations": False},
    "be": {"dir": "PVOL", "separate_elevations": False},
    "ch": {"dir": "SCAN", "separate_elevations": True},
    "dk": {"dir": "PVOL", "separate_elevations": False},
    "ee": {"dir": "SCAN", "separate_elevations": True},
    "fi": {"dir": "SCAN", "separate_elevations": True},
    "fr": {"dir": "SCAN", "separate_elevations": True},
    "hr": {"dir": "PVOL", "separate_elevations": False},
    "ie": {"dir": "PVOL", "separate_elevations": False},
    "is": {"dir": "PVOL", "separate_elevations": False},
    "lt": {"dir": "SCAN", "separate_elevations": True},
    "mt": {"dir": "PVOL", "separate_elevations": False},
    "nl": {"dir": "PVOL", "separate_elevations": False},
    "no": {"dir": "PVOL", "separate_elevations": False},
    "ro": {"dir": "PVOL", "separate_elevations": False},
    "se": {"dir": "SCAN", "separate_elevations": True},
    "si": {"dir": "PVOL", "separate_elevations": False},
}

VARIABLES = {
    "DBZH": {"label": "Reflectivity", "unit": "dBZ", "vmin": 0, "vmax": 70},
    "VRADH": {"label": "Radial Velocity", "unit": "m/s", "vmin": -50, "vmax": 50},
    "TH": {"label": "Raw Reflectivity", "unit": "dBZ", "vmin": 0, "vmax": 70},
    "VRAD": {"label": "Radial Velocity", "unit": "m/s", "vmin": -50, "vmax": 50, "alias": "VRADH"},
}

_inmem_cache = {}
INMEM_TTL = 120
_latest_key_cache = {}
LATEST_KEY_CACHE_TTL = 45
_elevation_cache = {}
ELEVATION_CACHE_TTL = 600

_geod = Geod(ellps="WGS84")


def _extract_timestamp(key):
    m = re.search(r'@(\d{8}T\d{4})@', key)
    return m.group(1) if m else None


def _latlon_to_tile(lat, lon, zoom):
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n)
    return x, y


def _cache_path(key):
    return os.path.join(CACHE_DIR, re.sub(r'[^a-zA-Z0-9._-]', '_', key))


def _free_space_bytes(path):
    try:
        return shutil.disk_usage(path).free
    except Exception:
        return 512 * 1024 * 1024


def _cleanup_cache_if_low_space(path=CACHE_DIR, min_free=512*1024*1024):
    if _free_space_bytes(path) >= min_free:
        return
    for root, _, files in os.walk(path):
        for name in files:
            if name.endswith(".tmp"):
                try:
                    os.remove(os.path.join(root, name))
                except Exception:
                    pass
    candidates = []
    for root, _, files in os.walk(path):
        for name in files:
            fp = os.path.join(root, name)
            try:
                candidates.append((os.path.getmtime(fp), fp))
            except Exception:
                pass
    candidates.sort()
    for _, fp in candidates:
        if _free_space_bytes(path) >= min_free:
            break
        try:
            os.remove(fp)
        except Exception:
            pass


def list_s3(prefix):
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    keys = []
    marker = None
    while True:
        url = f"{S3_BASE}/?prefix={prefix}&max-keys=1000"
        if marker:
            url += f"&marker={marker}"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        for contents in root.findall("s3:Contents", ns):
            key = contents.find("s3:Key", ns).text
            keys.append(key)
        is_truncated = root.find("s3:IsTruncated", ns)
        if is_truncated is None or is_truncated.text != "true":
            break
        next_marker = root.find("s3:NextMarker", ns)
        marker = next_marker.text if next_marker is not None else keys[-1]
    return keys


def _station_country(station):
    info = STATIONS.get(station)
    return info["country"] if info else "pl"


def _country_code(station):
    info = STATIONS.get(station)
    return info["country"].upper() if info else "PL"


def _discover_elevations(station_id, country):
    cache_key = f"elev:{station_id}"
    now = time.time()
    if cache_key in _elevation_cache:
        entry = _elevation_cache[cache_key]
        if now - entry["time"] < ELEVATION_CACHE_TTL:
            return entry["elevations"]

    cfg = COUNTRY_CONFIG[country]
    cc_upper = country.upper()
    dir_type = cfg["dir"]
    elevations = []
    is_separate = cfg["separate_elevations"]

    for days_ago in range(3):
        d = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y/%m/%d")
        prefix = f"{d}/{cc_upper}/{station_id}/{dir_type}/"
        try:
            keys = list_s3(prefix)
            if not keys:
                continue
            if is_separate:
                elev_set = set()
                for k in keys:
                    m = re.search(r"@([\d.]+)@(?:DBZH|VRADH|TH|VRAD)", k)
                    if m:
                        elev_set.add(m.group(1))
                elevations = sorted(elev_set, key=lambda x: float(x))
            else:
                has_multi = False
                for k in keys:
                    fn = k.split("/")[-1]
                    m = re.search(r"@([\d_.]+)@", fn)
                    if m and "_" in m.group(1) and ("DBZH" in fn or "TH" in fn):
                        elevations = sorted(m.group(1).split("_"), key=lambda x: float(x))
                        has_multi = True
                        break
                if not has_multi:
                    elev_set = set()
                    for k in keys:
                        m = re.search(r"@([\d.]+)@(?:DBZH|VRADH|TH|VRAD)", k)
                        if m:
                            elev_set.add(m.group(1))
                    elevations = sorted(elev_set, key=lambda x: float(x))
                    if elevations and not cfg["separate_elevations"]:
                        _set_station_separate(station_id, True)
            if elevations:
                break
        except Exception:
            continue

    elevations = [e for e in elevations if _is_valid_elevation(e)]
    _elevation_cache[cache_key] = {"elevations": elevations, "time": now}
    return elevations


def _get_station_separate(station_id):
    key = f"sep:{station_id}"
    now = time.time()
    entry = _elevation_cache.get(key)
    if entry and now - entry["time"] < ELEVATION_CACHE_TTL:
        return entry["separate"]
    return None


def _set_station_separate(station_id, val):
    key = f"sep:{station_id}"
    _elevation_cache[key] = {"separate": val, "time": time.time()}


def elevations_for_station(station):
    cc = _station_country(station)
    return _discover_elevations(station, cc)


def _is_valid_elevation(elevation):
    try:
        v = float(elevation.replace(",", "."))
        return not math.isnan(v) and not math.isinf(v)
    except (ValueError, TypeError):
        return False


def _ensure_elevation(elevation):
    el = elevation.replace(",", ".")
    if el.count(".") > 1:
        parts = el.split(".")
        el = parts[0] + "." + parts[1]
    return el


def latest_key(station, variable="DBZH", elevation=None):
    cache_key = f"{station}:{variable}:{elevation}"
    now = time.time()
    if cache_key in _latest_key_cache:
        entry = _latest_key_cache[cache_key]
        if now - entry["time"] < LATEST_KEY_CACHE_TTL:
            return entry["key"]
    country = _station_country(station)
    cfg = COUNTRY_CONFIG[country]
    cc_upper = country.upper()
    sep = _get_station_separate(station)
    if sep is None:
        sep = cfg["separate_elevations"]
    today = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    prefix = f"{today}/{cc_upper}/{station}/{cfg['dir']}/"
    keys = list_s3(prefix)
    if sep and elevation is not None:
        elev_str = _ensure_elevation(elevation)
        var_keys = [k for k in keys if f"@{elev_str}@" in k and f"@{variable}" in k and k.endswith(".h5")]
    else:
        var_keys = [k for k in keys if f"@{variable}" in k and k.endswith(".h5")]
    if not var_keys:
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y/%m/%d")
        prefix = f"{yesterday}/{cc_upper}/{station}/{cfg['dir']}/"
        keys = list_s3(prefix)
        if sep and elevation is not None:
            var_keys = [k for k in keys if f"@{elev_str}@" in k and f"@{variable}" in k and k.endswith(".h5")]
        else:
            var_keys = [k for k in keys if f"@{variable}" in k and k.endswith(".h5")]
    result = None
    if var_keys:
        def extract_ts(key):
            m = re.search(r'@(\d{8}T\d{4})@', key)
            return m.group(1) if m else ""
        var_keys.sort(key=extract_ts, reverse=True)
        result = var_keys[0]
    _latest_key_cache[cache_key] = {"key": result, "time": now}
    return result


def fetch_and_parse(key):
    _cleanup_cache_if_low_space()
    cache_key = key
    cache_fp = _cache_path(cache_key)
    if os.path.exists(cache_fp):
        try:
            with h5py.File(cache_fp, "r") as _:
                pass
            return cache_fp
        except Exception:
            os.remove(cache_fp)
    url = f"{S3_BASE}/{key}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    tmp = cache_fp + ".tmp"
    with open(tmp, "wb") as f:
        f.write(resp.content)
    os.replace(tmp, cache_fp)
    return cache_fp


def parse_file(filepath):
    with h5py.File(filepath, "r") as f:
        site_lat = float(f["where"].attrs["lat"])
        site_lon = float(f["where"].attrs["lon"])
        datasets = []
        for i in range(1, 25):
            g = f"dataset{i}"
            if g not in f:
                break
            where = f[f"{g}/where"].attrs
            data_what = f[f"{g}/data1/what"].attrs
            elangle = float(where["elangle"])
            if math.isnan(elangle) or math.isinf(elangle):
                continue
            nbins = int(where["nbins"])
            nrays = int(where["nrays"])
            rscale = float(where["rscale"])
            rstart = float(where.get("rstart", 0))
            gain = float(data_what.get("gain", 1.0))
            offset = float(data_what.get("offset", 0.0))
            nodata_val = float(data_what.get("nodata", 255))
            undetect_val = float(data_what.get("undetect", 0))
            data_raw = f[f"{g}/data1/data"][:]
            data_f = data_raw.astype(np.float32)
            mask = (data_raw == nodata_val) | (data_raw == undetect_val)
            data_f = data_f * gain + offset
            data_f[mask] = np.nan
            if f"{g}/how" in f and "startazA" in f[f"{g}/how"].attrs:
                az = f[f"{g}/how"].attrs["startazA"][:].astype(np.float32) % 360
                if len(az) != nrays:
                    step = 360.0 / nrays
                    a1gate_val = int(where.get("a1gate", 0))
                    az = (np.arange(nrays, dtype=np.float32) * step + a1gate_val) % 360
            else:
                step = 360.0 / nrays
                a1gate_val = int(where.get("a1gate", 0))
                az = (np.arange(nrays, dtype=np.float32) * step + a1gate_val) % 360
            rng = np.arange(nbins, dtype=np.float32) * rscale + rstart
            datasets.append({
                "elevation": elangle,
                "azimuths": az,
                "ranges": rng,
                "data": data_f,
                "nrays": nrays,
                "nbins": nbins,
                "rscale": rscale,
            })
    return {"lat": site_lat, "lon": site_lon, "datasets": datasets}


def get_parsed(key):
    now = time.time()
    if key in _inmem_cache:
        entry = _inmem_cache[key]
        if now - entry["time"] < INMEM_TTL:
            return entry["data"]
    fp = fetch_and_parse(key)
    parsed = parse_file(fp)
    _inmem_cache[key] = {"data": parsed, "time": now}
    return parsed


def _index_scan(az, rng, data, fwd_az, dist, method="nearest"):
    nrays = len(az)
    step = 360.0 / nrays
    a0 = az[0]
    if method == "linear":
        ray_frac = ((fwd_az - a0) % 360) / step
        ray0 = np.floor(ray_frac).astype(np.int32) % nrays
        ray1 = (ray0 + 1) % nrays
        ry = (ray_frac - np.floor(ray_frac)).astype(np.float32)
        r0 = rng[0]
        rstep = rng[1] - r0
        bin_frac = (dist - r0) / rstep
        bin0 = np.floor(bin_frac).astype(np.int32)
        bin1 = bin0 + 1
        bx = (bin_frac - np.floor(bin_frac)).astype(np.float32)
        last = len(rng) - 1
        bin0c = np.clip(bin0, 0, last)
        bin1c = np.clip(bin1, 0, last)
        c00 = data[ray0, bin0c]
        c10 = data[ray1, bin0c]
        c01 = data[ray0, bin1c]
        c11 = data[ray1, bin1c]
        w0 = (1 - bx) * c00 + bx * c01
        w1 = (1 - bx) * c10 + bx * c11
        values = (1 - ry) * w0 + ry * w1
    else:
        ray_idx = np.round(((fwd_az - a0) % 360) / step).astype(np.int32) % nrays
        r0 = rng[0]
        rstep = rng[1] - r0
        bin_idx = np.round((dist - r0) / rstep).astype(np.int32)
        np.clip(bin_idx, 0, len(rng) - 1, out=bin_idx)
        values = data[ray_idx, bin_idx].copy()
    values[(dist > rng[-1]) | (dist < 0)] = np.nan
    return values


REFLECTIVITY_CMAP = [
    (0.000, (0.6, 0.85, 1.0, 0.70)),
    (0.071, (0.0, 0.863, 0.863, 0.471)),
    (0.143, (0.0, 0.706, 1.0, 0.627)),
    (0.214, (0.0, 0.549, 0.275, 0.706)),
    (0.286, (0.314, 0.784, 0.235, 0.784)),
    (0.357, (0.667, 0.863, 0.235, 0.863)),
    (0.429, (1.0, 1.0, 0.0, 0.902)),
    (0.500, (1.0, 0.706, 0.0, 0.941)),
    (0.571, (1.0, 0.392, 0.0, 0.961)),
    (0.643, (1.0, 0.0, 0.0, 0.980)),
    (0.714, (0.784, 0.0, 0.0, 0.980)),
    (0.786, (0.588, 0.0, 0.314, 0.980)),
    (0.857, (0.784, 0.392, 0.784, 0.980)),
    (0.929, (1.0, 1.0, 1.0, 1.0)),
    (1.000, (1.0, 1.0, 1.0, 1.0)),
]

VELOCITY_CMAP = [
    (0.000, (0.518, 0.910, 0.922, 1.0)),
    (0.205, (0.518, 0.910, 0.922, 1.0)),
    (0.227, (0.616, 0.918, 0.929, 1.0)),
    (0.268, (0.392, 0.922, 0.522, 1.0)),
    (0.294, (0.031, 0.902, 0.035, 1.0)),
    (0.335, (0.067, 0.835, 0.067, 1.0)),
    (0.370, (0.145, 0.718, 0.145, 1.0)),
    (0.402, (0.145, 0.718, 0.145, 1.0)),
    (0.420, (0.251, 0.557, 0.251, 1.0)),
    (0.437, (0.294, 0.494, 0.290, 1.0)),
    (0.469, (0.392, 0.467, 0.369, 1.0)),
    (0.487, (0.490, 0.463, 0.447, 1.0)),
    (0.496, (0.529, 0.435, 0.455, 1.0)),
    (0.504, (0.518, 0.376, 0.396, 1.0)),
    (0.513, (0.506, 0.322, 0.337, 1.0)),
    (0.531, (0.459, 0.133, 0.141, 1.0)),
    (0.563, (0.435, 0.008, 0.012, 1.0)),
    (0.580, (0.553, 0.043, 0.071, 1.0)),
    (0.598, (0.608, 0.067, 0.102, 1.0)),
    (0.630, (0.678, 0.090, 0.141, 1.0)),
    (0.665, (0.824, 0.149, 0.220, 1.0)),
    (0.706, (0.980, 0.282, 0.396, 1.0)),
    (0.732, (0.980, 0.400, 0.529, 1.0)),
    (0.773, (0.988, 0.576, 0.737, 1.0)),
    (0.795, (0.992, 0.675, 0.757, 1.0)),
    (1.000, (0.992, 0.675, 0.757, 1.0)),
]


def build_colormap_lut(cmap_def, steps=256):
    stops = np.array([s[0] for s in cmap_def])
    colors = np.array([s[1] for s in cmap_def])
    x = np.linspace(0, 1, steps)
    lut = np.zeros((steps, 4))
    for c in range(4):
        lut[:, c] = np.interp(x, stops, colors[:, c])
    return (lut * 255).astype(np.uint8)


REFLECTIVITY_CMAP_2 = [
    (0.000, (0.702, 0.698, 0.698, 1.0)),
    (0.071, (0.702, 0.698, 0.698, 1.0)),
    (0.114, (0.392, 0.478, 0.659, 1.0)),
    (0.143, (0.251, 0.373, 0.631, 1.0)),
    (0.214, (0.353, 0.671, 0.831, 1.0)),
    (0.286, (0.118, 0.714, 0.290, 1.0)),
    (0.357, (0.008, 0.565, 0.039, 1.0)),
    (0.429, (0.0, 0.467, 0.020, 1.0)),
    (0.500, (0.051, 0.329, 0.0, 1.0)),
    (0.529, (0.545, 0.596, 0.0, 1.0)),
    (0.571, (0.969, 0.824, 0.0, 1.0)),
    (0.643, (1.0, 0.588, 0.0, 1.0)),
    (0.714, (0.937, 0.082, 0.004, 1.0)),
    (0.786, (0.804, 0.067, 0.008, 1.0)),
    (0.857, (0.596, 0.051, 0.0, 1.0)),
    (0.929, (0.271, 0.020, 0.004, 1.0)),
    (1.000, (0.886, 0.596, 0.882, 1.0)),
]
DBZH_LUT = build_colormap_lut(REFLECTIVITY_CMAP)
DBZH_LUT_2 = build_colormap_lut(REFLECTIVITY_CMAP_2)
VRADH_LUT = build_colormap_lut(VELOCITY_CMAP)


def render_tile(parsed, elevation_idx, z, x, y, var="DBZH", size=256, colorscheme="1"):
    n = 2 ** z
    max_global = n * size
    px = np.arange(x * size, (x + 1) * size)
    py = np.arange(y * size, (y + 1) * size)
    px_mesh, py_mesh = np.meshgrid(px, py)
    world_x = px_mesh.astype(np.float64) / max_global
    world_y = py_mesh.astype(np.float64) / max_global
    lon = world_x * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(np.pi - 2.0 * np.pi * world_y)))
    ds = parsed["datasets"][elevation_idx]
    site_lat, site_lon = parsed["lat"], parsed["lon"]
    fwd_az, _, dist = _geod.inv(
        np.full(lat.size, site_lon),
        np.full(lat.size, site_lat),
        lon.ravel(), lat.ravel()
    )
    values = _index_scan(ds["azimuths"], ds["ranges"], ds["data"],
                         np.asarray(fwd_az) % 360, dist)
    values = values.reshape(lat.shape)
    vmin = VARIABLES[var]["vmin"]
    vmax = VARIABLES[var]["vmax"]
    norm = (values - vmin) / (vmax - vmin)
    mask = np.isnan(norm)
    norm = np.clip(norm, 0, 1)
    norm[mask] = 0
    idx = (norm * 255).astype(np.uint32)
    is_refl = var in ("DBZH", "TH")
    if is_refl:
        lut = DBZH_LUT_2 if colorscheme == "2" else DBZH_LUT
    else:
        lut = VRADH_LUT
    rgb = lut[idx]
    rgba = np.zeros((size, size, 4), dtype=np.uint8)
    for c in range(4):
        rgba[:, :, c] = rgb[:, :, c]
    rgba[mask] = [0, 0, 0, 0]
    img = Image.fromarray(rgba, mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
