"""概念 2 / 3：Interior mutability（受控的可變性逃生口）

Rust 的意思：當你真的需要「共享又能改」，不要把同步責任丟給呼叫方，而是讓
物件對外呈現一個『不必操心共享』的介面，物件自己管好內部狀態的同步。

Python 對應：把 Lock 封裝進物件內部，呼叫方只管呼叫，不必記得上鎖。
這跟「洩漏的鎖」(呼叫方要自己 with lock) 是完全不同的體感。

跑法：python -X gil=0 mental_model/02_interior_mutability.py
"""

from __future__ import annotations

import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

N_THREADS = 8
PER_THREAD = 300_000


# --- 洩漏的鎖：呼叫方要自己記得同步（容易漏） --------------------------------
class LeakyCounter:
    def __init__(self) -> None:
        self.value = 0
        self.lock = threading.Lock()  # 鎖在這，但要不要用是呼叫方的事


# --- interior mutability：物件自己扛同步，對外介面乾淨 ------------------------
class SafeCounter:
    def __init__(self) -> None:
        self._value = 0
        self._lock = threading.Lock()

    def increment(self) -> None:  # 呼叫方不必知道有鎖
        with self._lock:
            self._value += 1

    @property
    def value(self) -> int:
        with self._lock:
            return self._value


def demo_encapsulated() -> int:
    counter = SafeCounter()

    def work() -> None:
        for _ in range(PER_THREAD):
            counter.increment()  # 乾淨介面，同步是物件的責任

    with ThreadPoolExecutor(max_workers=N_THREADS) as ex:
        for _ in range(N_THREADS):
            ex.submit(work)
    return counter.value


# --- 改很多次的擴充性：單一熱鎖會變瓶頸 => 分片後合併（不必動 David 的 iterator）---
# Rust 端對應 AtomicU64 / 分片鎖 / RW 鎖；Python 端最穩的是「每條執行緒自己累加，最後合併」。
def demo_sharded() -> dict[str, int]:
    def work() -> dict[str, int]:
        local: dict[str, int] = defaultdict(int)  # 每條執行緒私有，零競爭
        for i in range(PER_THREAD):
            local["even" if i % 2 == 0 else "odd"] += 1
        return local

    with ThreadPoolExecutor(max_workers=N_THREADS) as ex:
        parts = list(ex.map(lambda _: work(), range(N_THREADS)))

    merged: dict[str, int] = defaultdict(int)
    for part in parts:  # 合併在主執行緒，無鎖、無競爭
        for k, v in part.items():
            merged[k] += v
    return dict(merged)


if __name__ == "__main__":
    expected = N_THREADS * PER_THREAD
    print(f"[封裝鎖] SafeCounter.increment : {demo_encapsulated():>10} / 期望 {expected}")
    merged = demo_sharded()
    print(f"[分片合併] 改很多次的擴充解法  : {merged}  總計 {sum(merged.values())} / 期望 {expected}")
    print("\n心智模型：需要共享又要改 -> 把同步藏進物件（封裝鎖）；")
    print("          改很多次而熱鎖變瓶頸 -> 各自累加再合併，而不是硬搶一把鎖。")
