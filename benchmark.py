"""單一 GIL 模式的對照表主程式（每格 5 次取中位數 ± 標準差）。

跑法（在有 free-threaded Python 與多核的機器上）：

  # 1) 先建置 Rust 擴充
  uv run maturin develop --release

  # 2) 兩種 GIL 模式各跑一次（注意 --no-sync，否則 uv 會用快取覆蓋剛建好的擴充）
  uv run --no-sync python -X gil=1 benchmark.py   # GIL 啟用
  uv run --no-sync python -X gil=0 benchmark.py   # free-threaded

四列的意義：
  1/2. 純 Python 單/多執行緒（基準線）。
  3.   native（rayon 8）：先讀出資料再 detach，平行區完全不碰直譯器的「原生計算」。
       => 這種加速在 GIL build 就拿得到（numpy/Polars 那一招），是對照組。
  4.   interpreter-bound（rayon 8）：每個 worker 在 attach 狀態下重複呼叫一個 Python
       callback，工作本身必須持有直譯器 => GIL build 會序列化，free-threaded 才平行。
       這才是 free-threading 獨有的紅利。

想一眼看出「native 兩種 build 差不多」對比「interpreter-bound 差很多」，用 compare.py，
它會自動把兩種 build 並排跑出比值。本程式只負責印出「當前這一個 build」的表。
"""

from __future__ import annotations

import statistics
import sys
import sysconfig
import time
import unicodedata

import python_baseline as pb

# 參數：可依機器調整
N_ENVELOPES = 100
ITERATIONS = 1_000_000   # 純 Python / native 每顆紅包的運算量（鏡像 Rust compute_valid）
PY_THREADS = 8           # 純 Python ThreadPoolExecutor 的執行緒數
INTERP_CALLS = 80_000    # interpreter-bound：每顆紅包呼叫 Python callback 幾次（調到單執行緒數秒）
REPS = 5                 # 每格量測次數，取中位數 ± 標準差


def _gil_status() -> str:
    if hasattr(sys, "_is_gil_enabled"):
        return "GIL 啟用" if sys._is_gil_enabled() else "free-threaded（GIL 關閉）"
    return "未知（Python < 3.13，無 _is_gil_enabled）"


def _ft_build() -> str:
    """build 層級：這顆直譯器本身是不是 free-threaded 版本（Py_GIL_DISABLED）。"""
    return "是" if sysconfig.get_config_var("Py_GIL_DISABLED") else "否"


def _dwidth(s: str) -> int:
    """字串在等寬終端機的顯示寬度：CJK 全形字算 2 欄，其餘算 1 欄。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _pad(s: str, width: int, right: bool = False) -> str:
    fill = " " * max(0, width - _dwidth(s))
    return fill + s if right else s + fill


def _bench(fn, *args) -> tuple[float, float, object]:
    """跑 REPS 次，回傳 (中位數, 樣本標準差, 最後一次結果)。"""
    times: list[float] = []
    result: object = None
    for _ in range(REPS):
        start = time.perf_counter()
        result = fn(*args)
        times.append(time.perf_counter() - start)
    med = statistics.median(times)
    std = statistics.stdev(times) if len(times) > 1 else 0.0
    return med, std, result


def main() -> None:
    print("=" * 72)
    print(f"Python {sys.version.split()[0]} | free-threaded build：{_ft_build()} | {_gil_status()}"
          f" | 每格 {REPS} 次中位數 ± 標準差")

    # (顯示名稱, callable) —— 純 Python 兩列一定跑；PyO3 兩列在擴充存在時才跑
    rows: list[tuple[str, float, float, object]] = []

    med, std, res = _bench(pb.count_valid_single_thread, N_ENVELOPES, ITERATIONS)
    rows.append(("純 Python（單執行緒）", med, std, res))

    med, std, res = _bench(pb.count_valid_threads, N_ENVELOPES, PY_THREADS, ITERATIONS)
    rows.append((f"純 Python（{PY_THREADS} 執行緒）", med, std, res))

    try:
        import free_threaded_example as fte

        print(f"Rayon 執行緒數：{fte.get_thread_count()}")

        med, std, res = _bench(fte.count_valid_native_parallel)
        rows.append((f"native（rayon {PY_THREADS}，detach 純算術）", med, std, res))

        med, std, res = _bench(
            fte.count_valid_interpreter_bound_parallel, pb.light_scorer, INTERP_CALLS
        )
        rows.append((f"interpreter-bound（rayon {PY_THREADS}，呼叫 Python）", med, std, res))
    except ImportError:
        print("（未偵測到 Rust 擴充 free_threaded_example，先只跑純 Python 兩列）")
        print("  建置：uv run maturin develop --release，之後用 uv run --no-sync 執行")

    print("=" * 72)

    baseline = rows[0][1]  # 單執行緒中位數
    headers = ["版本", "時間(秒)", "speedup", "valid"]
    right = [False, True, True, True]
    table = [headers]
    for name, med, std, res in rows:
        speedup = f"{baseline / med:.2f}x" if med > 0 else "—"
        table.append([name, f"{med:.3f}±{std:.3f}", speedup, str(res)])

    widths = [max(_dwidth(row[i]) for row in table) for i in range(len(headers))]

    def _fmt(cells: list[str]) -> str:
        return " | ".join(_pad(c, widths[i], right[i]) for i, c in enumerate(cells))

    print(_fmt(headers))
    print("-" * (sum(widths) + 3 * (len(headers) - 1)))
    for row in table[1:]:
        print(_fmt(row))
    print("=" * 72)
    print("提示：兩種 build 各跑一次才看得到對照；想一眼並排比值請跑 compare.py。")


if __name__ == "__main__":
    main()
