"""概念 4：API 形狀（讓正確的用法成為唯一好寫的用法）

這支對應演講第二幕「上鎖」那條路的三個紀律。Rust 的 Mutex<T> 把鎖和資料綁在
一起，拿不到 guard 就碰不到資料，guard 的 lifetime 又讓借用逃不出 with 區塊。
Python 沒有這些型別層的保證，但 API 的形狀可以逼近同樣的效果：

    1. 鎖擁有資料：資料只在 with 區塊內 yield 出來，區塊外拿不到。
    2. 只提供完整操作：檢查與修改在同一把鎖內，呼叫方沒有機會自己拆開組合。
    3. 誠實面對極限：Python 擋得住疏忽，擋不住故意。

每段都有「容易寫錯的版本」與「API 形狀讓它難寫錯的版本」。純標準庫，可直接跑：
    python -X gil=0 mental_model/04_api_shape.py
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Generic, TypeVar

T = TypeVar("T")
N_THREADS = 8
PER_THREAD = 20_000
INITIAL_BALANCE = 100_000


# =============================================================================
# 1. 鎖擁有資料
# =============================================================================
# --- 容易寫錯：鎖和資料分開放，呼叫方要自己記得 with lock ---------------------
class SeparateLockAndData:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.data: dict[str, int] = {"hits": 0}


def separate_lock_misuse() -> int:
    box = SeparateLockAndData()

    def work() -> None:
        for _ in range(PER_THREAD):
            box.data["hits"] += 1  # 忘了 with box.lock，什麼都不會提醒你

    with ThreadPoolExecutor(max_workers=N_THREADS) as ex:
        for _ in range(N_THREADS):
            ex.submit(work)
        ex.shutdown(wait=True)
    return box.data["hits"]


# --- 難寫錯：Mutex 擁有資料，只在 with 區塊內 yield 出來 ----------------------
class Mutex(Generic[T]):
    """鎖擁有資料。唯一拿到資料的路是 with m.lock() as data，離開區塊就沒有名字指向它。
    對應 Rust 的 Mutex<T>：資料包在裡面，lock() 回傳 guard，透過 guard 才碰得到。"""

    def __init__(self, value: T) -> None:
        self._lock = threading.Lock()
        self._value = value

    @contextmanager
    def lock(self) -> Iterator[T]:
        with self._lock:
            yield self._value


def mutex_owns_data() -> int:
    m = Mutex({"hits": 0})

    def work() -> None:
        for _ in range(PER_THREAD):
            with m.lock() as data:  # 不經過這裡就拿不到 data，想忘記上鎖都難
                data["hits"] += 1

    with ThreadPoolExecutor(max_workers=N_THREADS) as ex:
        for _ in range(N_THREADS):
            ex.submit(work)
        ex.shutdown(wait=True)
    with m.lock() as data:
        return data["hits"]


# =============================================================================
# 2. 只提供完整操作
# =============================================================================
# --- 容易寫錯：提供 get / set，讓呼叫方自己組合「檢查再扣款」 ------------------
class LeakyAccount:
    def __init__(self, balance: int) -> None:
        self._lock = threading.Lock()
        self._balance = balance

    def get_balance(self) -> int:
        with self._lock:
            return self._balance

    def set_balance(self, value: int) -> None:
        with self._lock:
            self._balance = value


def leaky_account_overdraft() -> tuple[int, int]:
    """每個方法各自上鎖，但「檢查」和「扣款」是兩次呼叫，中間別人可以插進來。

    每條執行緒一直提 1 元直到帳戶拒絕為止。回傳 (最終餘額, 成功提款次數)。
    成功次數乘上 1 元就是實際付出去的錢；它超過初始餘額的部分就是超額提款。
    """
    acct = LeakyAccount(balance=INITIAL_BALANCE)
    successes = [0] * N_THREADS
    barrier = threading.Barrier(N_THREADS)

    def work(i: int) -> None:
        barrier.wait()
        while True:
            bal = acct.get_balance()  # 檢查
            if bal < 1:
                break
            acct.set_balance(bal - 1)  # 扣款，根據的是一個可能已經過期的 bal
            successes[i] += 1

    ts = [threading.Thread(target=work, args=(i,)) for i in range(N_THREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return acct.get_balance(), sum(successes)


# --- 難寫錯：只提供完整操作，檢查與扣款在同一把鎖內 ---------------------------
class Account:
    """沒有 get_balance / set_balance。呼叫方只能叫 withdraw，拆不開。"""

    def __init__(self, balance: int) -> None:
        self._lock = threading.Lock()
        self._balance = balance

    def withdraw(self, amount: int) -> bool:
        with self._lock:
            if self._balance < amount:
                return False
            self._balance -= amount
            return True

    def update(self, fn: Callable[[int], int]) -> int:
        """fetch_update 的精神：把「讀舊值、算新值、寫回」整段交給呼叫方的函式，
        但函式是在鎖內被呼叫的，所以呼叫方拿到的舊值不會過期。
        對應 Rust 的 AtomicU64::fetch_update。"""
        with self._lock:
            self._balance = fn(self._balance)
            return self._balance

    def balance(self) -> int:
        with self._lock:
            return self._balance


def whole_operation_account() -> tuple[int, int]:
    acct = Account(balance=INITIAL_BALANCE)
    successes = [0] * N_THREADS
    barrier = threading.Barrier(N_THREADS)

    def work(i: int) -> None:
        barrier.wait()
        while acct.withdraw(1):  # 檢查與扣款是同一個原子操作
            successes[i] += 1

    ts = [threading.Thread(target=work, args=(i,)) for i in range(N_THREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return acct.balance(), sum(successes)


# =============================================================================
# 3. 誠實面對極限
# =============================================================================
def guard_escape() -> None:
    """把 with 區塊內 yield 出來的 dict 存到外部變數，離開區塊後仍然改得動。

    Python 的 with 只管「區塊結束時釋放鎖」，管不了你偷偷留下的參照。
    Rust 的 MutexGuard 有 lifetime，從 guard 借出的 &mut T 活不過 guard 本身，
    編譯器會擋。這段示範的就是 Python 的 API 形狀擋得住疏忽，擋不住故意。
    """
    m = Mutex({"hits": 0})
    escaped: dict[str, int] | None = None
    with m.lock() as data:
        escaped = data  # 故意把參照帶出去
    assert escaped is not None
    escaped["hits"] = -1  # 沒有持鎖就改了裡面的資料
    with m.lock() as data:
        print(f"  離開 with 後沒持鎖直接改，鎖內看到的值變成 {data['hits']}")
    print("  Python 擋得住疏忽、擋不住故意：Rust 的 MutexGuard 有 lifetime，借用逃不出 guard。")


if __name__ == "__main__":
    expected = N_THREADS * PER_THREAD
    print("[1. 鎖擁有資料]")
    print(f"  鎖和資料分開放，忘了上鎖： {separate_lock_misuse():>8} / {expected}")
    print(f"  Mutex 擁有資料，with 才拿得到：{mutex_owns_data():>8} / {expected}")

    print(f"[2. 只提供完整操作]（初始餘額 {INITIAL_BALANCE}，每次提 1 元，提到被拒絕為止）")
    bal, ok = leaky_account_overdraft()
    print(f"  get/set 讓呼叫方自己組合：最終餘額 {bal}，成功提款 {ok} 次，超額提款 {ok - INITIAL_BALANCE} 元")
    bal, ok = whole_operation_account()
    print(f"  只提供 withdraw：          最終餘額 {bal}，成功提款 {ok} 次，超額提款 {ok - INITIAL_BALANCE} 元")
    acct = Account(balance=100)
    print(f"  update(fn) 在鎖內套用函式：{acct.update(lambda b: b * 2)}")

    print("[3. 誠實面對極限]")
    guard_escape()

    print("\n心智模型：上鎖那條路的三個紀律。")
    print("  鎖擁有資料：資料只從 with 區塊內拿得到，忘記上鎖這件事就寫不出來。")
    print("  只提供完整操作：check-then-act 整段放進同一把鎖，不給呼叫方拆開的機會。")
    print("  誠實面對極限：這些是靠 API 形狀換來的紀律，Rust 靠的是型別與 lifetime。")
