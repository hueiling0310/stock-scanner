"""
雙漲停 (Double Limit Up)

條件 (以掃描日為基準):
1. 掃描日「當天」與「前一個交易日」皆符合漲停條件。
2. 漲停條件定義為：當日收盤價相較前一交易日收盤價漲幅 >= 9.5%。

效能備註 (2026-08-12)：改用 df.index 直接查找 (in / get_loc)，
取代原本每次呼叫都重新 df.index.tolist() + list.index() 的線性掃描。
"""
from .base import SignalContext, SignalResult, register_signal

RISE_THRESHOLD_PCT = 9.5  # 單日漲停判定門檻 (%)，連續兩日皆須達到


@register_signal(
    key="double_limit_up",
    label="雙漲停",
    description="連續兩個交易日(含掃描日)收盤漲幅皆 >= 9.5%",
)
def check_double_limit_up(ctx: SignalContext) -> SignalResult:
    df = ctx.df
    dates = df.index

    if ctx.scan_date not in dates:
        return SignalResult(hit=False, detail="掃描日不在資料範圍內")

    idx = df.index.get_loc(ctx.scan_date)

    # 需要至少3天的資料 (今天、昨天、前天) 才能計算出連續兩天的漲幅
    if idx < 2:
        return SignalResult(hit=False, detail="資料不足，無法計算連續兩日之漲幅")

    # 取得近三日的收盤價
    today_close = df.iloc[idx]["Close"]
    prev_close = df.iloc[idx - 1]["Close"]
    prev_prev_close = df.iloc[idx - 2]["Close"]

    # 計算連續兩日的漲幅百分比
    today_rise_pct = (today_close - prev_close) / prev_close * 100
    prev_rise_pct = (prev_close - prev_prev_close) / prev_prev_close * 100

    # 判定連續兩天漲幅是否皆達到門檻以上
    if today_rise_pct >= RISE_THRESHOLD_PCT and prev_rise_pct >= RISE_THRESHOLD_PCT:
        return SignalResult(
            hit=True,
            detail=(
                f"連續兩日漲停！\n"
                f"前一日({dates[idx-1]})漲幅 {prev_rise_pct:.2f}% (收 {prev_close})，\n"
                f"掃描日({ctx.scan_date})漲幅 {today_rise_pct:.2f}% (收 {today_close})"
            ),
            marks=[dates[idx-1], ctx.scan_date] # 在圖表上將這兩天都標記出來
        )
    else:
        return SignalResult(
            hit=False,
            detail=(
                f"未達雙漲停標準。\n"
                f"前一日漲幅: {prev_rise_pct:.2f}%\n"
                f"掃描日漲幅: {today_rise_pct:.2f}%"
            ),
        )
