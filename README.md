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
| `src/lib.rs` `count_valid_native_parallel` | 補洞：對照組 | 算術 workload，Rust native（detach）：「釋放 GIL 跑原生計算」，GIL on 時就拿得到的平行，GIL on / GIL off 都加速（見下方「論證的洞」） |
| `src/lib.rs` `count_valid_interpreter_bound_parallel` | 補洞：主角 | callback workload（attached，呼叫 Python）：「必須持有直譯器的工作」，只有 GIL off 才平行，這才是 free-threading 的紅利 |
| `src/lib.rs` `RedEnvelope.bump_mut` | runtime borrow checker 證據 | `&mut self` 方法，給 `experiments/borrow_checker_probe.py` 用 |
| `src/lib.rs` 檔尾註解 | 第二幕投影片素材 | 壞版本（真實的 E0594 編譯錯誤）/ reduce / Mutex 三種寫法並排 |
| `python_baseline.py` | 四列表第 1、2 列 | 純 Python 單 / 多執行緒；多執行緒在 GIL on 不加速，本身就是第一幕的證據 |
| `benchmark.py` | reviewer concern B | 單一 GIL 狀態跑出四列對照表，每列標實際工作量與 Rayon worker 數 |
| `compare.py` | 四列表定稿 | 同一支 free-threaded 直譯器以 gil=1 / gil=0 各開子行程（GIL on / GIL off），併成一張表 |
| `tests/test_thread_safety.py` | 第一幕災難可重現 | 加鎖後必須正確的測試，加上一個刻意安排交錯的確定性 lost-update 教學測試 |
| `tests/test_report_rendering.py` | 對照表呈現規則 | 用假量測結果驗證倍率該不該印（N/A 規則、Rayon worker 數、未建置） |
| `tests/test_benchmark.py` | 擴充驗證 | 計時 + 給 pytest-run-parallel 壓測用 |
| `experiments/counter_shapes.py` | 第一幕共用元件 | 兩種形狀的計數器（`+= 1` 與夾一個呼叫）與 Barrier 起跑的 runner，probe 和測試共用，程式形狀保持原樣 |
| `experiments/switchpoint_probe.py` | 切換點探針（自然排程觀察） | 六格實驗（gil=1/0 × 形狀 A/B × 切換間隔預設/1e-6），附 `--dis` 看 bytecode；lost == 0 時印「本次未觀察到」 |
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
  直譯器，**GIL on 與 GIL off 都會平行**，同列加速約 1x。
  這證明那種加速在 GIL on 時就能取得。
- **`count_valid_interpreter_bound_parallel(scorer, calls_per_envelope)`（主角）**：每個 worker
  在 `attach` 狀態下、對每顆紅包反覆呼叫一個 Python callback（`scorer`）。呼叫 Python 一定得
  持有直譯器，所以這段工作**無法** detach 出去，**GIL on 序列化、GIL off 才真平行**。
  這個落差才是 free-threading 獨有的紅利。

這兩個函式是不同的工作（算術 workload 與 callback workload），所以只比各自那一列的
GIL on / GIL off 比值，不拿 callback 列去比純 Python 單執行緒。`compare.py` 會把兩列的
同列加速並排印出來，一眼看穿差異（見下）。

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

**推薦：一鍵並排 GIL on / GIL off**（`compare.py` 會自動以 gil=1 / gil=0 各開子行程、每格 5 次取
中位數 ± 標準差，並把算術 workload（Rust native）與 callback workload 的同列加速並列）：

```bash
uv run --no-sync python compare.py
```

**或手動**（單一 GIL 狀態一張表，跑兩次對照）：

```bash
uv run --no-sync python -X gil=1 benchmark.py   # GIL on
uv run --no-sync python -X gil=0 benchmark.py   # GIL off
```

四列的意義（時間因機器而異，數字一律以實測為準，別直接寫進投影片當定數）：

