"""
twse_ohlcv.db 存取工具

資料表 ohlcv_data 欄位:
  Date (TEXT, YYYY-MM-DD), Market, SecurityCode, SecurityName,
  Open, High, Low, Close, Volume

============================================================================
2026-09-09 重大修正：全面停用 WAL 模式 + 開啟連線時自動修復
============================================================================
症狀：只要 twse_ohlcv.db 被更新過一次，Stock simulator 就會在
      db_utils.get_stock_list() 丟出「database disk image is malformed」。

真正原因 (實測驗證過)：
  1. 掃描器的 save_scan_results()、模擬器的 save_to_database() 都會執行
     `PRAGMA journal_mode=WAL;`。WAL 是寫在 db 檔案 header 裡、跨連線持續
     生效的持久設定，一旦設定就會一直是 WAL，直到有人明確改回來。
  2. 之前加的「寫完再切回 DELETE」補救其實**無效**：SQLite 規定只有在
     「整個資料庫沒有任何其他連線」時才能切出 WAL 模式。但 Streamlit 頁面
     一開始就用 db_utils.get_connection() 建立了一條常駐讀取連線，所以切換
     一定會撞上 `database is locked`，模式繼續留在 WAL，
     twse_ohlcv.db-wal / -shm 這兩個 side-car 檔案也一直存在。
  3. GitHub Actions 的 update.yml 只做 `git add twse_ohlcv.db`，side-car
     檔案不會、也不可能跟著進 git。
  4. Streamlit Cloud 拉到新版主檔案後，搭配到對不上的 WAL 狀態，SQLite 判定
     成不合法映像檔 → database disk image is malformed。

修正方向：
  * 這顆 db 是「要進 git、被多個 repo 與多個部署環境共用的單一檔案」，
    WAL 的側寫檔天生無法跟著 git 走，所以這裡**完全不再啟用 WAL**，
    一律使用預設的 rollback journal (delete) 模式，確保任何時刻
    twse_ohlcv.db 都是自己一個檔案就完整可讀。
  * 寫入端改用 timeout 等待鎖定，並在 finally 明確 close()，
    不再依賴 `with sqlite3.connect(...)`（那只會 commit，不會關閉連線）。
  * get_connection() 加上「開啟時自我修復」：若因為殘留的 side-car 導致
    資料庫讀不開，會自動清掉這些殘留檔再重試一次，讓 app 不會整頁掛掉。
============================================================================
"""
import os
import sqlite3

import pandas as pd


# --------------------------------------------------------------------------
# 連線與自我修復
# --------------------------------------------------------------------------
def _sidecar_paths(db_path: str):
    """回傳這顆資料庫可能產生的 WAL side-car 檔案路徑 (-wal / -shm)。"""
    return [f"{db_path}-wal", f"{db_path}-shm"]


def _probe(conn: sqlite3.Connection) -> None:
    """對連線做一次極輕量的探測查詢。

    刻意不用 `PRAGMA integrity_check` / `quick_check`——那兩個是 O(檔案大小)
    的全表掃描，這顆 db 有數十 MB，Streamlit 每次 rerun 都跑會明顯拖慢。
    讀 sqlite_master 只碰到檔案開頭的幾個 page，成本接近零，但只要檔案 header
    或 WAL 狀態不一致 (也就是 malformed 的情況)，這一行就會立刻丟出
    sqlite3.DatabaseError，足以當作健康檢查。
    """
    conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()


def _remove_sidecars(db_path: str) -> list:
    """刪除殘留的 -wal / -shm，回傳實際刪掉的檔名清單。"""
    removed = []
    for path in _sidecar_paths(db_path):
        try:
            if os.path.exists(path):
                os.remove(path)
                removed.append(os.path.basename(path))
        except OSError:
            pass
    return removed


