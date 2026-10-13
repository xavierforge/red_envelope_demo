"""compare.py / benchmark.py 的呈現規則測試。

這裡不量測，用假的量測結果（mock）餵給純呈現函式，驗證的是「倍率該不該印」：
    - 預設工作量：native 列印相對 1 thread 的倍率。
    - 非預設 --iterations / --envelopes：native 列印 N/A 並說明工作量不同。
    - callback 列任何情況都不印相對算術基準的倍率。
    - Python 列用 Python 設定的執行緒數，Rust 兩列用子行程回報的 Rayon pool 大小。
    - 兩個 GIL 子行程 Rayon pool 不同：標示不一致，Rust 兩列的同列加速不解讀。
    - 擴充未建置：顯示未建置，不杜撰 worker 數。
    - 結尾對照只印實測數字；缺量測時不下結論。
純標準庫 + pytest，兩種 GIL 狀態下都能跑。
"""

from __future__ import annotations

import re

import pytest

import benchmark
import compare


# ---------------------------------------------------------------------------
# compare.py：render_report(on, off)
# ---------------------------------------------------------------------------
def _worker_result(
    *,
    gil_enabled: bool,
    envelopes: int = benchmark.N_ENVELOPES,
    iterations: int = benchmark.ITERATIONS,
    threads: int = 8,
    interp_calls: int = benchmark.INTERP_CALLS,
    rust_workers: int | None = 8,
    single: float = 20.0,
    multi: float = 20.0,
    native: float | None = 0.05,
    interp: float | None = 2.0,
) -> dict:
    """模擬 compare.py worker 子行程印出的 JSON。"""
    return {
        "python": "3.14.6",
        "ft_build": True,
        "gil_enabled": gil_enabled,
        "reps": 1,
        "rust_workers": rust_workers,
        "params": {
            "envelopes": envelopes,
            "iterations": iterations,
            "threads": threads,
            "interp_calls": interp_calls,
        },
        "timings": {
            "single": [single],
            "threads": [multi],
            "native": [native],
            "interp": [interp],
        },
    }


def _row(report: str, prefix: str) -> str:
    lines = [ln for ln in report.splitlines() if ln.startswith(prefix)]
    assert len(lines) == 1, f"找不到唯一的列 {prefix!r}"
    return lines[0]


def test_default_workload_shows_native_speedup() -> None:
    on = _worker_result(gil_enabled=True)
    off = _worker_result(gil_enabled=False, multi=4.0, interp=0.4)
    report = compare.render_report(on, off)
    native = _row(report, "算術 workload，Rust native")
    assert "(400.00x)" in native  # 20.0 / 0.05
    assert "N/A" not in native
    assert "約 1.0x" in native


@pytest.mark.parametrize(
    "envelopes, iterations",
    [(100, 20_000), (10, 1_000_000), (10, 20_000)],
)
def test_non_default_workload_hides_native_speedup(envelopes: int, iterations: int) -> None:
    on = _worker_result(gil_enabled=True, envelopes=envelopes, iterations=iterations, single=0.5)
    off = _worker_result(gil_enabled=False, envelopes=envelopes, iterations=iterations, single=0.5)
    report = compare.render_report(on, off)
    native = _row(report, "算術 workload，Rust native")
    assert "(N/A)" in native
    assert not re.search(r"\(\d+\.\d+x\)", native), "工作量不同時不該印跨實作倍率"
    assert "約 1.0x" in native, "同列 GIL on / GIL off 比值仍然成立"
    assert "跨實作倍率不成立" in report
    # 純 Python 兩列仍互相比較
    assert "(1.00x)" in _row(report, "算術 workload，純 Python，1 thread")
    assert "(0.03x)" in _row(report, "算術 workload，純 Python，8 threads")  # 0.5 / 20.0


@pytest.mark.parametrize("iterations", [benchmark.ITERATIONS, 20_000])
def test_callback_never_shows_cross_workload_speedup(iterations: int) -> None:
    on = _worker_result(gil_enabled=True, iterations=iterations)
    off = _worker_result(gil_enabled=False, iterations=iterations, interp=0.4)
    report = compare.render_report(on, off)
    callback = _row(report, "callback workload")
    assert not re.search(r"\d+\.\d+x\)", callback), "callback 列不得有括號倍率"
    assert "N/A" not in callback.split("|")[1]  # 秒數欄只有秒數
    assert "約 5.0x" in callback  # 同列比值 2.0 / 0.4


def test_python_threads_and_rayon_workers_are_labelled_separately() -> None:
    on = _worker_result(gil_enabled=True, threads=3, rust_workers=10)
    off = _worker_result(gil_enabled=False, threads=3, rust_workers=10)
    report = compare.render_report(on, off)
    assert "純 Python，3 threads" in report
    assert "Rust native，10 workers" in report
    assert "callback workload，10 workers" in report
    assert "Rust native，3 workers" not in report
    assert "gil=1 10，gil=0 10" in report


