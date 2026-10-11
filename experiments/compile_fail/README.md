# compile_fail：這個專案刻意編不過

這是一個獨立的 cargo 專案（只依賴 rayon，不依賴 pyo3，也不屬於 root 的 Cargo 設定），
唯一目的是產生一份真實的編譯錯誤，給 `src/lib.rs` 檔尾「(壞) 直接共享可變」那段註解引用。

`src/main.rs` 在 rayon 的 `par_iter().for_each` 閉包裡對一個 `let mut valid_count: u64`
做 `valid_count += 1`。閉包會被多條執行緒同時呼叫，所以 rayon 要求它是 `Fn`，
而 `Fn` 閉包只能拿到捕捉變數的共享借用，對它做可變操作就違反 Aliasing XOR Mutability。
借用檢查器在編譯期直接擋下，這就是第一幕 Python `counter += 1` 的對照組。

重現：

```bash
cd experiments/compile_fail
cargo check
```

完整 stderr 存在 `results/compile_fail_E0594.txt`。
