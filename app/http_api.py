from __future__ import annotations

import hmac
import json
import os
from datetime import datetime, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .game_stats import GameStatsMonitor
from .polling import PollingController
from .recorder import StatusRecorder


class Handler(BaseHTTPRequestHandler):
    recorder: StatusRecorder
    game_stats: GameStatsMonitor | None = None
    log_file: str
    timezone_name: str
    polling: PollingController | None = None
    control_token: str = ""

    def do_POST(self) -> None:
        self.close_connection = True
        if urlparse(self.path).path != "/polling":
            self._send_json(404, {"error": "not_found"})
            return
        if not self.control_token or self.polling is None:
            self._send_json(503, {"error": "polling_control_unavailable"})
            return
        auth = self.headers.get("Authorization", "").split()
        if (len(auth) != 2 or auth[0].lower() != "bearer"
                or not hmac.compare_digest(auth[1].encode("utf-8"), self.control_token.encode("utf-8"))):
            self._send_json(401, {"error": "unauthorized"})
            return
        if (len(self.headers.get_all("Content-Type", [])) != 1
                or self.headers.get_content_type() != "application/json"
                or self.headers.get_content_charset("utf-8").lower() != "utf-8"):
            self._send_json(415, {"error": "content_type_must_be_application_json_utf8"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if (self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1
                or not lengths[0].isascii() or not lengths[0].isdigit()):
            self._send_json(400, {"error": "invalid_content_length"})
            return
        length = int(lengths[0]) if len(lengths[0]) < 10 else 1025
        if length > 1024:
            self._send_json(413, {"error": "body_too_large"})
            return
        try:
            self.connection.settimeout(5)
            data = self.rfile.read(length)
            if len(data) != length:
                raise ValueError
            payload = json.loads(data.decode("utf-8"))
        except (ValueError, OSError):
            self._send_json(400, {"error": "invalid_json"})
            return
        if not isinstance(payload, dict) or set(payload) != {"enabled"} or type(payload["enabled"]) is not bool:
            self._send_json(400, {"error": "expected_enabled_boolean"})
            return
        self._send_json(200, self.polling.set_enabled(payload["enabled"]))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/polling":
            if self.polling is None:
                self._send_json(503, {"error": "polling_control_unavailable"})
            else:
                self._send_json(200, self.polling.snapshot())
            return
        if parsed.path == "/readme":
            try:
                data = (Path(__file__).resolve().parent.parent / "README.md").read_bytes()
            except OSError:
                self._send_json(503, {"error": "readme_unavailable"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path == "/healthz":
            self._send_json(200, {"ok": True})
            return

        if parsed.path in {"/game-stats", "/game-stats/changes"}:
            if self.game_stats is None:
                self._send_json(503, {"error": "game_stats_unavailable"})
                return
            if parsed.path == "/game-stats":
                self._send_json(200, self.game_stats.snapshot())
            else:
                qs = parse_qs(parsed.query or "")
                limit = _parse_int(qs.get("limit", [None])[0], default=100, min_v=1, max_v=1000)
                self._send_json(200, self.game_stats.changes(limit))
            return

        if parsed.path == "/status":
            qs = parse_qs(parsed.query or "")
            minutes = _parse_int(qs.get("minutes", [None])[0], default=15, min_v=1, max_v=24 * 60)
            order = (qs.get("order", ["desc"])[0] or "desc").lower()
            snaps = self.recorder.list_recent(minutes=minutes)
            if order != "asc":
                snaps = list(reversed(snaps))
            payload = [
                {"ts": s.ts.isoformat(), "status": s.status, "raw_text": s.raw_text} for s in snaps
            ]
            self._send_json(200, payload)
            return

        if parsed.path == "/today":
            segs = self.recorder.list_today_segments()
            payload = [
                {
                    "day": s.day,
                    "status": s.status,
                    "start_ts": s.start_ts.isoformat(),
                    "end_ts": s.end_ts.isoformat() if s.end_ts else None,
                }
                for s in segs
            ]
            self._send_json(200, payload)
            return

        if parsed.path == "/daily-status":
            qs = parse_qs(parsed.query or "")
            day = qs.get("date", [None])[0]
            if day is not None:
                try:
                    if datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d") != day:
                        raise ValueError
                except ValueError:
                    self._send_json(400, {"error": "date 必须是有效日期，格式为 YYYY-MM-DD"})
                    return
            payload = list_daily_status(
                log_file=self.log_file,
                timezone_name=self.timezone_name,
                day=day,
                limit=_parse_int(qs.get("limit", [None])[0], default=0, min_v=0, max_v=1000),
            )
            self._send_json(200, payload)
            return

        if parsed.path == "/late-offline":
            qs = parse_qs(parsed.query or "")
            payload = list_late_offline_dates(
                log_file=self.log_file,
                timezone_name=self.timezone_name,
                after=_parse_clock(qs.get("after", [None])[0], default=time(hour=20, minute=30)),
                limit=_parse_int(qs.get("limit", [None])[0], default=0, min_v=0, max_v=1000),
            )
            self._send_json(200, payload)
            return

        self._send_json(404, {"error": "not_found"})

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, code: int, payload: object) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve_http(
    host: str,
    port: int,
    recorder: StatusRecorder,
    *,
    log_file: str,
    timezone_name: str,
    game_stats: GameStatsMonitor | None = None,
    polling: PollingController | None = None,
) -> ThreadingHTTPServer:
    class _Handler(Handler):
        pass

    _Handler.recorder = recorder
    _Handler.game_stats = game_stats
    _Handler.polling = polling if polling is not None else (game_stats.polling if game_stats is not None else None)
    _Handler.control_token = os.getenv("CONTROL_TOKEN", "").strip()
    _Handler.log_file = log_file
    _Handler.timezone_name = timezone_name
    httpd = ThreadingHTTPServer((host, port), _Handler)
    return httpd


def list_late_offline_dates(
    *,
    log_file: str,
    timezone_name: str,
    after: time,
    limit: int = 0,
) -> list[dict[str, object]]:
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        if timezone_name in {"Asia/Shanghai", "Asia/Beijing"}:
            tz = ZoneInfo("Etc/GMT-8")
        else:
            tz = ZoneInfo("UTC")

    grouped: dict[str, dict[str, object]] = {}
    for path in _iter_log_files(log_file):
        try:
            content = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue

        for line in content.splitlines():
            payload = _extract_json_payload(line)
            if payload is None:
                continue
            if payload.get("event") != "transition":
                continue
            if payload.get("from") != "online" or payload.get("to") != "offline":
                continue

            ts_raw = payload.get("ts")
            if not isinstance(ts_raw, str):
                continue

            try:
                ts = datetime.fromisoformat(ts_raw)
            except ValueError:
                continue

            ts = ts.astimezone(tz)
            if ts.time() <= after:
                continue

            date_key = ts.date().isoformat()
            item = grouped.setdefault(
                date_key,
                {
                    "date": date_key,
                    "weekday": _weekday_cn(ts.weekday()),
                    "offline_times": [],
                },
            )
            item["offline_times"].append(ts.isoformat())

    result = list(grouped.values())
    result.sort(key=lambda x: x["date"], reverse=True)
    if limit > 0:
        result = result[:limit]
    return result


def list_daily_status(
    *,
    log_file: str,
    timezone_name: str,
    day: str | None = None,
    limit: int = 0,
) -> list[dict[str, object]]:
    tz = ZoneInfo("Asia/Shanghai" if timezone_name == "Asia/Beijing" else timezone_name)
    grouped = {}
    seen = set()
    events = []
    for path in _iter_log_files(log_file):
        try:
            with path.open(encoding="utf-8-sig") as stream:
                for line in stream:
                    payload = _extract_json_payload(line)
                    if payload is None or payload.get("event") != "transition":
                        continue
                    previous, current = payload.get("from"), payload.get("to")
                    if (previous, current) not in (("offline", "online"), ("online", "offline")):
                        continue
                    ts_raw = payload.get("ts")
                    if not isinstance(ts_raw, str):
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_raw)
                    except ValueError:
                        continue
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=tz)
                    ts = ts.astimezone(tz)
                    key = (ts, current)
                    if key in seen:
                        continue
                    seen.add(key)
                    events.append(key)
        except OSError:
            continue

    # 先合并所有轮转日志并配对，再按日期筛选，避免跨午夜会话被截断。
    sessions = []
    pending = None
    for ts, status in sorted(events, key=lambda event: event[0]):
        if status == "online":
            if pending is not None:
                sessions.append((pending, None))
            pending = ts
        else:
            sessions.append((pending, ts))
            pending = None
    if pending is not None:
        sessions.append((pending, None))

    for online_ts, offline_ts in sessions:
        anchor = online_ts if online_ts is not None else offline_ts
        date_key = anchor.date().isoformat()
        if day is not None and date_key != day:
            continue
        item = grouped.setdefault(date_key, {
            "date": date_key,
            "weekday": _weekday_cn(anchor.weekday()),
            "sessions": [],
        })
        item["sessions"].append({
            "online_time": online_ts.isoformat() if online_ts is not None else None,
            "offline_time": offline_ts.isoformat() if offline_ts is not None else None,
        })

    result = sorted(grouped.values(), key=lambda item: item["date"], reverse=True)
    return result[:limit] if limit > 0 else result


def _iter_log_files(log_file: str) -> list[Path]:
    p = Path(log_file)
    candidates = [p]
    if p.exists():
        for child in sorted(p.parent.glob(f"{p.name}*")):
            if child != p and child.is_file():
                candidates.append(child)
    else:
        for child in sorted(p.parent.glob(f"{p.name}*")):
            if child.is_file():
                candidates.append(child)
    unique: list[Path] = []
    seen: set[str] = set()
    for item in candidates:
        key = os.fspath(item)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _extract_json_payload(line: str) -> dict[str, object] | None:
    idx = line.find("{")
    if idx < 0:
        return None
    try:
        payload = json.loads(line[idx:])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _weekday_cn(idx: int) -> str:
    names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    return names[idx]


def _parse_clock(v: str | None, *, default: time) -> time:
    if not v:
        return default
    try:
        hour_str, minute_str = v.split(":", 1)
        hour = int(hour_str)
        minute = int(minute_str)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return default
        return time(hour=hour, minute=minute)
    except (TypeError, ValueError):
        return default


def _parse_int(v: str | None, *, default: int, min_v: int, max_v: int) -> int:
    if v is None:
        return default
    try:
        i = int(v)
    except ValueError:
        return default
    return max(min_v, min(max_v, i))
