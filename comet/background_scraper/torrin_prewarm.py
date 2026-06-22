"""Pre-warm Torrin's cache from the background scraper.

When the scraper finds torrents for a popular title, we pick the best single
release and ask Torrin to download + cache the actual file, so the first user to
request that title gets an instant cached stream instead of waiting on a download.

Torrin owns all the capacity control: the jobs it creates are lowest-priority and
capped, so this only ever uses spare capacity. Here we just pick a sensible
release and POST it. Gated by settings.TORRIN_PREWARM + a configured secret.
"""

from urllib.parse import quote

import aiohttp

from comet.core.logger import logger
from comet.core.models import settings
from comet.utils.http_client import http_client_manager

# Skip obvious season packs / oversized releases — Torrin also enforces a hard
# per-file cap, this just avoids pointless submissions.
MAX_PREWARM_SIZE = 25 * 1000 * 1000 * 1000  # 25 GB


def _enabled() -> bool:
    return bool(settings.TORRIN_PREWARM) and bool(settings.TORRIN_PREWARM_SECRET)


def _base_url() -> str:
    url = settings.TORRIN_PREWARM_URL
    if not url:
        tc = settings.TORRINCACHE_URL
        url = tc[0] if isinstance(tc, list) else tc
    return (url or "").rstrip("/")


def _pick_best(torrents: list) -> dict | None:
    """Pick the highest-seeded single release within the size cap."""
    candidates = [
        t
        for t in torrents
        if t.get("infoHash")
        and (not t.get("size") or t["size"] <= MAX_PREWARM_SIZE)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda t: (t.get("seeders") or 0), reverse=True)
    return candidates[0]


async def submit_prewarm(imdb_id: str, torrents: list) -> None:
    if not _enabled() or not torrents:
        return
    best = _pick_best(torrents)
    if not best:
        return

    info_hash = best["infoHash"].lower()
    name = best.get("title") or ""
    magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={quote(name)}"

    try:
        session = await http_client_manager.get_session()
        resp = await session.post(
            f"{_base_url()}/internal/prewarm",
            json={"magnet": magnet, "imdb_id": imdb_id or "", "name": name},
            headers={"X-Internal-Secret": settings.TORRIN_PREWARM_SECRET},
            timeout=aiohttp.ClientTimeout(total=15),
        )
        status = resp.status
        if status == 200:
            logger.log("BACKGROUND_SCRAPER", f"prewarm submitted {imdb_id} {info_hash[:8]}")
        elif status == 429:
            # Torrin is at its pre-warm capacity — fine, try again another time.
            pass
        else:
            logger.warning(f"prewarm rejected ({status}) for {imdb_id}")
    except Exception as e:
        logger.warning(f"prewarm request failed for {imdb_id}: {e}")
