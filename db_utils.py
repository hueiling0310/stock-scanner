"""
twse_ohlcv.db 存取工具

資料表 ohlcv_data 欄位:
  Date (TEXT, YYYY-MM-DD), Market, SecurityCode, SecurityName,
  Open, High, Low, Close, Volume
"""
import sqlite3
import pandas as pd


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


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    ensure_indexes(conn)
    return conn


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

    with sqlite3.connect(db_path, timeout=30.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
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
