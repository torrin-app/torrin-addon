"""Pre-warm Torrin's cache from the background scraper.

When the scraper finds torrents for a popular title, we send Torrin the top few
release candidates (best-seeded first) and Torrin downloads + caches the first
one that works, so the first user to request that title gets an instant cached
stream. Sending several means a dead / no-video release just falls through to the
next source instead of giving up.

Torrin owns all the capacity control: the jobs it creates are lowest-priority and
capped, so this only ever uses spare capacity. Gated by settings.TORRIN_PREWARM +
a configured secret.
"""

import aiohttp

from comet.core.logger import logger
from comet.core.models import settings
from comet.utils.http_client import http_client_manager

# Skip obvious season packs / oversized releases — Torrin also enforces a hard
# per-file cap, this just avoids pointless submissions.
MAX_PREWARM_SIZE = 25 * 1000 * 1000 * 1000  # 25 GB
MAX_CANDIDATES = 5  # how many fallback releases to hand Torrin per title


def _enabled() -> bool:
    return bool(settings.TORRIN_PREWARM) and bool(settings.TORRIN_PREWARM_SECRET)


def _base_url() -> str:
    url = settings.TORRIN_PREWARM_URL
    if not url:
        tc = settings.TORRINCACHE_URL
        url = tc[0] if isinstance(tc, list) else tc
    return (url or "").rstrip("/")


def _pick_candidates(torrents: dict) -> list:
    """torrents is {info_hash: {title, seeders, size, ...}} (TorrentManager.torrents).
    Returns up to MAX_CANDIDATES releases, best-seeded first, within the size cap.
    """
    items = [
        (ih, d)
        for ih, d in torrents.items()
        if ih
        and isinstance(d, dict)
        and (not d.get("size") or d["size"] <= MAX_PREWARM_SIZE)
    ]
    items.sort(key=lambda x: (x[1].get("seeders") or 0), reverse=True)
    return [
        {"info_hash": ih.lower(), "name": d.get("title") or ""}
        for ih, d in items[:MAX_CANDIDATES]
    ]


async def submit_prewarm(imdb_id: str, torrents) -> None:
    # Pre-warm must NEVER break the background scraper — wrap everything.
    try:
        if not _enabled() or not torrents:
            return
        candidates = _pick_candidates(torrents)
        if not candidates:
            return

        session = await http_client_manager.get_session()
        resp = await session.post(
            f"{_base_url()}/internal/prewarm",
            json={
                "imdb_id": imdb_id or "",
                "name": candidates[0]["name"],
                "candidates": candidates,
            },
            headers={"X-Internal-Secret": settings.TORRIN_PREWARM_SECRET},
            timeout=aiohttp.ClientTimeout(total=15),
        )
        if resp.status == 200:
            logger.log("BACKGROUND_SCRAPER", f"prewarm {imdb_id}: {len(candidates)} candidates")
        elif resp.status != 429:  # 429 = at capacity, fine
            logger.warning(f"prewarm rejected ({resp.status}) for {imdb_id}")
    except Exception as e:
        logger.warning(f"prewarm error for {imdb_id}: {e}")
