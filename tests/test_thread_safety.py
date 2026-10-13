"""第一幕的災難：lost update，分成「確定性測試」與「自然排程觀察」兩類。

共享狀態上的 read-modify-write race 一直都存在，不論 GIL on 或 GIL off。
但「自然排程下這一次會不會真的丟更新」取決於排程、CPU 資源與直譯器版本，
Barrier 起跑只保證執行緒重疊，並沒有固定讀與寫的交錯。所以這個測試套件裡：

    - 不要求未上鎖的 Counter 在自然排程下每次都丟更新（那是觀察，放在
      experiments/switchpoint_probe.py 與 results/switchpoint_probe_2026-10-11.txt）。
    - 不把 GIL on 下 `self.value += 1` 的零丟失寫成「永遠不會」；那是指定環境
      （2026-10-11，CPython 3.14.6 free-threading build，-X gil=1）下的觀察。
    - 保留「加鎖後結果必須正確」的測試：兩種形狀、兩種 GIL 狀態都要算對。
    - 新增一個小型、確定性的 lost-update 教學測試：用明確的同步點讓多條 thread
      先讀到同一個舊值，再各自寫回，驗證更新遺失。這是刻意安排的交錯，證明的是
      「read-modify-write 不是原子的」，它不是原始 `+= 1` 在自然排程下必定失敗的證據。

兩種形狀（定義在 experiments/counter_shapes.py，與 switchpoint_probe.py 共用）：
    run_a:  self.value += 1
    run_b:  self.value = self.value + step()

跑法：
    uv run --no-sync python -X gil=1 -m pytest tests/test_thread_safety.py -v
    uv run --no-sync python -X gil=0 -m pytest tests/test_thread_safety.py -v
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from experiments.counter_shapes import ITERS, THREADS, Counter, run_threads, step

EXPECTED = THREADS * ITERS
SYNC_TIMEOUT = 10.0  # 同步等待的上限（秒）；超時代表測試本身壞了，要讓它失敗而非卡住


def _lost(counter: Counter) -> int:
    return EXPECTED - counter.value


class LockedCounter(Counter):
    """修好版：兩種形狀都把 read-modify-write 整段包進 threading.Lock。
    對應 Rust 的 Mutex（interior mutability），GIL on / GIL off 都正確。"""

    def __init__(self) -> None:
        super().__init__()
        self.lock = threading.Lock()

    def run_a(self, n: int) -> None:
        for _ in range(n):
            with self.lock:
                self.value += 1

    def run_b(self, n: int) -> None:
        for _ in range(n):
            with self.lock:
                self.value = self.value + step()


@pytest.mark.parametrize("shape", ["run_a", "run_b"])
def test_counter_fixed_with_lock(shape: str) -> None:
    """加鎖後兩種形狀在 GIL on / GIL off 下都不丟更新。這是正確性要求，不是觀察。"""
    c = LockedCounter()
    run_threads(getattr(c, shape), THREADS, ITERS)
    assert _lost(c) == 0


def test_staged_interleaving_loses_updates() -> None:
    """刻意安排交錯的 lost update：確定性，不靠排程運氣。

    四條 thread 各自做一次 read-modify-write，但用 Barrier 把「讀」和「寫」分成兩個
    階段：全員都讀到 value == 0 之後，才允許任何人寫回。於是四次 +1 只留下 1。
    這證明 read-modify-write 不是原子的，跟 GIL on / GIL off 無關。

    注意：
      - 這是刻意安排的交錯，不能拿來宣稱原始 `+= 1` 在自然排程下必定失敗。
      - 沒有 sleep、沒有重試、沒有放大迴圈。
      - 等待時沒有持任何互斥鎖；Barrier 有 timeout，worker 的例外透過 Future 傳回測試端。
    """
    n = 4
    counter = Counter()
    everyone_has_read = threading.Barrier(n, timeout=SYNC_TIMEOUT)

    def worker() -> int:
        old = counter.value  # 1. 讀舊值
        everyone_has_read.wait()  # 2. 等全員都讀完（這時沒有人持鎖）
        counter.value = old + 1  # 3. 根據過期的舊值寫回
        return old

    with ThreadPoolExecutor(max_workers=n) as ex:
        futures = [ex.submit(worker) for _ in range(n)]
        olds = [f.result(timeout=SYNC_TIMEOUT) for f in futures]  # 例外在這裡丟回測試端

    assert olds == [0] * n, "每條 thread 都應讀到同一個舊值 0"
    lost = n - counter.value
    assert counter.value == 1
    assert lost == n - 1


def test_staged_interleaving_fixed_by_whole_operation_under_lock() -> None:
    """同樣的交錯意圖，把 read-modify-write 整段放進鎖裡就不會丟更新。

    這裡的 Barrier 放在進入鎖之前（等待時不持鎖），確保四條 thread 真的同時搶同一把鎖，
    而不是序列化地一個做完再換下一個。
    """
    n = 4
    counter = LockedCounter()
    everyone_is_ready = threading.Barrier(n, timeout=SYNC_TIMEOUT)

    def worker() -> None:
        everyone_is_ready.wait()  # 不持鎖時等待
        with counter.lock:
            counter.value += 1  # 讀、改、寫都在鎖內

    with ThreadPoolExecutor(max_workers=n) as ex:
        futures = [ex.submit(worker) for _ in range(n)]
        for f in futures:
            f.result(timeout=SYNC_TIMEOUT)

    assert counter.value == n
