"""防抖状态机：连续 N 次同向观察才确认状态变化，单次抖动不算。"""
from typing import Dict, Optional


class Debouncer:
    def __init__(self, threshold: int = 2):
        if threshold < 1:
            raise ValueError("threshold 必须 >= 1")
        self._n = threshold
        self._confirmed: Dict[str, bool] = {}   # key -> 已确认状态是否为坏（默认好）
        self._streak: Dict[str, int] = {}       # key -> 与已确认状态相反的连续观察次数

    def state(self, key: str) -> bool:
        return self._confirmed.get(key, False)

    def restore(self, key: str, bad: bool) -> None:
        """从持久化库恢复已确认状态；不产生事件。"""
        self._confirmed[key] = bad
        self._streak[key] = 0

    def observe(self, key: str, bad: bool) -> Optional[str]:
        """返回 "down"（好->坏）、"up"（坏->好）或 None。"""
        if bad == self.state(key):
            self._streak[key] = 0
            return None
        self._streak[key] = self._streak.get(key, 0) + 1
        if self._streak[key] >= self._n:
            self._confirmed[key] = bad
            self._streak[key] = 0
            return "down" if bad else "up"
        return None
