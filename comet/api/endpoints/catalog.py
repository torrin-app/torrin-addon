import aiohttp
from fastapi import APIRouter

from comet.core.models import settings
from comet.metadata.tmdb import TMDBApi, DEFAULT_TMDB_READ_ACCESS_TOKEN
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


async def _fetch_tmdb(path: str) -> list[dict]:
    token = settings.TMDB_READ_ACCESS_TOKEN or DEFAULT_TMDB_READ_ACCESS_TOKEN
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    session = await http_client_manager.get_session()
    url = f"https://api.themoviedb.org/3{path}"
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return data.get("results", [])
    except Exception:
        return []


def _tmdb_to_meta(item: dict, content_type: str) -> dict:
    title = item.get("title") or item.get("name") or ""
    year = (item.get("release_date") or item.get("first_air_date") or "")[:4]
    poster_path = item.get("poster_path")
    poster = f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else None

    imdb_prefix = "tt"
    tmdb_id = item.get("id")

    meta = {
        "id": f"tmdb:{tmdb_id}",
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
        return [_tmdb_to_meta(r, "movie") for r in results]

    elif catalog_id == "torrin-trending-series":
        results = await _fetch_tmdb(f"/trending/tv/week?page={page}")
        return [_tmdb_to_meta(r, "series") for r in results]

    elif catalog_id == "torrin-popular-movies":
        results = await _fetch_tmdb(f"/movie/popular?page={page}")
        return [_tmdb_to_meta(r, "movie") for r in results]

    elif catalog_id == "torrin-popular-series":
        results = await _fetch_tmdb(f"/tv/popular?page={page}")
        return [_tmdb_to_meta(r, "series") for r in results]

    return []


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
