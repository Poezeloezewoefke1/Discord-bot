"""Watching YouTube channels for new uploads.

No Discord and no network in this module — just the creator list, the feed
parser and the ID extraction, so all of it can be tested offline. The cog does
the fetching and the posting.

YouTube publishes a public Atom feed per channel, which needs no API key and no
scraping:

    https://www.youtube.com/feeds/videos.xml?channel_id=UC...
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from xml.etree import ElementTree

# Hardcoded, as asked. Handles rather than channel IDs: the id is resolved once
# at runtime and cached, so renaming a channel doesn't need a code change.
CREATORS = (
    {"handle": "@Pyro_Blits", "name": "Pyro_Blits"},
    {"handle": "@Microman_J", "name": "Microman_J"},
)

CHANNEL_PAGE = "https://www.youtube.com/{handle}"
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
PLAYLIST_FEED_URL = "https://www.youtube.com/feeds/videos.xml?playlist_id={playlist_id}"

# How often the loop runs. 5 minutes is ~288 requests a day per channel, which a
# public feed tolerates comfortably.
POLL_MINUTES = 5

ATOM = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

# YouTube channel ids are always UC + 22 url-safe characters.
CHANNEL_ID_RE = re.compile(r'"(?:channelId|externalId)"\s*:\s*"(UC[\w-]{22})"')
CANONICAL_RE = re.compile(r'/channel/(UC[\w-]{22})')


@dataclass(frozen=True)
class Video:
    video_id: str
    title: str
    url: str
    published: datetime | None
    thumbnail: str | None
    author: str | None

    @property
    def watch_url(self) -> str:
        return self.url or f"https://www.youtube.com/watch?v={self.video_id}"


def channel_page_url(handle: str) -> str:
    return CHANNEL_PAGE.format(handle=handle.lstrip("/"))


def feed_url(channel_id: str) -> str:
    return FEED_URL.format(channel_id=channel_id)


def uploads_playlist_id(channel_id: str) -> str:
    """UC… -> UU…, the channel's "all uploads" playlist.

    Every channel has one, and its id is the channel id with the UC prefix
    swapped for UU.
    """
    if not channel_id.startswith("UC"):
        return channel_id
    return "UU" + channel_id[2:]


def playlist_feed_url(channel_id: str) -> str:
    return PLAYLIST_FEED_URL.format(playlist_id=uploads_playlist_id(channel_id))


# Sources to try, in order. The uploads-playlist feed comes first because the
# plain channel feed leaves Shorts out — a Shorts-only channel looks completely
# empty through it, which is exactly what happened to @Pyro_Blits.
FEED_SOURCES = (
    ("uploads playlist", playlist_feed_url),
    ("channel feed", feed_url),
)


def feed_candidates(channel_id: str) -> list[tuple[str, str]]:
    return [(name, build(channel_id)) for name, build in FEED_SOURCES]


def extract_channel_id(page_html: str) -> str | None:
    """Pull the UC... id out of a channel page.

    A handle isn't usable with the feed endpoint, and there's no public
    handle→id lookup that doesn't need an API key, so it comes from the page.
    Returns None rather than raising: this runs inside a polling loop, and an
    exception there stops the loop for the rest of the shift.
    """
    if not page_html:
        return None
    match = CHANNEL_ID_RE.search(page_html) or CANONICAL_RE.search(page_html)
    return match.group(1) if match else None


def extract_channel_title(page_html: str) -> str | None:
    match = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', page_html or "")
    return match.group(1) if match else None


def _parse_published(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_feed(xml_text: str) -> list[Video]:
    """Newest first. Returns [] on anything malformed rather than raising."""
    if not xml_text:
        return []
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []

    videos = []
    for entry in root.findall("atom:entry", ATOM):
        video_id = entry.findtext("yt:videoId", namespaces=ATOM)
        if not video_id:
            continue

        link = entry.find("atom:link[@rel='alternate']", ATOM)
        url = link.get("href") if link is not None else ""

        group = entry.find("media:group", ATOM)
        thumbnail = None
        if group is not None:
            thumb = group.find("media:thumbnail", ATOM)
            if thumb is not None:
                thumbnail = thumb.get("url")

        author = entry.findtext("atom:author/atom:name", namespaces=ATOM)

        videos.append(
            Video(
                video_id=video_id,
                title=(entry.findtext("atom:title", namespaces=ATOM) or "New video").strip(),
                url=url or f"https://www.youtube.com/watch?v={video_id}",
                published=_parse_published(entry.findtext("atom:published", namespaces=ATOM)),
                thumbnail=thumbnail,
                author=author,
            )
        )

    videos.sort(key=lambda v: (v.published is not None, v.published), reverse=True)
    return videos


def new_videos(videos, already_announced) -> list[Video]:
    """Videos not yet announced, oldest first so a burst posts in order."""
    fresh = [v for v in videos if v.video_id not in already_announced]
    return list(reversed(fresh))
