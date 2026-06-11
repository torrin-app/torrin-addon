from fastapi import APIRouter, Request

from comet.core.config_validation import config_check
from comet.core.models import settings
from comet.api.endpoints.catalog import CATALOG_DEFS
from comet.debrid.manager import build_addon_name
from comet.utils.cache import (CachedJSONResponse, CachePolicies,
                               check_etag_match, generate_etag,
                               not_modified_response)

router = APIRouter()


@router.get(
    "/manifest.json",
    tags=["Stremio"],
    summary="Add-on Manifest",
    description="Returns the add-on manifest.",
)
@router.get(
    "/{b64config}/manifest.json",
    tags=["Stremio"],
    summary="Add-on Manifest",
    description="Returns the add-on manifest with existing configuration.",
)
async def manifest(request: Request, b64config: str = None):
    base_manifest = {
        "id": settings.ADDON_ID,
        "description": "Open-source debrid service with instant streaming.",
        "version": "2.0.0",
        "catalogs": CATALOG_DEFS,
        "resources": [
            {
                "name": "stream",
                "types": ["movie", "series"],
                "idPrefixes": ["tt", "kitsu"],
            },
            {
                "name": "catalog",
                "types": ["movie", "series"],
            },
        ],
        "types": ["movie", "series", "anime", "other"],
        "logo": "https://raw.githubusercontent.com/torrin-app/torrin/main/assets/logo.png",
        "background": "https://raw.githubusercontent.com/torrin-app/torrin/main/assets/torrin-banner.png",
        "behaviorHints": {"configurable": True, "configurationRequired": False},
        "stremioAddonsConfig": {"issuer": "https://stremio-addons.net", "signature": "eyJhbGciOiJkaXIiLCJlbmMiOiJBMTI4Q0JDLUhTMjU2In0..pacLbjk6PCw-rGbAKxABMw.9tB9oPOHrlU8K4CaFVStwG61osNaAi5sLj8G2UfMaSaSwyWIUTCWsoHkcSj5H1m-GKEO7f1gaXmV06JL3FYugBc592VrbLAVRYiEDHr5Bn6qWkvqsSSAfUJvS3_zGmGw._4xMu0_SKXHCVG616u41mw"},
    }

    config = config_check(b64config, strict_b64config=True)
    if not config:
        base_manifest["name"] = f"❌ | {settings.ADDON_NAME}"
        base_manifest["description"] = (
            f"⚠️ OBSOLETE CONFIGURATION, PLEASE RE-CONFIGURE ON {request.url.scheme}://{request.url.netloc} ⚠️"
        )
        return base_manifest

    base_manifest["name"] = build_addon_name(settings.ADDON_NAME, config)

    if settings.HTTP_CACHE_ENABLED:
        etag = generate_etag(base_manifest)
        if check_etag_match(request, etag):
            return not_modified_response(etag)

        return CachedJSONResponse(
            content=base_manifest,
            cache_control=CachePolicies.manifest(),
            etag=etag,
            vary=["Accept", "Accept-Encoding"],
        )

    return base_manifest