def _clear_stale_sidecars(db_path: str) -> None:
    """在開啟連線「之前」處理殘留的 WAL side-car 檔案。

    為什麼要用 mtime 判斷「過期」：
      Streamlit Cloud 上的 twse_ohlcv.db 是由 git 更新的——GitHub Actions 更新
      完資料庫後 push，Streamlit 端 git pull 就把「主檔案」整個換成新版，但留在
      容器磁碟上的 twse_ohlcv.db-wal / -shm 不會、也不可能跟著 git 一起更新。
      這時候 -shm 裡的索引描述的還是「舊主檔案」的頁面配置，SQLite 依它去讀新的
      主檔案，輕則默默讀到整批舊資料 (實測過：新版有 3000 檔卻只讀到舊的 400 檔)，
      重則直接判定成 database disk image is malformed——這就是「只要更新資料庫
      就報錯」的真正機制。

      「主檔案的 mtime 比 -wal 還新」正是這個情境的特徵 (git 覆蓋主檔案時會更新
      它的 mtime，而 -wal 停留在更早以前)，用它來辨識過期的 side-car 很準確，
      也不會誤傷「正在寫入中」的正常 WAL (那種情況 -wal 一定比主檔案新)。

    刪掉而不是 checkpoint 回主檔案，是刻意的：過期 side-car 裡裝的是「舊版主檔案」
    的頁面，把它寫回剛拉下來的新主檔案只會真的把檔案弄壞。這個專案的資料權威來源
    是 git 上的主檔案，容器磁碟上的 side-car 一律視為可丟棄的殘留物。
    """
    wal_path = f"{db_path}-wal"
    if not (os.path.exists(wal_path) or os.path.exists(f"{db_path}-shm")):
        return
    try:
        db_mtime = os.path.getmtime(db_path)
        wal_mtime = os.path.getmtime(wal_path) if os.path.exists(wal_path) else 0.0
    except OSError:
        return
    if db_mtime > wal_mtime:
        _remove_sidecars(db_path)


def _normalize_journal_mode(conn: sqlite3.Connection) -> None:
    """若這顆檔案還停留在 WAL 模式，嘗試把它切回 delete 模式。

    切換只有在「沒有其他連線」時才會成功，失敗時 SQLite 會丟 database is locked
    或直接回傳現有模式——這裡一律吞掉例外、不影響主流程：切不掉沒關係，
    真正保證安全的是「所有寫入端都不再主動啟用 WAL」這件事，
    這個函式只是順手把歷史遺留下來的 WAL 檔案救回正常模式。
    """
    try:
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        if str(mode).lower() == "wal":
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            conn.execute("PRAGMA journal_mode=DELETE;")
            conn.commit()
    except sqlite3.Error:
        pass


def get_connection(db_path: str) -> sqlite3.Connection:
    """建立資料庫連線，並自動處理 git 更新資料庫後殘留的 WAL side-car。

    三層保護 (由前到後)：
      1. 開啟前先清掉「過期」的 -wal/-shm (見 _clear_stale_sidecars 說明)，
         這一層擋掉「更新資料庫後讀到舊資料 / malformed」的主要情境。
      2. 開啟後做一次極輕量的探測查詢；若仍讀不開，清掉 side-car 再重試一次。
      3. 連線成功後順手把殘留的 WAL 模式切回 delete，避免再產生新的 side-car。
    """
    _clear_stale_sidecars(db_path)

    conn = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
    try:
        _probe(conn)
    except sqlite3.DatabaseError:
        # 讀不開 → 不管 mtime 如何，一律清掉 side-car 後重試
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _remove_sidecars(db_path)
        conn = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
        _probe(conn)  # 這次再失敗就讓它往外丟，代表主檔案本身真的壞了

    _normalize_journal_mode(conn)
    ensure_indexes(conn)
    return conn


def ensure_indexes(conn: sqlite3.Connection) -> None:
    """
    確保 ohlcv_data 常用查詢欄位有索引，避免每次 SELECT/DELETE 都做全表掃描。

    效能備註 (2026-08-12)：這張表原本完全沒有索引，導致：
      - get_stock_ohlcv() / get_stock_name() 這類「WHERE SecurityCode = ?」的查詢，
        每次都要掃過整張表。
      - Stock simulator 的「執行更新」按鈕在全市場更新時，會對每檔股票各發一條
        DELETE (約1,700~2,000條)，沒有索引的話每一條都要全表掃描，是資料庫更新
        變慢的主因之一。
    IF NOT EXISTS 保證重複呼叫是安全的 (已存在就跳過)，不會影響既有資料，
    第一次呼叫時 SQLite 會花一點時間建立索引，之後每次查詢/刪除都會快很多。
    """
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ohlcv_code_date ON ohlcv_data(SecurityCode, Date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ohlcv_date_market ON ohlcv_data(Date, Market)"
        )
        conn.commit()
    except sqlite3.OperationalError:
        # 資料表尚未建立時 (例如全新空白 db) 略過，等資料寫入後下次連線再補建索引即可
        pass


