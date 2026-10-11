"""一次跑出完整的「GIL 啟用 vs free-threaded」對照表（含 5 次取中位數 ± 誤差）。

用法（在 free-threaded Python 上）：

    uv run --no-sync python compare.py                # 預設每格跑 5 次取中位數
    uv run --no-sync python compare.py --reps 7       # 想更穩就多跑幾次
    uv run --no-sync python compare.py --iterations 20000 --interp-calls 2000  # 冒煙測試

    （--no-sync 很重要：否則 uv run 會用快取覆蓋剛 maturin develop 建好的擴充。）

為什麼要一個 orchestrator？
    GIL 的開關是在「啟動直譯器時」決定的（-X gil=0 / -X gil=1），同一個 process
    內途中無法切換。所以本 script 會用同一支直譯器分別以 gil=1、gil=0 各開一個
    子行程去量測，再把兩邊每列的中位數 ± 樣本標準差併成一張表。

這張表的重點（補上「free-threading 到底多買到什麼」這個洞）：
    - native（rayon）：平行區完全不碰直譯器的原生計算。兩種 build 都會平行 =>
      「移除 GIL 的加速」≈ 1x，代表這種加速 GIL 時代就有（numpy/Polars 那一招）。
    - interpreter-bound（rayon）：平行區內反覆呼叫 Python callback，必須持有直譯器。
      GIL build 序列化、free-threaded 才平行 => 「移除 GIL 的加速」會很大。
    兩列的「移除 GIL 的加速」並排，就一眼看出 free-threading 真正獨有的貢獻。
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import sysconfig
import time
import unicodedata

# 預設參數（和 benchmark.py 對齊，讓兩支程式量的是同一件事）
N_ENVELOPES = 100
ITERATIONS = 1_000_000
PY_THREADS = 8
INTERP_CALLS = 80_000

# 表格的列：(顯示名稱, 量測 key, 是否為 speedup 基準列)
ROW_DEFS = [
    ("純 Python（單執行緒）", "single", True),
    ("純 Python（{threads} 執行緒）", "threads", False),
    ("native（rayon {threads}，detach 純算術）", "native", False),
    ("interpreter-bound（rayon {threads}，呼叫 Python）", "interp", False),
]


# ---------------------------------------------------------------------------
# 顯示寬度對齊（中文全形字在終端機佔 2 欄，len() 會誤算成 1）
# ---------------------------------------------------------------------------
def _dwidth(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _pad(s: str, width: int, right: bool = False) -> str:
    fill = " " * max(0, width - _dwidth(s))
    return fill + s if right else s + fill


# ---------------------------------------------------------------------------
# worker：在被指定的 GIL 模式下量測，輸出 JSON 到 stdout（進度印到 stderr）
# ---------------------------------------------------------------------------
def _measure_once(
    envelopes: int, iterations: int, threads: int, interp_calls: int
) -> dict[str, float | None]:
    import python_baseline as pb

    out: dict[str, float | None] = {}

    start = time.perf_counter()
    pb.count_valid_single_thread(envelopes, iterations)
    out["single"] = time.perf_counter() - start

    start = time.perf_counter()
    pb.count_valid_threads(envelopes, threads, iterations)
    out["threads"] = time.perf_counter() - start

    try:
        import free_threaded_example as fte

        start = time.perf_counter()
        fte.count_valid_native_parallel()
        out["native"] = time.perf_counter() - start

        start = time.perf_counter()
        fte.count_valid_interpreter_bound_parallel(pb.light_scorer, interp_calls)
        out["interp"] = time.perf_counter() - start
    except ImportError:
        out["native"] = None
        out["interp"] = None

    return out


def run_worker(
    reps: int, envelopes: int, iterations: int, threads: int, interp_calls: int
) -> None:
    gil_on = sys._is_gil_enabled() if hasattr(sys, "_is_gil_enabled") else None
    tag = "gil=on " if gil_on else "gil=off"

    keys = ("single", "threads", "native", "interp")
    timings: dict[str, list[float | None]] = {k: [] for k in keys}
    for i in range(reps):
        print(f"  [{tag}] rep {i + 1}/{reps} …", file=sys.stderr, flush=True)
        for key, val in _measure_once(
            envelopes, iterations, threads, interp_calls
        ).items():
            timings[key].append(val)

    print(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "ft_build": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
                "gil_enabled": gil_on,
                "reps": reps,
                "timings": timings,
            }
        )
    )


# ---------------------------------------------------------------------------
# orchestrator：分別以 gil=1 / gil=0 開子行程，收集結果，併成表格
# ---------------------------------------------------------------------------
def _launch(gil_flag: str, args: argparse.Namespace) -> dict:
    cmd = [
        sys.executable,
        "-X",
        f"gil={gil_flag}",
        __file__,
        "--worker",
        "--reps",
        str(args.reps),
        "--iterations",
        str(args.iterations),
        "--envelopes",
        str(args.envelopes),
        "--threads",
        str(args.threads),
        "--interp-calls",
        str(args.interp_calls),
    ]
    # stdout 收 JSON；stderr 直接流到終端機，讓你看到即時進度
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"子行程 (gil={gil_flag}) 失敗，return code {proc.returncode}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _stat(samples: list[float | None]) -> tuple[float, float] | None:
    """回傳 (中位數, 樣本標準差)；若資料缺（擴充未建置）回傳 None。"""
    clean = [s for s in samples if s is not None]
    if not clean:
        return None
    spread = statistics.stdev(clean) if len(clean) > 1 else 0.0
    return statistics.median(clean), spread


def _cell(stat: tuple[float, float] | None, baseline_med: float | None) -> str:
    if stat is None:
        return "未建置"
    med, spread = stat
    speedup = f"{baseline_med / med:.2f}x" if baseline_med and med > 0 else "—"
    return f"{med:.3f}±{spread:.3f}s ({speedup})"


def _gain(on: dict, off: dict, key: str) -> float | None:
    """移除 GIL 的加速 = 同一列 GIL 時間 / free-threaded 時間。"""
    s_on = _stat(on["timings"][key])
    s_off = _stat(off["timings"][key])
    if not s_on or not s_off or s_off[0] <= 0:
        return None
    return s_on[0] / s_off[0]


def main() -> None:
    ap = argparse.ArgumentParser(description="GIL vs free-threaded 完整對照表")
    ap.add_argument(
        "--reps", type=int, default=5, help="每格量測次數（取中位數，預設 5）"
    )
    ap.add_argument(
        "--iterations",
        type=int,
        default=ITERATIONS,
        help="純 Python / native 每顆的運算量",
    )
    ap.add_argument("--envelopes", type=int, default=N_ENVELOPES, help="紅包數量")
    ap.add_argument(
        "--threads", type=int, default=PY_THREADS, help="純 Python 執行緒數"
    )
    ap.add_argument(
        "--interp-calls",
        type=int,
        default=INTERP_CALLS,
        help="interpreter-bound 每顆呼叫 Python callback 幾次",
    )
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.worker:
        run_worker(
            args.reps, args.envelopes, args.iterations, args.threads, args.interp_calls
        )
        return

    if not sysconfig.get_config_var("Py_GIL_DISABLED"):
        print("⚠  目前的直譯器不是 free-threaded build（Py_GIL_DISABLED != 1）。")
        print(
            "   -X gil=0 不會生效，free-threaded 欄會等於 GIL 欄。請改用 python3.14t。\n"
        )

    print(
        f"每格跑 {args.reps} 次取中位數 ± 樣本標準差；分別以 GIL 開 / 關各開一個子行程量測。"
    )
    print(
        "（單執行緒每次約十幾秒，整體可能要數分鐘，下面會顯示即時進度）\n", flush=True
    )

    on = _launch("1", args)
    off = _launch("0", args)

    threads = args.threads
    base_on = _stat(on["timings"]["single"])
    base_off = _stat(off["timings"]["single"])
    base_on_med = base_on[0] if base_on else None
    base_off_med = base_off[0] if base_off else None

    # 標頭
    print()
    print("=" * 82)
    print(
        f"完整對照表 | Python {on['python']} | free-threaded build：{'是' if on['ft_build'] else '否'} "
        f"| 每格 = {args.reps} 次中位數 ± 標準差"
    )
    print(
        f"子行程實測 GIL 狀態 → gil=1：{'啟用' if on['gil_enabled'] else '關閉'}"
        f" ; gil=0：{'啟用' if off['gil_enabled'] else '關閉'} | interp calls/顆 = {args.interp_calls}"
    )
    print("=" * 82)

    headers = ["版本", "GIL 啟用", "free-threaded", "移除 GIL 的加速"]
    table = [headers]
    for name_tpl, key, _is_base in ROW_DEFS:
        name = name_tpl.format(threads=threads)
        cell_on = _cell(_stat(on["timings"][key]), base_on_med)
        cell_off = _cell(_stat(off["timings"][key]), base_off_med)
        gain = _gain(on, off, key)
        gain_str = "—" if (key == "single" or gain is None) else f"約 {gain:.1f}x"
        table.append([name, cell_on, cell_off, gain_str])

    widths = [max(_dwidth(row[i]) for row in table) for i in range(len(headers))]

    def _fmt(cells: list[str]) -> str:
        return " | ".join(_pad(c, widths[i]) for i, c in enumerate(cells))

    print(_fmt(headers))
    print("-" * (sum(widths) + 3 * (len(headers) - 1)))
    for row in table[1:]:
        print(_fmt(row))
    print("=" * 82)
    print(
        "說明：括號 speedup 相對各欄「單執行緒」中位數；移除 GIL 的加速 = GIL 時間 / free-threaded 時間。"
    )

    # ---- 核心對照：native 的 GIL 紅利 vs interpreter-bound 的 GIL 紅利 ----
    g_native = _gain(on, off, "native")
    g_interp = _gain(on, off, "interp")
    print()
    print("GIL 紅利對照（移除 GIL 後，同一段工作快了幾倍）：")
    if g_native is not None:
        print(
            f"  native（原生計算，平行區不碰直譯器）    ：約 {g_native:.1f}x   ← GIL 時代就拿得到"
        )
    if g_interp is not None:
        print(
            f"  interpreter-bound（平行區呼叫 Python）   ：約 {g_interp:.1f}x   ← free-threading 才解鎖"
        )
    print("  解讀：native ≈ 1x 代表那種加速本來就有（釋放 GIL 跑原生計算）；")
    print("        interpreter-bound 遠大於 1x，才是 free-threading 帶來的真正價值。")


if __name__ == "__main__":
    main()
