"""擴充的功能測試。

兩種跑法：
  - 一般計時（pytest-benchmark）：
        uv run python -X gil=0 -m pytest -s tests/test_benchmark.py
  - 執行緒安全壓測（pytest-run-parallel，把同一測試丟多執行緒跑很多遍）：
        uv run python -X gil=0 -m pytest tests/test_benchmark.py \
            --parallel-threads=auto --iterations=100 --timeout=120
    預期：Rust 擴充全數通過，證明它 thread-safe by construction。
"""

import sys

import pytest

import python_baseline as pb

free_threaded_example = pytest.importorskip(
    "free_threaded_example",
    reason="先建置擴充：uv run maturin develop --release",
)


def _is_parallel_run(config) -> bool:
    """是否正在用 pytest-run-parallel 壓測（--parallel-threads 非 1）。"""
    try:
        val = config.getoption("parallel_threads")
    except ValueError:
        return False
    return str(val) not in ("1", "None")


@pytest.fixture(autouse=True)
def _skip_timing_under_parallel(request):
    """pytest-benchmark 的 benchmark fixture 只能用一次，無法在 run-parallel 的多次
    重跑下共用。計時測試（benchmark_timing）在壓測時略過；真正的執行緒安全由下面
    那些直接呼叫擴充的功能測試負責。"""
    if request.node.get_closest_marker("benchmark_timing") and _is_parallel_run(request.config):
        pytest.skip("計時測試在 pytest-run-parallel 壓測下略過（benchmark fixture 只能用一次）")


@pytest.fixture(scope="session", autouse=True)
def print_env_info():
    print("\n" + "=" * 59)
    print(f"Python version: {sys.version.split()[0]}")
    if hasattr(sys, "_is_gil_enabled"):
        print(f"GIL enabled: {sys._is_gil_enabled()}")
    print(f"Rayon thread count: {free_threaded_example.get_thread_count()}")
    print("=" * 59)
    yield


@pytest.mark.benchmark_timing
def test_parallel_processing(benchmark):
    result = benchmark(free_threaded_example.process_red_envelopes_parallel)
    assert isinstance(result, list)
    assert len(result) == 100


@pytest.mark.benchmark_timing
def test_count_valid(benchmark):
    count = benchmark(free_threaded_example.count_valid_envelopes_parallel)
    assert isinstance(count, int)
    assert 0 <= count <= 100


def test_count_matches_process():
    """彙總計數應等於 process 結果裡 True 的數量（兩條路一致 => 擴充正確）。"""
    flags = free_threaded_example.process_red_envelopes_parallel()
    count = free_threaded_example.count_valid_envelopes_parallel()
    assert count == sum(1 for f in flags if f)


# --- native 版（GIL 時代就有的原生計算平行）---------------------------------

def test_native_matches_original_and_process():
    """native 版與原本的 attach 版、process 版 valid 數必須一致（都走 compute_valid）。"""
    native = free_threaded_example.count_valid_native_parallel()
    original = free_threaded_example.count_valid_envelopes_parallel()
    flags = free_threaded_example.process_red_envelopes_parallel()
    assert native == original == sum(1 for f in flags if f)


def test_native_matches_pure_python_value():
    """native 的 valid 值就是純 Python 單執行緒基準線（100 顆 / 100 萬次）的已知結果 44。
    直接對值，不實跑 ~19 秒的純 Python，以免拖垮 pytest-run-parallel 壓測。"""
    assert free_threaded_example.count_valid_native_parallel() == 44


# --- interpreter-bound 版（free-threading 才解鎖的直譯器綁定工作）-----------

def test_interpreter_bound_matches_pure_python():
    """interpreter-bound 版與純 Python 參照值一致（用小 calls，壓測也跑得快）。"""
    calls = 50
    got = free_threaded_example.count_valid_interpreter_bound_parallel(
        pb.light_scorer, calls
    )
    assert got == pb.count_valid_interpreter_bound_reference(100, calls)


def test_interpreter_bound_deterministic():
    """平行不應改變結果：連跑兩次要一致，且落在合理範圍。"""
    a = free_threaded_example.count_valid_interpreter_bound_parallel(pb.light_scorer, 30)
    b = free_threaded_example.count_valid_interpreter_bound_parallel(pb.light_scorer, 30)
    assert a == b
    assert 0 <= a <= 100


def test_interpreter_bound_propagates_python_exception():
    """Python callback 丟例外時，Rust 端要把它往外傳、不 silently 吞掉。"""
    def boom(amount, k):
        raise ValueError("scorer failed")

    with pytest.raises(ValueError):
        free_threaded_example.count_valid_interpreter_bound_parallel(boom, 5)
