"""Runtime borrow checker 探針：8 條執行緒同時對同一顆 RedEnvelope 呼叫 bump_mut。

bump_mut 的簽章是 &mut self。PyO3 在 runtime 替 #[pyclass] 維護借用旗標：
一條執行緒持有 &mut self 時，另一條執行緒再借就會失敗，PyBorrowMutError 以
RuntimeError（訊息含 "Already borrowed"）浮到 Python。

預期：
    GIL build（-X gil=1）：呼叫被 GIL 序列化，一次只有一條執行緒在 Rust 裡，零錯誤。
    free-threaded（-X gil=0）：多條執行緒真的同時進入，出現 "Already borrowed"。

跑法（先 uv run maturin develop --release 建好擴充）：
    uv run --no-sync python -X gil=1 experiments/borrow_checker_probe.py
    uv run --no-sync python -X gil=0 experiments/borrow_checker_probe.py
"""

from __future__ import annotations

import sys
import threading

import free_threaded_example as fte

THREADS = 8
SPINS = 1_000_000


def main() -> int:
    env = fte.RedEnvelope(7)
    barrier = threading.Barrier(THREADS)
    lock = threading.Lock()
    stats = {"ok": 0, "already_borrowed": 0, "other_error": 0}
    samples: list[str] = []

    def worker() -> None:
        barrier.wait()
        try:
            env.bump_mut(SPINS)
        except RuntimeError as e:
            key = "already_borrowed" if "borrow" in str(e).lower() else "other_error"
            with lock:
                stats[key] += 1
                if len(samples) < 3:
                    samples.append(f"{type(e).__name__}: {e}")
        else:
            with lock:
                stats["ok"] += 1

    ts = [threading.Thread(target=worker) for _ in range(THREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    print(f"sys.version         = {sys.version}")
    print(f"sys._is_gil_enabled = {sys._is_gil_enabled()}")
    print(f"threads x spins     = {THREADS} x {SPINS}")
    print(f"ok                  = {stats['ok']}")
    print(f"already_borrowed    = {stats['already_borrowed']}")
    print(f"other_error         = {stats['other_error']}")
    for line in samples:
        print(f"  sample: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
