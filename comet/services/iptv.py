import aiohttp
from comet.core.models import settings
from comet.core.logger import logger


async def search_iptv(session: aiohttp.ClientSession, title: str, year: int, api_key: str) -> list:
    if not settings.STREMTHRU_URL or not title:
        return []

    try:
        url = f"{settings.STREMTHRU_URL}/api/iptv/search"
        params = {"q": title, "year": str(year)} if year else {"q": title}
        headers = {"Authorization": f"Bearer {api_key}"}
        async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return data.get("results") or []
    except Exception as e:
        logger.warning(f"IPTV movie search failed: {e}")
        return []


async def search_iptv_series(session: aiohttp.ClientSession, title: str, season: int, episode: int, api_key: str) -> list:
    if not settings.STREMTHRU_URL or not title or not season or not episode:
        return []

    try:
        url = f"{settings.STREMTHRU_URL}/api/iptv/search-series"
        params = {"q": title, "season": str(season), "episode": str(episode)}
        headers = {"Authorization": f"Bearer {api_key}"}
        async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return data.get("results") or []
    except Exception as e:
        logger.warning(f"IPTV series search failed: {e}")
        return []
