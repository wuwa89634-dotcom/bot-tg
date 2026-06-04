from __future__ import annotations

import os
import re
from dataclasses import dataclass
from http import HTTPStatus
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
    detail: str | None = None


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
        club_response = _get(club_url)
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            return ProfileCheck(
                ok=False,
                profile_id=profile_id,
                reason="auth_required",
                detail=f"club_url={club_url} status={status_code}",
            )
        if status_code == HTTPStatus.NOT_FOUND:
            return ProfileCheck(
                ok=False,
                profile_id=profile_id,
                reason="club_not_found",
                detail=f"club_url={club_url} status={status_code}",
            )
        return ProfileCheck(
            ok=False,
            profile_id=profile_id,
            reason="network",
            detail=f"club_url={club_url} status={status_code}",
        )
    except requests.RequestException as exc:
        return ProfileCheck(
            ok=False,
            profile_id=profile_id,
            reason="network",
            detail=f"club_url={club_url} error={type(exc).__name__}: {exc}",
        )

    members = parse_club_members(club_response.text, club_url)
    if not members:
        return ProfileCheck(
            ok=False,
            profile_id=profile_id,
            reason="members_unavailable",
            detail=f"club_url={club_url} status={club_response.status_code} bytes={len(club_response.text)}",
        )

    for member in members:
        if member.profile_id == profile_id:
            return ProfileCheck(
                ok=True,
                profile_id=profile_id,
                display_name=member.display_name,
            )

    display_name = _try_profile_display_name(profile_url) or f"MangaBuff #{profile_id}"

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
    headers = {
        "User-Agent": os.getenv(
            "MANGABUFF_USER_AGENT",
            (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
            ),
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://mangabuff.ru/",
    }
    cookie = os.getenv("MANGABUFF_COOKIE")
    if cookie:
        headers["Cookie"] = cookie

    response = requests.get(
        url,
        headers=headers,
        timeout=15,
    )
    response.raise_for_status()
    return response


def _try_profile_display_name(profile_url: str) -> str | None:
    try:
        response = _get(profile_url)
    except requests.RequestException:
        return None
    soup = BeautifulSoup(response.text, "html.parser")
    return _extract_display_name(soup)


def _extract_display_name(soup: BeautifulSoup) -> str | None:
    for selector in ("meta[property='og:title']", "h1", "title"):
        node = soup.select_one(selector)
        if not node:
            continue
        value = node.get("content") if node.name == "meta" else node.get_text(" ", strip=True)
        if value:
            return value.split("|", 1)[0].strip()
    return None
