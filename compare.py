"""一次跑出完整的「GIL on vs GIL off」對照表（含 5 次取中位數 ± 誤差）。

兩欄都是同一個 free-threaded build，以 -X gil=1 / gil=0 切換；GIL on 那欄不是一般版 CPython。

用法（在 free-threaded Python 上）：

    uv run --no-sync python compare.py                # 預設每格跑 5 次取中位數
    uv run --no-sync python compare.py --reps 7       # 想更穩就多跑幾次
    uv run --no-sync python compare.py --iterations 20000 --interp-calls 2000  # 冒煙測試

    （--no-sync 很重要：否則 uv run 會用快取覆蓋剛 maturin develop 建好的擴充。）

參數影響哪些列（重要，避免誤讀倍率）：
    --envelopes、--iterations  只影響純 Python 兩列。Rust native 固定處理 100 顆、每顆
                               1,000,000 次（寫死在 src/lib.rs），不跟著 CLI 變。
                               兩邊工作量不一致時，native 列相對 1 thread 的倍率印 N/A。
    --threads                  只控制純 Python 的執行緒數。Rust 兩列的 worker 數由各子行程
                               回報的 Rayon pool 大小決定（fte.get_thread_count()）。
    --interp-calls             只影響 callback 列（固定 100 顆，每顆呼叫這麼多次）。
    同列的 GIL on / GIL off 比值不受上述影響：同一列兩次跑的是同樣的工作量。
    callback 列一律不印相對 Python 算術基準的倍率，它是不同的工作。

為什麼要一個 orchestrator？
    GIL 的開關是在「啟動直譯器時」決定的（-X gil=0 / -X gil=1），同一個 process
    內途中無法切換。所以本 script 會用同一支直譯器分別以 gil=1、gil=0 各開一個
    子行程去量測，再把兩邊每列的中位數 ± 樣本標準差併成一張表。

這張表的重點（補上「free-threading 到底多買到什麼」這個洞）：
    - 算術 workload，Rust native（detach）：平行區完全不碰直譯器的原生計算。
      GIL on / GIL off 都會平行 => 教學預期同列比值接近 1x，代表這種加速 GIL on 時
      就拿得到（numpy/Polars 那一招）。
    - callback workload（attached，呼叫 Python）：平行區內反覆呼叫 Python callback，
      必須持有直譯器。GIL on 序列化、GIL off 才平行 => 教學預期同列比值遠大於 1x。
    結尾的對照只印本次實測的數字，教學預期另外標明，缺量測時不下結論。
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

from benchmark import (
    INTERP_CALLS,
    ITERATIONS,
    N_ENVELOPES,
    NA_WORKLOAD,
    PY_THREADS,
    RUST_ENVELOPES,
    RUST_ITERATIONS,
    native_comparable,
)

KEYS = ("single", "threads", "native", "interp")


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


def _rust_workers() -> int | None:
    """擴充存在時回傳 Rayon pool 的實際大小，否則 None（不杜撰 worker 數）。"""
    try:
        import free_threaded_example as fte
    except ImportError:
        return None
    return fte.get_thread_count()


def run_worker(
    reps: int, envelopes: int, iterations: int, threads: int, interp_calls: int
) -> None:
    gil_on = sys._is_gil_enabled() if hasattr(sys, "_is_gil_enabled") else None
    tag = "gil=on " if gil_on else "gil=off"

    timings: dict[str, list[float | None]] = {k: [] for k in KEYS}
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
                "rust_workers": _rust_workers(),
                "params": {
                    "envelopes": envelopes,
                    "iterations": iterations,
                    "threads": threads,
                    "interp_calls": interp_calls,
                },
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


def _cell(stat: tuple[float, float] | None, baseline_med: float | None, mode: str) -> str:
    """mode：'speedup' 印括號倍率；'na' 印 (N/A)；'plain' 只印秒數。"""
    if stat is None:
        return "未建置"
    med, spread = stat
    if mode == "plain":
        return f"{med:.3f}±{spread:.3f}s"
    if mode == "na":
        return f"{med:.3f}±{spread:.3f}s ({NA_WORKLOAD})"
    speedup = f"{baseline_med / med:.2f}x" if baseline_med and med > 0 else "—"
    return f"{med:.3f}±{spread:.3f}s ({speedup})"


def _gain(on: dict, off: dict, key: str) -> float | None:
    """同列加速 = 同一列 GIL on 時間 / GIL off 時間。"""
    s_on = _stat(on["timings"][key])
    s_off = _stat(off["timings"][key])
    if not s_on or not s_off or s_off[0] <= 0:
        return None
    return s_on[0] / s_off[0]


def row_defs(
    py_threads: int, rust_workers: int | None, rayon_consistent: bool, comparable: bool
) -> list[tuple[str, str, str]]:
    """四列的 (顯示名稱, 量測 key, 倍率模式)。

    Python 列顯示 Python 設定的執行緒數；Rust 兩列顯示子行程回報的 Rayon pool 大小。
    native 列只在純 Python 與 Rust 工作量一致時印相對 1 thread 的倍率，否則 N/A。
    callback 列一律只印秒數。
    """
    if rust_workers is None:
        rust = "未建置"
    elif rayon_consistent:
        rust = f"{rust_workers} workers"
    else:
        rust = "Rayon 設定不一致"
    return [
        ("算術 workload，純 Python，1 thread", "single", "speedup"),
        (f"算術 workload，純 Python，{py_threads} threads", "threads", "speedup"),
        (f"算術 workload，Rust native，{rust}（detach）", "native", "speedup" if comparable else "na"),
        (f"callback workload，{rust}（attached，呼叫 Python）", "interp", "plain"),
    ]


def render_report(on: dict, off: dict) -> str:
    """把兩個子行程的 JSON 結果排成完整報告。純呈現邏輯，不量測，方便測試。

    on / off 是 run_worker 印出的 dict：python, ft_build, gil_enabled, reps,
    rust_workers, params{envelopes, iterations, threads, interp_calls}, timings{key: [...]}。
    """
    params = on["params"]
    reps = on["reps"]
    envelopes, iterations = params["envelopes"], params["iterations"]
    py_threads, interp_calls = params["threads"], params["interp_calls"]
    comparable = native_comparable(envelopes, iterations)

    w_on, w_off = on.get("rust_workers"), off.get("rust_workers")
    ext_built = w_on is not None and w_off is not None
    rayon_consistent = ext_built and w_on == w_off

    base_on = _stat(on["timings"]["single"])
    base_off = _stat(off["timings"]["single"])
    base_on_med = base_on[0] if base_on else None
    base_off_med = base_off[0] if base_off else None

    out: list[str] = []
    out.append("=" * 82)
    out.append(
        f"完整對照表 | Python {on['python']} | free-threaded build：{'是' if on['ft_build'] else '否'} "
        f"| 每格 = {reps} 次中位數 ± 標準差"
    )
    out.append(
        f"子行程實測 GIL 狀態 → gil=1：{'GIL on' if on['gil_enabled'] else 'GIL off'}"
        f" ; gil=0：{'GIL on' if off['gil_enabled'] else 'GIL off'}"
    )
    out.append("=" * 82)

    headers = ["workload", "GIL on", "GIL off", "同列加速"]
    table = [headers]
    defs = row_defs(py_threads, w_on if ext_built else None, rayon_consistent, comparable)
    for name, key, mode in defs:
        cell_on = _cell(_stat(on["timings"][key]), base_on_med, mode)
        cell_off = _cell(_stat(off["timings"][key]), base_off_med, mode)
        gain = _gain(on, off, key)
        if key == "single" or gain is None:
            gain_str = "—"
        elif key in ("native", "interp") and not rayon_consistent:
            gain_str = f"{NA_WORKLOAD}（Rayon 不一致）"
        else:
            gain_str = f"約 {gain:.1f}x"
        table.append([name, cell_on, cell_off, gain_str])

    widths = [max(_dwidth(row[i]) for row in table) for i in range(len(headers))]

    def _fmt(cells: list[str]) -> str:
        return " | ".join(_pad(c, widths[i]) for i, c in enumerate(cells))

    out.append(_fmt(headers))
    out.append("-" * (sum(widths) + 3 * (len(headers) - 1)))
    out += [_fmt(row) for row in table[1:]]
    out.append("=" * 82)

    # ---- 各列實際工作量與量測參數（可追溯）----
    out.append("各列實際工作量：")
    out.append(
        f"  純 Python 兩列   ：{envelopes} 顆 × {iterations:,} 次，threads={py_threads}（--envelopes/--iterations/--threads）"
    )
    out.append(
        f"  Rust native 列   ：{RUST_ENVELOPES} 顆 × {RUST_ITERATIONS:,} 次，寫死在 src/lib.rs，不受 CLI 影響"
    )
    out.append(
        f"  callback 列      ：{RUST_ENVELOPES} 顆 × {interp_calls:,} calls（--interp-calls）"
    )
    if not ext_built:
        out.append("  Rayon workers    ：未建置（擴充不存在，Rust 兩列無資料）")
    elif rayon_consistent:
        out.append(f"  Rayon workers    ：gil=1 {w_on}，gil=0 {w_off}（Rust 兩列實際使用）")
    else:
        out.append(
            f"  Rayon workers    ：gil=1 {w_on}，gil=0 {w_off}，設定不一致，Rust 兩列的同列加速不做解讀"
        )
    out.append(f"  每格 {reps} 次；Python {on['python']}")

    out.append("說明：兩欄都是同一個 free-threaded build，以 -X gil=1/0 切換，左欄不是一般版 CPython。")
    out.append("      算術列的括號倍率相對各欄「純 Python 1 thread」的中位數。")
    if not comparable:
        out.append(
            f"      {NA_WORKLOAD}（native）：純 Python 工作量 {envelopes}×{iterations:,} 與 Rust native 固定的 "
            f"{RUST_ENVELOPES}×{RUST_ITERATIONS:,} 不同，跨實作倍率不成立；同列 GIL on / GIL off 仍可比。"
        )
    out.append("      callback workload 與算術 workload 是不同的工作，一律不印相對算術基準的倍率，只看同列比值。")

    # ---- 結尾對照：只印實測；教學預期另外標明 ----
    g_native = _gain(on, off, "native")
    g_interp = _gain(on, off, "interp")
    out.append("")
    out.append("GIL on / GIL off 同列對照（關掉 GIL 後，同一個 workload 快了幾倍）：")
    if not ext_built:
        out.append("  本次沒有 Rust 兩列的量測（擴充未建置），不下結論。")
    elif not rayon_consistent:
        out.append(f"  兩個子行程的 Rayon pool 大小不同（gil=1 {w_on}，gil=0 {w_off}），Rust 兩列的比值不做解讀。")
    else:
        if g_native is not None:
            out.append(f"  算術 workload，Rust native（detach）        ：本次實測約 {g_native:.1f}x")
        if g_interp is not None:
            out.append(f"  callback workload（attached，呼叫 Python）  ：本次實測約 {g_interp:.1f}x")
    out.append("  教學預期（請以上面的實測數字為準）：native 列接近 1x，代表原生實作加多執行緒平行的")
    out.append("  優勢在 GIL on 時就能取得；callback 列遠大於 1x，才是 GIL off 帶來的部分。")
    out.append("  兩列是不同的工作，各看自己的同列比值。")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="GIL on vs GIL off 完整對照表")
    ap.add_argument(
        "--reps", type=int, default=5, help="每格量測次數（取中位數，預設 5）"
    )
    ap.add_argument(
        "--iterations",
        type=int,
        default=ITERATIONS,
        help=f"純 Python 兩列每顆的運算量（Rust native 固定 {RUST_ITERATIONS:,}，不受此參數影響）",
    )
    ap.add_argument(
        "--envelopes",
        type=int,
        default=N_ENVELOPES,
        help=f"純 Python 兩列的紅包數量（Rust 兩列固定 {RUST_ENVELOPES} 顆，不受此參數影響）",
    )
    ap.add_argument(
        "--threads",
        type=int,
        default=PY_THREADS,
        help="純 Python 多執行緒列的執行緒數；Rust 兩列的 worker 數由 Rayon pool 決定，與此無關",
    )
    ap.add_argument(
        "--interp-calls",
        type=int,
        default=INTERP_CALLS,
        help="callback 列每顆呼叫 Python callback 幾次（只影響 callback 列）",
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
            "   -X gil=0 不會生效，GIL off 欄會等於 GIL on 欄。請改用 python3.14t。\n"
        )

    print(
        f"每格跑 {args.reps} 次取中位數 ± 樣本標準差；分別以 GIL on / GIL off 各開一個子行程量測。"
    )
    print(
        "（單執行緒每次約十幾秒，整體可能要數分鐘，下面會顯示即時進度）\n", flush=True
    )

    on = _launch("1", args)
    off = _launch("0", args)

    print()
    print(render_report(on, off))


if __name__ == "__main__":
    main()