def get_stock_list(conn: sqlite3.Connection) -> pd.DataFrame:
    """取得所有股票代碼與名稱清單"""
    q = """
        SELECT SecurityCode, SecurityName
        FROM ohlcv_data
        GROUP BY SecurityCode
        ORDER BY SecurityCode
    """
    return pd.read_sql(q, conn)


def get_stock_ohlcv(
    conn: sqlite3.Connection,
    code: str,
    start_date: str = None,
    end_date: str = None,
) -> pd.DataFrame:
    """
    取得單一股票在指定期間的 OHLCV 資料，
    回傳 DataFrame，index 為 Date (字串, 由舊到新排序)
    """
    q = "SELECT Date, Open, High, Low, Close, Volume FROM ohlcv_data WHERE SecurityCode = ?"
    params = [code]
    if start_date:
        q += " AND Date >= ?"
        params.append(start_date)
    if end_date:
        q += " AND Date <= ?"
        params.append(end_date)
    q += " ORDER BY Date"

    df = pd.read_sql(q, conn, params=params)
    df = df.set_index("Date")
    return df


def get_stock_name(conn: sqlite3.Connection, code: str) -> str:
    q = "SELECT SecurityName FROM ohlcv_data WHERE SecurityCode = ? LIMIT 1"
    cur = conn.cursor()
    cur.execute(q, (code,))
    row = cur.fetchone()
    return row[0] if row else code


