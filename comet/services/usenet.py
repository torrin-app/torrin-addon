import aiohttp
from comet.core.models import settings
from comet.core.logger import logger


async def get_cached_usenet(session: aiohttp.ClientSession, imdb_id: str, api_key: str) -> list:
    """Check if any usenet content is cached for this IMDB ID."""
    if not settings.STREMTHRU_URL or not imdb_id:
        return []

    try:
        url = f"{settings.STREMTHRU_URL}/api/usenet/cached"
        params = {"imdb": imdb_id}
        headers = {"Authorization": f"Bearer {api_key}"}
        async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                return []
            return await resp.json()
    except Exception as e:
        logger.warning(f"Usenet cache check failed: {e}")
        return []


async def search_usenet(session: aiohttp.ClientSession, imdb_id: str, api_key: str) -> list:
    """Search user's Usenet indexer for a movie by IMDB ID."""
    if not settings.STREMTHRU_URL or not imdb_id:
        return []

    try:
        url = f"{settings.STREMTHRU_URL}/api/usenet/search"
        params = {"imdb": imdb_id}
        headers = {"Authorization": f"Bearer {api_key}"}
        async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return []
            return await resp.json()
    except Exception as e:
        logger.warning(f"Usenet movie search failed: {e}")
        return []


async def search_usenet_series(session: aiohttp.ClientSession, imdb_id: str, season: int, episode: int, api_key: str) -> list:
    """Search user's Usenet indexer for a TV episode by IMDB ID + season + episode."""
    if not settings.STREMTHRU_URL or not imdb_id or not season or not episode:
        return []

    try:
        url = f"{settings.STREMTHRU_URL}/api/usenet/search"
        params = {"imdb": imdb_id, "season": str(season), "ep": str(episode)}
        headers = {"Authorization": f"Bearer {api_key}"}
        async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return []
            return await resp.json()
    except Exception as e:
        logger.warning(f"Usenet series search failed: {e}")
        return []


async def grab_usenet(session: aiohttp.ClientSession, result_id: str, title: str, imdb_id: str, api_key: str) -> dict | None:
    """Submit an NZB to Torrin for download via the grab endpoint."""
    if not settings.STREMTHRU_URL:
        return None

    try:
        url = f"{settings.STREMTHRU_URL}/api/usenet/grab"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        import json
        body = json.dumps({"id": result_id, "title": title, "imdb_id": imdb_id})
        async with session.post(url, headers=headers, data=body, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status not in (200, 202):
                return None
            return await resp.json()
    except Exception as e:
        logger.warning(f"Usenet grab failed: {e}")
        return None
