# Unlearning the GIL: Free-threaded Python × PyO3 實戰範例

> **What this is**: Companion repository for the PyCon TW 2026 talk *"Unlearning the GIL in Free-threaded Python: Rethinking Python Concurrency Through a Rust Lens"*. It includes a PyO3 + Rayon native extension, pure-Python baselines, Act I race-condition probes, and Act II pure-Python mental-model scripts.
>
> **Requirements**: CPython 3.14+ free-threaded build (`python3.14t` or `python3.15t`), Rust toolchain, `uv`, `maturin`, and a multi-core CPU.
>
> **Quick Start**:
> ```bash
> uv venv --python 3.14t && uv sync && uv run maturin develop --release
> uv run --no-sync python compare.py  # Takes ~5 mins; runs gil=1 and gil=0 in subprocesses
> ```
>
> **Slides**: [`Unlearning the GIL in Free-threaded Python.pptx`](Unlearning%20the%20GIL%20in%20Free-threaded%20Python.pptx) (slide numbers `S01`-`S65` below refer to this deck).
>
> **Raw Results**: All verbatim benchmark and probe outputs live in `results/`. Every number cited in the slides comes directly from [`results/compare_2026-10-11.txt`](results/compare_2026-10-11.txt).

這是 PyCon TW 2026 議程 **「別把 GIL 時代的思維帶進 Free-threaded Python：從 Rust 視角重新理解 Python 並行」** 的完整實驗與範例程式庫。

專案骨架延伸自《從 Pythonista 到 Rustacean》第 9 章的紅包演算法範例 (`attach` / `detach` + Rayon + `gil_used = false`)，並進一步加入跨執行緒狀態共享、直譯器邊界探針，以及純 Python 的防禦設計對照。

* **議程頁面**：https://tw.pycon.org/2026/zh-hant/conference/talk/372
* **演講投影片**：[`Unlearning the GIL in Free-threaded Python.pptx`](Unlearning%20the%20GIL%20in%20Free-threaded%20Python.pptx) (PowerPoint，共 65 頁，含 9 頁 Backup)

---

## 📋 專案結構與三幕劇導覽

為了讓你快速對照演講內容，本專案的檔案完全對應簡報的「開場與三幕劇」結構：

### 開場與第一幕：GIL 時代的盲點 (`experiments/`)
不需編譯 Rust 擴充，直接重現「有 GIL 時相安無事、關掉 GIL 後全面引爆」的業務邏輯漏洞：

| 檔案 | 對應簡報 | 實驗內容與觀察重點 |
| --- | --- | --- |
| `experiments/cache_probe.py` | 開場 (S03-S04)<br>第一幕 (S14-S15) | **Module-level 快取探針**：8 條 thread 同時走 `if key not in cache: cache[key] = compute(key)`。因 `compute()` 的 `CALL` 觸發週期性切換檢查，連 GIL 開啟時都有約 2% 重複計算；GIL 關閉後暴增至 36-54%。加 `--fixed` 可驗證上鎖後歸零。 |
| `experiments/counter_shapes.py` | 第一幕 (S09-S11) | **兩種長相的計數器**：定義邏輯相同但 bytecode 結構不同的 `run_a` (`self.value += 1`) 與 `run_b` (`self.value = self.value + step()`)，供探針與測試共用。 |
| `experiments/switchpoint_probe.py` | 第一幕 (S10-S12) | **GIL 切換點探針**：在自然排程下執行六宮格實驗 (`gil=1/0` × `形狀 A/B` × `切換間隔 5ms / 1µs`)，支援 `--dis` 檢視 bytecode 的 `JUMP_BACKWARD` 與 `CALL` 位置。 |

### 第二幕：把 Rust 的並行思維帶回 Python (`mental_model/` & `experiments/compile_fail/`)
給純 Python (`threading`) 開發者的防禦實踐，不需要寫 Rust 也能直接套用：

