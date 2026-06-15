import asyncio
import os

import aiohttp
from fastapi import APIRouter
from RTN import parse

# Live sports/IPTV catalog is gated behind a flag (default off).
SPORTS_ENABLED = os.getenv("SPORTS_CATALOG_ENABLED", "false").lower() in ("1", "true", "yes")

from comet.core.config_validation import config_check
from comet.core.logger import logger
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
LIBRARY_CATALOGS = [
    {
        "type": "movie",
        "id": "torrin-library-movies",
        "name": "My Library",
    },
    {
        "type": "series",
        "id": "torrin-library-series",
        "name": "My Shows",
    },
]

# Genre dropdown for the Sports catalog (maps to IPTV category names).
SPORTS_GENRES = [
    "PPV Live Events", "US Sports", "UK Sports", "Canada Sports", "beIN Sports",
    "ESPN Plus", "DAZN", "TSN Plus", "NFL", "NBA", "NHL", "MLB Baseball league",
    "NCAA", "NCAAF", "NCAAB", "Football", "EPL Premier League", "MLS", "UEFA Championship",
    "Bundesliga", "F1 Formula", "Formula1", "MOTOGP", "Golf", "Tennis Channel",
    "Rugby league", "WNBA League Pass", "FITE TV", "FIFA World Cup 2026", "SPORTS",
]

# Live sports channels, added when debrid configured (needs api key for streams).
SPORTS_CATALOGS = [
    {
        "type": "tv",
        "id": "torrin-sports",
        "name": "Torrin Sports",
        "genres": SPORTS_GENRES,
        "extra": [
            {"name": "genre", "options": SPORTS_GENRES, "isRequired": False},
            {"name": "skip", "isRequired": False},
        ],
    },
]

_SPORTS_PAGE = 100


def _sports_page(channels: list[dict], skip: int, genre: str | None) -> list[dict]:
    if genre:
        channels = [c for c in channels if c.get("category") == genre]
    page = channels[skip : skip + _SPORTS_PAGE]
    return [_sports_channel_meta(ch) for ch in page]


