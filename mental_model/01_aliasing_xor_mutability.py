"""概念 1 / 3：Aliasing XOR Mutability（共享與可變，二擇一）

給只用 threading module、不碰 PyO3 的人。純 Python、可直接跑。

Rust 的規則：同一份資料，任一時刻要嘛多個唯讀共享，要嘛唯一可變，不能並存，
而且編譯器強制。Python 完全沒這個閘，所以要你自己在腦裡跑這兩個問題：

    (1) 這份資料有沒有被跨執行緒『共享』(aliased)？
    (2) 它有沒有被『改』(mutated)？

只有「同時共享又改」才會出事。第一幕的三個災難(counter、模組 cache、lazy init)
全是這一種。修復只有三條路，對應底下三個 fix。

跑法：
    python -X gil=1 mental_model/01_aliasing_xor_mutability.py   # race 通常被 GIL 藏住
    python -X gil=0 mental_model/01_aliasing_xor_mutability.py   # race 現形，unsafe 會 < 期望
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

N_THREADS = 8
PER_THREAD = 500_000
EXPECTED = N_THREADS * PER_THREAD


# --- 災難：共享 + 可變，沒有紀律 --------------------------------------------
class SharedCounter:
    def __init__(self) -> None:
        self.value = 0


def disaster() -> int:
    c = SharedCounter()  # 被所有執行緒 alias

    def work() -> None:
        for _ in range(PER_THREAD):
            c.value += 1  # read-modify-write，非原子；同時 alias 又 mutate

    with ThreadPoolExecutor(max_workers=N_THREADS) as ex:
        for _ in range(N_THREADS):
            ex.submit(work)
    return c.value  # free-threaded build 下會 < EXPECTED（lost update）


# --- fix A：不要 alias。每條執行緒自己一份，最後合併（partition then merge） ---
def fix_dont_alias() -> int:
    def work() -> int:
        local = 0  # 每條執行緒的區域變數，沒有人共享它
        for _ in range(PER_THREAD):
            local += 1
        return local

    with ThreadPoolExecutor(max_workers=N_THREADS) as ex:
        parts = list(ex.map(lambda _: work(), range(N_THREADS)))
    return sum(parts)  # 合併在主執行緒，無 race


# --- fix B：不要 mutate。共享不可變資料，唯讀共享天生安全 --------------------
@dataclass(frozen=True)
class Envelope:
    amount: int


def fix_dont_mutate() -> int:
    # 凍結後只能讀不能改，多執行緒同時讀完全安全（對應 tuple / frozenset / frozen dataclass）
    shared = tuple(Envelope(i) for i in range(1000))

    def work() -> int:
        return sum(1 for e in shared if e.amount % 2 == 0)  # 純讀取

    with ThreadPoolExecutor(max_workers=N_THREADS) as ex:
        parts = list(ex.map(lambda _: work(), range(N_THREADS)))
    return parts[0]  # 每條算出來都一樣


# --- fix C：序列化存取。用鎖（見 02_interior_mutability.py 的封裝版） ----------
import threading


def fix_serialize() -> int:
    c = SharedCounter()
    lock = threading.Lock()

    def work() -> None:
        for _ in range(PER_THREAD):
            with lock:  # 對應 Rust 的 Mutex；同時只有一條能改
                c.value += 1

    with ThreadPoolExecutor(max_workers=N_THREADS) as ex:
        for _ in range(N_THREADS):
            ex.submit(work)
    return c.value


if __name__ == "__main__":
    got = disaster()
    print(f"[災難] 共享可變計數器      : {got:>10} / 期望 {EXPECTED}"
          + ("  <-- lost update!" if got != EXPECTED else "  (這次剛好沒露餡，換 -X gil=0 再跑)"))
    print(f"[fixA] 各給一份再合併      : {fix_dont_alias():>10} / 期望 {EXPECTED}")
    print(f"[fixC] 上鎖序列化          : {fix_serialize():>10} / 期望 {EXPECTED}")
    print(f"[fixB] 共享不可變(唯讀)    : {fix_dont_mutate()} 個偶數(每條執行緒一致)")
    print("\n心智模型：先問『共享嗎？被改嗎？』兩者皆是，就從 A/B/C 三條路擇一。")
