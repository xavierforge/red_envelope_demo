"""單一 GIL 狀態的對照表主程式（每格 5 次取中位數 ± 標準差）。

跑法（在有 free-threaded Python 與多核的機器上）：

  # 1) 先建置 Rust 擴充
  uv run maturin develop --release

  # 2) 兩種 GIL 狀態各跑一次（注意 --no-sync，否則 uv 會用快取覆蓋剛建好的擴充）
  uv run --no-sync python -X gil=1 benchmark.py   # GIL on
  uv run --no-sync python -X gil=0 benchmark.py   # GIL off

兩次都是同一個 free-threaded build，以 -X gil=1 / gil=0 切換；GIL on 那次不是一般版 CPython。

四列的意義：
  1/2. 算術 workload，純 Python 1 thread / PY_THREADS threads（基準線）。
  3.   算術 workload，Rust native（detach）：先讀出資料再 detach，平行區完全不碰直譯器。
       => 這種加速在 GIL on 時就拿得到（numpy/Polars 那一招），是對照組。
  4.   callback workload（attached，呼叫 Python）：每個 worker 在 attach 狀態下重複呼叫
       一個 Python callback，工作本身必須持有直譯器 => GIL on 會序列化，GIL off 才平行。
       這才是 GIL off 獨有的紅利。

工作量與參數（哪個常數影響哪一列）：
  - N_ENVELOPES、ITERATIONS 只影響第 1、2 列（純 Python）。
  - Rust native 固定處理 RUST_ENVELOPES 顆、每顆 RUST_ITERATIONS 次（寫死在 src/lib.rs 的
    make_envelopes(py, 100) 與 compute_valid），Python 這邊改常數不會跟著變。
  - 第 3 列相對 1 thread 的倍率，只在純 Python 的工作量與 Rust native 一致時才有意義；
    不一致時印 N/A。
  - callback 列處理 RUST_ENVELOPES 顆、每顆 INTERP_CALLS 次呼叫。它與算術 workload 是不同的
    工作，一律不印相對 Python 算術基準的倍率，只看 GIL on / GIL off 兩次的同列比值。
  - PY_THREADS 只控制純 Python 的執行緒數；Rust 兩列的 worker 數是 Rayon pool 的實際大小
    （fte.get_thread_count()），兩者可能不同。

想一眼看出「native 列 GIL on / GIL off 差不多」對比「callback 列差很多」，用 compare.py，
它會自動把兩種 GIL 狀態並排跑出比值。本程式只負責印出「當前這一個 GIL 狀態」的表。
"""

from __future__ import annotations

import statistics
import sys
import sysconfig
import time
import unicodedata
from dataclasses import dataclass

import python_baseline as pb

# 參數：可依機器調整（只影響純 Python 兩列，見檔頭說明）
N_ENVELOPES = 100
ITERATIONS = 1_000_000   # 純 Python 每顆紅包的運算量（鏡像 Rust compute_valid 的預設）
PY_THREADS = 8           # 純 Python ThreadPoolExecutor 的執行緒數
INTERP_CALLS = 80_000    # callback workload：每顆紅包呼叫 Python callback 幾次（調到單執行緒數秒）
REPS = 5                 # 每格量測次數，取中位數 ± 標準差

# Rust 端寫死的工作量（src/lib.rs：make_envelopes(py, 100)、compute_valid 的 1_000_000）。
# 這兩個數字描述擴充的行為，改這裡不會改變擴充。
RUST_ENVELOPES = 100
RUST_ITERATIONS = 1_000_000

NA_WORKLOAD = "N/A"  # 工作量不同，跨實作倍率不成立


def native_comparable(envelopes: int, iterations: int) -> bool:
    """純 Python 的工作量與 Rust native 寫死的工作量一致時，native 列才能比 1 thread。"""
    return envelopes == RUST_ENVELOPES and iterations == RUST_ITERATIONS


@dataclass(frozen=True)
class Row:
    name: str
    workload: str
    med: float
    std: float
    result: object
    # "python"：可比 1 thread；"native"：工作量一致時才可比；"callback"：一律不比
    kind: str


def _gil_status() -> str:
    if hasattr(sys, "_is_gil_enabled"):
        return "GIL on" if sys._is_gil_enabled() else "GIL off"
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


def speedup_cell(row: Row, baseline_med: float, comparable: bool) -> str:
    """相對 1 thread 的倍率顯示規則。

    python   ：一律顯示。
    native   ：純 Python 與 Rust native 工作量一致才顯示，否則 N/A。
    callback ：一律 N/A（與算術 workload 是不同的工作）。
    """
    if row.kind == "callback":
        return NA_WORKLOAD
    if row.kind == "native" and not comparable:
        return NA_WORKLOAD
    if row.med <= 0:
        return "—"
    return f"{baseline_med / row.med:.2f}x"