| 檔案 | 對應概念 | Python 實戰落地 |
| --- | --- | --- |
| `mental_model/01_aliasing_xor_mutability.py` | Aliasing XOR Mutability<br>(共享與可變互斥) | 展示共享計數器的 race condition，並對照三條修復路線：**不共享 (各自累加後合併)**、**凍結 (唯讀)**、**上鎖**。 |
| `mental_model/02_interior_mutability.py` | Interior Mutability<br>(內部可變性) | 示範如何將 `Lock` 封裝進物件內部；並展示高頻率修改時如何透過「分片累加最後合併」避免熱鎖競爭 (lock contention)。 |
| `mental_model/03_send_sync.py` | `Send` 與 `Sync`<br>(跨執行緒邊界規則) | 對照三種場景：**移交所有權** (`queue.put` 後徹底放手)、**執行緒局部資源** (`threading.local` 的適用場景與 thread pool 殘留陷阱)、**共享唯讀狀態** (`@dataclass(frozen=True)` 與淺層凍結限制)。 |
| `mental_model/04_api_shape.py` | API 封裝設計<br>(讓正確的寫法最順手) | 以銀行帳戶提款 (`Account.withdraw`) 為例：對比「開放 `get/set` 導致超額提領 30 萬」與「隱藏 `_balance` 只提供完整原子操作」。同時示範為何 Python 的 `with lock() as data:` 無法阻止變數洩漏出作用域。 |
| `experiments/compile_fail/` | 編譯期攔截 (S24, S49) | 獨立的 Cargo 專案，刻意觸發真實的 `E0594` 編譯錯誤 (對應 `src/lib.rs` 檔尾註解)。 |

### 第三幕：Free-threaded 的真實加速區與跨語言邊界 (`src/lib.rs` & 根目錄)
用一把尺 (`Attached` vs. `Detached`) 測量原生套件的加速紅利，並驗證 PyO3 的邊界保護：

| 檔案 / 函式 | 對應簡報 | 角色與設計目的 |
| --- | --- | --- |
| `python_baseline.py` | 四列表第 1, 2 列 | 純 Python 單執行緒與多執行緒 (`ThreadPoolExecutor`) 算術基準線。 |
| `src/lib.rs`<br>`count_valid_native_parallel` | 四列表第 3 列<br>(對照組：Detached) | **純 Rust 算術工作**：讀取資料後立刻 `detach` 脫離直譯器，交由 Rayon 8 核心平行計算。證明此類工作在 GIL 時代就已平行化，關掉 GIL 並不會更快 (同列加速約 `1.0x`)。 |
| `src/lib.rs`<br>`count_valid_interpreter_bound_parallel` | 四列表第 4 列<br>(主角：Attached) | **Rust 呼叫 Python Callback**：8 個 worker 必須維持 `attached` 狀態反覆呼叫 Python 的 `light_scorer()` 函式。GIL 開啟時被迫排隊序列化，GIL 關閉後才真正平行 (同列加速約 `6.0x`)。 |
| `src/lib.rs`<br>`count_valid_envelopes_parallel` | 第三幕 Side 1 (S49-S50) | 示範在 Rayon 平行閉包中，如何用 `AtomicU64` (`fetch_add(&self)`) 通過編譯器的 `Sync` 與借用檢查。 |
| `src/lib.rs`<br>`RedEnvelope.bump_mut` + `experiments/borrow_checker_probe.py` | 第三幕 Side 2 (S51) | **PyO3 執行期借用檢查探針**：8 條 Python thread 同時呼叫簽章為 `&mut self` 的方法，驗證 GIL 關閉時會當場拋出 `RuntimeError: Already borrowed`。 |
| `benchmark.py` / `compare.py` | 第三幕 (S44-S46) | 執行完整實驗並產出對照表 (`compare.py` 會自動開子行程跑完 `gil=1` 與 `gil=0` 並計算中位數)。 |
| `tests/` | 正確性與壓力測試 | 包含確定性的執行緒交錯測試 (`test_thread_safety.py`)、報表防呆邏輯測試 (`test_report_rendering.py`)，以及供 `pytest-run-parallel` 壓測用的 `test_benchmark.py`。 |

---

## 🛠️ 環境建置與執行

本專案需要 **Free-threaded Python (`python3.14t` 或 `3.15t`)** 與多核心 CPU 才能觀察到平行加速與競爭現象。

