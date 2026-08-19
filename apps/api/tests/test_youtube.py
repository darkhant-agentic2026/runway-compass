"""ISO-8601 duration parsing and the video-budget filter.

docs/08-testing.md#unit, and docs/00-overview.md decision 6: "video duration is computed,
not guessed." The whole reason this module exists is that a model asked for "a video under
15 minutes" will confidently return a 47-minute one — the duration is not in the text it is
reading — so the filter has to be arithmetic on a number the API gave us.

The API is a `MockTransport`, not a live call. docs/08-testing.md: "Web-search and YouTube
calls are recorded fixtures (VCR-style) in all automated tests; a `--live` flag hits the
real APIs, used manually and nightly." The payloads below are shaped exactly as
`search.list` and `videos.list` return them, including the two-call structure that exists
because `search.list` does not carry `contentDetails`.
"""

from __future__ import annotations

import httpx
import pytest

from coach.integrations.youtube import (
    MAX_CANDIDATES,
    VideoCandidate,
    YouTubeClient,
    YouTubeUnavailable,
    parse_iso_duration,
    rank,
)


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("PT12M3S", 723),
        ("PT1H2M3S", 3723),
        ("PT45S", 45),
        ("PT2H", 7200),
        ("P1DT1H", 90_000),
        ("PT0S", 0),
        ("P0D", 0),
    ],
)
def test_iso_durations_parse(value: str, seconds: int) -> None:
    assert parse_iso_duration(value) == seconds


@pytest.mark.parametrize("value", ["", "12M3S", "PTXYZ", "nonsense"])
def test_a_duration_that_is_not_one_is_none_rather_than_zero(value: str) -> None:
    """`None`, never `0`. Zero would fit every budget and rank first — a live stream is
    exactly the video that must not be recommended for a 15-minute task."""
    assert parse_iso_duration(value) is None


def _candidate(**overrides) -> VideoCandidate:
    base = {
        "video_id": "v1",
        "title": "A talk",
        "channel": "A channel",
        "published_at": "2026-01-01T00:00:00Z",
        "duration_iso": "PT10M",
        "minutes": 10,
        "views": 10_000,
        "likes": 500,
    }
    return VideoCandidate(**{**base, **overrides})


def test_videos_over_the_budget_are_dropped() -> None:
    candidates = [
        _candidate(video_id="fits", minutes=12),
        _candidate(video_id="exactly", minutes=15),
        _candidate(video_id="over", minutes=16),
    ]
    assert [c.video_id for c in rank(candidates, max_minutes=15)] == ["fits", "exactly"]


def test_a_zero_minute_video_is_dropped_rather_than_ranked_first() -> None:
    """`0 < c.minutes` rather than `c.minutes <= max`. A live stream reports `P0D`."""
    assert rank([_candidate(minutes=0)], max_minutes=45) == []


def test_ranking_prefers_engagement_then_recency() -> None:
    loved = _candidate(video_id="loved", views=1_000, likes=200)
    ignored = _candidate(video_id="ignored", views=1_000_000, likes=100)
    assert [c.video_id for c in rank([ignored, loved], max_minutes=45)] == [
        "loved",
        "ignored",
    ]


def test_a_video_with_too_few_views_does_not_score_a_perfect_ratio() -> None:
    """Three views and three likes is not the best material on the internet."""
    tiny = _candidate(video_id="tiny", views=3, likes=3)
    real = _candidate(video_id="real", views=50_000, likes=2_000)
    assert next(c.video_id for c in rank([tiny, real], max_minutes=45)) == "real"


def test_at_most_eight_candidates_come_back() -> None:
    """docs/03-agent-design.md: "Returns ≤ 8 candidates with exact minutes"."""
    many = [_candidate(video_id=f"v{i}", views=1000 + i, likes=i) for i in range(30)]
    assert len(rank(many, max_minutes=45)) == MAX_CANDIDATES


# --- against the API shape ----------------------------------------------------------------


