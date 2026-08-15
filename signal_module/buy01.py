"""
Buy01 (breakout_retest)

訊號定義 (以掃描日為基準):
1. 尋找「突破事件」(往前搜尋 BREAKOUT_SEARCH_DAYS 個交易日):
   - 對候選日 j，取 j 之前 BASE_WINDOW_DAYS 個交易日 (不含 j) 作為「底部區間」
     (BASE_WINDOW_DAYS=90，約對應3-6個月盤整區間的中庸值)
   - 底部區間須符合「箱型盤整」: (區間最高價High的最大值 - 區間最低價Low的最小值)
     / 區間最低價Low的最小值 <= BOX_AMPLITUDE_MAX (避免把持續上漲趨勢誤判為底部)
   - 頸線 = 底部區間內「最高價(High)」的最大值
   - j 當日收盤價 > 頸線 * (1 + BREAKOUT_BUFFER%)，視為有效突破 (留緩衝避免假突破)
   - 由掃描日往前逐日搜尋，取「最近一次」符合條件的突破日 (無天數間隔上限，
     BREAKOUT_SEARCH_DAYS 僅為搜尋範圍的計算上限，非型態本身的強制限制)
2. 掃描日「當天」同時符合:
   - 收盤價與 MA20 或 MA60 其中一條均線的差距在 ±RETEST_TOLERANCE% 以內
     (代表回測到「月線(MA20)」或「季線(MA60)」，兩者任一即可)
   - MA60(今日) > MA60(前一日) 且 MA20(今日) > MA20(前一日)，
     代表季線(MA60)與月線(MA20)「同時」正向上 (用4檔實際標記資料反推驗證過，
     必須兩條都向上才會精準對上實際的Buy01/Buy02日期，只要求任一向上會誤判)
   - 同時符合「3K反轉」型態 (今日相對昨日的價格關係，已放寬):
     - 價格條件: 今日收盤價 > 昨日收盤價
     - 均線條件: 今日收盤價 > 60MA
     - 乖離條件: 今日收盤價與 20MA 或 60MA 乖離率 (其中之一) 在 ±BIAS_TOLERANCE_3K% 以內
=> 代表個股突破3-6個月底部頸線後，拉回測試長均線、均線同步走揚，且掃描日當天出現3K反轉型態，
   「Buy01」訊號成立

需要 SignalContext.df 已包含 MA20 / MA60 欄位 (見 indicators.add_indicators)

---
可調參數 (依需求調整):
- BASE_WINDOW_DAYS   : 底部區間天數
- BOX_AMPLITUDE_MAX  : 底部箱型振幅上限 (%)
- BREAKOUT_BUFFER     : 突破頸線所需緩衝 (%)
- RETEST_TOLERANCE    : 回測長均線的容忍度 (%)
- BREAKOUT_SEARCH_DAYS: 往前搜尋突破事件的最大範圍 (交易日)
- MA_SLOPE_LOOKBACK   : 判斷 MA60/MA20 是否向上時，往前比較的天數 (目前為1，比較敏感；
                        若訊號太雜訊可考慮改為5或10)
- BIAS_TOLERANCE_3K   : 3K反轉型態裡，收盤價與 MA20/MA60 乖離率的容忍度 (%)

效能備註 (2026-08-12)：改用 df.index 直接查找 (in / get_loc)，
取代原本每次呼叫都重新 df.index.tolist() + list.index() 的線性掃描。
"""
import pandas as pd
from .base import SignalContext, SignalResult, register_signal

BASE_WINDOW_DAYS = 60          # 底部區間天數
BOX_AMPLITUDE_MAX = 35.0       # 底部箱型盤整振幅上限 (%)
BREAKOUT_BUFFER = 1.0          # 突破頸線所需緩衝比例 (%)
RETEST_TOLERANCE = 6.0         # 回測長均線(MA20/MA60)的容忍度 (%)
BREAKOUT_SEARCH_DAYS = 250     # 往前搜尋突破底部頸線事件的最大交易日範圍
MA_SLOPE_LOOKBACK = 1          # 判斷均線是否向上時，往前比較的天數
BIAS_TOLERANCE_3K = 10.0       # 3K反轉型態中，收盤價與MA20/MA60乖離率的容忍度 (%)


def _find_neckline_and_check_box(df: pd.DataFrame, j: int):
    """取得第 j 天之前 BASE_WINDOW_DAYS 天的底部區間，回傳 (頸線, 是否符合箱型盤整)"""
    start = j - BASE_WINDOW_DAYS
    if start < 0:
        return None, False
    window = df.iloc[start:j]
    if len(window) < BASE_WINDOW_DAYS:
        return None, False

    neckline = window["High"].max()
    low_min = window["Low"].min()
    if pd.isna(neckline) or pd.isna(low_min) or low_min <= 0:
        return None, False

    amplitude = (neckline - low_min) / low_min * 100
    return neckline, amplitude <= BOX_AMPLITUDE_MAX


def _check_breakout_day(df: pd.DataFrame, j: int):
    """判斷第 j 天是否為有效的「突破底部頸線」日，回傳 (是否突破, 頸線)"""
    neckline, is_box = _find_neckline_and_check_box(df, j)
    if neckline is None or not is_box:
        return False, neckline

    close_j = df.iloc[j]["Close"]
    if pd.isna(close_j):
        return False, neckline

    return close_j > neckline * (1 + BREAKOUT_BUFFER / 100), neckline


