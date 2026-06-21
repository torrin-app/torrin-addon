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

            params = [("imdb", request.media_only_id)]
            # Title (+ aliases) lets Torrin surface cached content that has no imdb tag
            # (usenet, hoster, manual adds) by matching the release name instead. Alias
            # titles matter for anime (romaji vs english, e.g. "Diamond no Ace" cached
            # as "Ace of the Diamond") and any AKA-titled content. Send all variants.
            titles = []
            if request.title:
                titles.append(request.title)
            if request.aliases:
                titles.extend(request.aliases.get("ez", []))
            seen_titles = set()
            for t in titles:
                if t and t not in seen_titles:
                    seen_titles.add(t)
                    params.append(("title", t))
            if request.year:
                params.append(("year", request.year))
            if request.media_type == "series":
                params.append(("season", request.season))
                params.append(("episode", request.episode))

            query_url = f"{self.url}/api/search?{urlencode(params)}"
            logger.log("SCRAPER", f"[TorrinCache] GET {query_url}")
            response = await self.session.get(
                query_url,
                headers={"Authorization": f"Bearer {key}"},
            )
            data = await response.json()
            logger.log(
                "SCRAPER",
                f"[TorrinCache] status={response.status} count={data.get('count')} title={request.title!r} s={request.season} e={request.episode}",
            )

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