# --------------------------------------------------------------------------
# 掃描結果存取工具 (signal_scan_results)
# --------------------------------------------------------------------------
# 用途：讓「台股掃描器」把每次掃描命中的訊號股票清單寫進資料庫，
# 「Stock simulator」再依日期讀出這張清單，做批量瀏覽 / 點卡片帶入單股K線圖。
# 兩邊本來就共用同一份 twse_ohlcv.db，所以不需要額外的檔案同步機制。
def _safe_float(value):
    try:
        if value is None or value == "" or value == "-":
            return None
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def ensure_scan_results_table(conn: sqlite3.Connection) -> None:
    """確保 signal_scan_results 資料表與索引存在 (IF NOT EXISTS，重複呼叫安全)。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_scan_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            signal_types TEXT,
            signal_score REAL,
            signal_grade TEXT,
            price REAL,
            pct REAL,
            volume_lots REAL,
            bucket TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scan_results_date ON signal_scan_results(scan_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scan_results_date_code ON signal_scan_results(scan_date, code)"
    )
    conn.commit()


def save_scan_results(db_path: str, all_signal_rows: list, signal_buckets: dict, scan_date: str) -> int:
    """
    把掃描器本次掃描命中的訊號股票清單寫入 signal_scan_results。

    all_signal_rows: 掃描器 st.session_state.last_scan_result["all_signal_rows"]，
        每檔股票一筆 (dict)，需含「代碼」「股票名稱」「價格」「漲跌%」「成交量(張)」
        「訊號分數」「追蹤等級」「訊號類型」等欄位 (對應主表格欄位名稱)。
    signal_buckets: 掃描器 st.session_state.last_scan_result["signal_buckets"]，
        {分頁名稱: [row, ...]}，用來算出每檔股票分別屬於哪些分頁 (優先追蹤、各訊號名稱)，
        寫進 bucket 欄位 (以「、」串接多個分頁名稱)。
    scan_date: 本次掃描日期 (YYYY-MM-DD)，寫入前會先刪除同一天的舊資料，避免重複寫入。

    回傳實際寫入的筆數。若 all_signal_rows 為空，僅清除當天舊資料、不寫入新資料。

    2026-09-09 修正：這裡原本會執行 `PRAGMA journal_mode=WAL;`，這是整個
    「database disk image is malformed」問題的源頭 (詳見檔案最上方的說明)。
    現在完全不再啟用 WAL，改用 timeout 等待鎖定 + 單一交易批次寫入，
    並在 finally 明確 close() 連線 (原本的 `with sqlite3.connect(...)` 只會
    commit、不會關閉連線，連線會一直開著直到被 GC 回收)。
    """
    import datetime as _dt

    # 掃描器的股票代碼帶 .TW/.TWO 後綴 (例如 "1303.TW")，但 twse_ohlcv.db 的
    # SecurityCode 欄位、db_utils.get_stock_list() 回傳的代碼都是不帶後綴的純數字
    # (例如 "1303")。這裡先把後綴去掉再存，才能跟 Stock simulator 既有的股票清單
    # (stock_options / SecurityCode) 對得上，「查看K線圖」按鈕才找得到對應股票。
    def _bare_code(raw_code: str) -> str:
        return str(raw_code).strip().split(".")[0]

    # 依代碼算出每檔股票屬於哪些 bucket (分頁)，例如 "優先追蹤、3K反轉"
    code_to_buckets = {}
    for bucket_name, rows in (signal_buckets or {}).items():
        for row in rows or []:
            code = _bare_code(row.get("代碼", ""))
            if not code:
                continue
            code_to_buckets.setdefault(code, [])
            if bucket_name not in code_to_buckets[code]:
                code_to_buckets[code].append(bucket_name)

    now_str = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    records = []
    for row in all_signal_rows or []:
        code = _bare_code(row.get("代碼", ""))
        if not code:
            continue
        records.append((
            scan_date,
            code,
            row.get("股票名稱", ""),
            row.get("訊號類型", ""),
            _safe_float(row.get("訊號分數")),
            row.get("追蹤等級", ""),
            _safe_float(row.get("價格")),
            _safe_float(row.get("漲跌%")),
            _safe_float(row.get("成交量(張)")),
            "、".join(code_to_buckets.get(code, [])),
            now_str,
        ))

    # 注意：這裡刻意「不」設定 journal_mode=WAL。維持 SQLite 預設的 rollback
    # journal，寫入期間產生的 -journal 暫存檔在交易結束後會自動刪除，
    # 不會像 WAL 那樣留下持久的 -wal/-shm，也不會把 WAL 模式烙印進檔案 header。
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        ensure_scan_results_table(conn)
        # 先刪除當天舊資料再寫入，避免同一天重複掃描時資料重複累積
        conn.execute("DELETE FROM signal_scan_results WHERE scan_date = ?", (scan_date,))
        if records:
            conn.executemany(
                """
                INSERT INTO signal_scan_results
                    (scan_date, code, name, signal_types, signal_score, signal_grade,
                     price, pct, volume_lots, bucket, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                records,
            )
        conn.commit()
    finally:
        conn.close()

    return len(records)


def get_scan_results(conn: sqlite3.Connection, scan_date: str, bucket: str = None) -> pd.DataFrame:
    """
    查詢指定日期的掃描結果清單，供 Stock simulator 的「掃描結果瀏覽」使用。

    bucket: 選填，指定時只回傳「bucket 欄位包含此分頁名稱」的股票
        (例如 bucket="優先追蹤" 或 bucket="3K反轉")。不指定則回傳當天全部。
    依 signal_score 由高到低排序。
    """
    ensure_scan_results_table(conn)
    q = "SELECT * FROM signal_scan_results WHERE scan_date = ?"
    params = [scan_date]
    if bucket:
        q += " AND bucket LIKE ?"
        params.append(f"%{bucket}%")
    q += " ORDER BY signal_score DESC, code ASC"
    return pd.read_sql(q, conn, params=params)


def get_scan_result_dates(conn: sqlite3.Connection, limit: int = 30) -> list:
    """回傳資料庫內已有掃描結果的日期清單 (新到舊)，供日期選擇器提供預設選項參考。"""
    ensure_scan_results_table(conn)
    q = "SELECT DISTINCT scan_date FROM signal_scan_results ORDER BY scan_date DESC LIMIT ?"
    cur = conn.cursor()
    cur.execute(q, (limit,))
    return [r[0] for r in cur.fetchall()]
