"""
signals/context.py
====================
共用基礎數值計算 (價格 / 漲跌% / MA位置 / MA排列 / 成交量 / 波動率 / RS)。
訊號本身的判斷已全部移交給 signal_module/ 底下可編輯的訊號模組，
這裡只保留主程式表格與評分共用、跟「訊號判斷」無關的基礎欄位。
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