```bash
# 1. 安裝 free-threaded Python (可透過 uv / pyenv / Homebrew / 官方安裝包)
uv python install 3.14t

# 2. 建立虛擬環境並安裝依賴
uv venv --python 3.14t
uv sync

# 3. 編譯並安裝 Rust (PyO3) 擴充模組
uv run maturin develop --release

```

> ⚠️ **給 `uv` 使用者的重要避坑提醒：建置後執行請務必加上 `--no-sync**`
> 直接執行 `uv run python ...` 時，`uv` 會在啟動前重新同步環境，把 `maturin develop --release` 剛編好的本地擴充用快取覆蓋掉 (你會發現新寫的 Rust 函式突然找不到)。請一律使用 `uv run --no-sync python ...` (或直接呼叫 `.venv/bin/python ...`)。
> 此外，部分舊版 `uv` (如 0.8.x) 在執行 `maturin develop` 時可能會誤將 `.venv` 重建到系統預設的一般版 Python。建置完成後，建議先跑一行指令確認直譯器版本帶有 `free-threading build` 字樣：
> ```bash
> uv run --no-sync python -c "import sys; print(sys.version)"
> 
> ```
> 
> 

---

## 📊 核心實驗：Free-threaded 到底讓哪種工作變快了？

很多人直覺以為拔掉 GIL 後，「Rust / C 原生擴充模組」會跑得更快。但實際上，像 NumPy 或 Polars 這類會在底層釋放 GIL (`detach`) 的純原生運算，二十年前就已經在享受多核平行了。

為了證明 **「真正獲得解放的是必須掛在直譯器上 (Attached) 的工作」**，我們設計了四種跑法進行對照。

### 一鍵跑出 GIL on / GIL off 並排對照表

```bash
# 自動以 -X gil=1 與 -X gil=0 各開子行程，每格連跑 5 次取中位數 ± 標準差
uv run --no-sync python compare.py

```

*(若想單獨觀察單一 GIL 狀態，也可手動執行 `uv run --no-sync python -X gil=1 benchmark.py` 與 `-X gil=0 benchmark.py`。)*

### 實測數據 (Apple M1 Pro, Python 3.14.6t)

* **測試環境**：Apple M1 Pro (6P+2E), macOS 26.6.2, Python 3.14.6t, PyO3 0.29.0, Rayon 1.12.0 (預設 8 workers)。
* **原始輸出存檔**：[`results/compare_2026-10-11.txt`](results/compare_2026-10-11.txt) (簡報 S05、S44-S46 唯一數據來源)。

| Workload (工作負載與執行模式) | GIL on (`-X gil=1`) | GIL off (`-X gil=0`) | 同列加速比 (GIL on / off) | 現象解析 |
| --- | --- | --- | --- | --- |
| **1. 算術 · 純 Python (1 thread)** | `18.938 ± 0.449s` (1.00x) | `19.111 ± 0.168s` (1.00x) | **基準** | 單執行緒基準線。 |
| **2. 算術 · 純 Python (8 threads)** | `19.057 ± 0.433s` (0.99x) | `3.811 ± 0.025s` (5.01x) | **≈ 5.0x** | GIL 開啟時 CPU-bound 執行緒無法平行；關掉 GIL 後獲得 **5 倍加速**。 |
| **3. 算術 · Rust Native (8 workers, Detached)** | `0.042 ± 0.027s` (447.4x) | `0.043 ± 0.005s` (441.7x) | **≈ 1.0x** | 進入 Rayon 前已 `detach` 脫離直譯器。**GIL 開啟時就已跑滿 8 核平行**，關掉 GIL 不會再變快。 |
| **4. Callback · Rust 呼叫 Python (8 workers, Attached)** | `2.096 ± 0.076s` | `0.349 ± 0.011s` | **≈ 6.0x** | 呼叫 Python 函式必須維持 `attached`。GIL 開啟時被迫排隊；關掉 GIL 後解鎖 **6 倍平行加速**。 |

