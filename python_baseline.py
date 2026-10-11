"""純 Python 基準線。

忠實鏡像 src/lib.rs 裡 validate_and_process 的計算，讓四列表的比較是公平的
（同樣的工作量）。提供單執行緒與多執行緒兩個版本：

  - 單執行緒：四列表第 1 列（基準 1x）。
  - 多執行緒：四列表第 2 列。CPU-bound 工作在 GIL build 下不會加速（甚至略慢），
    這本身就是第一幕「GIL 讓你的多執行緒對 CPU-bound 無效」的活證據；
    在 free-threaded build 下才會看到加速。

注意：這裡的跨執行緒彙總是安全的（每個 worker 回傳自己的結果，主執行緒再加總），
因為基準線要量的是「時間」，不是要演 race。race 的示範在 tests/test_thread_safety.py。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

MASK = (1 << 64) - 1  # 模擬 Rust u64 的 wrapping 行為


def _rotate_left_64(v: int, n: int) -> int:
    return ((v << n) | (v >> (64 - n))) & MASK


def validate_and_process(amount: int, iterations: int = 1_000_000) -> bool:
    """與 Rust 端逐行對應的 CPU 密集計算。"""
    result = 0
    for i in range(iterations):
        result = (result + i) & MASK
        result = (result * 42) & MASK
        result = (result + amount) & MASK
        result = result ^ (result >> 16)
        if i % 1000 == 0:
            result = _rotate_left_64(result, 7)
    return result % 2 == 0 and amount > 0


def _amounts(n: int = 100) -> list[int]:
    return [(x * 88) % 1000 + 1 for x in range(n)]


def count_valid_single_thread(n: int = 100, iterations: int = 1_000_000) -> int:
    """單執行緒：四列表第 1 列。"""
    return sum(
        1 for amount in _amounts(n) if validate_and_process(amount, iterations)
    )


def count_valid_threads(
    n: int = 100, workers: int = 8, iterations: int = 1_000_000
) -> int:
    """多執行緒：四列表第 2 列。

    用 ThreadPoolExecutor 把工作分給多條執行緒。GIL build 下不會加速；
    free-threaded build 下才會。彙總在主執行緒完成，沒有共享可變狀態的 race。
    """
    amounts = _amounts(n)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda a: validate_and_process(a, iterations), amounts))
    return sum(1 for ok in results if ok)


# ---------------------------------------------------------------------------
# interpreter-bound（直譯器綁定）工作用的共用素材
#
# 這一段是給 src/lib.rs 的 count_valid_interpreter_bound_parallel 當 callback，
# 也給測試當「純 Python 參照值」。放這裡當單一事實來源，benchmark / compare / tests
# 全都 import 同一份，保證三邊算的是同一件事。
# ---------------------------------------------------------------------------
def light_scorer(amount: int, k: int) -> int:
    """輕量的純 Python 評分函式：故意用直譯器跑幾步整數運算（每次很快），
    靠「被呼叫很多次」把 runtime 堆到數秒等級、且完全由 Python 主導。

    這是純函式（同輸入必同輸出），所以不論平行與否都算出同一個值——正確性
    不會因為多執行緒而改變。回傳 0~96 的小整數，讓 Rust 端用 i64 累加不會溢位。
    """
    x = (amount * 2_654_435_761 + k * 40_503) & 0xFFFFFFFF
    x ^= x >> 13
    x = (x * 2_246_822_519) & 0xFFFFFFFF
    x ^= x >> 16
    return x % 97


def _interp_bound_valid(amount: int, calls_per_envelope: int) -> bool:
    """和 Rust 端逐步對應：把 calls_per_envelope 次 light_scorer 的回傳值累加，
    再用 acc 的奇偶（且 amount > 0）決定有效與否。"""
    acc = 0
    for k in range(calls_per_envelope):
        acc += light_scorer(amount, k)
    return acc % 2 == 0 and amount > 0


def count_valid_interpreter_bound_reference(
    n: int = 100, calls_per_envelope: int = 200
) -> int:
    """純 Python 參照值，對應 Rust 的 count_valid_interpreter_bound_parallel。
    測試用它驗證平行版沒算錯（同 n、同 calls_per_envelope 應得同一個值）。"""
    return sum(
        1 for a in _amounts(n) if _interp_bound_valid(a, calls_per_envelope)
    )


if __name__ == "__main__":
    # 小型 smoke test（不是 benchmark）：確認邏輯能跑、單/多執行緒結果一致。
    n, iters = 8, 5_000
    single = count_valid_single_thread(n, iters)
    multi = count_valid_threads(n, workers=4, iterations=iters)
    print(f"single-thread valid count = {single}")
    print(f"multi-thread  valid count = {multi}")
    assert single == multi, "兩者結果應一致（基準線彙總無 race）"
    print("OK: 純 Python 基準線邏輯正確")
