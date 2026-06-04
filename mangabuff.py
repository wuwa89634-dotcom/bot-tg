from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


PROFILE_RE = re.compile(r"^https?://(?:www\.)?mangabuff\.ru/users/(\d+)(?:[/?#].*)?$")


@dataclass(frozen=True)
class ProfileCheck:
    ok: bool
    profile_id: int | None = None
    display_name: str | None = None
    reason: str | None = None


def parse_profile_url(text: str) -> int | None:
    match = PROFILE_RE.match(text.strip())
    if not match:
        return None
    return int(match.group(1))


@dataclass(frozen=True)
class ClubMember:
    profile_id: int
    display_name: str
    profile_url: str


def check_profile_in_club(
    profile_url: str,
    club_slug: str,
    club_url: str | None = None,
) -> ProfileCheck:
    profile_id = parse_profile_url(profile_url)
    if profile_id is None:
        return ProfileCheck(ok=False, reason="bad_url")

    club_url = club_url or f"https://mangabuff.ru/clubs/{club_slug}"

    try:
        profile_response = _get(profile_url)
        club_response = _get(club_url)
    except requests.RequestException:
        return ProfileCheck(ok=False, profile_id=profile_id, reason="network")

    soup = BeautifulSoup(profile_response.text, "html.parser")
    display_name = _extract_display_name(soup) or f"MangaBuff #{profile_id}"

    members = parse_club_members(club_response.text, club_url)
    for member in members:
        if member.profile_id == profile_id:
            return ProfileCheck(
                ok=True,
                profile_id=profile_id,
                display_name=member.display_name or display_name,
            )

    return ProfileCheck(
        ok=False,
        profile_id=profile_id,
        display_name=display_name,
        reason="not_in_club",
    )


def parse_club_members(html: str, base_url: str = "https://mangabuff.ru") -> list[ClubMember]:
    soup = BeautifulSoup(html, "html.parser")
    members: list[ClubMember] = []
    seen_ids: set[int] = set()

    for link in soup.select(".club__members .club__member-name[href*='/users/']"):
        href = link.get("href", "")
        match = re.search(r"/users/(\d+)(?:[/?#].*)?$", href)
        if not match:
            continue
        profile_id = int(match.group(1))
        if profile_id in seen_ids:
            continue

        seen_ids.add(profile_id)
        members.append(
            ClubMember(
                profile_id=profile_id,
                display_name=link.get_text(" ", strip=True) or f"MangaBuff #{profile_id}",
                profile_url=urljoin(base_url, f"/users/{profile_id}"),
            )
        )

    return members


def _get(url: str) -> requests.Response:
    response = requests.get(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
            )
        },
        timeout=15,
    )
    response.raise_for_status()
    return response


def _extract_display_name(soup: BeautifulSoup) -> str | None:
    for selector in ("meta[property='og:title']", "h1", "title"):
        node = soup.select_one(selector)
        if not node:
            continue
        value = node.get("content") if node.name == "meta" else node.get_text(" ", strip=True)
        if value:
            return value.split("|", 1)[0].strip()
    return None
