"""第一幕的災難，變成五個確定性測試。

共享狀態上的 read-modify-write race 一直都存在，不論哪種 build。
GIL build 下會不會露餡，取決於讀和寫之間有沒有 eval breaker 檢查點：
`self.value += 1` 剛好沒有，所以不會被切開；中間夾一個 Python 呼叫就會。
free-threaded build 下沒有 GIL 可以搶，兩種形狀都直接露餡。

兩種形狀（定義在 experiments/counter_shapes.py，與 switchpoint_probe.py 共用）：
    run_a:  self.value += 1
    run_b:  self.value = self.value + step()

跑法（各一次，看 pass / skip 的組合）：
    uv run --no-sync python -X gil=1 -m pytest tests/test_thread_safety.py -v
    uv run --no-sync python -X gil=0 -m pytest tests/test_thread_safety.py -v

六格原始數據（gil=1/0 x 形狀 A/B x 切換間隔預設/1e-6）見
results/switchpoint_probe_2026-10-11.txt。
"""

from __future__ import annotations

import sys
import threading

import pytest

from experiments.counter_shapes import ITERS, THREADS, Counter, run_threads, step

EXPECTED = THREADS * ITERS

gil_enabled = sys._is_gil_enabled()
only_gil = pytest.mark.skipif(not gil_enabled, reason="需要 GIL build（-X gil=1）")
only_free_threaded = pytest.mark.skipif(gil_enabled, reason="需要 free-threaded（-X gil=0）")


def _lost(counter: Counter) -> int:
    return EXPECTED - counter.value


class LockedCounter(Counter):
    """修好版：兩種形狀都把 read-modify-write 整段包進 threading.Lock。
    對應 Rust 的 Mutex（interior mutability），兩種 build 下都正確。"""

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



@only_gil
def test_tight_increment_never_races_under_gil() -> None:
    """GIL build 下 `self.value += 1` 永遠不會丟更新。

    3.10 起（bpo-29988）eval breaker 只在 CALL 之後、JUMP_BACKWARD 與 RESUME 檢查，
    LOAD_ATTR 到 STORE_ATTR 之間沒有檢查點，所以這段 read-modify-write 不會被
    切開。race 沒有消失，只是這個形狀剛好沒有露餡的機會。
    """
    c = Counter()
    run_threads(c.run_a, THREADS, ITERS)
    assert _lost(c) == 0


@only_free_threaded
def test_tight_increment_races_free_threaded() -> None:
    """free-threaded 下同一段 `self.value += 1` 直接丟更新：沒有 GIL 可以搶，
    8 條執行緒真的同時讀同一個舊值。"""
    c = Counter()
    run_threads(c.run_a, THREADS, ITERS)
    assert _lost(c) > 0


@only_gil
def test_call_in_window_races_under_gil() -> None:
    """GIL build 下，讀和寫之間夾一個 Python 呼叫（step()）就會丟更新。

    CALL / RESUME 是 eval breaker 檢查點，直譯器可以在這裡把 GIL 讓給別條執行緒。
    把切換間隔縮到 1e-6 秒，讓每個檢查點幾乎都觸發切換，結果就穩定露餡。
    """
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        c = Counter()
        run_threads(c.run_b, THREADS, ITERS)
    finally:
        sys.setswitchinterval(old)
    assert _lost(c) > 0


@only_free_threaded
def test_call_in_window_races_free_threaded() -> None:
    """free-threaded 下，夾呼叫的形狀一樣丟更新，而且不需要調切換間隔。"""
    c = Counter()
    run_threads(c.run_b, THREADS, ITERS)
    assert _lost(c) > 0


@pytest.mark.parametrize("shape", ["run_a", "run_b"])
def test_counter_fixed_with_lock(shape: str) -> None:
    """加鎖後兩種形狀在兩種 build 下都不丟更新。"""
    c = LockedCounter()
    run_threads(getattr(c, shape), THREADS, ITERS)
    assert _lost(c) == 0
