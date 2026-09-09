"""
twse_ohlcv.db 上架前的「正規化 + 完整性驗證」關卡

用途：在 GitHub Actions 把 twse_ohlcv.db commit / push 出去之前先跑這一支，
確保推出去的一定是「單一檔案就完整可讀」的乾淨資料庫。驗證不過就以非 0 結束，
讓 workflow 直接失敗、不要把壞掉的 db 推到三個 repo 去。

為什麼需要這道關卡 (2026-09-09)：
  Stock simulator 一直出現「database disk image is malformed」，根因是這顆 db
  曾被切換成 WAL 模式。WAL 模式是寫在檔案 header 裡的持久設定，資料會分散在
  twse_ohlcv.db 主檔案與 twse_ohlcv.db-wal / -shm 這兩個 side-car 檔案；
  但 workflow 只會 `git add twse_ohlcv.db`，side-car 不可能跟著進 git，
  於是別的環境 (Streamlit Cloud) 拉到的就是一顆「缺角」的資料庫。

這支腳本做四件事：
  1. wal_checkpoint(TRUNCATE)：把還留在 -wal 裡的內容全部寫回主檔案。
  2. journal_mode=DELETE：把檔案切回預設的 rollback journal 模式
     (這一步在獨立的 CI 程序裡只有這一條連線，所以一定切得成功；
      在 Streamlit app 裡因為頁面還握著另一條連線，是切不動的)。
  3. integrity_check：完整驗證檔案內容，必須回傳 ok。
  4. 確認 -wal / -shm 都已經不存在。

用法:
    python normalize_db.py [db 路徑，預設 twse_ohlcv.db]
"""
import os
import sqlite3
import sys


def normalize(db_path: str) -> int:
    if not os.path.exists(db_path):
        print(f"::error::找不到資料庫檔案 {db_path}")
        return 1

    size_mb = os.path.getsize(db_path) / 1024 / 1024
    print(f"檢查 {db_path} ({size_mb:.1f} MB)")

    conn = sqlite3.connect(db_path, timeout=60.0)
    try:
        before = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        print(f"  目前 journal_mode = {before}")

        # 1) 把 WAL 內容 checkpoint 回主檔案
        if str(before).lower() == "wal":
            result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchone()
            print(f"  wal_checkpoint(TRUNCATE) = {result}")

        # 2) 切回 delete 模式 (CI 裡只有這一條連線，必定成功)
        after = conn.execute("PRAGMA journal_mode=DELETE;").fetchone()[0]
        conn.commit()
        print(f"  切換後 journal_mode = {after}")
        if str(after).lower() != "delete":
            print(f"::error::無法將 journal_mode 切回 delete (目前為 {after})")
            return 1

        # 3) 完整性驗證
        check = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        print(f"  integrity_check = {check}")
        if str(check).lower() != "ok":
            print("::error::資料庫完整性驗證失敗，中止推送以免把壞檔案散布到其他 repo")
            return 1

        # 順手回報一下資料量，方便從 Actions log 看出這次更新有沒有正常寫入
        try:
            rows = conn.execute("SELECT COUNT(*) FROM ohlcv_data;").fetchone()[0]
            latest = conn.execute("SELECT MAX(Date) FROM ohlcv_data;").fetchone()[0]
            print(f"  ohlcv_data 共 {rows:,} 筆，最新日期 {latest}")
        except sqlite3.Error:
            pass
    finally:
        conn.close()

    # 4) 確認沒有殘留 side-car
    leftovers = [p for p in (f"{db_path}-wal", f"{db_path}-shm") if os.path.exists(p)]
    if leftovers:
        print(f"::error::仍有殘留的 side-car 檔案：{leftovers}")
        return 1

    print("✅ 資料庫檢查通過，可以安全 commit / push")
    return 0


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "twse_ohlcv.db"
    sys.exit(normalize(db))
