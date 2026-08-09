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


async def search_usenet(session: aiohttp.ClientSession, imdb_id: str, api_key: str, title: str = "") -> list:
    """Search user's Usenet indexer for a movie by IMDB ID."""
    if not settings.STREMTHRU_URL or not imdb_id:
        return []

    try:
        url = f"{settings.STREMTHRU_URL}/api/usenet/search"
        params = {"imdb": imdb_id}
        if title:
            params["title"] = title
        headers = {"Authorization": f"Bearer {api_key}"}
        async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return []
            return await resp.json()
    except Exception as e:
        logger.warning(f"Usenet movie search failed: {e}")
        return []


async def search_usenet_series(session: aiohttp.ClientSession, imdb_id: str, season: int, episode: int, api_key: str, title: str = "") -> list:
    """Search user's Usenet indexer for a TV episode by IMDB ID + season + episode."""
    if not settings.STREMTHRU_URL or not imdb_id or not season or not episode:
        return []

    try:
        url = f"{settings.STREMTHRU_URL}/api/usenet/search"
        params = {"imdb": imdb_id, "season": str(season), "ep": str(episode)}
        if title:
            params["title"] = title
        headers = {"Authorization": f"Bearer {api_key}"}
        async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return []
            return await resp.json()
    except Exception as e:
        logger.warning(f"Usenet series search failed: {e}")
        return []


async def get_job(session: aiohttp.ClientSession, job_id: str, api_key: str) -> dict | None:
    """Fetch a Torrin job by id (for cache-and-play polling)."""
    if not settings.STREMTHRU_URL or not job_id:
        return None

    try:
        url = f"{settings.STREMTHRU_URL}/api/jobs/{job_id}"
        headers = {"Authorization": f"Bearer {api_key}"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            return await resp.json()
    except Exception as e:
        logger.warning(f"Usenet job poll failed: {e}")
        return None


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


async def stream_usenet(session: aiohttp.ClientSession, result_id: str, title: str, imdb_id: str, api_key: str) -> str:
    """Ask Torrin for a live stream URL for a usenet result. Empty if streaming is off or the release is packed."""
    if not settings.STREMTHRU_URL:
        return ""

    try:
        url = f"{settings.STREMTHRU_URL}/api/usenet/stream"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        import json
        body = json.dumps({"id": result_id, "title": title, "imdb_id": imdb_id})
        async with session.post(url, headers=headers, data=body, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                return ""
            data = await resp.json()
            return data.get("url", "")
    except Exception as e:
        logger.warning(f"Usenet stream failed: {e}")
        return ""


def best_stream_url(job: dict | None) -> str:
    """Largest ready file's signed URL from a Torrin job, or empty if not ready."""
    if not job:
        return ""
    streams = [s for s in job.get("stream_urls") or [] if s.get("signed_url")]
    if not streams:
        return ""
    return max(streams, key=lambda s: s.get("size") or 0)["signed_url"]
