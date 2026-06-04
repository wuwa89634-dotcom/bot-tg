from __future__ import annotations

import os
import re
from dataclasses import dataclass
from http import HTTPStatus
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


PROFILE_RE = re.compile(r"^https?://(?:www\.)?mangabuff\.ru/users/(\d+)(?:[/?#].*)?$")
_SESSION: requests.Session | None = None
_LOGIN_DONE = False


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
    club_check_error: ProfileCheck | None = None

    try:
        club_response = _get(club_url)
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            club_check_error = ProfileCheck(
                ok=False,
                profile_id=profile_id,
                reason="auth_required",
                detail=f"club_url={club_url} status={status_code}",
            )
        elif status_code == HTTPStatus.NOT_FOUND:
            club_check_error = ProfileCheck(
                ok=False,
                profile_id=profile_id,
                reason="club_not_found",
                detail=f"club_url={club_url} status={status_code}",
            )
        else:
            club_check_error = ProfileCheck(
                ok=False,
                profile_id=profile_id,
                reason="network",
                detail=f"club_url={club_url} status={status_code}",
            )
    except requests.RequestException as exc:
        club_check_error = ProfileCheck(
            ok=False,
            profile_id=profile_id,
            reason="network",
            detail=f"club_url={club_url} error={type(exc).__name__}: {exc}",
        )
    else:
        members = parse_club_members(club_response.text, club_url)
        if not members:
            club_check_error = ProfileCheck(
                ok=False,
                profile_id=profile_id,
                reason="members_unavailable",
                detail=f"club_url={club_url} status={club_response.status_code} bytes={len(club_response.text)}",
            )
        else:
            for member in members:
                if member.profile_id == profile_id:
                    return ProfileCheck(
                        ok=True,
                        profile_id=profile_id,
                        display_name=member.display_name,
                        reason="club_members",
                    )

            display_name = _try_profile_display_name(profile_url) or f"MangaBuff #{profile_id}"
            return ProfileCheck(
                ok=False,
                profile_id=profile_id,
                display_name=display_name,
                reason="not_in_club",
            )

    profile_check = check_profile_page_for_club(profile_url, club_slug)
    if profile_check.ok:
        return profile_check

    if profile_check.reason in {"profile_auth_required", "profile_not_found"} and club_check_error:
        return ProfileCheck(
            ok=False,
            profile_id=profile_id,
            reason=club_check_error.reason,
            detail=f"{club_check_error.detail}; fallback_{profile_check.reason}={profile_check.detail}",
        )
    if profile_check.reason == "profile_network":
        return profile_check
    if club_check_error:
        return club_check_error

    return ProfileCheck(
        ok=False,
        profile_id=profile_id,
        display_name=profile_check.display_name or f"MangaBuff #{profile_id}",
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


def check_profile_page_for_club(profile_url: str, club_slug: str) -> ProfileCheck:
    profile_id = parse_profile_url(profile_url)
    if profile_id is None:
        return ProfileCheck(ok=False, reason="bad_url")

    try:
        response = _get(profile_url)
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            return ProfileCheck(
                ok=False,
                profile_id=profile_id,
                reason="profile_auth_required",
                detail=f"profile_url={profile_url} status={status_code}",
            )
        if status_code == HTTPStatus.NOT_FOUND:
            return ProfileCheck(
                ok=False,
                profile_id=profile_id,
                reason="profile_not_found",
                detail=f"profile_url={profile_url} status={status_code}",
            )
        return ProfileCheck(
            ok=False,
            profile_id=profile_id,
            reason="profile_network",
            detail=f"profile_url={profile_url} status={status_code}",
        )
    except requests.RequestException as exc:
        return ProfileCheck(
            ok=False,
            profile_id=profile_id,
            reason="profile_network",
            detail=f"profile_url={profile_url} error={type(exc).__name__}: {exc}",
        )

    soup = BeautifulSoup(response.text, "html.parser")
    display_name = _extract_display_name(soup) or f"MangaBuff #{profile_id}"
    if _html_contains_club(response.text, club_slug):
        return ProfileCheck(
            ok=True,
            profile_id=profile_id,
            display_name=display_name,
            reason="profile_page",
        )

    return ProfileCheck(
        ok=False,
        profile_id=profile_id,
        display_name=display_name,
        reason="not_in_club",
        detail=f"profile_url={profile_url} status={response.status_code} bytes={len(response.text)}",
    )


def _get(url: str) -> requests.Response:
    session = _get_session()
    response = session.get(
        url,
        timeout=15,
    )
    response.raise_for_status()
    return response


def _get_session() -> requests.Session:
    global _SESSION, _LOGIN_DONE
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.headers.update(_default_headers())

    cookie = os.getenv("MANGABUFF_COOKIE")
    if cookie:
        _SESSION.headers["Cookie"] = cookie
        return _SESSION

    if not _LOGIN_DONE and os.getenv("MANGABUFF_EMAIL") and os.getenv("MANGABUFF_PASSWORD"):
        _login(_SESSION)
        _LOGIN_DONE = True

    return _SESSION


def _default_headers() -> dict[str, str]:
    return {
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


def _login(session: requests.Session) -> None:
    login_url = os.getenv("MANGABUFF_LOGIN_URL", "https://mangabuff.ru/login")
    email = os.environ["MANGABUFF_EMAIL"]
    password = os.environ["MANGABUFF_PASSWORD"]
    login_field = os.getenv("MANGABUFF_LOGIN_FIELD", "email")
    password_field = os.getenv("MANGABUFF_PASSWORD_FIELD", "password")

    login_page = session.get(login_url, timeout=15)
    login_page.raise_for_status()

    soup = BeautifulSoup(login_page.text, "html.parser")
    csrf = _extract_csrf_token(soup)
    payload = {
        login_field: email,
        password_field: password,
    }
    if csrf:
        payload["_token"] = csrf

    headers = {"Referer": login_url}
    if csrf:
        headers["X-CSRF-TOKEN"] = csrf

    response = session.post(
        login_url,
        data=payload,
        headers=headers,
        timeout=15,
        allow_redirects=True,
    )
    response.raise_for_status()


def _extract_csrf_token(soup: BeautifulSoup) -> str | None:
    token_input = soup.select_one("input[name='_token']")
    if token_input and token_input.get("value"):
        return token_input["value"]

    meta = soup.select_one("meta[name='csrf-token']")
    if meta and meta.get("content"):
        return meta["content"]

    return None


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


def _html_contains_club(html: str, club_slug: str) -> bool:
    return (
        f"/clubs/{club_slug}" in html
        or f"clubs/{club_slug}" in html
        or f"https://mangabuff.ru/clubs/{club_slug}" in html
    )
