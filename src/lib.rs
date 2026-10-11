use pyo3::prelude::*;
use rayon::prelude::*;
use std::sync::atomic::{AtomicU64, Ordering};

// =============================================================================
// 這個檔案是整場演講第三幕的「落地案例」。每一段都對應第二幕的一個 Rust 原理，
// 註解標了 [幕] 與 [原理]，做投影片時可以直接照著拆。
//
// 三原理回顧（第二幕）：
//   1. Aliasing XOR Mutability：同一份資料，共享與可變二擇一。
//   2. Send / Sync：型別層的「這東西能不能跨執行緒搬動 / 共享」的閘。
//   3. Interior mutability：真的要共享又要改時，唯一正當做法（攤開同步責任）。
//
// 這一版特別補上一個「論證上的洞」：把並行工作拆成兩類函式並排對照——
//   A. count_valid_native_parallel（GIL 時代就能拿到的加速：釋放 GIL 跑原生計算）
//   B. count_valid_interpreter_bound_parallel（free-threading 才解鎖的加速：
//      工作本身必須持有直譯器，GIL build 會序列化）
// 這樣就能回答「你只是多握了不必握的 GIL 吧？」——A 證明那種加速本來就有，
// B 才是 free-threading 真正買到的新東西。
// =============================================================================

/// 紅包驗證的核心純 Rust 算術（約百萬次 wrapping 運算，只吃一個 amount）。
/// 抽成自由函式讓「持有直譯器的方法」與「不持有直譯器的 native 平行」共用同一份
/// 邏輯 —— 這是三個函式能算出一致 valid 值的單一事實來源。
fn compute_valid(amount: u32) -> bool {
    let mut result = 0u64;
    let iterations = 1_000_000;
    for i in 0..iterations {
        result = result.wrapping_add(i);
        result = result.wrapping_mul(42);
        result = result.wrapping_add(amount as u64);
        result = result ^ (result >> 16);
        if i % 1000 == 0 {
            result = result.rotate_left(7);
        }
    }
    result.is_multiple_of(2) && amount > 0
}

// pyo3 0.28 起 #[pymodule] 預設就是 gil_used = false，模組會被標記為
// free-thread-safe，載入 python3.14t 時不會觸發 CPython 自動重啟 GIL。
// 所以這裡不必再手寫 #[pymodule(gil_used = false)]，用預設即可。
#[pymodule]
mod free_threaded_example {
    use super::*;

    #[pyclass]
    struct RedEnvelope {
        amount: u32,
    }

    #[pymethods]
    impl RedEnvelope {
        #[new]
        fn new(amount: u32) -> Self {
            Self { amount }
        }

        /// 模擬紅包驗證的 CPU 密集計算（驗證有效性、計算幸運值等）。
        /// [原理1] 注意簽章是 &self：唯讀共享借用，不可變。
        /// 多條執行緒同時拿 &self 來算是安全的，因為沒有人在改它。
        /// 這正是「共享但不可變 => 天生安全（Sync）」的活例子。
        fn validate_and_process(&self) -> bool {
            compute_valid(self.amount)
        }

        /// [runtime borrow checker 證據] 簽章是 &mut self：對 self.amount 做 spins 次
        /// wrapping 自增再還原，確保有一段時間持有可變借用。
        /// PyO3 的 #[pyclass] 在 runtime 用 borrow flag 實作 Rust 的借用規則：
        /// 已經有 &mut self 在身上時，第二條執行緒再呼叫會拿到 PyBorrowMutError，
        /// 以 RuntimeError（"Already borrowed"）浮到 Python。見 experiments/borrow_checker_probe.py。
        fn bump_mut(&mut self, spins: u32) {
            let original = self.amount;
            for _ in 0..spins {
                // black_box 讓 release build 不能把這個迴圈當 dead code 刪掉。
                self.amount = std::hint::black_box(self.amount.wrapping_add(1));
            }
            std::hint::black_box(self.amount);
            self.amount = original;
        }
    }

    fn make_envelopes(py: Python<'_>, n: u32) -> Vec<Py<RedEnvelope>> {
        (0..n)
            .map(|x| {
                let amount = (x * 88) % 1000 + 1; // 金額 1~999
                Py::new(py, RedEnvelope::new(amount)).unwrap()
            })
            .collect()
    }

