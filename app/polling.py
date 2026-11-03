from __future__ import annotations

import threading
import time


class PollingController:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._enabled = True
        self._in_flight = {"status": 0, "games": 0}

    def snapshot(self) -> dict[str, object]:
        with self._condition:
            return {"enabled": self._enabled, "in_flight": dict(self._in_flight)}

    def set_enabled(self, enabled: bool) -> dict[str, object]:
        with self._condition:
            self._enabled = enabled
            self._condition.notify_all()
            return self.snapshot()

    def begin(self, source: str) -> bool:
        # 原子准入：暂停前获准的整轮抓取属于在途，允许完成。
        with self._condition:
            if not self._enabled:
                return False
            self._in_flight[source] += 1
            return True

    def finish(self, source: str) -> None:
        with self._condition:
            self._in_flight[source] -= 1

    def wait_until(self, deadline: float, stop: threading.Event) -> bool:
        with self._condition:
            while not stop.is_set():
                remaining = deadline - time.monotonic()
                if self._enabled and remaining <= 0:
                    return True
                self._condition.wait(remaining if self._enabled else None)
            return False

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()
