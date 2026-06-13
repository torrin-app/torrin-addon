import asyncio

import aiohttp
from fastapi import APIRouter

from comet.core.config_validation import config_check
from comet.core.models import settings
from comet.debrid.manager import get_debrid_credentials
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

# Library catalog is added dynamically when user has debrid configured.
LIBRARY_CATALOG = {
    "type": "other",
    "id": "torrin-library",
    "name": "My Library",
}

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


async def _fetch_user_magnets(api_key: str, store_name: str) -> list[dict]:
    """Fetch user's magnets from Torrin API."""
    session = await http_client_manager.get_session()
    headers = {
        "X-StremThru-Store-Name": store_name,
        "X-StremThru-Store-Authorization": f"Bearer {api_key}",
        "User-Agent": "comet",
    }
    try:
        async with session.get(
            f"{settings.STREMTHRU_URL}/v0/store/magnets?limit=200&offset=0&client_ip=127.0.0.1",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return []
            payload = await resp.json()
            return payload.get("data", {}).get("items", [])
    except Exception:
        return []


async def _get_poster_for_imdb(imdb_id: str) -> str | None:
    """Fetch poster URL from TMDB for an IMDB ID."""
    cache_key = f"poster:{imdb_id}"
    if cache_key in _imdb_cache:
        return _imdb_cache[cache_key]

    session = await http_client_manager.get_session()
    try:
        async with session.get(
            f"https://api.themoviedb.org/3/find/{imdb_id}?external_source=imdb_id",
            headers=_tmdb_headers(),
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status != 200:
                _imdb_cache[cache_key] = None
                return None
            data = await resp.json()
            for key in ("movie_results", "tv_results"):
                results = data.get(key, [])
                if results and results[0].get("poster_path"):
                    poster = f"{TMDB_IMAGE_BASE}{results[0]['poster_path']}"
                    _imdb_cache[cache_key] = poster
                    return poster
            _imdb_cache[cache_key] = None
            return None
    except Exception:
        _imdb_cache[cache_key] = None
        return None


async def _get_library(api_key: str, store_name: str) -> list[dict]:
    """Fetch user's cached content and return as catalog metas with torrin: prefix."""
    items = await _fetch_user_magnets(api_key, store_name)

    metas = []
    poster_tasks = []
    seen_hashes = set()
    for item in items:
        if item.get("status") != "downloaded":
            continue
        info_hash = item.get("hash", "")
        name = item.get("name", "")
        if not name or not info_hash or info_hash in seen_hashes:
            continue
        seen_hashes.add(info_hash)

        meta = {
            "id": f"torrin:{info_hash}",
            "type": "other",
            "name": name,
        }

        imdb_id = item.get("imdb_id", "")
        if imdb_id and imdb_id.startswith("tt"):
            meta["imdb_id"] = imdb_id
            poster_tasks.append((len(metas), _get_poster_for_imdb(imdb_id)))

        metas.append(meta)

    # Fetch posters in parallel.
    if poster_tasks:
        indices, tasks = zip(*poster_tasks)
        posters = await asyncio.gather(*tasks)
        for idx, poster in zip(indices, posters):
            if poster:
                metas[idx]["poster"] = poster

    return metas


async def _get_library_meta(api_key: str, store_name: str, info_hash: str) -> dict | None:
    """Get meta for a single library item by hash."""
    items = await _fetch_user_magnets(api_key, store_name)
    for item in items:
        if item.get("hash") == info_hash and item.get("status") == "downloaded":
            files = item.get("files", [])
            meta = {
                "id": f"torrin:{info_hash}",
                "type": "other",
                "name": item.get("name", info_hash),
            }
            # Fetch poster if IMDB ID available.
            imdb_id = item.get("imdb_id", "")
            if imdb_id and imdb_id.startswith("tt"):
                poster = await _get_poster_for_imdb(imdb_id)
                if poster:
                    meta["poster"] = poster
                    meta["background"] = poster
            if files:
                videos = []
                for f in files:
                    videos.append({
                        "id": f"torrin:{info_hash}:{f.get('index', 0)}",
                        "title": f.get("name", ""),
                        "released": item.get("added_at", ""),
                    })
                if len(videos) > 1:
                    meta["videos"] = videos
            return meta
    return None


async def _get_library_streams(api_key: str, store_name: str, info_hash: str) -> list[dict]:
    """Get streams for a library item - R2 signed URLs only, no scrapers."""
    items = await _fetch_user_magnets(api_key, store_name)
    for item in items:
        if item.get("hash") == info_hash and item.get("status") == "downloaded":
            streams = []
            for f in item.get("files", []):
                link = f.get("link", "")
                name = f.get("name", "")
                size = f.get("size", 0)
                if link and name:
                    size_str = ""
                    if size > 0:
                        if size > 1_000_000_000:
                            size_str = f" | {size / 1_000_000_000:.1f} GB"
                        elif size > 1_000_000:
                            size_str = f" | {size / 1_000_000:.0f} MB"
                    streams.append({
                        "name": "Torrin",
                        "description": f"{name}{size_str}",
                        "url": link,
                        "behaviorHints": {
                            "bingeGroup": f"torrin|{info_hash}",
                            "filename": name,
                        },
                    })
            return streams
    return []


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
    if catalog_id == "torrin-library" and b64config:
        config = config_check(b64config, strict_b64config=True)
        if config:
            debrid_entries = config.get("_debridEntries", [])
            if debrid_entries:
                entry = debrid_entries[0]
                metas = await _get_library(entry["apiKey"], entry["service"])
                return {"metas": metas}
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
    if catalog_id == "torrin-library" and b64config:
        config = config_check(b64config, strict_b64config=True)
        if config:
            debrid_entries = config.get("_debridEntries", [])
            if debrid_entries:
                entry = debrid_entries[0]
                metas = await _get_library(entry["apiKey"], entry["service"])
                return {"metas": metas}
    metas = await _get_catalog(catalog_id, skip=skip)
    return {"metas": metas}


# --- Library meta handler ---
@router.get(
    "/{b64config}/meta/{type}/torrin:{info_hash}.json",
    tags=["Stremio"],
    summary="Library Meta",
)
async def library_meta(type: str, info_hash: str, b64config: str):
    config = config_check(b64config, strict_b64config=True)
    if not config:
        return {"meta": None}
    debrid_entries = config.get("_debridEntries", [])
    if not debrid_entries:
        return {"meta": None}
    entry = debrid_entries[0]
    meta = await _get_library_meta(entry["apiKey"], entry["service"], info_hash)
    if not meta:
        return {"meta": None}
    return {"meta": meta}


# --- Library stream handler ---
@router.get(
    "/{b64config}/stream/{type}/torrin:{info_hash}.json",
    tags=["Stremio"],
    summary="Library Stream",
)
async def library_stream(type: str, info_hash: str, b64config: str):
    config = config_check(b64config, strict_b64config=True)
    if not config:
        return {"streams": []}
    debrid_entries = config.get("_debridEntries", [])
    if not debrid_entries:
        return {"streams": []}
    entry = debrid_entries[0]
    streams = await _get_library_streams(entry["apiKey"], entry["service"], info_hash)
    return {"streams": streams}
