import secrets

from fastapi import APIRouter, Body, Cookie, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from comet.core.config_validation import (get_addon_config_b64, resolve_config,
                                          save_addon_config)
from comet.core.models import settings, web_config
from comet.utils.cache import CachePolicies
from comet.utils.signed_session import (derive_session_secret,
                                        encode_signed_session,
                                        verify_signed_session)

router = APIRouter()
templates = Jinja2Templates("comet/templates")
CONFIGURE_SESSION_COOKIE = "configure_session"
CONFIGURE_PAGE_PASSWORD = settings.CONFIGURE_PAGE_PASSWORD
CONFIGURE_PAGE_PASSWORD_ENABLED = bool(CONFIGURE_PAGE_PASSWORD)
CONFIGURE_SESSION_SECRET = (
    derive_session_secret(CONFIGURE_PAGE_PASSWORD, "configure-page")
    if CONFIGURE_PAGE_PASSWORD_ENABLED
    else b""
)
CONFIGURE_PAGE_SESSION_TTL = max(60, settings.CONFIGURE_PAGE_SESSION_TTL)
PRIVATE_NO_CACHE_CONTROL = CachePolicies.no_cache().build()


def _encode_configure_session():
    return encode_signed_session(
        secret=CONFIGURE_SESSION_SECRET,
        ttl=CONFIGURE_PAGE_SESSION_TTL,
    )


def _verify_configure_session(configure_session: str | None):
    return verify_signed_session(
        token=configure_session,
        secret=CONFIGURE_SESSION_SECRET,
    )


def _next_url(request: Request):
    return (
        f"{request.url.path}?{request.url.query}"
        if request.url.query
        else request.url.path
    )


def _sanitize_next_url(next_url: str | None):
    if not next_url:
        return "/configure"

    if "\\" in next_url:
        return "/configure"

    if not next_url.startswith("/") or next_url.startswith("//"):
        return "/configure"

    return next_url


def _apply_private_no_cache(response):
    response.headers["Cache-Control"] = PRIVATE_NO_CACHE_CONTROL
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["Vary"] = "Cookie, Accept, Accept-Encoding"


def _render_configure_login(request: Request, next_url: str, error: str = ""):
    response = templates.TemplateResponse(
        "admin_login.html",
        {
            "request": request,
            "error": error,
            "form_action": "/configure/login",
            "password_label": "Configure Password",
            "password_placeholder": "Enter configure password",
            "next_url": _sanitize_next_url(next_url),
        },
    )
    _apply_private_no_cache(response)
    return response


@router.post(
    "/configure/login",
    tags=["Configuration"],
    summary="Configuration Login",
    description="Authenticates and unlocks the configuration page.",
)
async def configure_login(
    request: Request,
    password: str = Form(..., description="Configuration page password"),
    next_url: str = Form("/configure", alias="next"),
):
    if not CONFIGURE_PAGE_PASSWORD_ENABLED:
        return RedirectResponse(_sanitize_next_url(next_url), status_code=303)

    is_correct = secrets.compare_digest(password, CONFIGURE_PAGE_PASSWORD)
    if not is_correct:
        return _render_configure_login(
            request, next_url=_sanitize_next_url(next_url), error="Invalid password"
        )

    response = RedirectResponse(_sanitize_next_url(next_url), status_code=303)
    response.set_cookie(
        key=CONFIGURE_SESSION_COOKIE,
        value=_encode_configure_session(),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=CONFIGURE_PAGE_SESSION_TTL,
    )
    _apply_private_no_cache(response)
    return response


@router.get(
    "/configure",
    tags=["Configuration"],
    summary="Configuration Page",
    description="Renders the configuration page.",
)
@router.get(
    "/{b64config}/configure",
    tags=["Configuration"],
    summary="Configuration Page",
    description="Renders the configuration page with existing configuration.",
)
async def configure(
    request: Request,
    b64config: str | None = None,
    configure_session: str | None = Cookie(
        None, description="Configuration page session token"
    ),
):
    if b64config is not None and not await resolve_config(b64config, strict_b64config=True):
        return RedirectResponse("/configure", status_code=303)

    if CONFIGURE_PAGE_PASSWORD_ENABLED and not _verify_configure_session(
        configure_session
    ):
        return _render_configure_login(request, next_url=_next_url(request))

    response = templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "CUSTOM_HEADER_HTML": settings.CUSTOM_HEADER_HTML
            if settings.CUSTOM_HEADER_HTML
            else "",
            "webConfig": web_config,
            "proxyDebridStream": settings.PROXY_DEBRID_STREAM,
            "disableTorrentStreams": settings.DISABLE_TORRENT_STREAMS,
            "stremioApiPrefix": settings.STREMIO_API_PREFIX,
        },
    )

    if CONFIGURE_PAGE_PASSWORD_ENABLED:
        _apply_private_no_cache(response)
    elif settings.HTTP_CACHE_ENABLED:
        response.headers["Cache-Control"] = CachePolicies.configure_page().build()
        response.headers["Vary"] = "Accept, Accept-Encoding"

    return response


def _save_config_authorized(configure_session: str | None) -> bool:
    if not CONFIGURE_PAGE_PASSWORD_ENABLED:
        return True
    return bool(_verify_configure_session(configure_session))


@router.post(
    "/save-config",
    tags=["Configuration"],
    summary="Save Configuration",
    description="Persists a configuration server-side and returns a stable token.",
)
async def save_config(
    b64config: str = Body(..., embed=True),
    token: str | None = Body(None, embed=True),
    configure_session: str | None = Cookie(
        None, description="Configuration page session token"
    ),
):
    if not _save_config_authorized(configure_session):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    saved_token = await save_addon_config(b64config, token=token)
    if not saved_token:
        return JSONResponse({"error": "invalid config"}, status_code=400)

    response = JSONResponse({"token": saved_token})
    _apply_private_no_cache(response)
    return response


@router.get(
    "/{segment}/config-data",
    tags=["Configuration"],
    summary="Configuration Data",
    description="Returns the stored base64 config for a token (used to prefill the configure page).",
)
async def config_data(
    segment: str,
    configure_session: str | None = Cookie(
        None, description="Configuration page session token"
    ),
):
    if not _save_config_authorized(configure_session):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    stored_b64 = await get_addon_config_b64(segment)
    if stored_b64 is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    response = JSONResponse({"b64config": stored_b64})
    _apply_private_no_cache(response)
    return response
