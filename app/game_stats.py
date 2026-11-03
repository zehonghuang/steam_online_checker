from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bs4 import BeautifulSoup

from .polling import PollingController
from .steam_profile import fetch_profile_html


POLL_INTERVAL_S = 300


def parse_recent_games(html: str) -> list[dict[str, str | None]]:
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one(".profile_page") is None:
        raise ValueError("未识别到 Steam 个人主页")
    games = []
    for node in soup.select(".recent_game"):
        name = node.select_one(".game_name a")
        details = node.select_one(".game_info_details")
        app = re.search(r"/app/(\d+)(?:[/?#]|$)", name.get("href", "")) if name else None
        text = details.get_text("\n", strip=True) if details else ""
        hours = re.search(r"总时数\s*([\d,]+(?:\.\d+)?)\s*小时|([\d,]+(?:\.\d+)?)\s*hrs on record", text)
        if not app or not name.get_text(strip=True) or not hours:
            raise ValueError("最近游戏字段不完整，保留原基线")
        total = format(Decimal((hours.group(1) or hours.group(2)).replace(",", "")), "f")
        last_played = text[hours.end():].strip() or None
        games.append({
            "appid": app.group(1),
            "name": name.get_text(strip=True),
            "total_hours": total,
            "last_played_text": last_played,
        })
    return games


class GameStatsMonitor:
    def __init__(self, profile_url: str, event_file: str, *, timeout_s: int = 20, timezone_name: str = "Asia/Shanghai", polling: PollingController | None = None) -> None:
        self.polling = polling if polling is not None else PollingController()
        self.profile_url = profile_url
        self.timeout_s = timeout_s
        try:
            self._tz = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            if timezone_name in {"Asia/Shanghai", "Asia/Beijing"}:
                self._tz = timezone(timedelta(hours=8))
            else:
                self._tz = timezone.utc
        self._path = Path(event_file)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._baseline: dict[str, str] = {}
        self._games: list[dict[str, str | None]] = []
        self._last_success_at: str | None = None
        self._error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._recover()

    def _recover(self) -> None:
        if not self._path.exists():
            return
        # 只修复崩溃留下的未完成尾行；完整历史不轮转、不自动删除。
        with self._path.open("rb+") as stream:
            while True:
                start = stream.tell()
                line = stream.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    stream.truncate(start)
                    stream.flush()
                    os.fsync(stream.fileno())
                    break
                event = json.loads(line)
                if event["profile_url"] == self.profile_url:
                    self._baseline[event["appid"]] = event["total_hours"]

    def poll_once(self) -> None:
        with self._poll_lock:
            if not self.polling.begin("games"):
                return
            try:
                games = parse_recent_games(fetch_profile_html(self.profile_url, timeout_s=self.timeout_s))
                observed_at = datetime.now(self._tz).isoformat()
                with self._lock:
                    events = []
                    for game in games:
                        old = self._baseline.get(game["appid"])
                        if old is None or Decimal(old) != Decimal(game["total_hours"]):
                            events.append({
                                "event": "baseline" if old is None else "change",
                                "profile_url": self.profile_url,
                                **game,
                                "old_total_hours": old,
                                "observed_at": observed_at,
                            })
                    if events:
                        data = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events).encode("utf-8")
                        with self._path.open("ab") as stream:
                            start = stream.tell()
                            try:
                                stream.write(data)
                                stream.flush()
                                os.fsync(stream.fileno())
                            except Exception:
                                stream.truncate(start)
                                raise
                        for event in events:
                            self._baseline[event["appid"]] = event["total_hours"]
                    self._games = games
                    self._last_success_at = observed_at
                    self._error = None
            except Exception as exc:
                # 不将请求头、Cookie 或响应正文写入日志/API。
                error = f"{type(exc).__name__}: 游戏统计抓取、解析或持久化失败"
                with self._lock:
                    self._error = error
                logging.getLogger("steam-online-checker").warning("game_stats_failed %s", error)
            finally:
                self.polling.finish("games")

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "profile_url": self.profile_url,
                "poll_interval_s": POLL_INTERVAL_S,
                "last_success_at": self._last_success_at,
                "error": self._error,
                "games": deepcopy(self._games),
            }

    def changes(self, limit: int = 100) -> list[dict[str, object]]:
        with self._lock:
            result = deque(maxlen=max(1, min(1000, limit)))
            if self._path.exists():
                with self._path.open(encoding="utf-8") as stream:
                    for line in stream:
                        event = json.loads(line)
                        if event["profile_url"] == self.profile_url and event["event"] == "change":
                            result.append(event)
            return list(reversed(result))

    def start(self) -> None:
        if self._thread is not None:
            return

        def run() -> None:
            deadline = time.monotonic()
            while self.polling.wait_until(deadline, self._stop):
                started = time.monotonic()
                self.poll_once()
                deadline = started + POLL_INTERVAL_S

        self._thread = threading.Thread(target=run, name="game-stats-loop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.polling.wake()
        if self._thread is not None:
            self._thread.join(timeout=self.timeout_s + 1)
