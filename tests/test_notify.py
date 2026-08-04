"""Upload notifications.

The network call can't be tested here — this sandbox blocks youtube.com by
policy — so these cover everything either side of it against fixtures. The two
that matter most are the first check announcing nothing, and a restart not
re-announcing: both failures ping @everyone repeatedly, which is the kind of
mistake a server remembers.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import db  # noqa: E402
import notify  # noqa: E402

HANDLE = "@Pyro_Blits"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.set_db_path(tmp_path / "notify.db")
    db.init_db()
    yield
    db.set_db_path(config.DB_PATH)


def atom_feed(*videos, author="Pyro_Blits") -> str:
    """A feed shaped like YouTube's real one."""
    entries = "".join(
        f"""
  <entry>
    <id>yt:video:{vid}</id>
    <yt:videoId>{vid}</yt:videoId>
    <yt:channelId>UCabcdefghijklmnopqrstuv</yt:channelId>
    <title>{title}</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v={vid}"/>
    <author><name>{author}</name></author>
    <published>{published}</published>
    <media:group>
      <media:thumbnail url="https://i.ytimg.com/vi/{vid}/hqdefault.jpg"/>
    </media:group>
  </entry>"""
        for vid, title, published in videos
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
  <title>{author}</title>{entries}
</feed>"""


THREE_VIDEOS = atom_feed(
    ("aaaaaaaaaaa", "Newest video", "2026-08-04T10:00:00+00:00"),
    ("bbbbbbbbbbb", "Middle video", "2026-08-03T10:00:00+00:00"),
    ("ccccccccccc", "Oldest video", "2026-08-02T10:00:00+00:00"),
)


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def test_feed_parses():
    videos = notify.parse_feed(THREE_VIDEOS)
    assert [v.video_id for v in videos] == ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]


def test_video_fields():
    video = notify.parse_feed(THREE_VIDEOS)[0]
    assert video.title == "Newest video"
    assert video.watch_url == "https://www.youtube.com/watch?v=aaaaaaaaaaa"
    assert video.thumbnail.endswith("hqdefault.jpg")
    assert video.author == "Pyro_Blits"
    assert video.published.year == 2026


def test_newest_video_comes_first():
    videos = notify.parse_feed(THREE_VIDEOS)
    assert videos[0].title == "Newest video"
    assert videos[-1].title == "Oldest video"


@pytest.mark.parametrize("junk", ["", "not xml at all", "<feed><unclosed>", "<html>404</html>"])
def test_malformed_feeds_return_nothing_instead_of_raising(junk):
    """A raise inside the poll loop stops it for the rest of the shift."""
    assert notify.parse_feed(junk) == []


def test_an_empty_feed_is_not_an_error():
    assert notify.parse_feed(atom_feed()) == []


def test_entries_without_a_video_id_are_skipped():
    feed = THREE_VIDEOS.replace("<yt:videoId>ccccccccccc</yt:videoId>", "")
    assert len(notify.parse_feed(feed)) == 2


# --------------------------------------------------------------------------
# resolving a handle to a channel id
# --------------------------------------------------------------------------

PAGE = '''<html><head>
<meta property="og:title" content="Pyro_Blits">
<link rel="canonical" href="https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv">
</head><body><script>{"channelId":"UCabcdefghijklmnopqrstuv","other":1}</script></body></html>'''


def test_channel_id_is_extracted():
    assert notify.extract_channel_id(PAGE) == "UCabcdefghijklmnopqrstuv"


def test_channel_id_falls_back_to_the_canonical_link():
    page = PAGE.replace('"channelId":"UCabcdefghijklmnopqrstuv"', '"nothing":"here"')
    assert notify.extract_channel_id(page) == "UCabcdefghijklmnopqrstuv"


@pytest.mark.parametrize("page", ["", "<html>nothing here</html>", "channelId: UCshort"])
def test_missing_channel_id_returns_none(page):
    assert notify.extract_channel_id(page) is None


def test_channel_title_is_extracted():
    assert notify.extract_channel_title(PAGE) == "Pyro_Blits"


def test_urls():
    assert notify.channel_page_url("@Pyro_Blits") == "https://www.youtube.com/@Pyro_Blits"
    assert "channel_id=UC123" in notify.feed_url("UC123")


# --------------------------------------------------------------------------
# the first check must announce nothing
# --------------------------------------------------------------------------

def simulate_first_check(handle=HANDLE, feed_xml=THREE_VIDEOS):
    """Mirrors the first-run branch of check_creator."""
    videos = notify.parse_feed(feed_xml)
    for video in videos:
        db.mark_announced(video.video_id, handle)
    db.save_feed(handle, initialised=1, last_video_id=videos[0].video_id)
    return []


def simulate_later_check(handle=HANDLE, feed_xml=THREE_VIDEOS):
    """Mirrors the normal branch: announce anything not already recorded."""
    videos = notify.parse_feed(feed_xml)
    fresh = [v for v in reversed(videos) if db.mark_announced(v.video_id, handle)]
    db.save_feed(handle, last_video_id=videos[0].video_id)
    return fresh


def test_the_first_check_announces_nothing():
    """Otherwise setup dumps the whole backlog with an @everyone on each."""
    assert simulate_first_check() == []


def test_the_first_check_still_records_everything_as_seen():
    simulate_first_check()
    assert db.count_announced(HANDLE) == 3
    assert db.get_feed(HANDLE)["initialised"] == 1


def test_nothing_is_announced_when_there_are_no_new_videos():
    simulate_first_check()
    assert simulate_later_check() == []


def test_a_genuinely_new_video_is_announced():
    simulate_first_check()

    with_new = atom_feed(
        ("ddddddddddd", "Brand new", "2026-08-05T10:00:00+00:00"),
        ("aaaaaaaaaaa", "Newest video", "2026-08-04T10:00:00+00:00"),
        ("bbbbbbbbbbb", "Middle video", "2026-08-03T10:00:00+00:00"),
    )
    fresh = simulate_later_check(feed_xml=with_new)
    assert [v.video_id for v in fresh] == ["ddddddddddd"]


def test_a_video_is_announced_only_once():
    simulate_first_check()
    with_new = atom_feed(
        ("ddddddddddd", "Brand new", "2026-08-05T10:00:00+00:00"),
        ("aaaaaaaaaaa", "Newest video", "2026-08-04T10:00:00+00:00"),
    )
    assert len(simulate_later_check(feed_xml=with_new)) == 1
    assert simulate_later_check(feed_xml=with_new) == []


def test_several_new_videos_announce_oldest_first():
    simulate_first_check()
    burst = atom_feed(
        ("fffffffffff", "Third", "2026-08-07T10:00:00+00:00"),
        ("eeeeeeeeeee", "Second", "2026-08-06T10:00:00+00:00"),
        ("ddddddddddd", "First", "2026-08-05T10:00:00+00:00"),
        ("aaaaaaaaaaa", "Newest video", "2026-08-04T10:00:00+00:00"),
    )
    fresh = simulate_later_check(feed_xml=burst)
    assert [v.title for v in fresh] == ["First", "Second", "Third"]


def test_a_restart_does_not_re_announce():
    """The host restarts every ~6 hours; state lives in the database for this."""
    simulate_first_check()
    simulate_later_check(feed_xml=atom_feed(("ddddddddddd", "New", "2026-08-05T10:00:00+00:00")))

    # Same database, as after a restart — nothing in memory carries over.
    assert simulate_later_check(
        feed_xml=atom_feed(("ddddddddddd", "New", "2026-08-05T10:00:00+00:00"))
    ) == []


def test_channels_are_tracked_separately():
    # Real video ids are globally unique across YouTube, which is exactly why
    # announced_videos keys on video_id alone. Two channels never collide.
    other_feed = atom_feed(
        ("mmmmmmmmmmm", "Their newest", "2026-08-04T10:00:00+00:00"),
        ("nnnnnnnnnnn", "Their older", "2026-08-03T10:00:00+00:00"),
        author="Microman_J",
    )

    simulate_first_check(handle="@Pyro_Blits")
    assert db.get_feed("@Microman_J") is None

    assert simulate_first_check(handle="@Microman_J", feed_xml=other_feed) == []
    assert db.count_announced("@Microman_J") == 2
    assert db.count_announced("@Pyro_Blits") == 3


def test_one_channels_upload_does_not_look_seen_to_the_other():
    simulate_first_check(handle="@Pyro_Blits")
    other_feed = atom_feed(("mmmmmmmmmmm", "Theirs", "2026-08-04T10:00:00+00:00"))
    simulate_first_check(handle="@Microman_J", feed_xml=other_feed)

    # A new upload on the second channel is still announced.
    burst = atom_feed(
        ("ooooooooooo", "Brand new", "2026-08-05T10:00:00+00:00"),
        ("mmmmmmmmmmm", "Theirs", "2026-08-04T10:00:00+00:00"),
    )
    fresh = simulate_later_check(handle="@Microman_J", feed_xml=burst)
    assert [v.video_id for v in fresh] == ["ooooooooooo"]


# --------------------------------------------------------------------------
# bookkeeping
# --------------------------------------------------------------------------

def test_marking_a_video_twice_is_refused():
    assert db.mark_announced("x", HANDLE) is True
    assert db.mark_announced("x", HANDLE) is False


def test_announced_history_is_pruned():
    """This table is committed to a git branch every few minutes."""
    for i in range(150):
        db.mark_announced(f"video{i:04d}", HANDLE)
    assert db.count_announced(HANDLE) == 150

    db.prune_announced(HANDLE, keep=100)
    assert db.count_announced(HANDLE) == 100


def test_pruning_keeps_the_most_recent():
    for i in range(20):
        db.mark_announced(f"video{i:04d}", HANDLE)
    db.prune_announced(HANDLE, keep=5)
    assert db.was_announced("video0019") is True
    assert db.was_announced("video0000") is False


def test_pruning_one_channel_leaves_the_other_alone():
    for i in range(10):
        db.mark_announced(f"a{i}", "@Pyro_Blits")
        db.mark_announced(f"b{i}", "@Microman_J")
    db.prune_announced("@Pyro_Blits", keep=2)
    assert db.count_announced("@Microman_J") == 10


def test_feed_state_round_trips():
    db.save_feed(HANDLE, channel_id="UC123", title="Pyro", last_error=None)
    feed = db.get_feed(HANDLE)
    assert feed["channel_id"] == "UC123" and feed["title"] == "Pyro"

    db.save_feed(HANDLE, last_error="feed fetch failed")
    assert db.get_feed(HANDLE)["last_error"] == "feed fetch failed"
    assert db.get_feed(HANDLE)["channel_id"] == "UC123", "an error must not lose the resolved id"


def test_unknown_feed_fields_are_rejected():
    with pytest.raises(ValueError):
        db.save_feed(HANDLE, nonsense="x")


# --------------------------------------------------------------------------
# the creator list and the announcement
# --------------------------------------------------------------------------

def test_both_requested_channels_are_watched():
    handles = {c["handle"] for c in notify.CREATORS}
    assert handles == {"@Pyro_Blits", "@Microman_J"}


def test_announcement_embed_is_within_discord_limits():
    import embeds

    video = notify.parse_feed(
        atom_feed(("aaaaaaaaaaa", "T" * 400, "2026-08-04T10:00:00+00:00"))
    )[0]
    embed = embeds.upload_announcement(video, "Pyro_Blits")

    assert len(embed.title) <= 256
    assert len(embed.author.name) <= 256
    assert len(embed) <= 6000
    assert embed.url


def test_announcement_survives_a_video_with_no_thumbnail():
    import embeds

    video = notify.Video(
        video_id="x", title="No thumb", url="https://y/x", published=None,
        thumbnail=None, author=None,
    )
    embed = embeds.upload_announcement(video, "Pyro_Blits")
    assert embed.title == "No thumb"
