"""
signals/context.py
====================
共用基礎數值計算 (價格 / 漲跌% / MA位置 / MA排列 / 成交量 / 波動率 / RS)。
訊號本身的判斷已全部移交給 signal_module/ 底下可編輯的訊號模組，
這裡只保留主程式表格與評分共用、跟「訊號判斷」無關的基礎欄位。

2026-08-16 新增：個股 vs 大盤（發行量加權股價指數）強弱比較，共 5 輪確認後定案：
  - 大盤MA位置 / 大盤MA排列：跟個股完全同一套分類邏輯 (build_base_context)，
    只是 df 換成大盤歷史資料，並排顯示方便肉眼比對，不額外做數字運算。
  - RS超額報酬%：跟個股 RS加權報酬% 用同一套「4週加權」公式算出大盤自己的
    RS加權報酬%，直接相減 (個股 - 大盤)，正值代表比大盤強。
  - RS Rating：由呼叫端 (主程式) 在單次掃描全市場跑完後，對所有股票的
    RS超額報酬% 做百分位排名 (0~100)，不是這裡算的單股數值，故不在本檔案。
  - RS Line 創新高：個股收盤/大盤收盤 的比值序列，是否處於近 N 個交易日新高。
"""
import pandas as pd


def calc_custom_volatility(df, price_val, window=20):
    if df is None or df.empty:
        return None
    required_cols = ["Open", "High", "Low", "Close"]
    if not set(required_cols).issubset(df.columns):
        return None

    work = df.copy().reset_index(drop=True)
    for col in required_cols:
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=required_cols).reset_index(drop=True)

    if len(work) < window + 1:
        return None

    close_for_calc = work["Close"].copy()
    close_for_calc.iloc[-1] = float(price_val)

    prev_close = close_for_calc.shift(1)
    open_ = work["Open"]
    high = work["High"]
    low = work["Low"]
    close_ = close_for_calc

    is_bullish_k = close_ >= open_

    bull_range = (
        (prev_close - open_).abs()
        + (open_ - low).abs()
        + (low - high).abs()
        + (high - close_).abs()
    )
    bear_range = (
        (prev_close - open_).abs()
        + (open_ - low).abs()
        + (high - low).abs()
        + (low - close_).abs()
    )

    daily_swing = bull_range.where(is_bullish_k, bear_range)
    avg_20_swing = daily_swing.rolling(window=window, min_periods=window).mean()
    ma20 = close_for_calc.rolling(window=window, min_periods=window).mean()

    latest_avg_20_swing = avg_20_swing.iloc[-1]
    latest_ma20 = ma20.iloc[-1]
    if pd.isna(latest_avg_20_swing) or pd.isna(latest_ma20) or latest_ma20 == 0:
        return None

    return float(latest_avg_20_swing / latest_ma20 * 100)


def calc_rs_raw_value(df, price_val):
    """
    計算單月 RS 原始值 (週加權報酬率)
    權重：最近1週40%，前1週20%，再前1週20%，最初1週20%
    一週大約 5 個交易日
    """
    if df is None or len(df) < 21:
        return None

    close = df["Close"].copy().reset_index(drop=True)
    # 用最新價格替換最後一筆收盤價，以貼近盤中即時狀態
    close.iloc[-1] = float(price_val)

    n = len(close)

    def get_return(start_idx, end_idx):
        start_idx = max(0, start_idx)
        if start_idx >= end_idx:
            return 0.0
        start_price = close.iloc[start_idx]
        end_price = close.iloc[end_idx]
        if start_price == 0 or pd.isna(start_price):
            return 0.0
        return (end_price - start_price) / start_price

    end_idx = n - 1
    # W4 (最近1週/約 5 個交易日)
    w4_start = max(0, end_idx - 5)
    ret_w4 = get_return(w4_start, end_idx)

    # W3 (前1週)
    w3_start = max(0, w4_start - 5)
    ret_w3 = get_return(w3_start, w4_start)

    # W2 (再前1週)
    w2_start = max(0, w3_start - 5)
    ret_w2 = get_return(w2_start, w3_start)

    # W1 (最初1週)
    w1_start = max(0, w2_start - 5)
    ret_w1 = get_return(w1_start, w2_start)

    # 根據公式加權
    rs_raw = (ret_w4 * 0.4) + (ret_w3 * 0.2) + (ret_w2 * 0.2) + (ret_w1 * 0.2)
    return rs_raw * 100


