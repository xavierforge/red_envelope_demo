"""Switch-point probe: does a Python-level read-modify-write get split?

Variant A:  self.value += 1
            (LOAD_ATTR -> BINARY_OP -> STORE_ATTR; on the CPython 3.14 shown here the
             periodic eval-breaker check does not fall between the read and the write)
Variant B:  self.value = self.value + step()
            (LOAD_ATTR -> CALL step() -> BINARY_OP -> STORE_ATTR; the CALL / RESUME
             of step() is a periodic eval-breaker check point between read and write)

Run one variant per process; GIL mode is fixed at startup with -X gil=0|1.
Standard library only. Not a fix, not a test: a probe of natural scheduling.
The Barrier only makes the threads overlap; it does not pin the interleaving, so
the result is an observation for this machine, version and run. A run with
lost == 0 means "not observed this time", not "the operation is atomic".
The eval-breaker note above covers only the periodic check after the switch
interval expires; other GIL hand-off points (blocking I/O, sleep, C extensions
releasing it) are out of scope.
Counter / step() / the Barrier runner live in counter_shapes.py (shared with
tests/test_thread_safety.py); run this file from the repo root as a script.
"""

import argparse
import dis
import sys

from counter_shapes import ITERS, THREADS, Counter, run_threads


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--variant", choices=["A", "B"])
    p.add_argument("--interval", type=float, default=None,
                   help="sys.setswitchinterval(seconds); omit to keep default")
    p.add_argument("--dis", action="store_true",
                   help="print dis.dis() of both work loops and exit")
    args = p.parse_args()

    if args.dis:
        print("=== dis: Counter.run_a  (self.value += 1) ===")
        dis.dis(Counter.run_a)
        print()
        print("=== dis: Counter.run_b  (self.value = self.value + step()) ===")
        dis.dis(Counter.run_b)
        return 0

    if args.variant is None:
        p.error("--variant is required unless --dis is given")

    if args.interval is not None:
        sys.setswitchinterval(args.interval)   # before any thread exists

    print(f"sys.version          = {sys.version}")
    print(f"sys._is_gil_enabled  = {sys._is_gil_enabled()}")
    print(f"sys.getswitchinterval= {sys.getswitchinterval()}")
    print(f"variant              = {args.variant}")
    print(f"interval (arg)       = {args.interval}")
    print(f"threads x iters      = {THREADS} x {ITERS}")

    counter = Counter()
    run_threads(counter.run_a if args.variant == "A" else counter.run_b, THREADS, ITERS)

    expected = THREADS * ITERS
    got = counter.value
    lost = expected - got
    print(f"expected = {expected}")
    print(f"got      = {got}")
    print(f"lost     = {lost}")
    if lost == 0:
        print("note     = 本次未觀察到丟更新（自然排程的觀察），不代表操作具有 atomic 保證")
    else:
        print("note     = 本次觀察到丟更新；次數受排程與 CPU 資源影響，每次不同")
    return 0


if __name__ == "__main__":
    sys.exit(main())
