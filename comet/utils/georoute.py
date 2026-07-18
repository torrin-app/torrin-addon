"""Pick the nearest stream relay for each viewer and rewrite stream URL hosts.

The signed stream URL's signature covers only the path + expiry (not the host),
so swapping the host between relays is safe. Viewer coordinates come from the
Cloudflare "Add visitor location headers" managed transform (CF-IPLatitude /
CF-IPLongitude). Relays are configured via TORRIN_STREAM_RELAYS as
"host:lat:lng,host:lat:lng" — add a relay by adding an entry.
"""

import math
from urllib.parse import urlparse, urlunparse

from comet.core.models import settings


def _parse_relays():
    relays = []
    for part in (settings.TORRIN_STREAM_RELAYS or "").split(","):
        bits = part.strip().split(":")
        if len(bits) != 3:
            continue
        try:
            relays.append((bits[0], float(bits[1]), float(bits[2])))
        except ValueError:
            continue
    return relays


RELAYS = _parse_relays()
RELAY_HOSTS = {host for host, _, _ in RELAYS}


def _haversine_km(lat1, lng1, lat2, lng2):
    p = math.pi / 180
    a = (
        0.5
        - math.cos((lat2 - lat1) * p) / 2
        + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lng2 - lng1) * p)) / 2
    )
    return 12742 * math.asin(math.sqrt(a))


def nearest_host(lat, lng):
    if not RELAYS:
        return None
    return min(RELAYS, key=lambda r: _haversine_km(lat, lng, r[1], r[2]))[0]


def georoute_streams(request, streams):
    if not RELAYS:
        return streams
    try:
        lat = float(request.headers.get("cf-iplatitude"))
        lng = float(request.headers.get("cf-iplongitude"))
    except (TypeError, ValueError):
        return streams
    host = nearest_host(lat, lng)
    if not host:
        return streams
    for stream in streams:
        url = stream.get("url")
        if not url:
            continue
        parsed = urlparse(url)
        if parsed.netloc in RELAY_HOSTS and parsed.netloc != host:
            stream["url"] = urlunparse(parsed._replace(netloc=host))
    return streams