def build_base_context(df: pd.DataFrame, price) -> dict:
    """計算主程式表格與評分共用的基礎欄位 (不含任何訊號判斷)。"""
    if df is None or df.empty:
        raise ValueError("下載資料為空")
    if len(df) < 20:
        raise ValueError("歷史資料不足（至少需要 20 筆）")

    close_all = pd.to_numeric(df["Close"].squeeze(), errors="coerce")
    volume = pd.to_numeric(df["Volume"].squeeze(), errors="coerce") if "Volume" in df.columns else pd.Series(dtype="float64")
    if close_all.isna().all():
        raise ValueError("OHLC 資料格式異常")

    # 「昨收」改用日期比對，不要假設「df 最後一筆一定是今天、倒數第二筆一定是昨天」。
    # 資料源(富邦/Yfinance/本地DB)偶爾會因為限流、非交易時段等原因，讓「今天」這一筆缺席，
    # 此時如果單純用位置(iloc[-2])去抓「昨收」，實際上抓到的會是「前天」，
    # 漲跌%就會被誤算成兩天的漲幅、卻被當成一天的漲幅顯示，造成誤導。
    # 用日期明確篩出「今天以前最近一個交易日」的收盤價，不管 df 有沒有包含今天這一筆都能正確算出。
    if "Date" in df.columns:
        work = df.copy()
        work["Date"] = pd.to_datetime(work["Date"], errors="coerce")
        work = work.dropna(subset=["Date"]).sort_values("Date")
        today = pd.Timestamp.now(tz="Asia/Taipei").normalize().tz_localize(None)
        prior = work[work["Date"].dt.normalize() < today]
        if prior.empty:
            # 找不到「今天以前」的資料 (例如資料源整批都異常)，退回舊的位置假設當最後手段
            yesterday_close = float(close_all.iloc[-2]) if len(close_all) >= 2 else float(close_all.iloc[-1])
        else:
            yesterday_close = float(pd.to_numeric(prior["Close"], errors="coerce").iloc[-1])
        close = close_all
    else:
        yesterday_close = float(close_all.iloc[-2])
        close = close_all

    if pd.isna(yesterday_close) or yesterday_close == 0:
        raise ValueError("昨收資料異常")

    price_val = float(price)
    change_pct = float((price_val / yesterday_close - 1) * 100)
    ma5 = float(close.tail(5).mean())
    ma10 = float(close.tail(10).mean())
    ma20 = float(close.tail(20).mean())

    if price_val > ma5:
        ma_range = ">MA5"
    elif ma5 >= price_val > ma10:
        ma_range = "MA5~10"
    elif ma10 >= price_val > ma20:
        ma_range = "MA10~20"
    else:
        ma_range = "<MA20"

    if ma5 > ma10 > ma20:
        ma_trend = "多頭"
    elif ma5 < ma10 < ma20:
        ma_trend = "空頭"
    else:
        ma_trend = "糾結"

    latest_volume = 0.0
    if not volume.empty and pd.notna(volume.iloc[-1]):
        latest_volume = float(volume.iloc[-1])
    volume_lots = latest_volume / 1000

    volatility_pct = calc_custom_volatility(df, price_val, window=20)
    rs_raw = calc_rs_raw_value(df, price_val)

    return {
        "price": price_val,
        "pct": change_pct,
        "ma5": ma5, "ma10": ma10, "ma20": ma20,
        "ma_range": ma_range, "ma_trend": ma_trend,
        "latest_volume": latest_volume, "volume_lots": volume_lots,
        "volatility_pct": volatility_pct, "rs_raw": rs_raw,
    }


