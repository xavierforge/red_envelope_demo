"""共用元件：兩種形狀的 read-modify-write 計數器，以及用 Barrier 起跑的多執行緒 runner。

experiments/switchpoint_probe.py（切換點探針）與 tests/test_thread_safety.py（測試）
都從這裡 import，避免同一個 Counter 定義兩份。兩種形狀的程式碼刻意保持原樣，
讓 results/ 裡既有的實驗可以重現。

兩種形狀的差別在讀與寫之間有沒有一個 Python 呼叫：
    run_a:  self.value += 1
            LOAD_ATTR -> BINARY_OP -> STORE_ATTR
    run_b:  self.value = self.value + step()
            LOAD_ATTR -> CALL step() -> BINARY_OP -> STORE_ATTR

關於 eval breaker 的說明範圍：在本專案展示的 CPython 3.14 上、這兩條執行路徑裡，
切換間隔到期後的週期性檢查落在 CALL 之後、JUMP_BACKWARD 與 RESUME（bpo-29988 起的
設計），所以 run_a 的讀寫之間沒有這種檢查，run_b 的 step() 呼叫則有。這只描述
「週期性檢查」這一種交出 GIL 的時機，阻塞 I/O、sleep、C 擴充主動釋放等其他時機
不在這段說明的範圍內，也不保證其他版本的檢查點位置相同。

run_threads 的 Barrier 只保證執行緒同時起跑、真的重疊，並沒有固定讀與寫的交錯。
所以自然排程下有沒有丟更新是觀察結果：沒觀察到，不代表操作有 atomic 保證。
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
    """開 threads 條執行緒，用 Barrier 讓它們同時起跑，各自呼叫 fn(iters)。

    Barrier 只保證重疊，不安排交錯；結果會受排程與 CPU 資源影響。
    """
    barrier = threading.Barrier(threads)

    def worker() -> None:
        barrier.wait()  # 全員就位後才一起開跑，確保真的重疊
        fn(iters)

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
