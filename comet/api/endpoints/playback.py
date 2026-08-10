import asyncio
import time

import mediaflow_proxy.utils.http_utils
import orjson
from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse

from comet.core.config_validation import resolve_config
from comet.core.database import (DOWNLOAD_LINK_CACHE_TTL,
                                 build_scope_lookup_params, build_scope_params,
                                 database)
from comet.core.models import settings
from comet.debrid.exceptions import DebridLinkGenerationError
from comet.debrid.manager import (build_account_key_hash, get_debrid,
                                  get_debrid_credentials)
from comet.metadata.manager import MetadataScraper
from comet.services.status_video import build_status_video_response
from comet.services.usenet import best_stream_url, get_job, grab_usenet, stream_usenet
from comet.utils.georoute import georoute_url
from comet.services.streaming.manager import custom_handle_stream_request
from comet.utils.http_client import http_client_manager
from comet.utils.network import get_client_ip
from comet.utils.parsing import parse_optional_int

router = APIRouter()

USENET_PLAY_POLLS = 10
USENET_PLAY_INTERVAL = 2.0


async def cache_download_link(
    *,
    debrid_service: str,
    account_key_hash: str,
    info_hash: str,
    season: int | None,
    episode: int | None,
    download_url: str,
):
    params = {
        "debrid_service": debrid_service,
        "account_key_hash": account_key_hash,
        "info_hash": info_hash,
        "download_url": download_url,
        "updated_at": time.time(),
        **build_scope_params(season, episode),
    }
    await database.execute(
        """
        INSERT INTO download_links_cache (
            debrid_service,
            account_key_hash,
            info_hash,
            season,
            episode,
            season_norm,
            episode_norm,
            download_url,
            updated_at
        )
        VALUES (
            :debrid_service,
            :account_key_hash,
            :info_hash,
            :season,
            :episode,
            :season_norm,
            :episode_norm,
            :download_url,
            :updated_at
        )
        ON CONFLICT (
            debrid_service,
            account_key_hash,
            info_hash,
            season_norm,
            episode_norm
        ) DO UPDATE SET
            download_url = EXCLUDED.download_url,
            updated_at = EXCLUDED.updated_at
        """,
        params,
    )


