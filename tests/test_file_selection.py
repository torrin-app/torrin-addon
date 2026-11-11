import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from RTN import parse
from comet.debrid.stremthru import StremThru
from comet.services.debrid import DebridService
from comet.services.filtering import filter_worker
from comet.debrid.exceptions import DebridLinkGenerationError
from comet.utils.parsing import is_video
from comet.utils.file_selection import (
    apply_backend_episode_match,
    file_behavior_hints,
    format_selected_size,
    has_stream_route,
    with_release_description,
    with_storage_description,
)


class FileSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = StremThru(None, "tt123:2:8", "tt123", "realdebrid:test", "127.0.0.1")
        self.client.check_premium = AsyncMock()
        self.patches = [
            patch("comet.debrid.stremthru.get_executor", return_value=None),
            patch("comet.debrid.stremthru.torrent_update_queue.add_torrent_info", new_callable=AsyncMock),
            patch("comet.debrid.stremthru.EpisodeIndexService.get_target_air_date", new_callable=AsyncMock, return_value=None),
            patch("comet.debrid.stremthru.cache_availability", new_callable=AsyncMock),
            patch("comet.services.debrid.cache_availability", new_callable=AsyncMock),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    async def availability(self, items):
        self.client.get_instant = AsyncMock(return_value={"data": {"items": items}})
        return await self.client.get_availability([i["hash"] for i in items], {}, {}, {})

    def file(self, name="Show.S02E04.Combined.Stories.mkv", index=3, size=200, match="tt123:2:8"):
        return dict(name=name, index=index, size=size, episode_match=match, link=f"https://example.invalid/file/{index}")

    async def test_canonical_match_survives_availability_and_playback(self):
        file = self.file()
        result = await self.availability([dict(hash="a", status="cached", files=[file])])
        self.assertEqual([(f["index"], f["episode"]) for f in result], [(3, 8)])
        self.assertEqual(result[0]["title"], file["name"])
        self.client._post_store_json = AsyncMock(side_effect=[
            {"data": {"status": "downloaded", "files": [file]}},
            {"data": {"link": file["link"]}},
        ])
        link = await self.client.generate_download_link("a", "0", "Show", "Show S02", 2, 8)
        self.assertEqual(link, file["link"])
        await asyncio.sleep(0)

    async def test_foreign_scope_does_not_bypass_matching(self):
        result = await self.availability([dict(hash="a", status="cached", files=[self.file(match="tt123:2:7")])])
        self.assertEqual(result, [])

    async def test_cached_without_files_is_not_ready(self):
        self.assertEqual(await self.availability([dict(hash="a", status="cached", files=[])]), [])

    async def test_mixed_tiers_keep_parsed_files_aligned(self):
        result = await self.availability([
            dict(hash="a", status="acceleratable", files=[self.file("Show.S02E08.mkv", match=None)]),
            dict(hash="b", status="cached", files=[self.file("Show.S02E09.mkv", match=None)]),
        ])
        self.assertEqual([(f["info_hash"], f["cache_tier"]) for f in result], [("a", "acceleratable")])

    async def test_multi_episode_filename_without_backend_extension(self):
        result = await self.availability([dict(hash="a", status="cached", files=[self.file("Show.S02E07-E09.mkv", match=None)])])
        self.assertEqual(len(result), 1)

    async def test_episode_selection_replaces_pack_metadata(self):
        incoming = dict(info_hash="a", index=3, title="Show.S02E08.mkv", size=200,
                        season=2, episode=8, parsed=parse("Show.S02E08.mkv"), cache_tier="cached")
        torrents = {"a": dict(size=9000, fileIndex=0, title="Show.S02", parsed=parse("Show.S02"))}
        service = DebridService("realdebrid", "test", "127.0.0.1")
        with patch("comet.services.debrid.retrieve_debrid_availability", new_callable=AsyncMock, return_value=[incoming]):
            ready = await service.get_and_cache_availability(None, ["a"], {}, {}, {}, torrents, "tt123:2:8", "tt123", 2, 8)
        self.assertEqual(ready, {"a"})
        self.assertEqual((torrents["a"]["fileIndex"], torrents["a"]["size"], torrents["a"]["title"]), (3, 200, incoming["title"]))
        await asyncio.sleep(0)

    async def test_providers_keep_separate_cache_tiers(self):
        torrents = {"a": dict(size=9000, parsed=parse("Show.S02"))}
        for provider, tier in (("realdebrid", "cached"), ("alldebrid", "acceleratable")):
            incoming = dict(info_hash="a", index=3, title="Show.S02E08.mkv", size=200,
                            season=2, episode=8, parsed=parse("Show.S02E08.mkv"), cache_tier=tier)
            with patch("comet.services.debrid.retrieve_debrid_availability", new_callable=AsyncMock, return_value=[incoming]):
                await DebridService(provider, "test", "").get_and_cache_availability(
                    None, ["a"], {}, {}, {}, torrents, "tt123:2:8", "tt123", 2, 8
                )
        self.assertEqual(torrents["a"]["cache_tiers"], {"realdebrid": "cached", "alldebrid": "acceleratable"})
        self.assertEqual(torrents["a"]["cache_tier"], "cached")
        await asyncio.sleep(0)

    async def test_movie_uses_largest_video_over_stale_extra_index(self):
        self.client.sid = "tt123"
        main = self.file("Movie.2024.mkv", 4, 9000, None)
        extra = self.file("Movie.2024.Extras.mkv", 0, 200, None)
        self.client._post_store_json = AsyncMock(side_effect=[
            {"data": {"status": "downloaded", "files": [extra, main]}},
            {"data": {"link": main["link"]}},
        ])
        await self.client.generate_download_link("a", "0", "Movie", extra["name"], None, None)
        self.assertEqual(self.client._post_store_json.call_args_list[1].args[1], {"link": main["link"]})

    async def test_unmatched_episode_never_falls_back_to_largest(self):
        self.client._post_store_json = AsyncMock(return_value={
            "data": {"status": "downloaded", "files": [self.file(match=None)]}
        })
        with self.assertRaises(DebridLinkGenerationError):
            await self.client.generate_download_link("a", "3", "Show", "Show S02", 2, 8)
        self.assertEqual(self.client._post_store_json.await_count, 1)

    def test_discovery_keeps_backend_canonical_episode(self):
        torrent = dict(title="Show.S02E04.Combined.Stories.mkv", tracker="Torrin",
                       episode_match="tt123:2:8", requested_sid="tt123:2:8")
        result = filter_worker([torrent], "Show", None, None, "series", {}, False)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["parsed"].episodes, [8])

    def test_video_extensions_are_case_insensitive(self):
        for name in ("movie.MKV", "show.TS", "movie.m2ts"):
            self.assertTrue(is_video(name))
        self.assertFalse(is_video("show.srt"))

    def test_file_hints_use_basename_and_positive_file_size(self):
        hints = file_behavior_hints("Season 2/Show.S02E08.MKV", 200, "torrin|hash")
        self.assertEqual(hints, dict(filename="Show.S02E08.MKV", videoSize=200,
                                     bingeGroup="torrin|hash", notWebReady=True))
        self.assertFalse(file_behavior_hints("Movie.MP4", 100)["notWebReady"])
        self.assertNotIn("videoSize", file_behavior_hints("movie.mkv", 0))
        self.assertNotIn("filename", file_behavior_hints("Show Season 2", 9000))

    def test_storage_label_is_separate_from_discovery_source(self):
        description = "Show.S02E08.mkv\nSource: HDEncode"
        self.assertEqual(with_storage_description(description, "cairn"), description + "\nCairn")
        self.assertEqual(with_storage_description(description, "cache"), description + "\nStorage")
        self.assertEqual(with_storage_description(description, None), description)
        self.assertEqual(with_storage_description(description, "unknown"), description)

    def test_match_does_not_mutate_cached_parse(self):
        original = parse("Show.S02E04.mkv")
        scoped = apply_backend_episode_match(original, self.file(), self.client.sid)
        self.assertEqual(original.episodes, [4])
        self.assertEqual(scoped.episodes, [8])

    async def test_rejection_requires_exact_episode_scope_and_no_upstream(self):
        base = dict(hash="a", status="unknown", files=[], episode_status="no_match",
                    episode_sid="tt123:2:8", episode_scope="available_files")
        for changes, rejected in (({}, True), ({"episode_sid": "tt123:2:7"}, False),
                                  ({"episode_scope": None}, False),
                                  ({"episode_status": "unknown"}, False),
                                  ({"status": "acceleratable"}, False)):
            with self.subTest(changes=changes):
                item = base | changes
                self.client.get_instant = AsyncMock(return_value={"data": {"items": [item]}})
                statuses = {}
                await self.client.get_availability(["a"], {}, {}, {}, episode_statuses=statuses)
                self.assertEqual(statuses, {"a": "no_match"} if rejected else {})

    async def test_episode_rejection_is_fresh_provider_scoped_and_not_cached(self):
        torrents = {"a": dict(title="Show S02", parsed=parse("Show S02"))}
        service = DebridService("realdebrid", "test", "")
        async def reject(*args, **kwargs):
            kwargs["episode_statuses"]["a"] = "no_match"
            return []
        with patch("comet.services.debrid.retrieve_debrid_availability", side_effect=reject):
            ready = await service.get_and_cache_availability(
                None, ["a"], {}, {}, {}, torrents, "tt123:2:8", "tt123", 2, 8
            )
        self.assertEqual(ready, set())
        self.assertEqual(torrents["a"]["episode_statuses"], {"realdebrid": "no_match"})
        self.assertNotIn("cache_tiers", torrents["a"])
        incoming = dict(info_hash="a", index=3, title="Show.S02E08.mkv", size=200,
                        season=2, episode=8, parsed=parse("Show.S02E08.mkv"), cache_tier="cached")
        with patch("comet.services.debrid.retrieve_debrid_availability", return_value=[incoming]):
            ready = await DebridService("alldebrid", "test", "").get_and_cache_availability(
                None, ["a"], {}, {}, {}, torrents, "tt123:2:8", "tt123", 2, 8
            )
        self.assertEqual(ready, {"a"})
        self.assertEqual(torrents["a"]["episode_statuses"], {"realdebrid": "no_match"})
        with patch("comet.services.debrid.retrieve_debrid_availability", return_value=[]):
            await service.get_and_cache_availability(
                None, ["a"], {}, {}, {}, torrents, "tt123:2:9", "tt123", 2, 9
            )
        self.assertEqual(torrents["a"]["episode_statuses"], {})
        await asyncio.sleep(0)

    def test_release_description_preserves_real_names(self):
        name = "Show.S02.COMPLETE.1080p.WEB-DL-GROUP"
        filename = "Show.S02E07E08.mkv"
        description = with_release_description(filename, name, filename, "tt123:2:8")
        self.assertEqual(description, filename + "\nRelease: " + name + "\nSelected: S02E08")
        self.assertEqual(with_release_description(filename, filename, filename), filename)
        self.assertEqual(with_release_description("Movie.mkv", None, "Movie.mkv"), "Movie.mkv")

    async def test_selected_filenames_and_release_names_are_provider_scoped(self):
        original = "Show.S01-S03.COMPLETE.1080p"
        torrents = {"a": dict(title=original, size=9000, parsed=parse(original))}
        for service, filename, size, index in (
            ("realdebrid", "Show.S02E07E08.mkv", 200, 3),
            ("alldebrid", "Show.S02E08.mp4", 100, 7),
        ):
            self.client.store_name = service
            self.client.get_instant = AsyncMock(return_value={"data": {"items": [
                dict(hash="a", name=original, status="cached", files=[self.file(filename, index, size)])
            ]}})
            incoming = await self.client.get_availability(["a"], {}, {}, {})
            with patch("comet.services.debrid.retrieve_debrid_availability", return_value=incoming):
                await DebridService(service, "test", "").get_and_cache_availability(
                    None, ["a"], {}, {}, {}, torrents, "tt123:2:8", "tt123", 2, 8
                )
        selections = torrents["a"]["selected_files"]
        self.assertEqual(selections["realdebrid"]["title"], "Show.S02E07E08.mkv")
        self.assertEqual(selections["realdebrid"]["size"], 200)
        self.assertEqual(selections["realdebrid"]["fileIndex"], 3)
        self.assertEqual(selections["alldebrid"]["title"], "Show.S02E08.mp4")
        for selection in selections.values():
            self.assertEqual(selection["release_name"], original)
            self.assertEqual(selection["episode_match"], "tt123:2:8")
        await asyncio.sleep(0)

    def test_file_and_release_sizes_are_display_only(self):
        size = 1024 ** 3
        self.assertEqual(format_selected_size(size, 20 * size),
                         "💾 1.0 GB / 20.0 GB (file / release)")
        for total in (None, 0, size, size // 2):
            self.assertEqual(format_selected_size(size, total), "💾 1.0 GB")
        hints = file_behavior_hints("Show.S02E08.mkv", size)
        self.assertEqual(hints["videoSize"], size)
        self.assertEqual(format_selected_size(size, 20 * size, plain=True),
                         "Size: 1.0 GB / 20.0 GB (file / release)")

    def test_route_filter_preserves_unknown_and_other_provider_choices(self):
        rejected = {"episode_statuses": {"realdebrid": "no_match"}}
        self.assertFalse(has_stream_route(rejected, ["realdebrid"], {}, {}, False, False))
        self.assertTrue(has_stream_route(rejected, ["realdebrid", "alldebrid"], {}, {}, False, False))
        self.assertTrue(has_stream_route(rejected, ["realdebrid"], {}, {}, True, True))
        self.assertTrue(has_stream_route({}, ["realdebrid"], {}, {}, False, False))
        self.assertFalse(has_stream_route({}, ["realdebrid"], {}, {}, True, False))
        self.assertFalse(has_stream_route({}, ["realdebrid"], {"realdebrid": True}, {"realdebrid": "error"}, True, False))

    def test_rejected_candidate_does_not_consume_resolution_limit(self):
        from comet.api.endpoints.stream import _select_info_hashes_by_resolution

        torrents = {
            "rejected": dict(parsed=parse("Show.S02.1080p"), episode_statuses={"realdebrid": "no_match"}),
            "unknown": dict(parsed=parse("Show.S02.1080p")),
        }
        eligible = [
            key for key in torrents
            if has_stream_route(torrents[key], ["realdebrid"], {}, {}, False, False)
        ]
        selected = _select_info_hashes_by_resolution(eligible, torrents, {}, 1, False, True)
        self.assertEqual(selected, ["unknown"])


if __name__ == "__main__":
    unittest.main()
