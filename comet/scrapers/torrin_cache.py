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

            response = await self.session.get(
                f"{self.url}/api/search?{urlencode(params)}",
                headers={"Authorization": f"Bearer {key}"},
            )
            data = await response.json()

            for result in data.get("results", []):
                info_hash = (result.get("info_hash") or "").lower()
                if not info_hash:
                    continue
                files = result.get("files") or []
                files = sorted(files, key=lambda f: f.get("size") or f.get("file_size") or 0, reverse=True)
                file_index = files[0].get("index") if files else None
                # Keep the actual selected filename and stable index. The backend's
                # episode_match carries canonical scope when release numbering differs.
                file_name = files[0].get("file_name") if files else ""
                title = file_name or result.get("name", "")
                media_info = files[0].get("media_info") if files else None
                torrents.append(
                    {
                        "title": title,
                        "release_name": result.get("name"),
                        "release_size": result.get("size"),
                        "infoHash": info_hash,
                        "fileIndex": file_index,
                        "episode_match": files[0].get("episode_match") if files else None,
                        "requested_sid": (
                            f"{request.media_only_id}:{request.season}:{request.episode}"
                            if request.media_type == "series" else None
                        ),
                        # Ground-truth ffprobe metadata (resolution/codec/hdr/audio) from
                        # Torrin. Overrides the unreliable filename parse for the label.
                        "media_info": media_info,
                        # Everything from /api/search is already cached in R2 = instant.
                        # Give it a high seeder count so it ranks well and survives
                        # Comet's top-N filter + dedup (otherwise seeders=None sinks it
                        # to the bottom and a non-cached duplicate from another tracker
                        # gets kept/checked instead, so the cached copy never shows ⚡).
                        "seeders": 1000,
                        "size": int((files[0].get("size") if files else None) or result.get("size") or 0),
                        "tracker": "Torrin",
                        "sources": [],
                    }
                )
        except Exception as e:
            logger.warning(
                f"Exception while getting torrents for {request.title} with Torrin cache: {e}"
            )

        return torrents
