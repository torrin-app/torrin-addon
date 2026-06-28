from comet.core.logger import logger
from comet.core.models import settings
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest

_UNITS = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


def _parse_size(s: str) -> int:
    parts = (s or "").strip().split()
    if len(parts) != 2:
        return 0
    try:
        return int(float(parts[0]) * _UNITS.get(parts[1].upper(), 0))
    except ValueError:
        return 0


async def _resolve_imdb(request) -> str | None:
    """hdencode searches by IMDB; map kitsu anime ids to imdb when needed."""
    imdb = request.media_only_id or ""
    if imdb.startswith("tt"):
        return imdb
    parts = imdb.split(":")
    if len(parts) == 2 and parts[0] == "kitsu":
        from comet.services.anime import anime_mapper

        resolved = await anime_mapper.get_imdb_from_kitsu(parts[1])
        if resolved and str(resolved).startswith("tt"):
            return str(resolved)
    return None


def _alias_params(request) -> list:
    out, seen = [], set()
    for titles in (request.aliases or {}).values():
        for t in titles:
            if t and t != request.title and t not in seen:
                seen.add(t)
                out.append(("alias", t))
    return out


class HDEncodeScraper(BaseScraper):
    """Surfaces hdencode.org releases as streams, matched by IMDB id.

    Torrin scrapes hdencode (Content-Protector reveal) and, on play, caches the
    release (AllDebrid unlock + unrar) into the shared cache, so it shows up as a
    stream option alongside torrents/usenet. Catches scene releases that public
    torrent trackers miss. Gated by SCRAPE_HDENCODE; auths with the service key.
    """

    def __init__(self, manager, session, url: str):
        super().__init__(manager, session, url)

    async def scrape(self, request: ScrapeRequest):
        torrents = []

        key = getattr(settings, "TORRIN_SEARCH_KEY", None)
        if not key or not request.media_only_id:
            return torrents

        try:
            from urllib.parse import urlencode

            imdb = await _resolve_imdb(request)
            if not imdb:
                return torrents

            params = [("imdb", imdb)]
            if request.title:
                params.append(("title", request.title))
            params.extend(_alias_params(request))
            if request.media_type == "series":
                params.append(("season", request.season))
                params.append(("episode", request.episode))

            response = await self.session.get(
                f"{self.url}/api/hdencode/search?{urlencode(params)}",
                headers={"Authorization": f"Bearer {key}"},
            )
            data = await response.json()

            for result in data:
                info_hash = (result.get("info_hash") or "").lower()
                if not info_hash:
                    continue
                torrents.append(
                    {
                        "title": result.get("title", ""),
                        "infoHash": info_hash,
                        "fileIndex": None,
                        "seeders": None,
                        "size": _parse_size(result.get("size", "")),
                        "tracker": "HDEncode",
                        "sources": [],
                    }
                )
        except Exception as e:
            logger.warning(
                f"Exception while getting hdencode results for {request.title}: {e}"
            )

        return torrents