@router.get(
    "/{b64config}/playback/{hash}/{service_index}/{index}/{season}/{episode}",
    tags=["Stremio"],
    summary="Playback Proxy",
    description="Proxies the playback request to the Debrid service or returns a cached link.",
)
async def playback(
    request: Request,
    b64config: str,
    hash: str,
    service_index: str,
    index: str,
    season: str,
    episode: str,
    torrent_name: str = Query(),
    name: str = Query(),
    media_id: str | None = Query(default=None),
):
    config = await resolve_config(b64config, strict_b64config=True)
    if not config:
        return build_status_video_response(
            ["BAD_REQUEST"],
            default_key="BAD_REQUEST",
        )

    torrent_name = torrent_name.strip()
    name = name.strip()
    media_id = media_id.strip() if media_id else None
    if not torrent_name or not name:
        return build_status_video_response(
            ["BAD_REQUEST"],
            default_key="BAD_REQUEST",
        )

    parsed_service_index = parse_optional_int(service_index)
    season = parse_optional_int(season)
    episode = parse_optional_int(episode)

    debrid_service, debrid_api_key = get_debrid_credentials(
        config, parsed_service_index
    )
    account_key_hash = build_account_key_hash(debrid_api_key)

    session = await http_client_manager.get_session()
    min_timestamp = time.time() - DOWNLOAD_LINK_CACHE_TTL
    scope_params = build_scope_lookup_params(season, episode)
    cached_link = await database.fetch_one(
        """
        SELECT download_url
        FROM download_links_cache
        WHERE debrid_service = :debrid_service
        AND account_key_hash = :account_key_hash
        AND info_hash = :info_hash
        AND season_norm = :season_norm
        AND episode_norm = :episode_norm
        AND updated_at >= :min_timestamp
        """,
        {
            "debrid_service": debrid_service,
            "account_key_hash": account_key_hash,
            "info_hash": hash,
            "min_timestamp": min_timestamp,
            **scope_params,
        },
    )

    download_url = None
    if cached_link:
        download_url = cached_link["download_url"]

    ip = get_client_ip(request)
    should_proxy = (
        settings.PROXY_DEBRID_STREAM
        and settings.PROXY_DEBRID_STREAM_PASSWORD == config["debridStreamProxyPassword"]
    )
    if download_url is None:
        # Retrieve torrent sources from database for private trackers.
        if media_id:
            torrent_data = await database.fetch_one(
                """
                SELECT sources_json
                FROM torrents
                WHERE info_hash = :info_hash
                AND media_id = :media_id
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                {"info_hash": hash, "media_id": media_id},
            )
            if torrent_data is None:
                torrent_data = await database.fetch_one(
                    """
                    SELECT sources_json
                    FROM torrents
                    WHERE info_hash = :info_hash
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    {"info_hash": hash},
                )
        else:
            torrent_data = await database.fetch_one(
                """
                SELECT sources_json, media_id
                FROM torrents
                WHERE info_hash = :info_hash
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                {"info_hash": hash},
            )

        sources = []
        context_media_id = media_id
        if torrent_data:
            if torrent_data["sources_json"]:
                sources = orjson.loads(torrent_data["sources_json"])
            if context_media_id is None:
                context_media_id = torrent_data["media_id"]

        aliases = {}
        debrid_video_id = None
        debrid_media_only_id = context_media_id
        if context_media_id:
            metadata_scraper = MetadataScraper(session)
            media_type = "series" if season is not None else "movie"

            if "tt" in context_media_id:
                full_media_id = (
                    f"{context_media_id}:{season}:{episode}"
                    if media_type == "series"
                    else context_media_id
                )
            else:
                full_media_id = (
                    f"kitsu:{context_media_id}:{episode}"
                    if media_type == "series"
                    else f"kitsu:{context_media_id}"
                )

            debrid_video_id = full_media_id
            _, aliases = await metadata_scraper.fetch_metadata_and_aliases(
                media_type, full_media_id
            )

        debrid = get_debrid(
            session,
            debrid_video_id,
            debrid_media_only_id,
            debrid_service,
            debrid_api_key,
            ip if not should_proxy else "",
        )
        try:
            download_url = await debrid.generate_download_link(
                hash,
                index,
                name,
                torrent_name,
                season,
                episode,
                sources,
                aliases,
            )
        except DebridLinkGenerationError as error:
            status_keys = error.status_keys
            return build_status_video_response(
                status_keys,
                default_key=status_keys[0] if status_keys else "UNKNOWN",
            )

        if not download_url:
            return build_status_video_response(
                [],
                default_key="UNKNOWN",
            )

        await cache_download_link(
            debrid_service=debrid_service,
            account_key_hash=account_key_hash,
            info_hash=hash,
            season=season,
            episode=episode,
            download_url=download_url,
        )

    if should_proxy:
        return await custom_handle_stream_request(
            request.method,
            download_url,
            mediaflow_proxy.utils.http_utils.get_proxy_headers(request),
            media_id=torrent_name,
            ip=ip,
        )

    return RedirectResponse(georoute_url(request, download_url), status_code=302)


@router.get(
    "/{b64config}/usenet/play",
    tags=["Stremio"],
    summary="Usenet Playback",
    description="Grabs an uncached Usenet NZB and streams it once ready (cache-and-play).",
)
async def usenet_play(
    request: Request,
    b64config: str,
    rid: str = Query(),
    imdb: str = Query(default=""),
    title: str = Query(default=""),
    source: str = Query(default=""),
    nzb_url: str = Query(default=""),
):
    config = await resolve_config(b64config, strict_b64config=True)
    if not config:
        return build_status_video_response(["BAD_REQUEST"], default_key="BAD_REQUEST")

    debrid_entries = config.get("_debridEntries") or []
    api_key = debrid_entries[0].get("apiKey", "") if debrid_entries else ""
    if not rid or not api_key:
        return build_status_video_response(["BAD_REQUEST"], default_key="BAD_REQUEST")

    session = await http_client_manager.get_session()

    stream_url = await stream_usenet(session, rid, title, imdb, api_key, source, nzb_url)
    if stream_url:
        return RedirectResponse(georoute_url(request, stream_url), status_code=302)

    job = await grab_usenet(session, rid, title, imdb, api_key, source, nzb_url)
    if job is None:
        return build_status_video_response(
            ["DOWNLOAD_SERVER_ERROR"], default_key="DOWNLOAD_SERVER_ERROR"
        )

    url = best_stream_url(job)
    job_id = job.get("id", "")
    polls = 0
    while not url and job_id and polls < USENET_PLAY_POLLS:
        await asyncio.sleep(USENET_PLAY_INTERVAL)
        url = best_stream_url(await get_job(session, job_id, api_key))
        polls += 1

    if url:
        return RedirectResponse(georoute_url(request, url), status_code=302)
    return build_status_video_response(
        ["MEDIA_NOT_CACHED_YET"], default_key="MEDIA_NOT_CACHED_YET"
    )
