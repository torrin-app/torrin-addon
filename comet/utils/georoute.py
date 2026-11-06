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


_REGION_NAMES = {
    "eu": "Europe",
    "za": "Africa",
    "in": "India",
    "us": "United States",
    "sg": "Singapore",
    "br": "Brazil",
}


def _derive_label(host):
    code = host.split(".", 1)[0].rsplit("-", 1)[-1].lower()
    return _REGION_NAMES.get(code, code.upper())


def _parse_relays():
    relays = []
    for part in (settings.TORRIN_STREAM_RELAYS or "").split(","):
        bits = part.strip().split(":")
        if len(bits) < 3:
            continue
        try:
            host, lat, lng = bits[0], float(bits[1]), float(bits[2])
        except ValueError:
            continue
        label = bits[3] if len(bits) >= 4 and bits[3] else _derive_label(host)
        relays.append((host, lat, lng, label))
    return relays


RELAYS = _parse_relays()
RELAY_HOSTS = {host for host, _, _, _ in RELAYS}


def relay_options():
    return [{"value": host, "label": label} for host, _, _, label in RELAYS]


def pinned_host(region):
    if region and region != "auto" and region in RELAY_HOSTS:
        return region
    return None


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


def _pick_host(request, region=None):
    if not RELAYS:
        return None
    pinned = pinned_host(region)
    if pinned:
        return pinned
    try:
        lat = float(request.headers.get("cf-iplatitude"))
        lng = float(request.headers.get("cf-iplongitude"))
    except (TypeError, ValueError):
        return None
    return nearest_host(lat, lng)


def georoute_url(request, url, region=None):
    if not url:
        return url
    host = _pick_host(request, region)
    if not host:
        return url
    parsed = urlparse(url)
    if parsed.netloc in RELAY_HOSTS and parsed.netloc != host:
        return urlunparse(parsed._replace(netloc=host))
    return url


def georoute_streams(request, streams, region=None):
    host = _pick_host(request, region)
    if not host:
        return streams
    for stream in streams:
        url = stream.get("url")
        if url:
            stream["url"] = georoute_url(request, url, region)
    return streams
