// 最小重現：在 rayon 的平行閉包裡直接對一個共享的可變計數器做 += 1。
// 這段程式刻意編不過，對應 src/lib.rs 檔尾「(壞) 直接共享可變」那一段。
use rayon::prelude::*;

fn main() {
    let amounts: Vec<u32> = (1..=100).collect();
    let mut valid_count: u64 = 0;
    amounts.par_iter().for_each(|&amount| {
        if amount % 2 == 0 {
            valid_count += 1;
        }
    });
    println!("{valid_count}");
}
