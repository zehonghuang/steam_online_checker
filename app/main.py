from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
import logging
import logging.handlers
import os
import sys
import threading
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .game_stats import GameStatsMonitor
from .http_api import serve_http
from .polling import PollingController
from .recorder import StatusRecorder
from .steam_profile import get_profile_status


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="steam-online-checker")
    p.add_argument(
        "--url",
        default=os.getenv("STEAM_PROFILE_URL", "").strip()
        or "https://steamcommunity.com/profiles/76561198406118342",
    )
    p.add_argument("--timeout", type=int, default=int(os.getenv("TIMEOUT_S", "20")))
    p.add_argument("--interval", type=int, default=int(os.getenv("INTERVAL_S", "60")))
    p.add_argument("--once", action="store_true", default=False)
    p.add_argument("--http-host", default=os.getenv("HTTP_HOST", "0.0.0.0"))
    p.add_argument("--http-port", type=int, default=int(os.getenv("PORT", os.getenv("HTTP_PORT", "80"))))
    p.add_argument("--no-http", action="store_true", default=False)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_file = os.getenv("LOG_FILE", os.path.join("logs", "steam_online_checker.log"))
    timezone_name = os.getenv("TIMEZONE", "Asia/Shanghai")

    os.environ["TZ"] = timezone_name
    if hasattr(time, "tzset"):
        time.tzset()

    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)

    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        if timezone_name in {"Asia/Shanghai", "Asia/Beijing"}:
            tz = timezone(timedelta(hours=8))
        else:
            tz = timezone.utc

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    formatter.converter = lambda ts: datetime.fromtimestamp(ts, tz).timetuple()
    root = logging.getLogger()
    root.setLevel(log_level)

    h_stdout = logging.StreamHandler(sys.stdout)
    h_stdout.setFormatter(formatter)
    root.addHandler(h_stdout)

    h_file = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=int(os.getenv("LOG_MAX_BYTES", str(5 * 1024 * 1024))),
        backupCount=int(os.getenv("LOG_BACKUP_COUNT", "3")),
        encoding="utf-8-sig",
    )
    h_file.setFormatter(formatter)
    root.addHandler(h_file)

    log = logging.getLogger("steam-online-checker")

    recorder = StatusRecorder(
        retention_minutes=int(os.getenv("RETENTION_MINUTES", "1440")),
        timezone_name=timezone_name,
    )

    polling = PollingController()
    stop = threading.Event()

    def poll_loop() -> None:
        last_status: str | None = None
        deadline = time.monotonic()
        while polling.wait_until(deadline, stop):
            if not polling.begin("status"):
                continue
            try:
                status = get_profile_status(args.url, timeout_s=args.timeout)
                snap, changed = recorder.record(status)
                payload = {
                    "url": args.url,
                    "status": snap.status,
                    "raw_text": snap.raw_text,
                    "ts": snap.ts.isoformat(),
                }
                log.info(json.dumps(payload, ensure_ascii=False))
                if changed and last_status is not None:
                    log.info(
                        json.dumps(
                            {
                                "event": "transition",
                                "from": last_status,
                                "to": snap.status,
                                "ts": snap.ts.isoformat(),
                            },
                            ensure_ascii=False,
                        )
                    )
                last_status = snap.status
            except Exception:
                log.exception("fetch_failed url=%s", args.url)
            finally:
                polling.finish("status")

            if args.once:
                break

            deadline = time.monotonic() + max(1, int(args.interval))

    game_stats = GameStatsMonitor(
        args.url,
        os.path.join(os.path.dirname(log_file) or ".", "game_stats.jsonl"),
        timeout_s=args.timeout,
        timezone_name=timezone_name,
        polling=polling,
    )
    if args.once:
        game_stats.poll_once()
        poll_loop()
        return 0

    game_stats.start()
    if args.no_http:
        try:
            poll_loop()
        finally:
            game_stats.stop()
        return 0

    t = threading.Thread(target=poll_loop, name="poll-loop", daemon=True)
    t.start()

    httpd = serve_http(
        args.http_host,
        args.http_port,
        recorder,
        log_file=log_file,
        timezone_name=timezone_name,
        game_stats=game_stats,
        polling=polling,
    )
    log.info("http_listening host=%s port=%s", args.http_host, args.http_port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        polling.wake()
        game_stats.stop()
        t.join(timeout=args.timeout + 1)
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

