# 紅包範例 demo（free-threaded Python × PyO3）

> **What this is**: the act-three demo for a PyCon TW 2026 talk on free-threaded Python seen through Rust's
> aliasing, Send/Sync and interior-mutability rules. A PyO3 + rayon extension, a pure-Python baseline,
> four pure-Python mental-model scripts, and the probes behind the act-one race demos.
> **Requirements**: CPython 3.14 free-threaded build (`python3.14t`), Rust toolchain, `uv`, `maturin`, a multi-core CPU.
> **How to run the comparison**: `uv venv --python 3.14t && uv sync && uv run maturin develop --release`,
> then `uv run --no-sync python compare.py` (about five minutes; it runs gil=1 and gil=0 in subprocesses).
> **Where the measured results live**: `results/` holds every raw output verbatim; `results/compare_2026-10-11.txt`
> is the only source of numbers used in the slides.

這是 PyCon TW 2026 演講「別把 GIL 時代的思維帶進 Free-threaded Python：從 Rust 視角重新理解
Python 並行」第三幕的 demo。骨架沿用《從 Pythonista 到 Rustacean》第 9 章的範例（attach/detach +
rayon + `gil_used = false`），加上一個共享彙總把第二幕的三個 Rust 原理逼出來，以及純 Python 基準線。
議程頁：https://tw.pycon.org/2026/zh-hant/conference/talk/372
投影片：演講後補上

## 檔案與它在敘事裡的位置

| 檔案 | 對應 | 它示範什麼 |
| --- | --- | --- |
| `src/lib.rs` `process_red_envelopes_parallel` | 第三幕第一點 | attach / detach（lock 到 attach 的心智模型） |
| `src/lib.rs` `count_valid_envelopes_parallel` | 第三幕高潮 + 第一幕回馬槍 | 共享計數器：Rust 不給你編譯壞版本（Aliasing XOR Mutability），用 AtomicU64 修好（interior mutability），Py 能跨執行緒因為 Send/Sync |
| `src/lib.rs` `count_valid_native_parallel` | 補洞：對照組 | 「釋放 GIL 跑原生計算」，GIL 時代就有的平行，兩種 build 都加速（見下方「論證的洞」） |
| `src/lib.rs` `count_valid_interpreter_bound_parallel` | 補洞：主角 | 「必須持有直譯器的工作」，只有 free-threaded build 才平行，這才是 free-threading 的紅利 |
| `src/lib.rs` `RedEnvelope.bump_mut` | runtime borrow checker 證據 | `&mut self` 方法，給 `experiments/borrow_checker_probe.py` 用 |
| `src/lib.rs` 檔尾註解 | 第二幕投影片素材 | 壞版本（真實的 E0594 編譯錯誤）/ reduce / Mutex 三種寫法並排 |
| `python_baseline.py` | 四列表第 1、2 列 | 純 Python 單 / 多執行緒；多執行緒在 GIL build 不加速，本身就是第一幕的證據 |
| `benchmark.py` | reviewer concern B | 單一 build 跑出四列對照表 |
| `compare.py` | 四列表定稿 | 同一支直譯器以 gil=1 / gil=0 各開子行程，併成一張表 |
| `tests/test_thread_safety.py` | 第一幕災難可重現 | 五個確定性測試：race 何時露餡取決於讀寫之間有沒有 eval breaker 檢查點 |
| `tests/test_benchmark.py` | 擴充驗證 | 計時 + 給 pytest-run-parallel 壓測用 |
| `experiments/counter_shapes.py` | 第一幕共用元件 | 兩種形狀的計數器（`+= 1` 與夾一個呼叫）與 Barrier 起跑的 runner，probe 和測試共用 |
| `experiments/switchpoint_probe.py` | 切換點探針 | 六格實驗（gil=1/0 × 形狀 A/B × 切換間隔預設/1e-6），附 `--dis` 看 bytecode |
| `experiments/cache_probe.py` | 開場的模組層級快取 | 64 個 key、8 條執行緒走 `if key not in cache: cache[key] = compute(key)`，數重複計算次數，`--fixed` 整段上鎖 |
| `experiments/borrow_checker_probe.py` | runtime borrow checker 探針 | 8 條執行緒同時呼叫 `bump_mut`，統計 `Already borrowed` |
| `experiments/compile_fail/` | 第二幕編譯失敗證據 | 獨立 cargo 專案，刻意編不過，產生 lib.rs 檔尾引用的 E0594 |
| `results/` | 演講引用的數字 | 所有實測輸出原封不動存檔，投影片只從這裡取數 |
| `mental_model/` | 第二幕的 Python 欄 | 四支純 Python snippet，見下方「純 Python 心智模型 snippet」 |