    /// [幕3 主秀] 平行驗證紅包，回傳每個紅包的有效與否。
    /// 這段演的是 attach / detach 心智模型（對應第三幕第一點的 lock -> attach）：
    ///   - 外層 Python::attach：接上直譯器，建立 Python 物件。
    ///   - outer_py.detach：退出直譯器，進入「不持有直譯器」的平行區，rayon 才能真平行。
    ///   - 每個 worker 內層 Python::attach：要碰 Py 物件時再各自接上。
    /// 在 GIL build 上，內層 attach 等於搶 GIL，worker 們會在計算期間被序列化（沒有加速）；
    /// 在 free-threaded build 上，attach 不搶全域鎖，才真的平行 => 這就是兩個世界的差異。
    #[pyfunction]
    fn process_red_envelopes_parallel() -> PyResult<Vec<bool>> {
        let results: Vec<bool> = Python::attach(|outer_py| {
            let envelopes = make_envelopes(outer_py, 100);
            // [原理2] envelopes: Vec<Py<RedEnvelope>>。Py<T> 是 Send + Sync，
            // 所以它能被帶進下面的 detach 平行區、被 rayon 跨執行緒使用。
            // 如果換成某個 !Send/!Sync 的型別，這裡會直接編譯不過 => Send/Sync 在替你把關。
            outer_py.detach(|| {
                envelopes
                    .par_iter()
                    .map(|envelope| {
                        Python::attach(|inner_py: Python<'_>| {
                            envelope.borrow(inner_py).validate_and_process()
                        })
                    })
                    .collect()
            })
        });
        Ok(results)
    }

    /// [幕3 高潮 = 第一幕 counter 的回馬槍] 平行統計「有幾個紅包有效」。
    /// 這就是第一幕 `counter += 1` 的同一隻共享計數器，只是搬到 Rust。
    ///
    /// 注意：這一版每個 worker 是在「attach 狀態下」跑那段純 Rust 算術。它跟下面的
    /// native 版是刻意的對照——native 版證明「這種算術根本不需要持有直譯器」，
    /// 兩者的 valid 值必須一致（都走 compute_valid）。
    ///
    /// [原理1] 為什麼不能直接寫 `let mut count = 0; ... count += 1;`？
    /// 因為 rayon 的 for_each 要求閉包是 Fn（會被多執行緒同時呼叫），
    /// 而在多執行緒閉包裡可變借用同一個 count，違反 Aliasing XOR Mutability，
    /// 借用檢查器會在「編譯期」直接擋下。對照第一幕：Python 讓同樣的錯默默 race，
    /// Rust 連編都不讓你編。教學用的「壞版本」見本檔最下方註解。
    ///
    /// [原理3] 這裡用 AtomicU64：interior mutability 的一種，完全不用鎖。
    /// 「改很多次」的共享計數器，用 atomic 比用 Mutex 更省（無鎖、無競爭點）。
    #[pyfunction]
    fn count_valid_envelopes_parallel() -> PyResult<u64> {
        let count = Python::attach(|outer_py| {
            let envelopes = make_envelopes(outer_py, 100);
            // [原理3] AtomicU64 是 Sync，所以 &AtomicU64 是 Send，閉包捕捉它之後才能跨執行緒。
            let valid_count = AtomicU64::new(0);

            outer_py.detach(|| {
                envelopes.par_iter().for_each(|envelope| {
                    let is_valid = Python::attach(|inner_py: Python<'_>| {
                        envelope.borrow(inner_py).validate_and_process()
                    });
                    if is_valid {
                        valid_count.fetch_add(1, Ordering::Relaxed);
                    }
                });
            });

            // Ordering::Relaxed 足夠：for_each 結束時 rayon 已經 join 所有 worker，
            // 之後的 load 一定看到完整的值。
            valid_count.load(Ordering::Relaxed)
        });
        Ok(count)
    }

    /// [對照組 A：GIL 時代就有的加速] 純原生計算的平行版。
    ///
    /// 補洞的重點就在這裡：先在 attach 狀態把每顆紅包的 amount 讀成一份純 Rust 的
    /// Vec<u32>，接著 detach，在「完全不持有直譯器」的狀態下用 rayon 跑 compute_valid。
    /// 因為平行區內一次都不碰直譯器，這種加速在 GIL build 與 free-threaded build 上
    /// 「都」拿得到——numpy / Polars 二十年來就是靠這招（釋放 GIL 做原生計算）。
    /// 所以它不是 free-threading 的新東西，是用來對照的基準線。
    ///
    /// valid 值走的是同一個 compute_valid，因此和上面兩個函式完全一致。
    #[pyfunction]
    fn count_valid_native_parallel() -> PyResult<u64> {
        let count = Python::attach(|py| {
            let envelopes = make_envelopes(py, 100);
            // attach 狀態下把資料讀出來，之後平行區就再也不需要直譯器。
            let amounts: Vec<u32> = envelopes.iter().map(|e| e.borrow(py).amount).collect();

            py.detach(|| {
                let valid_count = AtomicU64::new(0);
                amounts.par_iter().for_each(|&amount| {
                    if compute_valid(amount) {
                        valid_count.fetch_add(1, Ordering::Relaxed);
                    }
                });
                valid_count.load(Ordering::Relaxed)
            })
        });
        Ok(count)
    }

    /// [主角 B：free-threading 才解鎖的加速] 直譯器綁定工作的平行版。
    ///
    /// 每個 worker 在 rayon 平行區、於 attach 狀態下，對每顆紅包「重複呼叫一個 Python
    /// callback」calls_per_envelope 次，把回傳值累加後決定有效與否。關鍵在於：呼叫
    /// Python 一定得持有直譯器，所以這段工作「無法」像 native 版那樣 detach 出去跑。
    ///   - GIL build：內層 attach 等於搶 GIL，8 條 worker 被序列化 => 幾乎沒有加速。
    ///   - free-threaded build：attach 不搶全域鎖 => 8 條 worker 真平行。
    /// 這個「GIL build 序列化、free-threaded build 才平行」的落差，正是 free-threading
    /// 獨有的紅利，也是 native 版拿不到、必須靠拿掉 GIL 才有的東西。
    ///
    /// scorer 用 Py<PyAny>（Send + Sync）捕捉進 rayon 閉包；worker 內 attach 後再 bind
    /// 呼叫。Python 例外一律用 `?` 往外傳（try_for_each），不 silently 吞掉。
    #[pyfunction]
    #[pyo3(signature = (scorer, calls_per_envelope=200))]
    fn count_valid_interpreter_bound_parallel(
        scorer: Py<PyAny>,
        calls_per_envelope: u32,
    ) -> PyResult<u64> {
        Python::attach(|outer_py| {
            let envelopes = make_envelopes(outer_py, 100);
            let valid_count = AtomicU64::new(0); // [原理3] 一樣用無鎖 atomic 彙總

            outer_py.detach(|| -> PyResult<()> {
                envelopes.par_iter().try_for_each(|envelope| -> PyResult<()> {
                    // [必須持有直譯器] 呼叫 Python callback => 一定得 attach。
                    let is_valid = Python::attach(|inner_py: Python<'_>| -> PyResult<bool> {
                        let scorer = scorer.bind(inner_py);
                        let amount = envelope.borrow(inner_py).amount;
                        let mut acc: i64 = 0;
                        for k in 0..calls_per_envelope {
                            // 傳入 (amount, k) 讓每次呼叫都做不同的工，不是白呼叫。
                            let out = scorer.call1((amount, k))?;
                            acc = acc.wrapping_add(out.extract::<i64>()?);
                        }
                        Ok(acc % 2 == 0 && amount > 0)
                    })?;
                    if is_valid {
                        valid_count.fetch_add(1, Ordering::Relaxed);
                    }
                    Ok(())
                })
            })?;

            Ok(valid_count.load(Ordering::Relaxed))
        })
    }

    #[pyfunction]
    fn get_thread_count() -> PyResult<usize> {
        Ok(rayon::current_num_threads())
    }
}

