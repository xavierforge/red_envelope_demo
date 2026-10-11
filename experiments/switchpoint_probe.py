"""Switch-point probe: does a Python-level read-modify-write get split?

Variant A:  self.value += 1
            (LOAD_ATTR -> BINARY_OP -> STORE_ATTR, no eval-breaker check between)
Variant B:  self.value = self.value + step()
            (LOAD_ATTR -> CALL step() -> BINARY_OP -> STORE_ATTR; the CALL / RESUME
             of step() is an eval-breaker check point between read and write)

Run one variant per process; GIL mode is fixed at startup with -X gil=0|1.
Standard library only. Not a fix, not a test: a probe.
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
    print(f"expected = {expected}")
    print(f"got      = {got}")
    print(f"lost     = {expected - got}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
