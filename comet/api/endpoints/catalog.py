import asyncio

import aiohttp
from fastapi import APIRouter

from comet.core.models import settings
from comet.metadata.tmdb import DEFAULT_TMDB_READ_ACCESS_TOKEN
from comet.utils.http_client import http_client_manager

router = APIRouter()

TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

CATALOG_DEFS = [
    {
        "type": "movie",
        "id": "torrin-trending-movies",
        "name": "Trending Movies",
    },
    {
        "type": "series",
        "id": "torrin-trending-series",
        "name": "Trending Series",
    },
    {
        "type": "movie",
        "id": "torrin-popular-movies",
        "name": "Popular Movies",
    },
    {
        "type": "series",
        "id": "torrin-popular-series",
        "name": "Popular Series",
    },
]

# Cache TMDB->IMDB mappings to avoid repeated lookups.
_imdb_cache: dict[str, str | None] = {}


def _tmdb_headers() -> dict:
    token = settings.TMDB_READ_ACCESS_TOKEN or DEFAULT_TMDB_READ_ACCESS_TOKEN
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


async def _fetch_tmdb(path: str) -> list[dict]:
    session = await http_client_manager.get_session()
    url = f"https://api.themoviedb.org/3{path}"
    try:
        async with session.get(url, headers=_tmdb_headers(), timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return data.get("results", [])
    except Exception:
        return []


async def _get_imdb_id(tmdb_id: int, media_type: str) -> str | None:
    cache_key = f"{media_type}:{tmdb_id}"
    if cache_key in _imdb_cache:
        return _imdb_cache[cache_key]

    endpoint = "movie" if media_type == "movie" else "tv"
    session = await http_client_manager.get_session()
    url = f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}/external_ids"
    try:
        async with session.get(url, headers=_tmdb_headers(), timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                _imdb_cache[cache_key] = None
                return None
            data = await resp.json()
            imdb_id = data.get("imdb_id")
            _imdb_cache[cache_key] = imdb_id
            return imdb_id
    except Exception:
        _imdb_cache[cache_key] = None
        return None


async def _tmdb_to_meta(item: dict, content_type: str) -> dict | None:
    title = item.get("title") or item.get("name") or ""
    year = (item.get("release_date") or item.get("first_air_date") or "")[:4]
    poster_path = item.get("poster_path")
    poster = f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else None
    tmdb_id = item.get("id")

    imdb_id = await _get_imdb_id(tmdb_id, content_type)
    if not imdb_id:
        return None

    meta = {
        "id": imdb_id,
        "type": content_type,
        "name": title,
    }
    if poster:
        meta["poster"] = poster
    if year:
        meta["releaseInfo"] = year
    if item.get("overview"):
        meta["description"] = item["overview"]
    if item.get("vote_average"):
        meta["imdbRating"] = str(round(item["vote_average"], 1))

    return meta


async def _get_catalog(catalog_id: str, skip: int = 0) -> list[dict]:
    page = (skip // 20) + 1

    if catalog_id == "torrin-trending-movies":
        results = await _fetch_tmdb(f"/trending/movie/week?page={page}")
        metas = await asyncio.gather(*[_tmdb_to_meta(r, "movie") for r in results])

    elif catalog_id == "torrin-trending-series":
        results = await _fetch_tmdb(f"/trending/tv/week?page={page}")
        metas = await asyncio.gather(*[_tmdb_to_meta(r, "series") for r in results])

    elif catalog_id == "torrin-popular-movies":
        results = await _fetch_tmdb(f"/movie/popular?page={page}")
        metas = await asyncio.gather(*[_tmdb_to_meta(r, "movie") for r in results])

    elif catalog_id == "torrin-popular-series":
        results = await _fetch_tmdb(f"/tv/popular?page={page}")
        metas = await asyncio.gather(*[_tmdb_to_meta(r, "series") for r in results])

    else:
        return []

    return [m for m in metas if m is not None]


@router.get(
    "/catalog/{type}/{catalog_id}.json",
    tags=["Stremio"],
    summary="Catalog",
)
@router.get(
    "/{b64config}/catalog/{type}/{catalog_id}.json",
    tags=["Stremio"],
    summary="Catalog",
)
async def catalog(type: str, catalog_id: str, b64config: str = None):
    metas = await _get_catalog(catalog_id)
    return {"metas": metas}


@router.get(
    "/catalog/{type}/{catalog_id}/skip={skip}.json",
    tags=["Stremio"],
    summary="Catalog with pagination",
)
@router.get(
    "/{b64config}/catalog/{type}/{catalog_id}/skip={skip}.json",
    tags=["Stremio"],
    summary="Catalog with pagination",
)
async def catalog_skip(type: str, catalog_id: str, skip: int = 0, b64config: str = None):
    metas = await _get_catalog(catalog_id, skip=skip)
    return {"metas": metas}
