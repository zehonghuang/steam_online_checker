from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from threading import Lock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .steam_profile import ProfileStatus


@dataclass(frozen=True)
class Snapshot:
    ts: datetime
    status: str
    raw_text: str | None


@dataclass(frozen=True)
class Segment:
    day: str
    status: str
    start_ts: datetime
    end_ts: datetime | None


class StatusRecorder:
    def __init__(
        self,
        *,
        retention_minutes: int = 24 * 60,
        timezone_name: str = "Asia/Shanghai",
    ) -> None:
        self._lock = Lock()
        self._retention = timedelta(minutes=retention_minutes)
        try:
            self._tz = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            if timezone_name in {"Asia/Shanghai", "Asia/Beijing"}:
                self._tz = timezone(timedelta(hours=8))
            else:
                self._tz = timezone.utc
        self._snapshots: list[Snapshot] = []
        self._segments: list[Segment] = []

    def record(self, status: ProfileStatus, *, now: datetime | None = None) -> tuple[Snapshot, bool]:
        ts = (now or datetime.now(self._tz)).astimezone(self._tz)
        snap = Snapshot(ts=ts, status=status.status, raw_text=status.raw_text)
        with self._lock:
            prev = self._snapshots[-1] if self._snapshots else None
            changed = prev is None or prev.status != snap.status
            self._snapshots.append(snap)
            self._prune_locked(now=ts)
            if changed:
                self._record_segment_transition_locked(snap)
        return snap, changed

    def list_recent(self, *, minutes: int = 15, now: datetime | None = None) -> list[Snapshot]:
        ts = (now or datetime.now(self._tz)).astimezone(self._tz)
        cutoff = ts - timedelta(minutes=minutes)
        with self._lock:
            return [s for s in self._snapshots if s.ts >= cutoff]

    def list_today_segments(self, *, now: datetime | None = None) -> list[Segment]:
        ts = (now or datetime.now(self._tz)).astimezone(self._tz)
        day = ts.date().isoformat()
        with self._lock:
            return [s for s in self._segments if s.day == day]

    def _prune_locked(self, *, now: datetime) -> None:
        cutoff = now - self._retention
        while self._snapshots and self._snapshots[0].ts < cutoff:
            self._snapshots.pop(0)
        keep_days = {now.date().isoformat(), (now.date() - timedelta(days=1)).isoformat()}
        self._segments = [s for s in self._segments if s.day in keep_days]

    def _record_segment_transition_locked(self, snap: Snapshot) -> None:
        day = snap.ts.date().isoformat()
        if self._segments and self._segments[-1].end_ts is None:
            last = self._segments[-1]
            if last.day != day:
                midnight = datetime.combine(snap.ts.date(), time.min, tzinfo=snap.ts.tzinfo)
                self._segments[-1] = Segment(
                    day=last.day,
                    status=last.status,
                    start_ts=last.start_ts,
                    end_ts=midnight,
                )
                self._segments.append(
                    Segment(day=day, status=last.status, start_ts=midnight, end_ts=None)
                )
                return

            self._segments[-1] = Segment(
                day=last.day,
                status=last.status,
                start_ts=last.start_ts,
                end_ts=snap.ts,
            )

        self._segments.append(Segment(day=day, status=snap.status, start_ts=snap.ts, end_ts=None))