| workload | GIL on | GIL off |
| --- | --- | --- |
| 算術 workload，純 Python，1 thread | 基準 1x | 基準 1x |
| 算術 workload，純 Python，N threads | 約不加速（GIL 下 CPU-bound 無效） | 隨核心加速 |
| 算術 workload，Rust native，M workers（detach） | **會加速**（不碰直譯器） | 會加速 |
| callback workload，M workers（attached，呼叫 Python） | **約不加速**（attach 搶 GIL 序列化） | **隨核心加速** |

兩欄都是同一個 free-threaded build，以 `-X gil=1/0` 切換，左欄不是一般版 CPython。

教學預期是：最後兩列的同列加速（GIL on 時間 / GIL off 時間）native 列接近 1x（那種加速
GIL on 時就能取得），callback 列遠大於 1x（GIL off 才解鎖）。這一組比值就是整場演講對
「free-threading 到底多買到什麼」最直接的數字回答。`compare.py` 結尾只印本次實測的比值，
教學預期另外標明；擴充未建置或兩個子行程的 Rayon pool 大小不同時，不下結論。

#### 哪個參數影響哪一列（2026-10-13 起的呈現規則）

- `--envelopes`、`--iterations`（`benchmark.py` 的 `N_ENVELOPES`、`ITERATIONS`）只影響純 Python 兩列。
  Rust native 固定處理 100 顆、每顆 1,000,000 次，寫死在 `src/lib.rs`。
- 純 Python 的工作量與 Rust native 不一致時，native 列相對 1 thread 的倍率印 `N/A`，並說明工作量不同；
  純 Python 單、多執行緒仍可互比；每列自己的 GIL on / GIL off 比值仍然成立（同列兩次的工作量一致）。
- callback 列一律不印相對 Python 算術基準的倍率，它是不同的工作。
- `--threads`（`PY_THREADS`）只控制純 Python 的執行緒數，N 就是這個值。Rust 兩列的 M 是各子行程回報的
  Rayon pool 大小（`get_thread_count()`）；兩個子行程回報不同時，標示「Rayon 設定不一致」並停用
  Rust 兩列的同列加速解讀。擴充未安裝時印「未建置」，不杜撰 worker 數。
- 輸出底部列出各列實際工作量與全部參數，結果可追溯。
- 這些規則由 `tests/test_report_rendering.py` 用假量測結果驗證（驗證的是呈現邏輯，不量測）。

### 實測數字（2026-10-11）

量測機器：Apple M1 Pro（6P+2E），macOS 26.6.2，Python 3.14.6 free-threading build，
pyo3 0.29.0，rayon 1.12.0。每格 5 次取中位數 ± 樣本標準差，算術列括號內是相對該欄 1 thread 的加速。
完整終端機輸出在 [`results/compare_2026-10-11.txt`](results/compare_2026-10-11.txt)，
這份是投影片引用的唯一數字來源。時間因機器而異，換機器請重跑。

| workload | GIL on | GIL off | 同列加速 |
| --- | --- | --- | --- |
| 算術 workload，純 Python，1 thread | 18.938±0.449s（1.00x） | 19.111±0.168s（1.00x） | 基準 |
| 算術 workload，純 Python，8 threads | 19.057±0.433s（0.99x） | 3.811±0.025s（5.01x） | 約 5.0x |
| 算術 workload，Rust native，8 workers（detach） | 0.042±0.027s（447.44x） | 0.043±0.005s（441.66x） | 約 1.0x |
| callback workload，8 workers（attached，呼叫 Python） | 2.096±0.076s | 0.349±0.011s | 約 6.0x |

兩欄都是同一個 free-threaded build，以 `-X gil=1/0` 切換，左欄不是一般版 CPython。
callback workload 與算術 workload 是不同的工作，只看同列 GIL on / GIL off 的比值。

讀法：native 列相對 1 thread 的倍率是 447.44x（GIL on）與 441.66x（GIL off）。這個倍率同時包含
原生實作與多執行緒平行的效果，這兩項優勢在 GIL on 時就能取得，而同列開關 GIL 約 1.0x，顯示本次
native workload 沒有額外受惠。callback 列關掉 GIL 後快約 6 倍，這才是 free-threading 解鎖的部分。
純 Python 8 threads 在 GIL on 完全不加速（0.99x），本身就是第一幕的證據。