// =============================================================================
// [幕2/幕3 教學素材] 三種「共享計數器」寫法的對照，做投影片時可並排展示。
// 只有英雄版（上面的 AtomicU64）被編譯進模組；下面兩段是給投影片用的對照。
//
// (壞) 直接共享可變：編譯失敗，這就是 Aliasing XOR Mutability 在編譯期現形。
// 最小重現在 experiments/compile_fail/（獨立 cargo 專案，刻意編不過），
// 完整 stderr 見 results/compile_fail_E0594.txt。以下是 cargo check 的原文：
//
//   let mut valid_count: u64 = 0;
//   amounts.par_iter().for_each(|&amount| {
//       if amount % 2 == 0 {
//           valid_count += 1;
//       }
//   });
//
//   error[E0594]: cannot assign to `valid_count`, as it is a captured variable in a `Fn` closure
//     --> src/main.rs:10:13
//      |
//    7 |     let mut valid_count: u64 = 0;
//      |         --------------- `valid_count` declared here, outside the closure
//    8 |     amounts.par_iter().for_each(|&amount| {
//      |                                 --------- in this closure
//    9 |         if amount % 2 == 0 {
//   10 |             valid_count += 1;
//      |             ^^^^^^^^^^^^^^^^ cannot assign
//
//   // 對照第一幕：Python 的 `counter += 1` 在這裡不會報錯，會默默丟更新（lost update）。
//
// (解法 A：各給一份再合併 / reduce) 根本不需要共享可變狀態，最 Rusty：
//
//   let valid_count = envelopes
//       .par_iter()
//       .filter(|e| Python::attach(|p| e.borrow(p).validate_and_process()))
//       .count() as u64;
//   // 對應第二幕「不要 alias，每條執行緒自己一份，最後合併」。
//
// (解法 B：上鎖) interior mutability 的典型容器，能用但有競爭點：
//
//   let valid_count = std::sync::Mutex::new(0u64);
//   envelopes.par_iter().for_each(|e| {
//       if check(e) {
//           *valid_count.lock().unwrap() += 1;  // 對應 Python 的 threading.Lock
//       }
//   });
//   // rayon 的 par_iter 是 scoped borrow，直接借用 Mutex 即可；
//   // Arc 用在所有權必須活過 scope 的情況（例如 thread::spawn）。
//   // 「改一次鎖一次」很單純；改很多次時，鎖會成為瓶頸 => 才有 atomic / 分片鎖 / RW 鎖的動機。
// =============================================================================
