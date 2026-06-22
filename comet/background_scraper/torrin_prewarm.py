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

# Size caps. Movies are single files; a series "pack" is a whole season in one
# torrent, so it gets a bigger allowance (one cache then serves every episode).
MAX_PREWARM_SIZE = 25 * 1000 * 1000 * 1000  # 25 GB (movies / single episodes)
MAX_PACK_SIZE = 60 * 1000 * 1000 * 1000  # 60 GB (season packs)
MAX_CANDIDATES = 5  # how many fallback releases to hand Torrin per title


def _enabled() -> bool:
    return bool(settings.TORRIN_PREWARM) and bool(settings.TORRIN_PREWARM_SECRET)


def _base_url() -> str:
    url = settings.TORRIN_PREWARM_URL
    if not url:
        tc = settings.TORRINCACHE_URL
        url = tc[0] if isinstance(tc, list) else tc
    return (url or "").rstrip("/")


def _is_season_pack(d: dict) -> bool:
    """A torrent is a season pack when it isn't scoped to a single episode."""
    parsed = d.get("parsed")
    eps = getattr(parsed, "episodes", None) or []
    return len(eps) != 1


def _pick_candidates(torrents: dict, is_series: bool = False) -> list:
    """torrents is {info_hash: {title, seeders, size, parsed, ...}}. Returns up to
    MAX_CANDIDATES releases. For series we prefer season packs (one cache covers the
    whole season) then seeders; for movies just seeders.
    """
    max_size = MAX_PACK_SIZE if is_series else MAX_PREWARM_SIZE
    items = [
        (ih, d)
        for ih, d in torrents.items()
        if ih
        and isinstance(d, dict)
        and (not d.get("size") or d["size"] <= max_size)
    ]
    if is_series:
        items.sort(
            key=lambda x: (_is_season_pack(x[1]), x[1].get("seeders") or 0),
            reverse=True,
        )
    else:
        items.sort(key=lambda x: (x[1].get("seeders") or 0), reverse=True)
    return [
        {"info_hash": ih.lower(), "name": d.get("title") or ""}
        for ih, d in items[:MAX_CANDIDATES]
    ]


async def submit_prewarm(imdb_id: str, torrents, is_series: bool = False) -> None:
    # Pre-warm must NEVER break the background scraper — wrap everything.
    try:
        if not _enabled() or not torrents:
            return
        candidates = _pick_candidates(torrents, is_series=is_series)
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
