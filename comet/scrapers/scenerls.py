from comet.core.logger import logger
from comet.core.models import settings
from comet.scrapers.base import BaseScraper
from comet.scrapers.hdencode import _parse_size
from comet.scrapers.models import ScrapeRequest


class ScenerlsScraper(BaseScraper):
    """Surfaces scene-rls.net releases as streams, matched by title + year.

    Like hdencode, torrin scrapes scene-rls (NFO reveal) and, on play, caches the
    release (AllDebrid unlock + unrar) into the shared cache. scene-rls indexes by
    release name (no IMDB), so we search by title and let torrin filter by year /
    SxxExx. Gated by SCRAPE_SCENERLS; auths with the service key.
    """

    def __init__(self, manager, session, url: str):
        super().__init__(manager, session, url)

    async def scrape(self, request: ScrapeRequest):
        torrents = []

        key = getattr(settings, "TORRIN_SEARCH_KEY", None)
        if not key or not request.title:
            return torrents

        try:
            from urllib.parse import urlencode

            params = [("title", request.title)]
            if request.media_only_id:
                params.append(("imdb", request.media_only_id))
            if request.media_type == "series":
                params.append(("season", request.season))
                params.append(("episode", request.episode))
            elif request.year:
                params.append(("year", request.year))

            response = await self.session.get(
                f"{self.url}/api/scenerls/search?{urlencode(params)}",
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
                        "tracker": "Scene-RLS",
                        "sources": [],
                    }
                )
        except Exception as e:
            logger.warning(
                f"Exception while getting scene-rls results for {request.title}: {e}"
            )

        return torrents