## 論證的洞：free-threading 到底多買到什麼？

一個懂行的人會質疑：`count_valid_envelopes_parallel` 的熱迴圈是**純 Rust 算術**（只讀
`amount`），根本不需要持有直譯器。既然 `detach` 之後再算就能平行，那「釋放 GIL 做原生
計算」在 GIL 時代早就有了（numpy / Polars 二十年來就是這樣），free-threading 買到什麼新東西？

這一版用**兩個函式對照**把洞變成賣點。同樣 100 顆紅包、同樣用 `AtomicU64` 彙總：

- **`count_valid_native_parallel`（對照組）**：先 `attach` 把每顆的 `amount` 讀成 `Vec<u32>`，
  再 `detach`，在「完全不持有直譯器」的狀態下用 rayon 跑純 Rust 計算。因為平行區一次都不碰
  直譯器，**GIL build 與 free-threaded build 都會平行**，「移除 GIL 的加速」約 1x。
  這證明那種加速本來就有。
- **`count_valid_interpreter_bound_parallel(scorer, calls_per_envelope)`（主角）**：每個 worker
  在 `attach` 狀態下、對每顆紅包反覆呼叫一個 Python callback（`scorer`）。呼叫 Python 一定得
  持有直譯器，所以這段工作**無法** detach 出去，**GIL build 序列化、free-threaded 才真平行**。
  這個落差才是 free-threading 獨有的紅利。

`compare.py` 會把兩者「移除 GIL 的加速」並排印出來，一眼看穿差異（見下）。

## 建置

需要 free-threaded Python（`python3.14t`）與多核 CPU 才看得到加速。

```bash
# 安裝 free-threaded Python（擇一）：uv python install 3.14t / pyenv install 3.14t-dev / Homebrew / conda
uv venv --python 3.14t
uv sync
uv run maturin develop --release
```

> ⚠ **建置後執行請加 `--no-sync`**：`uv run python ...` 會在跑之前重新同步環境，把
> `maturin develop` 剛裝好的擴充用快取覆蓋掉（你會發現新函式不見了）。用
> `uv run --no-sync python ...`（或直接 `.venv/bin/python ...`）才會用到剛建好的版本。
> 更糟的情況也遇過：舊版 uv（0.8.x）在 `uv run maturin develop --release` 時把整個 `.venv`
> 重建到另一支非 free-threaded 的 3.14 上。建置完先用
> `uv run --no-sync python -c "import sys; print(sys.version)"` 確認還是 free-threading build。

## 跑出對照表

**推薦：一鍵並排兩種 build**（`compare.py` 會自動以 gil=1 / gil=0 各開子行程、每格 5 次取
中位數 ± 標準差，並把 native 與 interpreter-bound 的「移除 GIL 加速」並列）：

```bash
uv run --no-sync python compare.py
```

**或手動**（單一 build 一張表，跑兩次對照）：

```bash
uv run --no-sync python -X gil=1 benchmark.py   # GIL 啟用
uv run --no-sync python -X gil=0 benchmark.py   # free-threaded
```

四列的意義（時間因機器而異，數字一律以實測為準，別直接寫進投影片當定數）：

| 版本 | GIL build | free-threaded build |
| --- | --- | --- |
| 純 Python（單執行緒） | 基準 1x | 基準 1x |
| 純 Python（多執行緒） | 約不加速（GIL 下 CPU-bound 無效） | 隨核心加速 |
| native（rayon，detach 純算術） | **會加速**（不碰直譯器） | 會加速 |
| interpreter-bound（rayon，呼叫 Python） | **約不加速**（attach 搶 GIL 序列化） | **隨核心加速** |

關鍵對比是最後兩列的「移除 GIL 的加速」（GIL 時間 / free-threaded 時間）：native 約 1x
（那種加速本來就有），interpreter-bound 遠大於 1x（free-threading 才解鎖）。這一組比值就是
整場演講對「free-threading 到底多買到什麼」最直接的數字回答。

### 實測數字（2026-10-11）

量測機器：Apple M1 Pro（6P+2E），macOS 26.6.2，Python 3.14.6 free-threading build，
pyo3 0.29.0，rayon 1.12.0。每格 5 次取中位數 ± 樣本標準差，括號內是相對該欄單執行緒的加速。
完整終端機輸出在 [`results/compare_2026-10-11.txt`](results/compare_2026-10-11.txt)，
這份是投影片引用的唯一數字來源。時間因機器而異，換機器請重跑。

