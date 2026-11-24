#!/usr/bin/env python3
"""A copy of LIPAS's routes and places that Polku shows: each type's layer as a zoom-9 PMTiles archive of JSON tiles
beside its features as the WFS served them, all uploaded as one candidate in polku-lipas beside polku-osm.

The source is LIPAS's WFS, the University of Jyväskylä's register of sports places, CC BY 4.0: the same layers and
fields Polku reads when the copy can't answer, in Finland's grid (EPSG:3067) at full precision, so a feature and a
route's parts (reitti_id, which Polku's share links name) are the same from either. LIPAS's REST API has no parts' IDs.
The fetch asks for one page at a time, with a pause between pages and one retry after a longer one."""

from datetime import datetime
import json
import math
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.osm_data import wfs
from tools.osm_data.publish import require

LABEL = 'LIPAS'
SOURCE = 'https://lipas.fi/geoserver/lipas/ows'
SRS = 'EPSG:3067'
ZOOM = 9  # Polku's tiles of LIPAS (js/lipas.js)
PAGE, PAUSE, BACK_OFF = 5000, 5, 300  # features a request, and seconds between pages and before the one retry
FOLDER = 'polku-lipas'
BUDGET = 1024**3  # live, its predecessor, candidate and staging: four copies of 66 MB (2026-11-24)
# Polku's types (js/config.js, LIPAS_TYPES) and their layers, lines and points. A route's parts are ordered by its id and
# part. Landing places (205, rantautumispaikka) had none on 2026-11-24, so they aren't copied and Polku asks LIPAS.
LINES = {4401: 'kuntorata', 4402: 'latu', 4403: 'kavelyreitti', 4404: 'luontopolku', 4405: 'retkeilyreitti',
          4406: 'monikayttoreitti', 4411: 'maastopyorailyreitti', 4412: 'pyorailyreitti', 4421: 'moottorikelkkareitti',
          4451: 'melontareitti', 4452: 'vesiretkeilyreitti'}
POINTS = {202: 'telttailu_ja_leiriytyminen', 203: 'veneilyn_palvelupaikka', 204: 'luontotorni',
          206: 'ruoanlaittopaikka', 207: 'opastuspiste', 301: 'laavu_kota_kammi', 302: 'tupa',
          304: 'ulkoilumaja_hiihtomaja', 1550: 'luistelureitti', 3220: 'uimaranta', 3230: 'uimapaikka',
          4810: 'ampumarata', 4820: 'ampumaurheilukeskus', 4840: 'jousiammuntamaastorata'}
DATASETS = {str(t): (f'lipas:lipas_{t}_{layer}', None, 'id,reitti_id' if t in LINES else 'id')
            for t, layer in sorted(LINES.items() | POINTS.items())}
# what Polku reads of a feature (js/lipas.js, propsOf() and the tiles' feature IDs)
KEEP = ('id', 'reitti_id', 'nimi_fi', 'nimi_se', 'lisatieto_fi', 'www', 'kunta_nimi_fi', 'reitin_pituus_km',
        'pintamateriaali', 'yleiso_wc')
ATTRIBUTION = '© LIPAS, University of Jyväskylä, CC BY 4.0'
LICENSE = 'CC BY 4.0'
LICENSE_TEXT = f'''Routes and sports places from LIPAS, the national database of sports facilities in Finland, © University of
Jyväskylä, licensed under Creative Commons Attribution 4.0 International:
https://creativecommons.org/licenses/by/4.0/

Source: {SOURCE}, the layers lipas_<type>_<name> of the types Polku shows. The .jsonl.xz files hold their features as
the service gave them, in ETRS-TM35FIN (EPSG:3067), one per line. Changed in the map tiles: only the properties Polku
shows.
'''


def coordinates(geometry):
    """The geometry's points, [x, y], or None for a geometry Polku doesn't read."""
    kind, c = geometry.get('type'), geometry.get('coordinates')
    lines = [[c]] if kind == 'Point' else [c] if kind == 'LineString' else c if kind == 'MultiLineString' else None
    ok = lines and all(type(line) is list and line and all(type(p) is list and len(p) >= 2 and all(
        type(v) in (int, float) and math.isfinite(v) for v in p) for p in line) for line in lines)
    return [p for line in lines for p in line] if ok else None


def element(name, feature):
    """The feature as its type's tiles hold it: the properties Polku reads, and its geometry as served. A feature of
    another type, without an ID or with another kind of geometry stops the copy."""
    properties, geometry = feature.get('properties') or {}, feature.get('geometry') or {}
    require(type(properties.get('id')) is int and properties.get('tyyppikoodi') == int(name)
            and coordinates(geometry) and (geometry['type'] == 'Point') == (int(name) in POINTS)
            and (type(properties.get('reitti_id')) is int) == (int(name) in LINES),
            'Unexpected LIPAS feature ' + str(feature.get('id')))
    return {'properties': {k: properties[k] for k in KEEP if properties.get(k) is not None},
            'geometry': {'type': geometry['type'], 'coordinates': geometry['coordinates']}}


def rank(e):
    """By place and part, then by the whole element: LIPAS's rows twice over come in no set order."""
    return e['properties']['id'], e['properties'].get('reitti_id', 0), json.dumps(e, sort_keys=True)


# ETRS-TM35FIN to longitude and latitude: Krüger's series on GRS80, as Polku's tm35ToLonLat() (js/geo.js)
_F = 1 / 298.257222101
_N = _F / (2 - _F)
_A = 6378137 / (1 + _N) * (1 + _N**2 / 4 + _N**4 / 64)
_B = (_N / 2 - 2 / 3 * _N**2 + 37 / 96 * _N**3, _N**2 / 48 + _N**3 / 15, 17 / 480 * _N**3)
_D = (2 * _N - 2 / 3 * _N**2 - 2 * _N**3, 7 / 3 * _N**2 - 8 / 5 * _N**3, 56 / 15 * _N**3)


def lon_lat(x, y):
    xi, eta = y / (0.9996 * _A), (x - 500000) / (0.9996 * _A)
    xp, ep = xi, eta
    for j in (1, 2, 3):
        xp -= _B[j - 1] * math.sin(2 * j * xi) * math.cosh(2 * j * eta)
        ep -= _B[j - 1] * math.cos(2 * j * xi) * math.sinh(2 * j * eta)
    chi = math.asin(math.sin(xp) / math.cosh(ep))
    lat = chi + sum(_D[j - 1] * math.sin(2 * j * chi) for j in (1, 2, 3))
    return 27 + math.degrees(math.atan2(math.sinh(ep), math.cos(xp))), math.degrees(lat)


def extent(e):
    points = [lon_lat(*p[:2]) for p in coordinates(e['geometry'])]
    return [min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)]


def edited(properties):
    """The feature's last edit: LIPAS's WFS writes it in Finnish time with a Z (2026-01-13T08:59:27.835Z is 06:59 UTC)."""
    when = properties.get('muokattu_viimeksi')
    return when and datetime.fromisoformat(when.removesuffix('Z')).replace(tzinfo=ZoneInfo('Europe/Helsinki'))


if __name__ == '__main__':
    wfs.main(sys.modules[__name__])
