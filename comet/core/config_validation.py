import base64
import secrets
import time
from functools import lru_cache

import orjson

from comet.core.models import (ConfigModel, database, default_config,
                               rtn_ranking_default, rtn_settings_default,
                               settings)

# Prefix that marks a URL segment as a stable config token (server-side
# stored config) rather than an inline base64 config. A real inline config is
# base64-encoded JSON, which always starts with "ey" and never with this
# prefix, so the two forms are unambiguous.
CONFIG_TOKEN_PREFIX = "cfg_"


def _normalize_debrid_config(validated_config: dict) -> dict:
    debrid_entries = []
    enable_torrent = False

    debrid_services = validated_config["debridServices"]
    if debrid_services:
        debrid_entries = [
            {"service": entry["service"], "apiKey": entry["apiKey"]}
            for entry in debrid_services
        ]
        enable_torrent = validated_config["enableTorrent"]
    else:
        legacy_service = validated_config["debridService"]

        if legacy_service == "torrent":
            enable_torrent = True
        else:
            debrid_entries.append(
                {
                    "service": legacy_service,
                    "apiKey": validated_config["debridApiKey"],
                }
            )

    if not debrid_entries and not enable_torrent:
        enable_torrent = True

    validated_config["_debridEntries"] = debrid_entries
    validated_config["_enableTorrent"] = enable_torrent

    return validated_config


def _default_validated_config():
    return _DEFAULT_VALIDATED_CONFIG


_DEFAULT_VALIDATED_CONFIG = default_config.copy()
_DEFAULT_VALIDATED_CONFIG["_debridEntries"] = []
_DEFAULT_VALIDATED_CONFIG["_enableTorrent"] = True


@lru_cache(maxsize=4096)
def _parse_and_validate_config(b64config: str):
    try:
        config = orjson.loads(base64.b64decode(b64config).decode())

        validated_config = ConfigModel(**config)
        validated_config = validated_config.model_dump()

        options = validated_config["options"]
        validated_config["options"] = {
            "remove_ranks_under": options["remove_ranks_under"],
            "allow_english_in_languages": options["allow_english_in_languages"],
            "remove_unknown_languages": options["remove_unknown_languages"],
            "remove_all_trash": validated_config["removeTrash"],
        }

        rtn_settings = rtn_settings_default.model_copy(
            update={
                "resolutions": rtn_settings_default.resolutions.model_copy(
                    update=validated_config["resolutions"]
                ),
                "options": rtn_settings_default.options.model_copy(
                    update=validated_config["options"]
                ),
                "languages": rtn_settings_default.languages.model_copy(
                    update=validated_config["languages"]
                ),
            }
        )

        validated_config["rtnSettings"] = rtn_settings
        validated_config["rtnRanking"] = rtn_ranking_default

        if (
            settings.PROXY_DEBRID_STREAM
            and settings.PROXY_DEBRID_STREAM_PASSWORD
            == validated_config["debridStreamProxyPassword"]
            and validated_config["debridApiKey"] == ""
            and not validated_config["debridServices"]
        ):
            validated_config["debridService"] = (
                settings.PROXY_DEBRID_STREAM_DEBRID_DEFAULT_SERVICE
            )
            validated_config["debridApiKey"] = (
                settings.PROXY_DEBRID_STREAM_DEBRID_DEFAULT_APIKEY
            )

        validated_config = _normalize_debrid_config(validated_config)

        return validated_config
    except Exception:
        return None


def config_check(b64config: str | None, strict_b64config: bool = False):
    if not b64config:
        return _default_validated_config()

    validated_config = _parse_and_validate_config(b64config)
    if validated_config is not None:
        return validated_config

    if strict_b64config:
        return None

    return _default_validated_config()


def is_config_token(segment: str | None) -> bool:
    return bool(segment) and segment.startswith(CONFIG_TOKEN_PREFIX)


async def _lookup_token_b64(token: str) -> str | None:
    row = await database.fetch_one(
        "SELECT config_b64 FROM addon_configs WHERE token = :token",
        {"token": token},
        force_primary=True,
    )
    return row["config_b64"] if row else None


async def resolve_config(segment: str | None, strict_b64config: bool = False):
    """Resolve a URL config segment to a validated config.

    Accepts either a stable config token (looked up server-side) or a legacy
    inline base64 config. Token resolution reuses the same cached parser keyed
    by the stored base64 string, so editing a token's config (which rewrites
    that string) never serves a stale parse.
    """
    if not segment:
        return _default_validated_config()

    if is_config_token(segment):
        stored_b64 = await _lookup_token_b64(segment)
        if stored_b64 is not None:
            validated_config = _parse_and_validate_config(stored_b64)
            if validated_config is not None:
                return validated_config

        # Unknown token or stored config no longer valid.
        if strict_b64config:
            return None
        return _default_validated_config()

    return config_check(segment, strict_b64config=strict_b64config)


async def save_addon_config(b64config: str, token: str | None = None) -> str | None:
    """Persist a base64 config under a stable token and return that token.

    If a valid existing token is supplied, its config is updated in place so
    the install URL never changes. Returns None if the config is invalid.
    """
    if not b64config or _parse_and_validate_config(b64config) is None:
        return None

    now = time.time()

    if is_config_token(token) and await _lookup_token_b64(token) is not None:
        await database.execute(
            "UPDATE addon_configs SET config_b64 = :b64, updated_at = :now "
            "WHERE token = :token",
            {"b64": b64config, "now": now, "token": token},
        )
        return token

    new_token = CONFIG_TOKEN_PREFIX + secrets.token_urlsafe(24)
    await database.execute(
        "INSERT INTO addon_configs (token, config_b64, created_at, updated_at) "
        "VALUES (:token, :b64, :now, :now)",
        {"token": new_token, "b64": b64config, "now": now},
    )
    return new_token


async def get_addon_config_b64(segment: str | None) -> str | None:
    """Return the raw base64 config for a URL segment (token or inline)."""
    if not segment:
        return None
    if is_config_token(segment):
        return await _lookup_token_b64(segment)
    return segment
