"""Consume request-scoped file matches from the authenticated Torrin API."""

import re


def apply_backend_episode_match(parsed, file, sid):
    """Preserve the real filename while applying the backend's canonical scope.

    A match for another request must never bypass ordinary filename matching.
    Older servers/providers without this field keep their existing behavior.
    """
    if not sid or file.get("episode_match") != sid:
        return parsed
    match = re.fullmatch(r"tt\d+:(\d+):([1-9]\d*)", sid)
    if match is None:
        return parsed
    result = parsed.model_copy(deep=True)
    result.seasons = [int(match[1])]
    result.episodes = [int(match[2])]
    return result


def file_behavior_hints(filename, size=None, binge_group=None):
    from comet.utils.parsing import is_video

    basename = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    hints = {"notWebReady": not basename.lower().endswith(".mp4")}
    if is_video(basename):
        hints["filename"] = basename
        if isinstance(size, (int, float)) and size > 0:
            hints["videoSize"] = size
    if binge_group:
        hints["bingeGroup"] = binge_group
    return hints


def with_storage_description(description, source):
    label = {"cairn": "Cairn", "cache": "Storage"}.get(source)
    if label is None:
        return description
    return f"{description}\n{label}" if description else f"{label}"


def with_release_description(description, release_name, filename, episode_match=None):
    """Keep the original release and the selected file as separate details."""
    lines = [description] if description else []
    if release_name and release_name != filename:
        lines.append(f"Release: {release_name}")
    match = re.fullmatch(r"tt\d+:(\d+):([1-9]\d*)", episode_match or "")
    if match:
        lines.append(f"Selected: S{int(match[1]):02d}E{int(match[2]):02d}")
    return "\n".join(lines)


def format_selected_size(file_size, release_size=None, plain=False):
    """The release total is display-only; player hints always use file_size."""
    from comet.utils.formatting import format_bytes

    if not isinstance(file_size, (int, float)) or file_size <= 0:
        return None
    value = format_bytes(file_size)
    if isinstance(release_size, (int, float)) and release_size > file_size:
        value += f" / {format_bytes(release_size)} (file / release)"
    return f"Size: {value}" if plain else f"💾 {value}"


def has_stream_route(torrent, services, cache_status, errors, cached_only, enable_torrent):
    """Apply route eligibility before a candidate consumes a result-limit slot."""
    if enable_torrent:
        return True
    rejected = torrent.get("episode_statuses", {})
    return any(
        service not in errors
        and rejected.get(service) != "no_match"
        and (not cached_only or cache_status.get(service, False))
        for service in services
    )
