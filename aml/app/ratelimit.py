"""登入嘗試的來源位址速率限制。

帳號鎖定只擋單一帳號——攻擊者換帳號輪流嘗試就繞過了。系統對外開放後，
這是必要的第二道。

狀態存於行程記憶體。目前為單一應用容器部署，足夠；若日後水平擴充為
多個 worker，各自計數會使實際容許次數倍增，屆時須改為共用儲存。
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

_attempts: dict[str, deque[float]] = defaultdict(deque)


def _prune(bucket: deque[float], cutoff: float) -> None:
    while bucket and bucket[0] < cutoff:
        bucket.popleft()


def too_many(key: str, *, limit: int, window: int, at: float | None = None) -> bool:
    now = time.time() if at is None else at
    bucket = _attempts[key]
    _prune(bucket, now - window)
    return len(bucket) >= limit


def record(key: str, *, at: float | None = None) -> None:
    _attempts[key].append(time.time() if at is None else at)


def clear(key: str | None = None) -> None:
    """登入成功後清除該來源的計數；不帶參數則全部清除（測試用）。"""
    if key is None:
        _attempts.clear()
    else:
        _attempts.pop(key, None)
