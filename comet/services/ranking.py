from RTN import Torrent, check_fetch, get_rank, sort_torrents

# Torrin-backed sources (guaranteed-cached scene encodes) bypass trash filtering
# and the per-resolution cap so they always surface. Real torrent trash is still
# hidden by removeTrash; disable the scraper to hide these entirely.
RELEASE_SOURCE_TRACKERS = {"HDEncode", "Scene-RLS"}


def rank_worker(
    torrents,
    rtn_settings,
    rtn_ranking,
    max_results_per_resolution,
    max_size,
    remove_trash,
):
    ranked_torrents = set()
    release_torrents = set()
    for info_hash, torrent in torrents.items():
        if max_size != 0:
            torrent_size = torrent["size"]
            if torrent_size is not None and torrent_size > max_size:
                continue

        parsed = torrent["parsed"]
        raw_title = torrent["title"]

        is_fetchable, failed_keys = check_fetch(parsed, rtn_settings)
        rank = get_rank(parsed, rtn_settings, rtn_ranking)

        is_release = torrent.get("tracker") in RELEASE_SOURCE_TRACKERS

        if remove_trash and not is_release:
            if not is_fetchable or rank < rtn_settings.options["remove_ranks_under"]:
                continue

        try:
            ranked = Torrent(
                infohash=info_hash,
                raw_title=raw_title,
                data=parsed,
                fetch=is_fetchable,
                rank=rank,
                lev_ratio=0.0,
            )
        except Exception:
            continue

        if is_release:
            release_torrents.add(ranked)
        else:
            ranked_torrents.add(ranked)

    result = sort_torrents(ranked_torrents, max_results_per_resolution)
    result.update(sort_torrents(release_torrents))
    return result
