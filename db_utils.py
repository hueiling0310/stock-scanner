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
