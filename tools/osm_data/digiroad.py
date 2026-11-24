#!/usr/bin/env python3
"""A copy of four Digiroad layers: traffic volumes (the roads of 800 or more vehicles a day), paved roads, private roads
and speed limits. Each is a zoom-11 PMTiles archive of JSON tiles beside its features as the WFS served them, all
uploaded as one candidate in polku-digiroad beside polku-osm.

The source is Väylävirasto's WFS, CC BY 4.0. Väylävirasto's updates to it ended on 2026-09-18,
when Fintraffic took Digiroad over, so the copy is made by hand, and again only when a source with newer data exists.
The fetch asks for one page at a time, with a pause between pages and one retry after a longer one."""

from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.osm_data import wfs
from tools.osm_data.publish import require

LABEL = 'Digiroad'
SOURCE = 'https://avoinapi.vaylapilvi.fi/vaylatiedot/digiroad/ows'
SRS = 'EPSG:4326'
BUSY = 800  # vehicles a day
ZOOM = 11
PAGE, PAUSE, BACK_OFF = 5000, 5, 300  # features a request, and seconds between pages and before the one retry
FOLDER = 'polku-digiroad'
BUDGET = 5 * 1024**3  # live, its predecessor, candidate and staging: four copies of about 1 GB
SPEEDS = {20, 30, 40, 50, 60, 70, 80, 90, 100, 120}  # km/h
# The paving classes of the style dr_paallystetty_tie: Asfaltti, Kivi, Sitomaton kulutuskerros, Muut päällysteluokat
# and Päällystetty, tyyppi tuntematon
SURFACES = {1: 'asphalt', 2: 'stone', 3: 'unbound', 4: 'other', 99: 'unknown'}
RESTRICTIONS = {'tieto toimitettu - rajoituksia': 'yes', 'tieto toimitettu - ei rajoituksia': 'no',
                'ei toimitettu': 'unreported'}
# Each dataset, private roads first (the slowest to fetch, and their key the least proven): its WFS layer, its filter,
# the unique field that orders its pages, and what its tiles keep of a feature's properties: what the layer's WMS
# style draws, or None for a value the style doesn't draw
DATASETS = {
    # style dr_yksityistietieto: whether the road's restrictions were reported, and if there are any
    'private': ('digiroad:tiekunnalliset_yksityistiet', None, 'link_id',
                lambda p: {'restrictions': RESTRICTIONS[p['lisatiedot']]} if p.get('lisatiedot') in RESTRICTIONS
                else None),
    'traffic': ('digiroad:dr_liikennemaara', f'arvo>={BUSY}', 'id',
                lambda p: {'aadt': p['arvo']} if type(p.get('arvo')) is int and p['arvo'] >= BUSY else None),
    # style dr_paallystetty_tie: the paving class, and state roads (hallinn_lk 1) from a smaller scale than others
    'paved': ('digiroad:dr_paallystetty_tie', None, 'id',
              lambda p: {'surface': SURFACES[p['arvo']], 'state': p['hallinn_lk'] == 1} if p.get('arvo') in SURFACES
              and type(p.get('hallinn_lk')) is int and p['hallinn_lk'] >= 1 else None),
    # the layer's default style: the limit
    'speed': ('digiroad:dr_nopeusrajoitus', None, 'id_pk',
              lambda p: {'limit': p['arvo']} if p.get('arvo') in SPEEDS else None),
}
ATTRIBUTION = '© Fintraffic / Digiroad, from Väylävirasto\'s open data, CC BY 4.0'
LICENSE = 'CC BY 4.0'
LICENSE_TEXT = f'''Traffic volumes, paved roads, private roads and speed limits from Digiroad, © Fintraffic, from the open
data of Väylävirasto (the Finnish Transport Infrastructure Agency), licensed under Creative Commons Attribution 4.0
International: https://creativecommons.org/licenses/by/4.0/

Source: {SOURCE}, layers dr_liikennemaara, dr_paallystetty_tie,
tiekunnalliset_yksityistiet and dr_nopeusrajoitus. The .jsonl.xz files hold their features as the service gave them,
one per line; of the traffic volumes, only the roads of {BUSY} or more vehicles a day. Changed in the map tiles: only
the properties the layers' styles draw, and coordinates rounded to six decimal places without heights.
'''


def element(name, feature):
    """The feature as the dataset's tiles hold it: its ID, what its layer's style draws, and its lines in WGS84 to six
    decimal places without heights. A feature the style doesn't draw stops the copy."""
    properties, geometry = feature.get('properties', {}), feature.get('geometry') or {}
    kept = DATASETS[name][3](properties)
    require(kept is not None and geometry.get('type') in {'LineString', 'MultiLineString'},
            'Unexpected Digiroad feature ' + str(feature.get('id')))
    lines = [geometry['coordinates']] if geometry['type'] == 'LineString' else geometry['coordinates']
    key = feature['id'].rpartition('.')[2]
    return {'id': int(key) if key.isdigit() else key, **kept,
            'lines': [[[round(p[0], 6), round(p[1], 6)] for p in line] for line in lines]}


def key(feature):
    return feature['id']


def extent(road):
    points = [p for line in road['lines'] for p in line]
    return [min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)]


def rank(road):
    return road['id']


def edited(properties):
    """The time of the feature's last edit, which Digiroad writes in Finnish time (31.10.2020 13:48:30), or None."""
    when = properties.get('muokkauspv')
    return when and datetime.strptime(when, '%d.%m.%Y %H:%M:%S').replace(tzinfo=ZoneInfo('Europe/Helsinki'))


if __name__ == '__main__':
    wfs.main(sys.modules[__name__])
