"""Cache probe: does a module-level dict cache compute the same key twice?

The opening scene's shape, unchanged:

    if key not in cache:
        cache[key] = compute(key)

CONTAINS_OP (the check) and STORE_SUBSCR (the act) are separated by a CALL
to compute(); that CALL is an eval-breaker check point, so even the GIL
build can hand the interpreter to another thread between check and act.
compute() takes about 1 ms of pure Python, which lands inside the default
5 ms switch interval often enough to show duplicates without any tuning.

--fixed wraps the whole check-then-act in one lock; duplicates must be 0.
Run one cell per process; GIL mode is fixed at startup with -X gil=0|1.
Standard library only. Not a fix, not a test: a probe.
"""

import argparse
import dis
import sys
import threading
import time

THREADS = 8
N_KEYS = 64
COMPUTE_ITERS = 25_000  # about 1 ms of pure Python on an M1 Pro (calibrated below)

cache: dict[int, int] = {}
calls = 0
calls_lock = threading.Lock()   # protects the call counter only; the cache stays naked
fix_lock = threading.Lock()     # --fixed: one lock around the whole check-then-act


def compute(key: int) -> int:
    """About 1 ms of pure Python. Counts how many times it was called (race-free)."""
    global calls
    with calls_lock:
        calls += 1
    acc = 0
    for i in range(COMPUTE_ITERS):
        acc = (acc + i * key) & 0xFFFF
    return acc


def get(key: int) -> int:
    if key not in cache:            # check
        cache[key] = compute(key)   # act (with a CALL in between)
    return cache[key]


def get_fixed(key: int) -> int:
    with fix_lock:                  # check and act under the same lock
        if key not in cache:
            cache[key] = compute(key)
        return cache[key]


def worker(fn, barrier: threading.Barrier) -> None:
    barrier.wait()
    for key in range(N_KEYS):
        fn(key)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=None,
                   help="sys.setswitchinterval(seconds); omit to keep default")
    p.add_argument("--fixed", action="store_true",
                   help="use get_fixed (check-then-act under one lock)")
    p.add_argument("--dis", action="store_true",
                   help="print dis.dis() of get() and get_fixed() and exit")
    args = p.parse_args()

    if args.dis:
        print("=== dis: get  (if key not in cache: cache[key] = compute(key)) ===")
        dis.dis(get)
        print()
        print("=== dis: get_fixed  (same, under one lock) ===")
        dis.dis(get_fixed)
        return 0

    if args.interval is not None:
        sys.setswitchinterval(args.interval)   # before any thread exists

    samples = []
    for _ in range(3):                         # calibration calls, not counted below
        t0 = time.perf_counter()
        compute(-1)
        samples.append((time.perf_counter() - t0) * 1e3)
    compute_ms = sorted(samples)[1]
    global calls
    calls = 0

    print(f"sys.version          = {sys.version}")
    print(f"sys._is_gil_enabled  = {sys._is_gil_enabled()}")
    print(f"sys.getswitchinterval= {sys.getswitchinterval()}")
    print(f"interval (arg)       = {args.interval}")
    print(f"fixed                = {args.fixed}")
    print(f"threads x keys       = {THREADS} x {N_KEYS}")
    print(f"compute(key) takes   = {compute_ms:.2f} ms (median of 3 calls, before threads)")

    fn = get_fixed if args.fixed else get
    barrier = threading.Barrier(THREADS)
    ts = [threading.Thread(target=worker, args=(fn, barrier)) for _ in range(THREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    print(f"expected   = {N_KEYS}")
    print(f"calls      = {calls}")
    print(f"duplicates = {calls - N_KEYS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