| 版本 | GIL 啟用 | free-threaded | 移除 GIL 的加速 |
| --- | --- | --- | --- |
| 純 Python（單執行緒） | 18.938±0.449s（1.00x） | 19.111±0.168s（1.00x） | 基準 |
| 純 Python（8 執行緒） | 19.057±0.433s（0.99x） | 3.811±0.025s（5.01x） | 約 5.0x |
| native（rayon 8，detach 純算術） | 0.042±0.027s（447.44x） | 0.043±0.005s（441.66x） | 約 1.0x |
| interpreter-bound（rayon 8，呼叫 Python） | 2.096±0.076s（9.04x） | 0.349±0.011s（54.78x） | 約 6.0x |

讀法：native 兩欄幾乎一樣（約 1.0x），那種加速 GIL 時代就有；interpreter-bound 移除 GIL 後快約 6 倍，
這才是 free-threading 解鎖的部分。純 Python 8 執行緒在 GIL build 完全不加速（0.99x），本身就是第一幕的證據。

## 第一幕災難可重現

共享狀態上的 read-modify-write race 一直都存在。GIL build 下會不會露餡，取決於讀和寫之間
有沒有 eval breaker 檢查點：`self.value += 1` 剛好沒有（3.10 起，bpo-29988，檢查點只在
CALL 之後、JUMP_BACKWARD 與 RESUME），所以不會被切開；中間夾一個 Python 呼叫
（`self.value = self.value + step()`）就會。free-threaded build 下兩種形狀都直接露餡。

`tests/test_thread_safety.py` 把這件事寫成五個確定性測試，用 `sys._is_gil_enabled()` 決定
哪些要跑、哪些 skip：

| 測試 | gil=1 | gil=0 |
| --- | --- | --- |
| `test_tight_increment_never_races_under_gil`：`+= 1`，assert lost == 0 | PASS | skip |
| `test_tight_increment_races_free_threaded`：`+= 1`，assert lost > 0 | skip | PASS |
| `test_call_in_window_races_under_gil`：夾呼叫，`setswitchinterval(1e-6)`，assert lost > 0 | PASS | skip |
| `test_call_in_window_races_free_threaded`：夾呼叫，assert lost > 0 | skip | PASS |
| `test_counter_fixed_with_lock[run_a / run_b]`：加鎖後兩種形狀 lost == 0 | PASS | PASS |

```bash
uv run --no-sync python -X gil=1 -m pytest tests/test_thread_safety.py -v   # 4 passed, 2 skipped
uv run --no-sync python -X gil=0 -m pytest tests/test_thread_safety.py -v   # 4 passed, 2 skipped
```

測試用的 `Counter`（`run_a` / `run_b`）、`step()` 與 Barrier 起跑的 runner 都在
`experiments/counter_shapes.py`，與探針共用一份定義。

`experiments/switchpoint_probe.py` 是切換點探針：一次跑一個形狀，用 `--interval` 調切換間隔，
`--dis` 印出兩個迴圈的 bytecode。六格實驗（gil=1/0 × 形狀 A/B × 切換間隔預設/1e-6，每格 5 次）
的原始輸出在 [`results/switchpoint_probe_2026-10-11.txt`](results/switchpoint_probe_2026-10-11.txt)。

```bash
uv run --no-sync python -X gil=1 experiments/switchpoint_probe.py --dis
uv run --no-sync python -X gil=1 experiments/switchpoint_probe.py --variant B --interval 0.000001
uv run --no-sync python -X gil=0 experiments/switchpoint_probe.py --variant A
```

`experiments/cache_probe.py` 是開場那個模組層級字典快取的探針：64 個 key，8 條執行緒 Barrier 起跑後
各自走過全部 key，取值函式就是 `if key not in cache: cache[key] = compute(key)`，`compute` 約 1 ms。
`CONTAINS_OP`（檢查）和 `STORE_SUBSCR`（寫入）之間夾著對 `compute` 的 `CALL`，那就是 eval breaker 的
檢查點，所以 GIL build 用預設 5 ms 切換間隔也會重複計算。`--fixed` 把 check-then-act 整段包進同一把鎖。
原始輸出（含 `--dis` 的 bytecode）在 [`results/cache_probe_2026-10-11.txt`](results/cache_probe_2026-10-11.txt)：
gil=1 預設間隔每次約 10 個重複，1e-6 約 430 個，free-threaded 約 180 到 280 個，`--fixed` 為 0。

```bash
uv run --no-sync python -X gil=1 experiments/cache_probe.py --dis
uv run --no-sync python -X gil=1 experiments/cache_probe.py
uv run --no-sync python -X gil=1 experiments/cache_probe.py --interval 0.000001
uv run --no-sync python -X gil=0 experiments/cache_probe.py
uv run --no-sync python -X gil=0 experiments/cache_probe.py --fixed
```

