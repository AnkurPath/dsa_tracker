from __future__ import annotations

import re
from dataclasses import dataclass
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
  }
}
"""


@dataclass(frozen=True)
class LeetCodeProblem:
    slug: str
    title: str
    url: str
    difficulty: str | None  # Easy | Medium | Hard


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


def fetch_problem(url: str) -> LeetCodeProblem:
    """Parse URL and fetch title + difficulty from LeetCode GraphQL."""
    slug = extract_slug(url)
    canonical = f"https://leetcode.com/problems/{slug}/"
    fallback_title = slug.replace("-", " ").title()

    try:
        response = httpx.post(
            GRAPHQL_URL,
            json={"query": QUESTION_QUERY, "variables": {"titleSlug": slug}},
            headers={
                "Content-Type": "application/json",
                "Referer": "https://leetcode.com",
                "User-Agent": "Mozilla/5.0 (compatible; DSA-Tracker/1.0)",
            },
            timeout=15.0,
            trust_env=False,
        )
        response.raise_for_status()
        payload = response.json()
        question = (payload.get("data") or {}).get("question")
        if not question:
            return LeetCodeProblem(
                slug=slug,
                title=fallback_title,
                url=canonical,
                difficulty=None,
            )
        difficulty = question.get("difficulty")
        if difficulty not in {"Easy", "Medium", "Hard"}:
            difficulty = None
        return LeetCodeProblem(
            slug=question.get("titleSlug") or slug,
            title=question.get("title") or fallback_title,
            url=canonical,
            difficulty=difficulty,
        )
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        # Still allow adding the problem if LeetCode is unreachable
        return LeetCodeProblem(
            slug=slug,
            title=fallback_title,
            url=canonical,
            difficulty=None,
        )


# Backwards-compatible helper used by older call sites / tests
def parse_leetcode_url(url: str) -> tuple[str, str, str]:
    """Return (slug, title, canonical_url) from a LeetCode problem URL."""
    info = fetch_problem(url)
    return info.slug, info.title, info.url