歷史原始輸出與新版呈現規則的差別：[`results/compare_2026-10-11.txt`](results/compare_2026-10-11.txt)
是 2026-10-11 用當時的程式跑的，原封不動保留。那一版用預設參數（純 Python 與 Rust native 工作量一致，
100 顆 × 1,000,000 次），所以 447.44x 與 441.66x 這兩個倍率成立，本表照用；它的列名與欄名是舊標籤，
Rust 兩列當時寫的 8 是 `--threads` 的值，與本機 Rayon pool 的 8 剛好相同。2026-10-13 起的
`compare.py` 與 `benchmark.py` 改用本表的標籤、印各列實際工作量與 Rayon worker 數，並套用上面的 N/A 規則。

## 第一幕災難可重現

共享狀態上的 read-modify-write race 一直都存在，不論 GIL on 或 GIL off。GIL on 下這一次會不會
露餡，和讀與寫之間有沒有週期性的 eval breaker 檢查有關：在本專案展示的 CPython 3.14 上、這兩條
執行路徑裡，切換間隔到期後的檢查落在 CALL 之後、JUMP_BACKWARD 與 RESUME（bpo-29988 起的設計）。
`self.value += 1` 的讀寫之間沒有這種檢查，中間夾一個 Python 呼叫（`self.value = self.value + step()`）
就有。這段說明只涵蓋週期性檢查這一種交出 GIL 的時機；阻塞 I/O、sleep、C 擴充主動釋放等其他時機
不在範圍內，也不保證其他版本的檢查點位置相同。

自然排程下有沒有丟更新是觀察結果，受排程與 CPU 資源影響。2026-10-11 在本機（3.14.6）的觀察：
GIL on 下 `+= 1` 零丟失、夾呼叫並把切換間隔縮到 1e-6 時穩定丟失；GIL off 下兩種形狀都丟失。
零丟失只代表那次沒觀察到，不代表 `+= 1` 有 atomic 保證。這些觀察放在探針與 `results/`，不放在測試套件裡。

`tests/test_thread_safety.py` 只保留確定性的部分，GIL on / GIL off 都是 4 passed：

| 測試 | 驗證什麼 |
| --- | --- |
| `test_counter_fixed_with_lock[run_a / run_b]` | 加鎖後兩種形狀 lost == 0，正確性要求 |
| `test_staged_interleaving_loses_updates` | 刻意安排交錯：4 條 thread 用 Barrier 先全部讀到同一個舊值再寫回，結果必為 1，丟 3 次更新。證明 read-modify-write 不是原子的；它不是原始 `+= 1` 在自然排程下必定失敗的證據 |
| `test_staged_interleaving_fixed_by_whole_operation_under_lock` | 同樣的交錯意圖，整段放進鎖裡就不丟更新（等待時不持鎖，Barrier 有 timeout，worker 例外傳回測試端） |

```bash
uv run --no-sync python -X gil=1 -m pytest tests/test_thread_safety.py -v   # 4 passed
uv run --no-sync python -X gil=0 -m pytest tests/test_thread_safety.py -v   # 4 passed
```

測試用的 `Counter`（`run_a` / `run_b`）、`step()` 與 Barrier 起跑的 runner 都在
`experiments/counter_shapes.py`，與探針共用一份定義，程式形狀保持原樣，既有實驗仍可重現。

`experiments/switchpoint_probe.py` 是切換點探針，呈現的是自然排程下的觀察：一次跑一個形狀，用
`--interval` 調切換間隔，`--dis` 印出兩個迴圈的 bytecode；lost == 0 時印「本次未觀察到丟更新，
不代表操作具有 atomic 保證」。六格實驗（gil=1/0 × 形狀 A/B × 切換間隔預設/1e-6，每格 5 次）
的原始輸出在 [`results/switchpoint_probe_2026-10-11.txt`](results/switchpoint_probe_2026-10-11.txt)
（那一版的探針還沒有 note 那一行，其餘輸出格式相同）。

