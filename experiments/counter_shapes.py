"""共用元件：兩種形狀的 read-modify-write 計數器，以及用 Barrier 起跑的多執行緒 runner。

experiments/switchpoint_probe.py（切換點探針）與 tests/test_thread_safety.py（確定性測試）
都從這裡 import，避免同一個 Counter 定義兩份。

兩種形狀的差別只在讀與寫之間有沒有一個 Python 呼叫：
    run_a:  self.value += 1
            LOAD_ATTR -> BINARY_OP -> STORE_ATTR，中間沒有 eval breaker 檢查點
    run_b:  self.value = self.value + step()
            LOAD_ATTR -> CALL step() -> BINARY_OP -> STORE_ATTR，
            step() 的 CALL / RESUME 就是夾在讀寫之間的檢查點
"""

from __future__ import annotations

import threading
from collections.abc import Callable

THREADS = 8
ITERS = 200_000


def step() -> int:
    """純 Python 被呼叫端。重點是那個 CALL，回傳值本身無所謂。"""
    return 1


class Counter:
    def __init__(self) -> None:
        self.value = 0

    def run_a(self, n: int) -> None:
        for _ in range(n):
            self.value += 1

    def run_b(self, n: int) -> None:
        for _ in range(n):
            self.value = self.value + step()


def run_threads(fn: Callable[[int], None], threads: int = THREADS, iters: int = ITERS) -> None:
    """開 threads 條執行緒，用 Barrier 讓它們同時起跑，各自呼叫 fn(iters)。"""
    barrier = threading.Barrier(threads)

    def worker() -> None:
        barrier.wait()  # 全員就位後才一起開跑，確保真的重疊
        fn(iters)

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
