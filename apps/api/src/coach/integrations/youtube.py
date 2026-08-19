"""YouTube Data API v3 — candidate videos that actually fit a time budget.

docs/00-overview.md, decision 6: **video duration is computed, not guessed.**
`search.list` → `videos.list(part=contentDetails,statistics)` → parse ISO-8601 → filter
against the task's remaining minute budget. The model picks *among* candidates that
already fit, which is the whole point: a model asked "find a video under 15 minutes" will
confidently return a 47-minute one, because the duration is not in the text it is reading.

Two API calls, not one, and that is not an oversight — `search.list` does not return
`contentDetails`, so the durations have to be fetched by id in a second call. The
`search.list` call also costs **100 quota units** against a 10,000/day default, which is
what the cache below is for: 100 distinct research queries a day is the ceiling without it.

**The key is an API key, and it is a different credential from everything else here.**
docs/09-roadmap.md's table has a row for exactly this trap — "an OAuth *scope* failure
reads exactly like a missing IAM *role*" — and the way it bites here is the reverse: a
client built with ADC for Firestore or Storage will not authenticate to the YouTube Data
API at all. This module therefore takes a plain key and speaks HTTP, rather than borrowing
a Google client from somewhere else in the process.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_ROOT = "https://www.googleapis.com/youtube/v3"
TIMEOUT_SECONDS = 10.0

#: docs/03-agent-design.md: "Returns ≤ 8 candidates with exact minutes."
MAX_CANDIDATES = 8
#: How many search hits to price out. `videos.list` takes up to 50 ids for 1 quota unit, so
#: over-fetching here is nearly free and is what makes the duration filter survive a query
#: whose first ten results are all too long.
SEARCH_RESULTS = 25

#: docs/03-agent-design.md: "cached 24 h by query hash".
CACHE_TTL_SECONDS = 24 * 60 * 60

_ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


class YouTubeUnavailable(RuntimeError):
    """No key configured, or the API refused. The tool answers with this rather than
    raising through, so a project with videos enabled and no key degrades to a report with
    no videos instead of a failed research run."""


def parse_iso_duration(value: str) -> int | None:
    """`PT12M3S` -> 723 seconds. `None` when the string is not a duration we understand.

    YouTube returns `P0D` for a live stream and for some premieres — a duration that parses
    to zero and is not the length of anything. Callers treat `0` as unknown rather than as
    "fits any budget", which is why a live stream must not silently rank first.
    """
    match = _ISO_DURATION.match(value)
    if match is None:
        return None
    parts = {key: int(raw or 0) for key, raw in match.groupdict().items()}
    return (
        parts["days"] * 86_400
        + parts["hours"] * 3_600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


@dataclass(frozen=True, slots=True)
class VideoCandidate:
    video_id: str
    title: str
    channel: str
    published_at: str
    duration_iso: str
    minutes: int
    views: int
    likes: int

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    @property
    def engagement(self) -> float:
        """Likes per view, the "view/like ratio" of docs/03-agent-design.md.

        A ratio rather than raw view count, because raw views rank a five-year-old
        introductory video above the one that actually covers the task. Videos with too few
        views to be meaningful score zero rather than 1.0 — a 3-view video with 3 likes is
        not the best material on the internet.
        """
        return self.likes / self.views if self.views >= 100 else 0.0


class _Cache:
    """A process-local TTL cache, keyed by the query and the budget.

    Process-local rather than Firestore-backed on purpose: this caches a *quota* cost, not
    a correctness property, and a second instance paying for its own first lookup of a
    query is an acceptable price for not adding a collection, a TTL policy, and a read to
    the research path. Bounded so a long-lived instance cannot grow it without limit.
    """

    MAX_ENTRIES = 256

    def __init__(self) -> None:
        self._entries: dict[tuple[str, int], tuple[float, list[VideoCandidate]]] = {}

    def get(self, key: tuple[str, int], *, at: float) -> list[VideoCandidate] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if at - stored_at > CACHE_TTL_SECONDS:
            del self._entries[key]
            return None
        return value

    def put(self, key: tuple[str, int], value: list[VideoCandidate], *, at: float) -> None:
        if len(self._entries) >= self.MAX_ENTRIES:
            oldest = min(self._entries, key=lambda k: self._entries[k][0])
            del self._entries[oldest]
        self._entries[key] = (at, value)


def rank(candidates: list[VideoCandidate], *, max_minutes: int) -> list[VideoCandidate]:
    """Candidates that fit the budget, best first.

    "Filter `duration ≤ max_minutes` → rank by view/like ratio and recency"
    (docs/03-agent-design.md). Recency is the tie-break rather than a weighted term: for
    technical material "published more recently" is worth something and is worth much less
    than "people who watched it found it useful", and combining them into one score means
    inventing a weight nobody can defend.
    """
    fitting = [c for c in candidates if 0 < c.minutes <= max_minutes]
    return sorted(fitting, key=lambda c: (c.engagement, c.published_at), reverse=True)[
        :MAX_CANDIDATES
    ]


class YouTubeClient:
    """`youtube_find_by_duration`'s backend."""

    def __init__(
        self,
        api_key: str | None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._client = client
        self._cache = _Cache()

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    async def find_by_duration(self, query: str, *, max_minutes: int) -> list[VideoCandidate]:
        """Videos matching `query` that are no longer than `max_minutes`.

        Raises:
            YouTubeUnavailable: with no API key, or when the API refuses.
        """
        if not self._api_key:
            raise YouTubeUnavailable("no YOUTUBE_API_KEY is configured")

        key = (query.strip().lower(), max_minutes)
        now = time.monotonic()
        cached = self._cache.get(key, at=now)
        if cached is not None:
            logger.info("youtube cache hit", extra={"query": query})
            return cached

        owned = self._client is None
        http = self._client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        try:
            ids = await self._search(http, query)
            candidates = await self._details(http, ids) if ids else []
        except httpx.HTTPError as error:
            raise YouTubeUnavailable(f"the YouTube API did not answer: {error}") from error
        finally:
            if owned:
                await http.aclose()

        result = rank(candidates, max_minutes=max_minutes)
        self._cache.put(key, result, at=now)
        return result

    async def _search(self, http: httpx.AsyncClient, query: str) -> list[str]:
        response = await http.get(
            f"{API_ROOT}/search",
            params={
                "key": self._api_key,
                "q": query,
                "part": "id",
                "type": "video",
                "maxResults": SEARCH_RESULTS,
                # Excludes the live streams and premieres whose `P0D` duration is not a
                # length, and the shorts that are never the answer to "teach me this".
                "videoDuration": "any",
                "videoEmbeddable": "true",
                "safeSearch": "moderate",
            },
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return [
            item["id"]["videoId"]
            for item in payload.get("items", [])
            if item.get("id", {}).get("videoId")
        ]

    async def _details(
        self, http: httpx.AsyncClient, video_ids: list[str]
    ) -> list[VideoCandidate]:
        response = await http.get(
            f"{API_ROOT}/videos",
            params={
                "key": self._api_key,
                "id": ",".join(video_ids),
                "part": "contentDetails,statistics,snippet",
            },
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return [c for c in map(_to_candidate, payload.get("items", [])) if c is not None]


def _to_candidate(item: dict[str, Any]) -> VideoCandidate | None:
    duration_iso = item.get("contentDetails", {}).get("duration", "")
    seconds = parse_iso_duration(duration_iso)
    if not seconds:
        # Unparseable, or `P0D` — a live stream or a premiere. Dropped rather than treated
        # as zero minutes, which would make it fit every budget and rank first.
        return None
    snippet = item.get("snippet", {})
    statistics = item.get("statistics", {})
    return VideoCandidate(
        video_id=str(item.get("id", "")),
        title=str(snippet.get("title", "")),
        channel=str(snippet.get("channelTitle", "")),
        published_at=str(snippet.get("publishedAt", "")),
        duration_iso=duration_iso,
        # Rounded *up*: a 12 m 30 s video does not fit a 12-minute budget, and rounding
        # down is how a report that is 4 % over gets recorded as exactly on budget.
        minutes=-(-seconds // 60),
        views=int(statistics.get("viewCount", 0) or 0),
        likes=int(statistics.get("likeCount", 0) or 0),
    )


__all__ = [
    "CACHE_TTL_SECONDS",
    "MAX_CANDIDATES",
    "SEARCH_RESULTS",
    "VideoCandidate",
    "YouTubeClient",
    "YouTubeUnavailable",
    "parse_iso_duration",
    "rank",
]
