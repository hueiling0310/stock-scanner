"""
技術指標計算工具: KD(9,3,3)、各天期均線(MA5/10/20/60)、成交量10日均量、乖離率(Bias)、布林通道(BBand)
"""
import numpy as np
import pandas as pd


def compute_kd(df: pd.DataFrame, n: int = 9, k_smooth: int = 3, d_smooth: int = 3, seed: float = 50.0):
    """
    計算 KD 指標 (標準 9,3,3 平滑法)
    RSV = (Close - N日內最低) / (N日內最高 - N日內最低) * 100
    K = 前日K * (2/3) + 今日RSV * (1/3)
    D = 前日D * (2/3) + 今日K * (1/3)
    起始 K=D=seed(預設50)
    """
    low_n = df["Low"].rolling(n, min_periods=1).min()
    high_n = df["High"].rolling(n, min_periods=1).max()
    rng = (high_n - low_n).replace(0, np.nan)
    rsv = (df["Close"] - low_n) / rng * 100
    rsv = rsv.fillna(50)

    k_alpha = 1.0 / k_smooth
    d_alpha = 1.0 / d_smooth

    K, D = [], []
    prev_k, prev_d = seed, seed
    for v in rsv:
        k = prev_k * (1 - k_alpha) + v * k_alpha
        d = prev_d * (1 - d_alpha) + k * d_alpha
        K.append(k)
        D.append(d)
        prev_k, prev_d = k, d

    return pd.Series(K, index=df.index), pd.Series(D, index=df.index)


def compute_rsi(df: pd.DataFrame, n: int = 9) -> pd.Series:
    """
    計算 RSI 指標 (Wilder's Smoothing 平滑法，預設 9 日)
    RS = 平均漲幅 / 平均跌幅
    RSI = 100 - 100/(1+RS)
    """
    delta = df["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1.0 / n, min_periods=n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / n, min_periods=n, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    # 資料筆數不足或平均跌幅為0時 (avg_loss=0 -> rs=inf/nan)，分別視情況補值
    rsi = rsi.where(avg_loss != 0, 100.0)
    rsi = rsi.fillna(50.0)
    return rsi


def add_indicators(df: pd.DataFrame, ma_periods=(10, 20, 60)) -> pd.DataFrame:
    """
    在 df 上新增 MA5/10/20/60、乖離率(20MA/60MA)、VolMA10、K/D、布林通道與帶寬 欄位 (回傳新的 DataFrame)。

    ma_periods: (短期, 中期, 長期) 根數的 tuple，預設 (10, 20, 60) 對應日K的 MA10/20/60。
    週K/月K模式下 (2026-09-04新增) 呼叫端會傳入例如 (12, 26, 52) 或 (4, 6, 12)，
    但欄位名稱固定仍叫 MA10/MA20/MA60 (訊號模組完全不用改，只有內部實際計算的
    rolling window 根數不同)。

    布林通道 (BB_UB/BB_LB/BB_BW) 刻意不跟著 ma_periods[1] 走、而是永遠用「真正的20根K棒」
    (bb_mid，跟 MA20 欄位分開獨立計算)，因為使用者要求布林通道的統計意義 (20根K棒的
    標準差) 不應該因為 MA20 被重新定義成週K的26週/月K的6月而跟著改變週期長度。
    """
    df = df.copy()
    p_short, p_mid, p_long = ma_periods

    # 1. 補上 5MA / 10MA / 20MA / 60MA (欄位名稱固定，實際根數依 ma_periods 決定)
    df["MA5"] = df["Close"].rolling(5, min_periods=1).mean()
    df["MA10"] = df["Close"].rolling(p_short, min_periods=1).mean()
    df["MA20"] = df["Close"].rolling(p_mid, min_periods=1).mean()
    df["MA60"] = df["Close"].rolling(p_long, min_periods=1).mean()

    # 2. 補上 20MA 與 60MA 的乖離率 (單位: %)
    # 公式：(收盤價 - 均線) / 均線 * 100 (沿用 MA20/MA60 欄位，會跟著 ma_periods 改變)
    df["Bias20"] = (df["Close"] - df["MA20"]) / df["MA20"] * 100
    df["Bias60"] = (df["Close"] - df["MA60"]) / df["MA60"] * 100

    # 3. 原有的成交量均量與 KD 指標
    df["VolMA10"] = df["Volume"].rolling(10, min_periods=1).mean()
    K, D = compute_kd(df)
    df["K"] = K
    df["D"] = D

    # 3.5 新增 RSI(9) 指標
    df["RSI9"] = compute_rsi(df, 9)

    # 4. 布林通道 (固定20根K棒, 2 std) —— 獨立計算 bb_mid，不直接借用 MA20 欄位，
    # 避免週K/月K模式下 MA20 被改成26週/6月時，布林通道跟著失真。
    bb_mid = df["Close"].rolling(20, min_periods=1).mean()
    df["BB_std"] = df["Close"].rolling(20, min_periods=1).std()
    df["BB_UB"] = bb_mid + 2 * df["BB_std"]
    df["BB_LB"] = bb_mid - 2 * df["BB_std"]
    # 計算帶寬 (以百分比表示)
    df["BB_BW"] = (df["BB_UB"] - df["BB_LB"]) / bb_mid * 100

    return df