def _check_3k_reversal(df: pd.DataFrame, idx: int):
    """判斷第 idx 天(今日)是否符合「3K反轉」型態 (已放寬版)，回傳 (是否符合, 說明文字或失敗原因)"""
    today = df.iloc[idx]
    y1 = df.iloc[idx - 1]   # 昨日

    required = [today["Close"], y1["Close"], today["MA60"]]
    if any(pd.isna(v) for v in required):
        return False, "資料不足 (缺少昨日OHLC或均線資料)"

    if not (today["Close"] > y1["Close"]):
        return False, "價格條件不成立 (今日收盤價未大於昨日收盤)"

    if not (today["Close"] > today["MA60"]):
        return False, "均線條件不成立 (今日收盤價未站上60MA)"

    bias_ma20 = abs(today["Close"] - today["MA20"]) / today["MA20"] * 100 if pd.notna(today["MA20"]) and today["MA20"] > 0 else None
    bias_ma60 = abs(today["Close"] - today["MA60"]) / today["MA60"] * 100 if today["MA60"] > 0 else None
    ok_bias = (bias_ma20 is not None and bias_ma20 <= BIAS_TOLERANCE_3K) or (bias_ma60 is not None and bias_ma60 <= BIAS_TOLERANCE_3K)
    if not ok_bias:
        return False, f"乖離條件不成立 (與20MA/60MA乖離率皆超過 ±{BIAS_TOLERANCE_3K}%)"

    return True, "3K反轉型態成立"


@register_signal(
    key="buy01",
    label="Buy01",
    description="突破3-6個月箱型底部頸線後，拉回測試MA20/MA60任一長均線、MA20與MA60同時向上，且掃描日符合3K反轉型態",
)
def check_buy01(ctx: SignalContext) -> SignalResult:
    df = ctx.df
    dates = df.index

    if ctx.scan_date not in dates:
        return SignalResult(hit=False, detail="掃描日不在資料範圍內")

    if "MA20" not in df.columns or "MA60" not in df.columns:
        return SignalResult(hit=False, detail="資料缺少 MA20 / MA60 指標欄位")

    idx = df.index.get_loc(ctx.scan_date)
    if idx < BASE_WINDOW_DAYS + MA_SLOPE_LOOKBACK:
        return SignalResult(hit=False, detail="資料不足，無法計算底部區間與均線斜率")

    today = df.iloc[idx]
    prev = df.iloc[idx - MA_SLOPE_LOOKBACK]

    # 1. MA60 與 MA20 是否「同時」向上 (兩條都要，不是任一)
    ma60_up = pd.notna(today["MA60"]) and pd.notna(prev["MA60"]) and today["MA60"] > prev["MA60"]
    ma20_up = pd.notna(today["MA20"]) and pd.notna(prev["MA20"]) and today["MA20"] > prev["MA20"]
    if not (ma60_up and ma20_up):
        return SignalResult(hit=False, detail=f"{ctx.scan_date} MA60與MA20並非同時向上 (MA60{'向上' if ma60_up else '未向上'}、MA20{'向上' if ma20_up else '未向上'})，不成立")
    up_ma_label = "MA60、MA20"

    # 2. 掃描日是否符合「3K反轉」型態
    is_3k, reason_3k = _check_3k_reversal(df, idx)
    if not is_3k:
        return SignalResult(hit=False, detail=f"{ctx.scan_date} {reason_3k}，不成立")

    # 3. 掃描日是否貼近 MA20 或 MA60 (回測長均線，任一即可)
    retest_ma = None
    for ma_col in ("MA20", "MA60"):
        ma_val = today[ma_col]
        if pd.isna(ma_val) or ma_val <= 0:
            continue
        diff_pct = abs(today["Close"] - ma_val) / ma_val * 100
        if diff_pct <= RETEST_TOLERANCE:
            retest_ma = ma_col
            break

    if retest_ma is None:
        return SignalResult(
            hit=False,
            detail=f"{ctx.scan_date} 收盤價與 MA20/MA60 差距皆超過 ±{RETEST_TOLERANCE}%，未回測長均線",
        )

    # 4. 往前搜尋是否曾出現「突破底部頸線」事件 (取最近一次)
    search_start = max(BASE_WINDOW_DAYS, idx - BREAKOUT_SEARCH_DAYS)
    breakout_date, neckline_price = None, None
    for j in range(idx - 1, search_start - 1, -1):
        is_breakout, neckline = _check_breakout_day(df, j)
        if is_breakout:
            breakout_date, neckline_price = dates[j], neckline
            break

    if breakout_date is None:
        return SignalResult(
            hit=False,
            detail=(
                f"{ctx.scan_date} 雖已回測{retest_ma}({up_ma_label}向上)且符合3K反轉型態，"
                f"但往前{BREAKOUT_SEARCH_DAYS}個交易日內未找到符合"
                f"「{BASE_WINDOW_DAYS}日箱型底部+頸線突破」的事件，不成立"
            ),
        )

    return SignalResult(
        hit=True,
        detail=(
            f"{breakout_date} 突破底部頸線({neckline_price:.2f})，"
            f"{ctx.scan_date}(掃描日) 回測{retest_ma}({up_ma_label}向上)且符合3K反轉型態 => Buy01成立"
        ),
        marks=[breakout_date, ctx.scan_date],
    )