```bash
uv run --no-sync python -X gil=1 experiments/switchpoint_probe.py --dis
uv run --no-sync python -X gil=1 experiments/switchpoint_probe.py --variant B --interval 0.000001
uv run --no-sync python -X gil=0 experiments/switchpoint_probe.py --variant A
```

`experiments/cache_probe.py` 是開場那個模組層級字典快取的探針：64 個 key，8 條執行緒 Barrier 起跑後
各自走過全部 key，取值函式就是 `if key not in cache: cache[key] = compute(key)`，`compute` 約 1 ms。
`CONTAINS_OP`（檢查）和 `STORE_SUBSCR`（寫入）之間夾著對 `compute` 的 `CALL`，那是週期性 eval breaker
檢查的位置，所以 GIL on 用預設 5 ms 切換間隔也觀察到重複計算。`--fixed` 把 check-then-act 整段包進同一把鎖。
原始輸出（含 `--dis` 的 bytecode）在 [`results/cache_probe_2026-10-11.txt`](results/cache_probe_2026-10-11.txt)：
GIL on 預設間隔每次約 10 個重複，1e-6 約 430 個，GIL off 約 180 到 280 個，`--fixed` 為 0。

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
GIL on 8 次全部成功、零錯誤（呼叫被 GIL 序列化）；GIL off 1 次成功、7 次
`RuntimeError: Already borrowed`。同一份檔案保留了第一次的結果與診斷：原本的自增迴圈會被
release build 當 dead code 刪掉，`&mut self` 只持有約 100 ns，GIL on / GIL off 都零錯誤；加上
`std::hint::black_box` 後迴圈才真的跑，借用旗標才被撞到。

## pytest-run-parallel：壓測擴充的執行緒安全（不是 benchmark）

它把同一個測試丟到 thread pool 跑很多遍來逼出執行緒安全 bug，量不出 speedup。
用途是確認 Rust 擴充扛得住多執行緒同時呼叫：

```bash
uv run --no-sync python -X gil=0 -m pytest tests/test_benchmark.py \
    --parallel-threads=auto --iterations=100 --timeout=120
```

預期：功能測試全數在多執行緒下通過（native 與 callback 兩個函式都涵蓋），
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
| `04_api_shape.py` | API 形狀（上鎖那條路的三個紀律） | 主角是只提供完整操作（withdraw 與 update(fn)，get/set 版實跑出超額提款）/ 鎖擁有資料的 Mutex 包裝是便利介面（with 不建立作用域，as 綁定的名字在區塊後仍在且沒有持鎖）/ 誠實面對極限（with 之後直接再用 data 跑得過、沒有錯誤，Rust 的 MutexGuard 有 lifetime） |

四支的原始輸出（01 兩種 GIL 狀態、02 到 04 GIL off）在 [`results/mental_model_outputs_2026-10-11.txt`](results/mental_model_outputs_2026-10-11.txt)；
04 在 2026-10-13 措辭修正後的 GIL off 輸出在 [`results/mental_model_outputs_2026-10-13.txt`](results/mental_model_outputs_2026-10-13.txt)。

這些 snippet 刻意用使用者自己的例子（第一幕的 counter、SQLAlchemy 式的 per-thread、
frozen dataclass）。

## 三原理速查（第二幕到第三幕）

1. Aliasing XOR Mutability：`count += 1` 的壞版本在 Rust 編譯期被擋（見 lib.rs 檔尾與
   `experiments/compile_fail/`）；同一個錯在 Python（test_thread_safety.py）只會默默 race。
2. Send / Sync：`Py<RedEnvelope>` 是 Send+Sync，才能被帶進 rayon 平行區。
3. Interior mutability：用 `AtomicU64`（原子操作，不需要 Mutex）或 `Mutex`（鎖）安全地共享可變計數器，
   對應 Python 的 `threading.Lock`。兩者在高競爭下都會競爭同一個位置，atomic 不保證比 Mutex 快；
   各自累加後合併是降低競爭的一種選擇。
