from comet.core.logger import logger
from comet.core.models import settings
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest


class TorrinCacheScraper(BaseScraper):
    """Surfaces titles already cached in Torrin's own R2 cache, matched by IMDB id.

    Catches content the public indexers miss (niche releases, hoster imports) that
    another user already pulled into the shared cache, so it shows up as a stream
    option for everyone. Gated by SCRAPE_TORRIN_CACHE; auths with a service key.
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

            query = {"imdb": request.media_only_id}
            # Title (+ year) lets Torrin surface cached content that has no imdb tag
            # (usenet, hoster, manual adds) by matching the release name instead.
            if request.title:
                query["title"] = request.title
            if request.year:
                query["year"] = request.year
            if request.media_type == "series":
                query["season"] = request.season
                query["episode"] = request.episode

            response = await self.session.get(
                f"{self.url}/api/search?{urlencode(query)}",
                headers={"Authorization": f"Bearer {key}"},
            )
            data = await response.json()

            for result in data.get("results", []):
                info_hash = (result.get("info_hash") or "").lower()
                if not info_hash:
                    continue
                files = result.get("files") or []
                file_index = files[0].get("index") if files else None
                torrents.append(
                    {
                        "title": result.get("name", ""),
                        "infoHash": info_hash,
                        "fileIndex": file_index,
                        "seeders": None,
                        "size": int(result.get("size") or 0),
                        "tracker": "Torrin",
                        "sources": [],
                    }
                )
        except Exception as e:
            logger.warning(
                f"Exception while getting torrents for {request.title} with Torrin cache: {e}"
            )

        return torrents