def calc_rs_line_new_high(stock_df: pd.DataFrame, price_val, benchmark_df: pd.DataFrame, lookback: int = 60):
    """
    計算「RS Line 創新高」旗標。
    RS Line = 個股收盤 ÷ 大盤收盤（逐日比值），經典的相對強度型態判斷：
    即使股價還沒創新高，只要這條比值線比大盤還早創新高，通常代表提前吸籌、
    有機會領漲。這裡判斷「今天」是否處於近 lookback 個交易日的新高（含今天）。

    stock_df / benchmark_df 皆需含 Date, Close 欄位（Date 需能被 to_datetime 解析）。
    兩邊用 Date 對齊 (inner join)，對齊後資料筆數不足 lookback 時回傳 None
    （無法判斷，不當作「否」，避免掃描器把「資料不足」誤顯示成「未創新高」）。
    回傳 True / False / None。
    """
    if stock_df is None or benchmark_df is None or stock_df.empty or benchmark_df.empty:
        return None
    if "Date" not in stock_df.columns or "Date" not in benchmark_df.columns:
        return None

    s = stock_df[["Date", "Close"]].copy()
    s["Date"] = pd.to_datetime(s["Date"], errors="coerce")
    s = s.dropna(subset=["Date"]).sort_values("Date").reset_index(drop=True)
    if s.empty:
        return None
    # 用即時價覆蓋最後一筆收盤價，貼近盤中即時狀態 (跟 calc_rs_raw_value 邏輯一致)
    s.loc[s.index[-1], "Close"] = float(price_val)

    b = benchmark_df[["Date", "Close"]].copy()
    b["Date"] = pd.to_datetime(b["Date"], errors="coerce")
    b = b.dropna(subset=["Date"]).sort_values("Date").rename(columns={"Close": "BenchClose"})

    merged = pd.merge(s, b, on="Date", how="inner")
    merged["Close"] = pd.to_numeric(merged["Close"], errors="coerce")
    merged["BenchClose"] = pd.to_numeric(merged["BenchClose"], errors="coerce")
    merged = merged.dropna(subset=["Close", "BenchClose"])
    merged = merged[merged["BenchClose"] != 0]
    if len(merged) < lookback:
        return None

    merged["RSLine"] = merged["Close"] / merged["BenchClose"]
    window = merged["RSLine"].tail(lookback)
    latest = window.iloc[-1]
    window_max = window.max()
    if pd.isna(latest) or pd.isna(window_max):
        return None
    # 用極小容忍值處理浮點誤差，「打平前高」也算創新高
    return bool(latest >= window_max - 1e-9)


def build_relative_strength_fields(base: dict, benchmark_ctx: dict) -> dict:
    """
    給定個股的 base (build_base_context 回傳) 與大盤的 benchmark_ctx
    (同樣用 build_base_context 對大盤歷史資料算出來的字典，全市場掃描只需算一次、
    全部股票共用比對，不逐股重算)，組出「個股 vs 大盤」的比較欄位。
    benchmark_ctx 為 None（例如大盤資料當次抓取失敗）時，全部回傳 "-"，
    不影響掃描器其餘既有欄位與流程。
    """
    if not benchmark_ctx:
        return {
            "benchmark_ma_range": "-",
            "benchmark_ma_trend": "-",
            "rs_excess": None,
        }

    rs_excess = None
    stock_rs = base.get("rs_raw")
    bench_rs = benchmark_ctx.get("rs_raw")
    if stock_rs is not None and bench_rs is not None:
        rs_excess = float(stock_rs) - float(bench_rs)

    return {
        "benchmark_ma_range": benchmark_ctx.get("ma_range", "-"),
        "benchmark_ma_trend": benchmark_ctx.get("ma_trend", "-"),
        "rs_excess": rs_excess,
    }


_TREND_RANK = {"多頭": 1, "糾結": 0, "空頭": -1}


def classify_relative_strength(rs_excess, rs_rating, ma_trend, benchmark_ma_trend, rs_line_new_high) -> str:
    """
    綜合「RS超額報酬% / RS Rating / MA排列(個股 vs 大盤) / RS創新高」四項訊號，
    給出「大盤相比(強/弱)」的簡化文字判斷，方便一眼掃過整份掃描結果表格，
    不用逐檔對照好幾個欄位自己判斷。

    評分規則（每項 -1~+1 分，RS創新高只加分不扣分）：
      1. RS超額報酬% 為正/負 → +1 / -1
      2. RS Rating >= 70 / <= 30 → +1 / -1（中段 30~70 不加不扣）
      3. MA排列強度（多頭 > 糾結 > 空頭）個股相對大盤更強/更弱 → +1 / -1
         （例如大盤是「糾結」、個股是「多頭」→ +1；大盤「多頭」個股「糾結」→ -1）
      4. RS創新高為 True → +1（沒創新高不扣分，只是額外的加分確認項）
    加總分數 >= 2 判定「強」，<= -2 判定「弱」，其餘（分數不足、訊號互相矛盾）判定「持平」。

    rs_excess 為 None（例如大盤資料當次抓取失敗，整體比較欄位都算不出來）時，直接回傳 "-"。
    """
    if rs_excess is None:
        return "-"

    score = 0.0
    if rs_excess > 0:
        score += 1
    elif rs_excess < 0:
        score -= 1

    if isinstance(rs_rating, (int, float)):
        if rs_rating >= 70:
            score += 1
        elif rs_rating <= 30:
            score -= 1

    if ma_trend in _TREND_RANK and benchmark_ma_trend in _TREND_RANK:
        diff = _TREND_RANK[ma_trend] - _TREND_RANK[benchmark_ma_trend]
        if diff > 0:
            score += 1
        elif diff < 0:
            score -= 1

    if rs_line_new_high is True:
        score += 1

    if score >= 2:
        return "強"
    if score <= -2:
        return "弱"
    return "持平"
