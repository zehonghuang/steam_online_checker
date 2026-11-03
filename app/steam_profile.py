from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import requests
from bs4 import BeautifulSoup


OnlineStatus = Literal["online", "offline", "unknown"]


@dataclass(frozen=True)
class ProfileStatus:
    status: OnlineStatus
    raw_text: str | None = None


def fetch_profile_html(
    profile_url: str,
    *,
    timeout_s: int = 20,
) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://steamcommunity.com/",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    cookies = {"Steam_Language": "schinese"}
    params = {"l": "schinese"}

    resp = requests.get(
        profile_url,
        headers=headers,
        cookies=cookies,
        params=params,
        timeout=timeout_s,
    )
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or resp.encoding
    return resp.text


def parse_online_status_from_html(html: str) -> ProfileStatus:
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one(".profile_in_game_header")
    if node is None:
        return ProfileStatus(status="unknown", raw_text=None)

    raw = node.get_text(strip=True)
    normalized = raw.replace("\xa0", " ").strip().lower()

    if (
        "在线" in raw
        or "正在游戏" in raw
        or "in-game" in normalized
        or normalized in {"currently online", "online"}
    ):
        return ProfileStatus(status="online", raw_text=raw)
    if "离线" in raw or normalized in {"offline"}:
        return ProfileStatus(status="offline", raw_text=raw)
    return ProfileStatus(status="unknown", raw_text=raw)


def get_profile_status(profile_url: str, *, timeout_s: int = 20) -> ProfileStatus:
    html = fetch_profile_html(profile_url, timeout_s=timeout_s)
    return parse_online_status_from_html(html)