def render_table(
    rows: list[Row],
    *,
    envelopes: int,
    iterations: int,
    py_threads: int,
    interp_calls: int,
    rust_workers: int | None,
    reps: int,
) -> str:
    """把量測結果排成表格。純呈現邏輯，不量測，方便測試。"""
    comparable = native_comparable(envelopes, iterations)
    baseline = rows[0].med
    headers = ["workload", "工作量", "時間(秒)", "speedup", "valid"]
    right = [False, False, True, True, True]
    table = [headers]
    for row in rows:
        table.append(
            [
                row.name,
                row.workload,
                f"{row.med:.3f}±{row.std:.3f}",
                speedup_cell(row, baseline, comparable),
                str(row.result),
            ]
        )
    widths = [max(_dwidth(r[i]) for r in table) for i in range(len(headers))]

    def _fmt(cells: list[str]) -> str:
        return " | ".join(_pad(c, widths[i], right[i]) for i, c in enumerate(cells))

    lines = [_fmt(headers), "-" * (sum(widths) + 3 * (len(headers) - 1))]
    lines += [_fmt(r) for r in table[1:]]
    lines.append("=" * 72)
    lines.append("量測參數（可追溯）：")
    lines.append(
        f"  純 Python 列：envelopes={envelopes}, iterations={iterations}, threads={py_threads}"
    )
    lines.append(
        f"  Rust native 列：envelopes={RUST_ENVELOPES}, iterations={RUST_ITERATIONS}（寫死在 src/lib.rs）"
    )
    lines.append(f"  callback 列：envelopes={RUST_ENVELOPES}, calls/顆={interp_calls}")
    workers = "未建置" if rust_workers is None else str(rust_workers)
    lines.append(f"  Rayon workers（Rust 兩列實際使用）：{workers}；每格 {reps} 次")
    lines.append("說明：本表是同一個 free-threaded build 在單一 GIL 狀態下的結果，GIL on 不是一般版 CPython。")
    lines.append("      speedup 相對第 1 列（純 Python 1 thread）的中位數。")
    if not comparable:
        lines.append(
            f"      {NA_WORKLOAD}（native）：純 Python 的工作量與 Rust native 固定的 "
            f"{RUST_ENVELOPES}×{RUST_ITERATIONS} 不同，跨實作倍率不成立。"
        )
    lines.append(
        f"      {NA_WORKLOAD}（callback）：callback workload 與算術 workload 是不同的工作，"
        "只看 GIL on / GIL off 兩次的同列比值。"
    )
    lines.append("提示：GIL on / GIL off 各跑一次才看得到對照；想一眼並排比值請跑 compare.py。")
    return "\n".join(lines)


def measure() -> tuple[list[Row], int | None]:
    """實際量測四列。回傳 (rows, Rayon worker 數或 None)。"""
    rows: list[Row] = []
    py_workload = f"{N_ENVELOPES} 顆 × {ITERATIONS:,} 次"

    med, std, res = _bench(pb.count_valid_single_thread, N_ENVELOPES, ITERATIONS)
    rows.append(Row("算術 workload，純 Python，1 thread", py_workload, med, std, res, "python"))

    med, std, res = _bench(pb.count_valid_threads, N_ENVELOPES, PY_THREADS, ITERATIONS)
    rows.append(
        Row(f"算術 workload，純 Python，{PY_THREADS} threads", py_workload, med, std, res, "python")
    )

    try:
        import free_threaded_example as fte
    except ImportError:
        print("（未偵測到 Rust 擴充 free_threaded_example，先只跑純 Python 兩列）")
        print("  建置：uv run maturin develop --release，之後用 uv run --no-sync 執行")
        return rows, None

    rust_workers: int = fte.get_thread_count()
    print(f"Rayon 執行緒數（實測）：{rust_workers}")

    med, std, res = _bench(fte.count_valid_native_parallel)
    rows.append(
        Row(
            f"算術 workload，Rust native，{rust_workers} workers（detach）",
            f"{RUST_ENVELOPES} 顆 × {RUST_ITERATIONS:,} 次",
            med, std, res, "native",
        )
    )

    med, std, res = _bench(
        fte.count_valid_interpreter_bound_parallel, pb.light_scorer, INTERP_CALLS
    )
    rows.append(
        Row(
            f"callback workload，{rust_workers} workers（attached，呼叫 Python）",
            f"{RUST_ENVELOPES} 顆 × {INTERP_CALLS:,} calls",
            med, std, res, "callback",
        )
    )
    return rows, rust_workers


def main() -> None:
    print("=" * 72)
    print(f"Python {sys.version.split()[0]} | free-threaded build：{_ft_build()} | {_gil_status()}"
          f" | 每格 {REPS} 次中位數 ± 標準差")
    rows, rust_workers = measure()
    print("=" * 72)
    print(
        render_table(
            rows,
            envelopes=N_ENVELOPES,
            iterations=ITERATIONS,
            py_threads=PY_THREADS,
            interp_calls=INTERP_CALLS,
            rust_workers=rust_workers,
            reps=REPS,
        )
    )


if __name__ == "__main__":
    main()
