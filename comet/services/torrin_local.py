"""Query Torrin's local-library cache tier.

Torrin indexes media that lives on disk (a seed library, cold storage, etc) and
serves it straight off disk as an instant source. For a given title we ask Torrin
"do you have this locally?" and turn any matches into Stremio streams. Best-effort:
never breaks stream resolution.
"""

import aiohttp

from comet.core.logger import logger
from comet.core.models import settings


def _enabled() -> bool:
    return (
        bool(settings.TORRIN_LOCAL_ENABLED)
        and bool(settings.TORRIN_LOCAL_URL)
        and bool(settings.TORRIN_LOCAL_SECRET)
    )


def _candidate_titles(title: str, aliases) -> list:
    """title plus any aliases, deduped (case-insensitive), capped."""
    out = []
    if title:
        out.append(title)
    if isinstance(aliases, dict):
        for v in aliases.values():
            if isinstance(v, (list, tuple, set)):
                out.extend(str(x) for x in v)
            elif v:
                out.append(str(v))
    elif isinstance(aliases, (list, tuple, set)):
        out.extend(str(x) for x in aliases)

    seen = set()
    res = []
    for t in out:
        t = (t or "").strip()
        key = t.lower()
        if t and key not in seen:
            seen.add(key)
            res.append(t)
    return res[:8]


async def search_local(
    session, title, aliases, year, media_type, season, episode, user_key=""
) -> list:
    if not _enabled():
        return []
    try:
        titles = _candidate_titles(title, aliases)
        if not titles:
            return []
        params = [("title", t) for t in titles]
        if media_type == "movie":
            if year:
                params.append(("year", str(year)))
        else:
            if season is not None:
                params.append(("season", str(season)))
            if episode is not None:
                params.append(("episode", str(episode)))
        # The caller's Torrin store key, so a play can be attributed to their library.
        if user_key:
            params.append(("key", user_key))

        url = settings.TORRIN_LOCAL_URL.rstrip("/") + "/internal/local"
        resp = await session.get(
            url,
            params=params,
            headers={"X-Internal-Secret": settings.TORRIN_LOCAL_SECRET},
            timeout=aiohttp.ClientTimeout(total=8),
        )
        if resp.status != 200:
            return []
        data = await resp.json()
        return data.get("results", []) or []
    except Exception as e:
        logger.warning(f"local library search failed: {e}")
        return []
