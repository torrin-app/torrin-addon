from fastapi import APIRouter, Header, HTTPException, Query

from comet.core.logger import logger
from comet.core.models import settings
from comet.metadata.manager import MetadataScraper
from comet.services.orchestration import TorrentManager
from comet.utils.http_client import http_client_manager

router = APIRouter()


def _verify_key(api_key: str | None):
    if not settings.TORZNAB_API_KEY:
        raise HTTPException(status_code=503, detail="torznab search disabled")
    if api_key != settings.TORZNAB_API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


@router.get("/torznab/search", tags=["Torznab"], summary="Torrent candidate search")
async def torznab_search(
    x_api_key: str | None = Header(None, alias="X-Api-Key"),
    search_type: str = Query("movie", alias="type"),
    imdb: str = Query(""),
    q: str = Query(""),
    season: int | None = Query(None),
    ep: int | None = Query(None),
    limit: int = Query(100),
):
    _verify_key(x_api_key)

    media_type = "series" if search_type == "series" else "movie"
    if not imdb:
        return {"results": []}

    if media_type == "series" and season is not None and ep is not None:
        media_id = f"{imdb}:{season}:{ep}"
    elif media_type == "series" and season is not None:
        media_id = f"{imdb}:{season}"
    else:
        media_id = imdb

    session = await http_client_manager.get_session()
    metadata, aliases = await MetadataScraper(session).fetch_metadata_and_aliases(
        media_type, media_id, imdb, season, ep
    )
    if metadata is None:
        logger.log("SCRAPER", f"❌ torznab: no metadata for {media_id}")
        return {"results": []}

    manager = TorrentManager(
        media_type,
        media_id,
        imdb,
        metadata["title"],
        metadata["year"],
        metadata["year_end"],
        metadata["season"],
        metadata["episode"],
        aliases,
        settings.REMOVE_ADULT_CONTENT,
        search_season=metadata["season"],
        search_episode=metadata["episode"],
    )

    await manager.get_cached_torrents()
    if not manager.torrents:
        await manager.scrape_torrents()

    results = []
    for info_hash, t in manager.torrents.items():
        seeders = t.get("seeders") or 0
        results.append(
            {
                "title": t.get("title") or "",
                "size": t.get("size") or 0,
                "seeders": seeders,
                "peers": seeders,
                "infohash": info_hash,
                "tracker": t.get("tracker") or "",
                "cached": False,
            }
        )
        if len(results) >= limit:
            break

    logger.log("SCRAPER", f"🧲 torznab: {len(results)} results for {media_id}")
    return {"results": results}
