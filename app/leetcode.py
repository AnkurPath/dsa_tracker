from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

LEETCODE_SLUG_RE = re.compile(
    r"^https?://(?:www\.)?leetcode\.com/problems/([a-z0-9-]+)/?",
    re.IGNORECASE,
)

GRAPHQL_URL = "https://leetcode.com/graphql/"
QUESTION_QUERY = """
query questionTitle($titleSlug: String!) {
  question(titleSlug: $titleSlug) {
    title
    titleSlug
    difficulty
    topicTags {
      name
      slug
    }
  }
}
"""

USER_PROFILE_QUERY = """
query userPublicProfile($username: String!) {
  matchedUser(username: $username) {
    username
  }
}
"""

RECENT_AC_QUERY = """
query recentAcSubmissions($username: String!, $limit: Int!) {
  recentAcSubmissionList(username: $username, limit: $limit) {
    id
    title
    titleSlug
    timestamp
  }
}
"""

LEETCODE_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    "Referer": "https://leetcode.com",
    "User-Agent": "Mozilla/5.0 (compatible; DSA-Revision-Helper/1.0)",
}


@dataclass(frozen=True)
class LeetCodeTopic:
    name: str
    slug: str


@dataclass(frozen=True)
class LeetCodeProblem:
    slug: str
    title: str
    url: str
    difficulty: str | None  # Easy | Medium | Hard
    topics: tuple[LeetCodeTopic, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RecentAcSubmission:
    slug: str
    title: str
    timestamp: int


def extract_slug(url: str) -> str:
    url = url.strip()
    match = LEETCODE_SLUG_RE.match(url)
    if match:
        return match.group(1).lower().rstrip("/")

    parsed = urlparse(url)
    if "leetcode.com" not in (parsed.netloc or ""):
        raise ValueError("Please enter a valid LeetCode problem URL.")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parts[0] != "problems":
        raise ValueError(
            "URL must look like https://leetcode.com/problems/two-sum/"
        )
    return parts[1].lower().rstrip("/")


def _parse_topics(raw: list | None) -> tuple[LeetCodeTopic, ...]:
    if not raw:
        return ()
    topics: list[LeetCodeTopic] = []
    for item in raw:
        name = (item or {}).get("name")
        slug = (item or {}).get("slug")
        if name and slug:
            topics.append(LeetCodeTopic(name=name, slug=slug))
    return tuple(topics)


def _graphql(query: str, variables: dict) -> dict:
    response = httpx.post(
        GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers=DEFAULT_HEADERS,
        timeout=15.0,
        trust_env=False,
    )
    response.raise_for_status()
    return response.json()


def normalize_leetcode_username(username: str) -> str:
    username = username.strip()
    if not LEETCODE_USERNAME_RE.match(username):
        raise ValueError(
            "LeetCode username must be 3–64 characters: letters, numbers, _ or -."
        )
    return username


def verify_leetcode_username(username: str) -> str:
    """Validate format and confirm the profile exists on LeetCode."""
    username = normalize_leetcode_username(username)
    try:
        payload = _graphql(USER_PROFILE_QUERY, {"username": username})
    except httpx.HTTPError as exc:
        raise ValueError("Could not reach LeetCode. Try again in a moment.") from exc
    matched = (payload.get("data") or {}).get("matchedUser")
    if not matched or not matched.get("username"):
        raise ValueError(f'No LeetCode user named "{username}".')
    return matched["username"]


def fetch_problem_by_slug(slug: str, title_hint: str | None = None) -> LeetCodeProblem:
    """Fetch title, difficulty, and topics for a problem slug."""
    slug = slug.strip().lower().rstrip("/")
    canonical = f"https://leetcode.com/problems/{slug}/"
    fallback_title = title_hint or slug.replace("-", " ").title()

    try:
        payload = _graphql(QUESTION_QUERY, {"titleSlug": slug})
        question = (payload.get("data") or {}).get("question")
        if not question:
            return LeetCodeProblem(
                slug=slug,
                title=fallback_title,
                url=canonical,
                difficulty=None,
                topics=(),
            )
        difficulty = question.get("difficulty")
        if difficulty not in {"Easy", "Medium", "Hard"}:
            difficulty = None
        return LeetCodeProblem(
            slug=question.get("titleSlug") or slug,
            title=question.get("title") or fallback_title,
            url=canonical,
            difficulty=difficulty,
            topics=_parse_topics(question.get("topicTags")),
        )
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return LeetCodeProblem(
            slug=slug,
            title=fallback_title,
            url=canonical,
            difficulty=None,
            topics=(),
        )


def fetch_problem(url: str) -> LeetCodeProblem:
    """Parse URL and fetch title, difficulty, and topics from LeetCode GraphQL."""
    return fetch_problem_by_slug(extract_slug(url))


def fetch_recent_ac_submissions(
    username: str, limit: int = 20
) -> list[RecentAcSubmission]:
    """Public recent accepted submissions for a LeetCode username (max ~20)."""
    username = normalize_leetcode_username(username)
    limit = max(1, min(limit, 20))
    try:
        payload = _graphql(
            RECENT_AC_QUERY, {"username": username, "limit": limit}
        )
    except httpx.HTTPError as exc:
        raise ValueError("Could not reach LeetCode. Try again in a moment.") from exc

    raw = (payload.get("data") or {}).get("recentAcSubmissionList") or []
    results: list[RecentAcSubmission] = []
    seen: set[str] = set()
    for item in raw:
        slug = ((item or {}).get("titleSlug") or "").strip().lower()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        title = (item or {}).get("title") or slug.replace("-", " ").title()
        try:
            timestamp = int((item or {}).get("timestamp") or 0)
        except (TypeError, ValueError):
            timestamp = 0
        results.append(
            RecentAcSubmission(slug=slug, title=title, timestamp=timestamp)
        )
    return results


def parse_leetcode_url(url: str) -> tuple[str, str, str]:
    """Return (slug, title, canonical_url) from a LeetCode problem URL."""
    info = fetch_problem(url)
    return info.slug, info.title, info.url