## runtime borrow checker 探針

PyO3 在 runtime 替 `#[pyclass]` 維護借用旗標。`RedEnvelope.bump_mut(spins)` 的簽章是
`&mut self`，`experiments/borrow_checker_probe.py` 用 8 條執行緒同時呼叫它，統計
`RuntimeError`（PyO3 的 `PyBorrowMutError`，訊息含 `Already borrowed`）。

```bash
uv run --no-sync python -X gil=1 experiments/borrow_checker_probe.py
uv run --no-sync python -X gil=0 experiments/borrow_checker_probe.py
```

2026-10-11 的實測（[`results/borrow_checker_probe_2026-10-11.txt`](results/borrow_checker_probe_2026-10-11.txt)）：
GIL build 8 次全部成功、零錯誤（呼叫被 GIL 序列化）；free-threaded 1 次成功、7 次
`RuntimeError: Already borrowed`。同一份檔案保留了第一次的結果與診斷：原本的自增迴圈會被
release build 當 dead code 刪掉，`&mut self` 只持有約 100 ns，兩種 build 都零錯誤；加上
`std::hint::black_box` 後迴圈才真的跑，借用旗標才被撞到。

## pytest-run-parallel：壓測擴充的執行緒安全（不是 benchmark）

它把同一個測試丟到 thread pool 跑很多遍來逼出執行緒安全 bug，量不出 speedup。
用途是確認 Rust 擴充扛得住多執行緒同時呼叫：

```bash
uv run --no-sync python -X gil=0 -m pytest tests/test_benchmark.py \
    --parallel-threads=auto --iterations=100 --timeout=120
```

預期：功能測試全數在多執行緒下通過（native 與 interpreter-bound 兩個函式都涵蓋），
計時測試（`benchmark_timing`，用 pytest-benchmark fixture）會自動略過，因為那個 fixture 只能
用一次，無法在壓測的重跑下共用。這正是賣點：thread-safe by construction。

## 純 Python 心智模型 snippet（給只用 threading、不碰 PyO3 的人）

`mental_model/` 四支檔案把第二幕每個 Rust 概念配一段**純 Python、可直接跑**的對照，
不需要 Rust 或擴充。這是投影片 side-by-side 的「Python 那一欄」，也是照顧 threading
使用者、回答 reviewer concern C 的核心。每支都示範「不守紀律會怎樣 + 怎麼修」。

```bash
python -X gil=0 mental_model/01_aliasing_xor_mutability.py
python -X gil=0 mental_model/02_interior_mutability.py
python -X gil=0 mental_model/03_send_sync.py
python -X gil=0 mental_model/04_api_shape.py
```

| 檔案 | Rust 概念 | Python 落地（threading 使用者立刻能用） |
| --- | --- | --- |
| `01_aliasing_xor_mutability.py` | Aliasing XOR Mutability | 共享計數器 race + 三修法：各給一份合併 / 凍結唯讀 / 上鎖 |
| `02_interior_mutability.py` | Interior mutability | 把 Lock 封裝進物件（乾淨介面）；改很多次改用分片合併，不硬搶熱鎖 |
| `03_send_sync.py` | Send / Sync | 三段：移交（queue.Queue，put 之後不再碰）/ 每條 thread 自己的資源（threading.local 放資源，task 狀態用區域變數加回傳值合併）/ 共享唯讀（frozen dataclass，並示範 frozen 只是淺層） |
| `04_api_shape.py` | API 形狀（上鎖那條路的三個紀律） | 鎖擁有資料（只在 with 內 yield）/ 只提供完整操作（withdraw 與 update(fn)，get/set 版實跑出超額提款）/ 誠實面對極限（參照逃出 with 仍可改，Rust 的 MutexGuard 有 lifetime） |

四支的原始輸出（01 兩種 build、02 到 04 free-threaded）在 [`results/mental_model_outputs_2026-10-11.txt`](results/mental_model_outputs_2026-10-11.txt)。

這些 snippet 刻意用使用者自己的例子（第一幕的 counter、SQLAlchemy 式的 per-thread、
frozen dataclass）。

## 三原理速查（第二幕到第三幕）

1. Aliasing XOR Mutability：`count += 1` 的壞版本在 Rust 編譯期被擋（見 lib.rs 檔尾與
   `experiments/compile_fail/`）；同一個錯在 Python（test_thread_safety.py）只會默默 race。
2. Send / Sync：`Py<RedEnvelope>` 是 Send+Sync，才能被帶進 rayon 平行區。
3. Interior mutability：用 `AtomicU64`（無鎖）或 `Mutex`（鎖）安全地共享可變計數器，
   對應 Python 的 `threading.Lock`。