> **💡 數據解讀重點**：
> 1. 兩欄皆使用同一支 `python3.14t` 直譯器，僅透過 `-X gil=1/0` 切換，排除不同編譯配置之干擾。
> 2. **純算術 (第 1-3 列)** 與 **Callback (第 4 列)** 是完全不同的工作負載，因此請只比較**同一列內部的 GIL on / off 加速比**，切勿將第 4 列的秒數拿去除以第 1 列。
> 3. `benchmark.py` 與 `compare.py` 內建嚴格的防呆呈現規則 (由 `tests/test_report_rendering.py` 驗證)：若使用者透過 CLI 參數 (`--envelopes`, `--iterations`) 修改了純 Python 的工作量，使其與寫死在 Rust 內的數量 (100 顆 × 100 萬次) 不同，跨列倍率會自動標示為 `N/A` 以防止誤導。
> 
> 

---

## 💥 重現第一幕與第三幕的執行緒安全現場

### 1. 快取與計數器的切換點探針 (Act I)

在 GIL 開啟時，Read-Modify-Write 的 race condition 會不會暴雷，取決於**讀取與寫回之間有沒有踩到 CPython 的週期性檢查點** (自 Python 3.10 / bpo-29988 起，週期性 eval breaker 檢查設在 `JUMP_BACKWARD`、`CALL` 與 `RESUME`)。

```bash
# 觀察模組層級快取 (if key not in cache: cache[key] = compute(key))
uv run --no-sync python -X gil=1 experiments/cache_probe.py --dis     # 檢視中間夾著 CALL 的 bytecode
uv run --no-sync python -X gil=1 experiments/cache_probe.py           # GIL on (預設 5ms): 每次約 10 筆重複計算
uv run --no-sync python -X gil=0 experiments/cache_probe.py           # GIL off: 暴增至 180~280 筆重複計算
uv run --no-sync python -X gil=0 experiments/cache_probe.py --fixed   # 將完整操作包進 Lock: 0 筆重複

# 觀察兩種計數器長相 (A: += 1 無 CALL | B: + step() 夾一個 CALL)
uv run --no-sync python -X gil=1 experiments/switchpoint_probe.py --dis
uv run --no-sync python -X gil=1 experiments/switchpoint_probe.py --variant B --interval 0.000001
uv run --no-sync python -X gil=0 experiments/switchpoint_probe.py --variant A

```

*(原始實測紀錄存於 [`results/cache_probe_2026-10-11.txt`](results/cache_probe_2026-10-11.txt) 與 [`results/switchpoint_probe_2026-10-11.txt`](results/switchpoint_probe_2026-10-11.txt)。)*

### 2. 確定性的執行緒交錯測試 (`tests/test_thread_safety.py`)

自然排程下的 race condition 受 OS 與 CPU 負載影響。為了在自動化測試中穩定重現，我們使用 `threading.Barrier` 強制讓 4 條 thread 同時讀到同一個舊值再寫回，100% 重現掉失更新，並驗證把完整操作鎖起來的修復效果：

```bash
uv run --no-sync python -X gil=1 -m pytest tests/test_thread_safety.py -v   # 4 passed
uv run --no-sync python -X gil=0 -m pytest tests/test_thread_safety.py -v   # 4 passed

```

### 3. PyO3 執行期借用檢查探針 (Act III Side 2)

PyO3 會在執行期替 `#[pyclass]` 維護借用旗標。當 8 條 Python thread 同時呼叫簽章為 `&mut self` 的 `RedEnvelope.bump_mut()` 時：

```bash
uv run --no-sync python -X gil=1 experiments/borrow_checker_probe.py  # GIL on: 8 成功, 0 失敗 (被 GIL 排隊序列化)
uv run --no-sync python -X gil=0 experiments/borrow_checker_probe.py  # GIL off: 1 成功, 7 拋出 RuntimeError: Already borrowed

```

*(原始實測與 `std::hint::black_box` 防止編譯器消除死迴圈之診斷紀錄存於 [`results/borrow_checker_probe_2026-10-11.txt`](results/borrow_checker_probe_2026-10-11.txt)。)*

### 4. 用 `pytest-run-parallel` 進行並行壓力測試

`pytest-run-parallel` 的用途不是測量加速，而是把同一個單元測試丟進多執行緒池瘋狂重跑上百次，用來逼出潛藏的執行緒安全漏洞：