async def _get_sports_channels(api_key: str) -> list[dict]:
    """Fetch live sports channels from Torrin for the catalog."""
    if not settings.STREMTHRU_URL or not api_key:
        return []
    try:
        url = f"{settings.STREMTHRU_URL}/api/iptv/sports"
        headers = {"Authorization": f"Bearer {api_key}"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("channels") or []
    except Exception as e:
        logger.warning(f"sports channels fetch failed: {e}")
        return []


def _sports_channel_meta(ch: dict) -> dict:
    sid = ch.get("stream_id")
    return {
        "id": f"torrin-sports:{sid}",
        "type": "tv",
        "name": ch.get("name", "") or f"Channel {sid}",
        "poster": ch.get("logo", "") or "",
        "posterShape": "square",
        "genres": [ch.get("category", "")] if ch.get("category") else [],
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


async def _get_poster_by_name(name: str, content_type: str) -> str | None:
    """Search TMDB by RTN parsed title."""
    try:
        from RTN import parse as rtn_parse
        clean = rtn_parse(name).parsed_title
    except Exception:
        clean = name
    if not clean or len(clean) < 2:
        return None

    cache_key = f"poster:name:{clean}"
    if cache_key in _imdb_cache:
        return _imdb_cache[cache_key]

    endpoint = "search/tv" if content_type == "series" else "search/movie"
    session = await http_client_manager.get_session()
    try:
        async with session.get(
            f"https://api.themoviedb.org/3/{endpoint}",
            params={"query": clean},
            headers=_tmdb_headers(),
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status != 200:
                _imdb_cache[cache_key] = None
                return None
            data = await resp.json()
            results = data.get("results", [])
            if results and results[0].get("poster_path"):
                poster = f"{TMDB_IMAGE_BASE}{results[0]['poster_path']}"
                _imdb_cache[cache_key] = poster
                return poster
            _imdb_cache[cache_key] = None
            return None
    except Exception:
        _imdb_cache[cache_key] = None
        return None


async def _get_poster(name: str, content_type: str, imdb_id: str = "") -> str | None:
    """Get poster - try IMDB ID first, fall back to name search."""
    if imdb_id and imdb_id.startswith("tt"):
        poster = await _get_poster_for_imdb(imdb_id)
        if poster:
            return poster
    return await _get_poster_by_name(name, content_type)


import re

def _is_series_name(name: str) -> bool:
    """True if RTN parses the name as TV: any season/episode, or a 'complete'
    collection. RTN handles every format (S01E05, 3x02, Season 1, Complete
    Series, absolute numbering, ...) — far more robust than a regex."""
    try:
        p = parse(name)
        return bool(p.seasons or p.episodes or getattr(p, "complete", False))
    except Exception:
        return False


def _detect_type(name: str, files: list) -> str:
    """Detect movie vs series using the RTN parser (not a regex), so single
    episodes in any naming format land under Shows, not Movies."""
    if _is_series_name(name):
        return "series"
    # Fallback: a folder of multiple episode-like files is a series.
    if len(files) > 1:
        episode_files = sum(1 for f in files if _is_series_name(f.get("name", "")))
        if episode_files > 1:
            return "series"
    return "movie"


async def _get_library(api_key: str, store_name: str, filter_type: str = "") -> list[dict]:
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

        content_type = _detect_type(name, item.get("files", []))

        if filter_type and content_type != filter_type:
            continue

        meta = {
            "id": f"torrin:{info_hash}",
            "type": content_type,
            "name": name,
        }

        imdb_id = item.get("imdb_id", "")
        if imdb_id and imdb_id.startswith("tt"):
            meta["imdb_id"] = imdb_id
        poster_tasks.append((len(metas), _get_poster(name, content_type, imdb_id)))

        metas.append(meta)

    # Fetch posters in parallel.
    if poster_tasks:
        indices, tasks = zip(*poster_tasks)
        posters = await asyncio.gather(*tasks)
        for idx, poster in zip(indices, posters):
            if poster:
                metas[idx]["poster"] = poster

    return metas


_EP_PATTERN = re.compile(r'[Ss](\d{1,2})[Ee](\d{1,2})')

# TMDB show metadata cache.
_tmdb_show_cache: dict[str, dict | None] = {}
_tmdb_ep_cache: dict[str, dict | None] = {}


async def _fetch_tmdb_show_meta(title: str) -> dict | None:
    """Search TMDB for a TV show and return metadata."""
    if title in _tmdb_show_cache:
        return _tmdb_show_cache[title]

    session = await http_client_manager.get_session()
    try:
        async with session.get(
            f"https://api.themoviedb.org/3/search/tv",
            params={"query": title},
            headers=_tmdb_headers(),
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status != 200:
                _tmdb_show_cache[title] = None
                return None
            data = await resp.json()
            results = data.get("results", [])
            if not results:
                _tmdb_show_cache[title] = None
                return None
            show = results[0]
            year = (show.get("first_air_date") or "")[:4]
            meta = {
                "tmdb_id": show["id"],
                "overview": show.get("overview", ""),
                "genres": [str(g) for g in show.get("genre_ids", [])],
                "releaseInfo": year,
            }
            # Fetch genre names.
            genre_map = await _get_tv_genres()
            if genre_map:
                meta["genres"] = [genre_map.get(g, "") for g in show.get("genre_ids", []) if genre_map.get(g)]
            _tmdb_show_cache[title] = meta
            return meta
    except Exception:
        _tmdb_show_cache[title] = None
        return None


_tv_genres_cache: dict[int, str] | None = None


async def _get_tv_genres() -> dict[int, str]:
    """Fetch TV genre ID -> name mapping from TMDB."""
    global _tv_genres_cache
    if _tv_genres_cache is not None:
        return _tv_genres_cache

    session = await http_client_manager.get_session()
    try:
        async with session.get(
            "https://api.themoviedb.org/3/genre/tv/list",
            headers=_tmdb_headers(),
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json()
            _tv_genres_cache = {g["id"]: g["name"] for g in data.get("genres", [])}
            return _tv_genres_cache
    except Exception:
        return {}


async def _fetch_tmdb_episode(tmdb_id: int, season: int, episode: int) -> dict | None:
    """Fetch episode metadata from TMDB."""
    cache_key = f"{tmdb_id}:{season}:{episode}"
    if cache_key in _tmdb_ep_cache:
        return _tmdb_ep_cache[cache_key]

    session = await http_client_manager.get_session()
    try:
        async with session.get(
            f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season}/episode/{episode}",
            headers=_tmdb_headers(),
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status != 200:
                _tmdb_ep_cache[cache_key] = None
                return None
            data = await resp.json()
            result = {
                "name": data.get("name", ""),
                "overview": data.get("overview", ""),
            }
            still = data.get("still_path")
            if still:
                result["thumbnail"] = f"{TMDB_IMAGE_BASE}{still}"
            _tmdb_ep_cache[cache_key] = result
            return result
    except Exception:
        _tmdb_ep_cache[cache_key] = None
        return None


async def _get_library_meta(api_key: str, store_name: str, info_hash: str) -> dict | None:
    """Get meta for a single library item by hash."""
    items = await _fetch_user_magnets(api_key, store_name)
    for item in items:
        if item.get("hash") == info_hash and item.get("status") == "downloaded":
            files = item.get("files", [])
            name = item.get("name", info_hash)
            content_type = _detect_type(name, files)

            meta = {
                "id": f"torrin:{info_hash}",
                "type": content_type,
                "name": name,
            }

            # Fetch poster.
            imdb_id = item.get("imdb_id", "")
            poster = await _get_poster(name, content_type, imdb_id)
            if poster:
                meta["poster"] = poster
                meta["background"] = poster

            if content_type == "series" and files:
                videos = []
                for f in files:
                    fname = f.get("name", "")
                    ep_match = _EP_PATTERN.search(fname)
                    # Skip extras: only include files with S##E## pattern.
                    if not ep_match:
                        continue
                    season = int(ep_match.group(1))
                    episode = int(ep_match.group(2))
                    video = {
                        "id": f"torrin:{info_hash}:{f.get('index', 0)}",
                        "title": fname,
                        "season": season,
                        "episode": episode,
                        "released": item.get("added_at", ""),
                    }
                    videos.append(video)
                # Sort by season then episode.
                videos.sort(key=lambda v: (v["season"], v["episode"]))
                meta["videos"] = videos

                # Fetch show metadata from TMDB for description/genres.
                try:
                    from RTN import parse as rtn_parse
                    clean_title = rtn_parse(name).parsed_title
                except Exception:
                    clean_title = name
                tmdb_meta = await _fetch_tmdb_show_meta(clean_title)
                if tmdb_meta:
                    if tmdb_meta.get("overview"):
                        meta["description"] = tmdb_meta["overview"]
                    if tmdb_meta.get("genres"):
                        meta["genres"] = tmdb_meta["genres"]
                    if tmdb_meta.get("releaseInfo"):
                        meta["releaseInfo"] = tmdb_meta["releaseInfo"]
                    tmdb_id = tmdb_meta.get("tmdb_id")
                    if tmdb_id:
                        # Enrich episodes with thumbnails/overviews.
                        ep_tasks = []
                        for v in videos:
                            ep_tasks.append(_fetch_tmdb_episode(tmdb_id, v["season"], v["episode"]))
                        ep_results = await asyncio.gather(*ep_tasks)
                        for v, ep_data in zip(videos, ep_results):
                            if ep_data:
                                if ep_data.get("name"):
                                    v["title"] = ep_data["name"]
                                if ep_data.get("overview"):
                                    v["overview"] = ep_data["overview"]
                                if ep_data.get("thumbnail"):
                                    v["thumbnail"] = ep_data["thumbnail"]
            return meta
    return None


async def _get_library_streams(api_key: str, store_name: str, info_hash: str, file_idx: int | None = None) -> list[dict]:
    """Get streams for a library item - R2 signed URLs only, no scrapers."""
    items = await _fetch_user_magnets(api_key, store_name)
    for item in items:
        if item.get("hash") == info_hash and item.get("status") == "downloaded":
            files = item.get("files", [])
            name = item.get("name", "")
            content_type = _detect_type(name, files)

            # For movies with multiple files, only return the largest (main movie).
            if content_type == "movie" and file_idx is None and len(files) > 1:
                largest = max(files, key=lambda f: f.get("size", 0))
                files = [largest]

            streams = []
            for f in files:
                idx = f.get("index", 0)
                if file_idx is not None and idx != file_idx:
                    continue
                link = f.get("link", "")
                fname = f.get("name", "")
                size = f.get("size", 0)
                if link and fname:
                    size_str = ""
                    if size > 0:
                        if size > 1_000_000_000:
                            size_str = f" | {size / 1_000_000_000:.1f} GB"
                        elif size > 1_000_000:
                            size_str = f" | {size / 1_000_000:.0f} MB"
                    streams.append({
                        "name": "Torrin",
                        "description": f"{fname}{size_str}",
                        "url": link,
                        "behaviorHints": {
                            "bingeGroup": f"torrin|{info_hash}",
                            "filename": fname,
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
    if catalog_id in ("torrin-library-movies", "torrin-library-series") and b64config:
        filter_type = "movie" if catalog_id == "torrin-library-movies" else "series"
        config = config_check(b64config, strict_b64config=True)
        if config:
            debrid_entries = config.get("_debridEntries", [])
            if debrid_entries:
                entry = debrid_entries[0]
                metas = await _get_library(entry["apiKey"], entry["service"], filter_type)
                return {"metas": metas}
    if catalog_id == "torrin-sports":
        if not SPORTS_ENABLED:
            return {"metas": []}
        config = config_check(b64config, strict_b64config=True) if b64config else None
        if config:
            debrid_entries = config.get("_debridEntries", [])
            if debrid_entries:
                channels = await _get_sports_channels(debrid_entries[0]["apiKey"])
                return {"metas": _sports_page(channels, 0, None)}
        return {"metas": []}
    metas = await _get_catalog(catalog_id)
    return {"metas": metas}


def _parse_sports_extra(extra: str) -> tuple[int, str | None]:
    """Parse Stremio extra segment like 'genre=NFL&skip=100'."""
    skip, genre = 0, None
    for part in extra.split("&"):
        if part.startswith("skip="):
            try:
                skip = int(part[5:])
            except ValueError:
                skip = 0
        elif part.startswith("genre="):
            from urllib.parse import unquote
            genre = unquote(part[6:]) or None
    return skip, genre


@router.get(
    "/{b64config}/catalog/tv/torrin-sports/{extra}.json",
    tags=["Stremio"],
    summary="Sports Catalog (genre/skip)",
)
async def sports_catalog_extra(extra: str, b64config: str):
    if not SPORTS_ENABLED:
        return {"metas": []}
    config = config_check(b64config, strict_b64config=True)
    if not config:
        return {"metas": []}
    debrid_entries = config.get("_debridEntries", [])
    if not debrid_entries:
        return {"metas": []}
    skip, genre = _parse_sports_extra(extra)
    channels = await _get_sports_channels(debrid_entries[0]["apiKey"])
    return {"metas": _sports_page(channels, skip, genre)}


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
    if catalog_id == "torrin-sports":
        if not SPORTS_ENABLED:
            return {"metas": []}
        config = config_check(b64config, strict_b64config=True) if b64config else None
        if config:
            debrid_entries = config.get("_debridEntries", [])
            if debrid_entries:
                channels = await _get_sports_channels(debrid_entries[0]["apiKey"])
                return {"metas": _sports_page(channels, skip, None)}
        return {"metas": []}
    if catalog_id in ("torrin-library-movies", "torrin-library-series") and b64config:
        filter_type = "movie" if catalog_id == "torrin-library-movies" else "series"
        config = config_check(b64config, strict_b64config=True)
        if config:
            debrid_entries = config.get("_debridEntries", [])
            if debrid_entries:
                entry = debrid_entries[0]
                metas = await _get_library(entry["apiKey"], entry["service"], filter_type)
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
    "/{b64config}/stream/{type}/torrin:{torrin_id}.json",
    tags=["Stremio"],
    summary="Library Stream",
)
async def library_stream(type: str, torrin_id: str, b64config: str):
    config = config_check(b64config, strict_b64config=True)
    if not config:
        return {"streams": []}
    debrid_entries = config.get("_debridEntries", [])
    if not debrid_entries:
        return {"streams": []}
    entry = debrid_entries[0]

    # Parse torrin_id: "HASH" for movies, "HASH:INDEX" for episodes.
    parts = torrin_id.split(":")
    info_hash = parts[0]
    file_idx = int(parts[1]) if len(parts) > 1 else None

    streams = await _get_library_streams(entry["apiKey"], entry["service"], info_hash, file_idx)
    return {"streams": streams}


# --- Sports TV meta handler ---
@router.get(
    "/{b64config}/meta/tv/torrin-sports:{stream_id}.json",
    tags=["Stremio"],
    summary="Sports Channel Meta",
)
async def sports_meta(stream_id: str, b64config: str):
    if not SPORTS_ENABLED:
        return {"meta": None}
    config = config_check(b64config, strict_b64config=True)
    if not config:
        return {"meta": None}
    debrid_entries = config.get("_debridEntries", [])
    if not debrid_entries:
        return {"meta": None}
    channels = await _get_sports_channels(debrid_entries[0]["apiKey"])
    for ch in channels:
        if str(ch.get("stream_id")) == stream_id:
            return {"meta": _sports_channel_meta(ch)}
    return {"meta": None}


# --- Sports TV stream handler ---
@router.get(
    "/{b64config}/stream/tv/torrin-sports:{stream_id}.json",
    tags=["Stremio"],
    summary="Sports Channel Stream",
)
async def sports_stream(stream_id: str, b64config: str):
    if not SPORTS_ENABLED:
        return {"streams": []}
    config = config_check(b64config, strict_b64config=True)
    if not config:
        return {"streams": []}
    debrid_entries = config.get("_debridEntries", [])
    if not debrid_entries or not settings.STREMTHRU_URL:
        return {"streams": []}
    api_key = debrid_entries[0]["apiKey"]
    # .ts so the player treats it as progressive MPEG-TS (the upstream serves raw
    # TS, not an HLS playlist). The Torrin proxy fetches the provider's m3u8.
    url = f"{settings.STREMTHRU_URL}/api/stream/iptv-live/{stream_id}.ts?token={api_key}"
    return {
        "streams": [
            {
                "name": "[Torrin📺] Live",
                "description": "Live sports channel",
                "url": url,
                "behaviorHints": {"notWebReady": True},
            }
        ]
    }