def test_rayon_mismatch_disables_rust_gain_interpretation() -> None:
    on = _worker_result(gil_enabled=True, rust_workers=8)
    off = _worker_result(gil_enabled=False, rust_workers=4, interp=0.4)
    report = compare.render_report(on, off)
    assert "Rayon 設定不一致" in _row(report, "算術 workload，Rust native")
    assert "N/A（Rayon 不一致）" in _row(report, "算術 workload，Rust native")
    assert "N/A（Rayon 不一致）" in _row(report, "callback workload")
    assert "Rust 兩列的比值不做解讀" in report
    assert "本次實測約" not in report
    # 純 Python 列不受影響
    assert "約 1.0x" in _row(report, "算術 workload，純 Python，8 threads")


def test_extension_missing_shows_unbuilt_and_no_conclusion() -> None:
    on = _worker_result(gil_enabled=True, rust_workers=None, native=None, interp=None)
    off = _worker_result(gil_enabled=False, rust_workers=None, native=None, interp=None)
    report = compare.render_report(on, off)
    assert "Rust native，未建置" in report
    assert "callback workload，未建置" in report
    assert "未建置" in _row(report, "算術 workload，Rust native").split("|")[1]
    assert "Rayon workers    ：未建置" in report
    assert "擴充未建置），不下結論" in report
    assert "本次實測約" not in report
    assert not re.search(r"workers", _row(report, "callback workload"))


def test_conclusion_uses_measured_numbers_and_marks_expectation() -> None:
    on = _worker_result(gil_enabled=True, native=0.05, interp=2.0)
    off = _worker_result(gil_enabled=False, native=0.025, interp=2.0)  # 故意與教學預期相反
    report = compare.render_report(on, off)
    assert "Rust native（detach）        ：本次實測約 2.0x" in report
    assert "callback workload（attached，呼叫 Python）  ：本次實測約 1.0x" in report
    assert "教學預期（請以上面的實測數字為準）" in report


def test_report_lists_every_workload_parameter() -> None:
    on = _worker_result(gil_enabled=True, envelopes=10, iterations=20_000, threads=2, interp_calls=300)
    off = _worker_result(gil_enabled=False, envelopes=10, iterations=20_000, threads=2, interp_calls=300)
    report = compare.render_report(on, off)
    assert "10 顆 × 20,000 次，threads=2" in report
    assert f"{benchmark.RUST_ENVELOPES} 顆 × {benchmark.RUST_ITERATIONS:,} 次，寫死在 src/lib.rs" in report
    assert f"{benchmark.RUST_ENVELOPES} 顆 × 300 calls" in report


# ---------------------------------------------------------------------------
# benchmark.py：render_table(rows, ...)
# ---------------------------------------------------------------------------
def _rows(rust_workers: int = 8) -> list[benchmark.Row]:
    return [
        benchmark.Row("算術 workload，純 Python，1 thread", "w", 20.0, 0.0, 44, "python"),
        benchmark.Row("算術 workload，純 Python，8 threads", "w", 4.0, 0.0, 44, "python"),
        benchmark.Row(f"算術 workload，Rust native，{rust_workers} workers（detach）", "w", 0.05, 0.0, 44, "native"),
        benchmark.Row(f"callback workload，{rust_workers} workers（attached，呼叫 Python）", "w", 2.0, 0.0, 58, "callback"),
    ]


def _render(rows, **overrides) -> str:
    kw = dict(
        envelopes=benchmark.N_ENVELOPES,
        iterations=benchmark.ITERATIONS,
        py_threads=8,
        interp_calls=benchmark.INTERP_CALLS,
        rust_workers=8,
        reps=5,
    )
    kw.update(overrides)
    return benchmark.render_table(rows, **kw)


def test_benchmark_default_constants_show_native_speedup() -> None:
    table = _render(_rows())
    assert "400.00x" in _row(table, "算術 workload，Rust native")
    assert "N/A" in _row(table, "callback workload")
    assert "5.00x" in _row(table, "算術 workload，純 Python，8 threads")


@pytest.mark.parametrize(
    "envelopes, iterations", [(100, 20_000), (10, 1_000_000)]
)
def test_benchmark_changed_python_constants_hide_native_speedup(envelopes: int, iterations: int) -> None:
    table = _render(_rows(), envelopes=envelopes, iterations=iterations)
    native = _row(table, "算術 workload，Rust native")
    assert "N/A" in native and "400.00x" not in native
    assert "N/A（native）" in table and "跨實作倍率不成立" in table
    assert "N/A" in _row(table, "callback workload")


def test_benchmark_rows_use_rayon_count_not_py_threads() -> None:
    table = _render(_rows(rust_workers=10), py_threads=3, rust_workers=10)
    assert "Rust native，10 workers" in table
    assert "callback workload，10 workers" in table
    assert "threads=3" in table
    assert "Rayon workers（Rust 兩列實際使用）：10" in table


def test_benchmark_without_extension_reports_unbuilt() -> None:
    table = _render(_rows()[:2], rust_workers=None)
    assert "Rayon workers（Rust 兩列實際使用）：未建置" in table


def test_native_comparable_rule() -> None:
    assert benchmark.native_comparable(benchmark.RUST_ENVELOPES, benchmark.RUST_ITERATIONS)
    assert not benchmark.native_comparable(99, benchmark.RUST_ITERATIONS)
    assert not benchmark.native_comparable(benchmark.RUST_ENVELOPES, 1)