def _api(calls: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": {"videoId": "short"}},
                        {"id": {"videoId": "long"}},
                        {"id": {"videoId": "live"}},
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "short",
                        "snippet": {
                            "title": "Nurseries explained",
                            "channelTitle": "Async Weekly",
                            "publishedAt": "2026-02-01T00:00:00Z",
                        },
                        "contentDetails": {"duration": "PT12M30S"},
                        "statistics": {"viewCount": "50000", "likeCount": "3000"},
                    },
                    {
                        "id": "long",
                        "snippet": {
                            "title": "The three-hour deep dive",
                            "channelTitle": "Async Weekly",
                            "publishedAt": "2026-02-02T00:00:00Z",
                        },
                        "contentDetails": {"duration": "PT3H1M"},
                        "statistics": {"viewCount": "90000", "likeCount": "8000"},
                    },
                    {
                        "id": "live",
                        "snippet": {
                            "title": "Live right now",
                            "channelTitle": "Async Weekly",
                            "publishedAt": "2026-02-03T00:00:00Z",
                        },
                        "contentDetails": {"duration": "P0D"},
                        "statistics": {"viewCount": "10", "likeCount": "1"},
                    },
                ]
            },
        )

    return httpx.MockTransport(handler)


async def test_the_two_call_shape_returns_only_videos_that_fit() -> None:
    """`search.list` → `videos.list(part=contentDetails)`, then filter.

    Two calls, asserted as two calls: `search.list` does not return durations, so a
    "simplification" to one would leave the filter with nothing to filter on and the
    guarantee in docs/00-overview.md decision 6 silently gone.
    """
    calls: list[str] = []
    async with httpx.AsyncClient(transport=_api(calls)) as http:
        client = YouTubeClient("a-key", client=http)
        videos = await client.find_by_duration("structured concurrency", max_minutes=15)

    assert [path.rsplit("/", 1)[-1] for path in calls] == ["search", "videos"]
    assert [v.video_id for v in videos] == ["short"]
    assert videos[0].minutes == 13  # 12 m 30 s, rounded *up*
    assert videos[0].channel == "Async Weekly"


async def test_a_second_identical_query_is_served_from_the_cache() -> None:
    """`search.list` costs 100 quota units against a 10,000/day default, so an uncached
    research path tops out at 100 queries a day."""
    calls: list[str] = []
    async with httpx.AsyncClient(transport=_api(calls)) as http:
        client = YouTubeClient("a-key", client=http)
        await client.find_by_duration("structured concurrency", max_minutes=15)
        await client.find_by_duration("Structured Concurrency ", max_minutes=15)

    assert len(calls) == 2  # not four


async def test_a_different_budget_is_a_different_cache_entry() -> None:
    calls: list[str] = []
    async with httpx.AsyncClient(transport=_api(calls)) as http:
        client = YouTubeClient("a-key", client=http)
        under = await client.find_by_duration("q", max_minutes=15)
        over = await client.find_by_duration("q", max_minutes=200)

    assert len(calls) == 4
    assert [v.video_id for v in under] == ["short"]
    assert {v.video_id for v in over} == {"short", "long"}


async def test_no_api_key_is_a_clean_refusal_rather_than_a_crash() -> None:
    """A project with videos enabled and no key degrades to a report with no videos, not
    to a failed research run — which is why the tool answers rather than raises."""
    client = YouTubeClient(None)
    assert client.configured is False
    with pytest.raises(YouTubeUnavailable):
        await client.find_by_duration("anything", max_minutes=15)


async def test_an_api_error_becomes_youtube_unavailable() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(403, json={"error": {}}))
    async with httpx.AsyncClient(transport=transport) as http:
        client = YouTubeClient("a-key", client=http)
        with pytest.raises(YouTubeUnavailable):
            await client.find_by_duration("anything", max_minutes=15)


async def test_the_search_does_not_filter_for_capabilities_the_app_lacks() -> None:
    """The query is asserted, not just its result.

    Two parameters were narrowing the candidate pool for nothing. `videoEmbeddable=true`
    restricts to videos this app may embed, and it never embeds one — the learner gets a
    link to youtube.com. `videoDuration` has three coarse buckets that cannot express "no
    longer than 13 minutes", so pre-filtering with one could only discard candidates the
    real check in `rank` would have accepted.

    A test on the returned videos cannot see either of these: the recorded fixture answers
    whatever it is asked. This is the "assert the call, not the output" case from
    CLAUDE.md, and the same shape as pinning a signed URL's arguments.
    """
    sent: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.params)
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"items": [{"id": {"videoId": "short"}}]})
        return httpx.Response(200, json={"items": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await YouTubeClient("a-key", client=http).find_by_duration("q", max_minutes=13)

    search = sent[0]
    assert "videoEmbeddable" not in search
    assert "videoDuration" not in search
    # What the search *must* still carry, so this does not become a test that only
    # asserts absences.
    assert search["type"] == "video"
    assert search["part"] == "id"
    assert search["key"] == "a-key"