```bash
uv run --no-sync python -X gil=0 -m pytest tests/test_benchmark.py \
    --parallel-threads=auto --iterations=100 --timeout=120

```

---

## 🧠 純 Python 防禦心法執行指令 (Act II)

不需要安裝 Rust，直接用 Python 執行 `mental_model/` 下的四支腳本，體驗如何把 Rust 的型別安全規則轉化為 Python 的 **直覺反應、API 封裝與文件詞彙**：

```bash
uv run --no-sync python -X gil=0 mental_model/01_aliasing_xor_mutability.py
uv run --no-sync python -X gil=0 mental_model/02_interior_mutability.py
uv run --no-sync python -X gil=0 mental_model/03_send_sync.py
uv run --no-sync python -X gil=0 mental_model/04_api_shape.py

```

*(原始輸出存於 [`results/mental_model_outputs_2026-10-11.txt`](results/mental_model_outputs_2026-10-11.txt) 與 [`results/mental_model_outputs_2026-10-13.txt`](results/mental_model_outputs_2026-10-13.txt)。)*

---

## ✅ 帶回辦公室的「六題自檢表」 (The 6-Question Checklist)

這份清單整理自演講結尾，下次在 Code Review 或準備將專案遷移至 Free-threaded Python 時，可以直接拿這六題來檢查：

### 第一部分：檢查你自己的 Python 程式碼 (保護業務邏輯)

打開 codebase，隨便挑一個 module-level 變數、cache、計數器或資料庫 `Session`，問這三題：

1. **Who else holds this? (還有誰同時拿著它？)**
  * 它會被多條 worker thread、或被第三方套件的平行 callback 同時碰到嗎？


2. **Is anyone changing it? (有沒有人在修改它？)**
* 包含表面像是唯讀、但內部會偷改狀態的操作 (如 lazy initialization、LRU cache 淘汰、iterator 推進)？


3. **If both are YES, which road do we take? (如果前兩題都是 Yes，走哪條路？)**
* **不共享 (Don't Share)**：丟進 `queue.put()` 就放手，或在每個 task 用區域變數累加最後再 merge (切記別在 `ThreadPoolExecutor` 裡用 `threading.local()` 存 task 狀態)。
* **凍結 (Freeze)**：改用 `tuple`、`frozenset`、`@dataclass(frozen=True)` 或 Python 3.15 的 `frozendict` (注意：Python 的凍結皆為 shallow freeze)。
* **封裝完整操作 (Lock the Whole Operation)**：把狀態藏進私有屬性 (如 `_balance`)，不提供零碎的 getter/setter，只對外暴露包在同一把鎖裡的完整方法 (如 `withdraw()`)。



### 第二部分：檢查你的第三方套件與讀懂文件 (評估升級與邊界)

4. **Does the doc's "not thread-safe" mean `!Send` or `!Sync`? (文件說的不安全，是不能搬還是不能共用？)**
* 不能跨 thread 移交 (`!Send`)：例如預設 `check_same_thread=True` 的 `sqlite3.Connection`。
* 能移交但不能同時共用 (`!Sync`)：例如 SQLAlchemy 的 `Session`、共用同一個 `iterator`、或 `asyncio` 的 event loop (必須改走 `call_soon_threadsafe` 通道)。


5. **Can the package run without falling back to the GIL? (套件能不能在無 GIL 下跑？)**
* 檢查 [py-free-threading.github.io](https://py-free-threading.github.io/tracking/) 追蹤表，確認該套件是否已釋出對應的 `cp314t` wheel 或支援 Python 3.15+ 的 `abi3t` stable ABI。


6. **Where does the workload stay attached? (哪一部分的工作掛在直譯器上？)**
* 純 C/Rust 脫離直譯器 (`detached`) 算矩陣的操作本來就已經平行了；真正會在 Free-threaded 下獲得倍數加速的，是**純 Python 多執行緒迴圈**以及**原生套件平行回頭呼叫你 Python callback (`attached`)** 的地方——而那裡，也正是你需要回頭問第 1 到第 3 題的地方。
