"""概念 3 / 3：Send / Sync（跨執行緒的搬動與共享保證）

Rust 在型別層設了一道閘：
    Send  = 這東西能不能安全地「搬」到另一條執行緒（移交所有權）。
    Sync  = 這東西能不能用 &T 安全地「共享」給多條執行緒（等價於 &T: Send）。
不安全的型別根本過不了編譯。

Python 沒有這道閘：任何物件都能被丟給任何執行緒，直譯器不會攔你，出事也不報。
所以這個概念帶回 Python 的用法是：在共享前，自己先當那道閘，問一句
「我是要『搬』(Send) 還是要『共享』(Sync)？如果共享，它安全嗎（不可變或有鎖）？」

三段落地，對應 threading 使用者每天會遇到的情境：
    1. 移交（Send）：queue.Queue 做 producer / consumer，put 之後就不再碰。
    2. 每條 thread 自己的資源（owned）：threading.local 放 thread 的資源，
       task 的狀態用區域變數加回傳值合併。
    3. 共享唯讀（Sync）：frozen dataclass，並示範它只是淺層不可變。

跑法：
    python -X gil=0 mental_model/03_send_sync.py
"""

from __future__ import annotations

import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

N_THREADS = 8


# --- 不安全：一個可變、非執行緒安全的物件 -----------------------------------
class UnsafeAccumulator:
    """模擬一個非執行緒安全的資源（想成某個 client、buffer、非安全的連線）。"""

    def __init__(self) -> None:
        self.items: list[int] = []

    def add(self, x: int) -> None:
        # check-then-act：先讀長度再 append，兩步之間可被插入
        n = len(self.items)
        self.items.append(x)
        assert len(self.items) == n + 1  # 被跨執行緒共享時，free-threaded build 下可能不成立


# --- 1. 移交（Send）：producer 建好 job，put 之後就不再碰它 ------------------
def send_style() -> int:
    """用 queue.Queue 把可變的 job 從 producer 移交給 consumer。

    移交之後所有權屬於對方，自己不再存取。這就是 Rust 的 Send 在 Python 裡唯一
    能做的事：靠紀律，沒有編譯器替你擋。job 是個可變的 list，但任一時刻只有
    一條執行緒碰它（producer 建完就放手，consumer 取出後才改），所以不需要鎖。
    """
    jobs: queue.Queue[list[int] | None] = queue.Queue()
    done: list[int] = []
    done_lock = threading.Lock()

    def producer(n_jobs: int) -> None:
        for i in range(n_jobs):
            job = [i * 10_000 + k for k in range(10_000)]  # 可變的 job
            jobs.put(job)  # 放手。從這一行起 producer 不再讀寫 job。
        for _ in range(N_THREADS):
            jobs.put(None)  # 關門訊號

    def consumer() -> None:
        while (job := jobs.get()) is not None:
            job.append(sum(job))  # 取出後它就是我的，改它不需要跟任何人協調
            with done_lock:
                done.append(job[-1])

    consumers = [threading.Thread(target=consumer) for _ in range(N_THREADS)]
    for t in consumers:
        t.start()
    producer(N_THREADS * 4)
    for t in consumers:
        t.join()
    return len(done)


# --- 2. 每條 thread 自己的資源（owned）：threading.local 放的是 thread 的資源 ---
# 這就是「Session per thread」「每條執行緒自己的連線 / RNG」的紀律（呼應第一幕 SQLAlchemy）。
_local = threading.local()


def _get_accumulator() -> UnsafeAccumulator:
    if not hasattr(_local, "acc"):
        _local.acc = UnsafeAccumulator()  # 每條執行緒第一次用時各自建立
    return _local.acc


def owned_style() -> int:
    """每條執行緒自己一份 UnsafeAccumulator，沒有跨執行緒共享，所以不需要鎖。

    threading.local 放的是 thread 的資源（像 connection），生命週期跟著 thread。
    task 的狀態要用區域變數加回傳值合併，因為 ThreadPoolExecutor 會重用 thread：
    同一條 thread 可能接到兩個 chunk，直接拿 len(acc.items) 當這個 task 的結果，
    第二次就會把上一個 chunk 算進去。所以這裡先記 before，最後回傳差值。
    """

    def work(chunk: range) -> int:
        acc = _get_accumulator()  # thread 的資源，可能已經裝著上一個 task 的東西
        before = len(acc.items)  # task 的狀態：區域變數
        for x in chunk:
            acc.add(x)
        return len(acc.items) - before  # 只回報這個 task 做了多少

    chunks = [range(i * 10_000, (i + 1) * 10_000) for i in range(N_THREADS * 4)]
    with ThreadPoolExecutor(max_workers=N_THREADS) as ex:
        parts = list(ex.map(work, chunks))  # 回傳值在主執行緒合併
    return sum(parts)


# --- 3. 共享唯讀（Sync）：要共享，就只共享不可變的東西 -----------------------
@dataclass(frozen=True)
class Config:
    rate: int
    label: str


def sync_style() -> int:
    shared = Config(rate=42, label="lucky")  # 不可變，多執行緒同時讀安全

    def work(chunk: range) -> int:
        return sum(x * shared.rate for x in chunk)  # 純讀 shared

    chunks = [range(i * 1000, (i + 1) * 1000) for i in range(N_THREADS)]
    with ThreadPoolExecutor(max_workers=N_THREADS) as ex:
        parts = list(ex.map(work, chunks))
    return sum(parts)


@dataclass(frozen=True)
class ConfigWithList:
    rate: int
    tags: list[str] = field(default_factory=list)


def shallow_frozen_demo() -> None:
    """frozen 只凍住欄位綁定，欄位指到的 list 仍然可變。

    Rust 的 &T 預設是深層的：拿到 &Config 就改不了裡面任何東西。
    Python 的 frozen dataclass 只擋 `cfg.tags = ...`，擋不住 `cfg.tags.append(...)`。
    要真的共享唯讀，容器欄位得換成 tuple / frozenset 之類的不可變型別。
    """
    cfg = ConfigWithList(rate=42)
    try:
        cfg.rate = 1  # type: ignore[misc]
    except AttributeError as e:
        print(f"  重新綁定欄位被擋：{type(e).__name__}: {e}")
    cfg.tags.append("mutated")  # 不會被擋
    print(f"  對 list 欄位 append 沒被擋：cfg.tags = {cfg.tags}")


if __name__ == "__main__":
    print(f"[移交 Send]      queue.Queue 移交 job，consumer 完成 {send_style()} 件")
    print(f"[owned]          threading.local 每執行緒自己一份，共處理 {owned_style()} 筆")
    print(f"[共享唯讀 Sync]  frozen dataclass 多執行緒同時讀，合計 {sync_style()}")
    print("[淺層不可變]     frozen 只凍住欄位綁定：")
    shallow_frozen_demo()
    print("\n心智模型：共享前先當那道閘。")
    print("  要『移交』給另一條執行緒 -> Send：put 之後就放手，對方取出後才碰，任一時刻只有一方存取。")
    print("  要『每條執行緒自己一份』 -> owned：threading.local 放 thread 的資源，task 的狀態用區域變數加回傳值合併。")
    print("  要『共享』給多條執行緒   -> Sync：只共享不可變資料（注意 frozen 是淺層的），否則就得上鎖（概念 2）。")
    print("  Python 不會替你檢查，所以這道問句要你自己問。")
