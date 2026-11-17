"""Coarse IP geolocation for routing a forwarded viewer to the nearest relay.

Aggregators (aiostreams) forward the real fetcher's IP to the addon, but
Cloudflare's injected geo only describes the connecting IP, so that forwarded IP
has to be located locally. Continent granularity is enough to pick among the
handful of continental relays; each continent maps to a representative point
that feeds the same haversine used for direct viewers.
"""

import threading
from functools import lru_cache

import maxminddb

from comet.core.models import settings

_CONTINENT_LATLNG = {
    "AF": (0.0, 20.0),
    "AS": (34.0, 100.0),
    "EU": (54.0, 15.0),
    "NA": (40.0, -100.0),
    "SA": (-15.0, -60.0),
    "OC": (-25.0, 135.0),
    "AN": (-75.0, 0.0),
}

_reader = None
_loaded = False
_lock = threading.Lock()


def _db():
    global _reader, _loaded
    if _loaded:
        return _reader
    with _lock:
        if not _loaded:
            _loaded = True
            path = (settings.GEOIP_DB_PATH or "").strip()
            if path:
                try:
                    _reader = maxminddb.open_database(path)
                except Exception:
                    _reader = None
    return _reader


@lru_cache(maxsize=4096)
def locate(ip):
    reader = _db()
    if not reader or not ip:
        return None
    try:
        rec = reader.get(ip)
    except Exception:
        return None
    code = ((rec or {}).get("continent") or {}).get("code")
    return _CONTINENT_LATLNG.get(code)
