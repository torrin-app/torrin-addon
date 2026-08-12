from urllib.parse import urlencode

from comet.core.logger import logger
from comet.core.models import settings
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest


def _res_from_height(h):
    if not h:
        return None
    try:
        h = int(h)
    except (TypeError, ValueError):
        return None
    if h >= 2000:
        return "2160p"
    if h >= 1000:
        return "1080p"
    if h >= 700:
        return "720p"
    if h >= 400:
        return "480p"
    return None


def _norm_codec(v):
    if not v:
        return None
    v = str(v).lower()
    if "265" in v or "hevc" in v:
        return "hevc"
    if "264" in v or "avc" in v:
        return "h264"
    if "av1" in v:
        return "av1"
    if "vp9" in v:
        return "vp9"
    return None


def _norm_hdr(v):
    if not v:
        return None
    v = str(v).lower()
    if "dv" in v or "dolby" in v:
        return "DV"
    if "hdr10" in v or v == "hdr":
        return "HDR10"
    if "hlg" in v:
        return "HLG"
    return None


def _norm_channels(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    return {"1.0": 1, "2.0": 2, "5.1": 6, "7.1": 8, "1": 1, "2": 2, "6": 6, "8": 8}.get(
        str(v).strip()
    )


def _media_info(t):
    """Map TorrentClaw's (flat) scan fields onto Torrin's media_info shape
    (utils.parsing.format_media_info_line / apply_media_info). TC has no bitrate
    or duration, so those stay absent — Torrin's own ffprobe fills them once cached."""
    vi = t.get("videoInfo") or {}
    mi = {}

    res = t.get("quality") or _res_from_height(vi.get("height"))
    if res:
        mi["resolution"] = res
    vc = _norm_codec(t.get("codec") or vi.get("codec"))
    if vc:
        mi["video_codec"] = vc
    hdr = _norm_hdr(t.get("hdrType") or vi.get("hdr"))
    if hdr:
        mi["hdr"] = hdr

    # TC has no bitrate field, but scanned releases carry videoInfo.duration, so
    # derive the overall average bitrate = size * 8 / duration (bits/sec).
    dur = vi.get("duration")
    if dur:
        try:
            dur = float(dur)
            if dur > 0:
                mi["duration_sec"] = dur
                sb = t.get("sizeBytes")
                if sb:
                    mi["bitrate"] = int(int(sb) * 8 / dur)
        except (TypeError, ValueError):
            pass

    audio = [
        {
            "codec": (a.get("codec") or "").lower(),
            "channels": _norm_channels(a.get("channels")),
            "language": a.get("lang") or a.get("language"),
        }
        for a in t.get("audioTracks") or []
    ]
    if not audio and t.get("audioCodec"):
        audio = [
            {
                "codec": (t.get("audioCodec") or "").lower(),
                "channels": _norm_channels(t.get("audioChannels")),
                "language": (t.get("languages") or [None])[0],
            }
        ]
    if audio:
        mi["audio"] = audio

    subs = [
        {"language": s.get("lang") or s.get("language")}
        for s in (t.get("subtitleTracks") or [])
        if s.get("lang") or s.get("language")
    ]
    if not subs:
        subs = [{"language": lang} for lang in (t.get("subtitleLanguages") or [])]
    if subs:
        mi["subtitles"] = subs

    return mi or None


def _iter_torrents(data):
    """/api/v1/search returns either a flat torrent list or content items with a
    nested `torrents` array. Handle both."""
    results = data.get("results") or data.get("data") or []
    for r in results:
        nested = r.get("torrents")
        if isinstance(nested, list) and nested:
            for t in nested:
                yield t
        else:
            yield r


class TorrentClawScraper(BaseScraper):
    """TorrentClaw as a content source, enriched with TrueSpec file analysis.

    Each result carries real resolution/codec/HDR/audio/subtitle metadata plus a
    0-100 qualityScore, so we can fill media_info for UNCACHED releases (where
    Torrin has no ffprobe data yet) and label/rank them accurately before download.

    Gated by SCRAPE_TORRENTCLAW + TORRENTCLAW_API_KEY (PRO tier). Uses the native
    /api/v1/search JSON, NOT torznab (torznab drops trueSpec).
    """

    def __init__(self, manager, session, url: str):
        super().__init__(manager, session, url)

    async def scrape(self, request: ScrapeRequest):
        torrents = []

        key = getattr(settings, "TORRENTCLAW_API_KEY", None)
        if not key:
            return torrents

        try:
            params = []
            if request.media_only_id:
                params.append(("imdbid", request.media_only_id))
            elif request.title:
                params.append(("q", request.title))
            else:
                return torrents

            if request.media_type == "series":
                params.append(("type", "show"))
                if request.season is not None:
                    params.append(("season", request.season))
                if request.episode is not None:
                    params.append(("episode", request.episode))
            else:
                params.append(("type", "movie"))
            params.append(("limit", 50))

            response = await self.session.get(
                f"{self.url}/api/v1/search?{urlencode(params)}",
                headers={"Authorization": f"Bearer {key}"},
            )
            # PRO cap is 1000/min, 10000/day. On a rate-limit (429) or any non-200,
            # skip quietly so labels just fall back to the filename parse; never error
            # or retry. Comet's scrape cache means we only reach here on a cache miss.
            if response.status != 200:
                if response.status == 429:
                    logger.debug("TorrentClaw rate limit hit (429), skipping enrichment")
                return torrents
            data = await response.json()

            for result in _iter_torrents(data):
                info_hash = (
                    result.get("infoHash") or result.get("info_hash") or ""
                ).lower()
                if not info_hash:
                    continue
                torrents.append(
                    {
                        "title": result.get("rawTitle") or result.get("title") or "",
                        "infoHash": info_hash,
                        "seeders": result.get("seeders"),
                        "size": int(result.get("sizeBytes") or result.get("size") or 0),
                        "tracker": "TorrentClaw",
                        "sources": [],
                        # Ground-truth scan metadata for uncached releases.
                        "media_info": _media_info(result),
                    }
                )
        except Exception as e:
            logger.warning(
                f"Exception while getting torrents for {request.title} with TorrentClaw: {e}"
            )

        return torrents
