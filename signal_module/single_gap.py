"""
單跳空 (Single Gap)

條件:
- 最近 3 根K線 (含今日/掃描日) 之中，至少有 1 根出現「向上跳空」
  (該日最低點 Low > 前一交易日最高點 High)

效能備註 (2026-08-12)：改用 df.index 直接查找 (in / get_loc)，
取代原本每次呼叫都重新 df.index.tolist() + list.index() 的線性掃描。
"""
from .base import SignalContext, SignalResult, register_signal

WINDOW = 1            # 檢查跳空向上的觀察視窗天數 (含掃描日)
REQUIRED_GAPS = 1      # 視窗內至少需出現幾根跳空向上K線，才算單跳空


@register_signal(
    key="single_gap",
    label="單跳空",
    description="最近3根K線中至少有1根出現向上跳空",
)
def check_single_gap(ctx: SignalContext) -> SignalResult:
    df = ctx.df
    dates = df.index

    if ctx.scan_date not in dates:
        return SignalResult(hit=False, detail="掃描日不在資料範圍內")

    idx = df.index.get_loc(ctx.scan_date)
    window_start = max(1, idx - (WINDOW - 1))

    gap_dates = []
    for i in range(window_start, idx + 1):
        cur = df.loc[dates[i]]
        prev = df.loc[dates[i - 1]]
        # 向上跳空定義: 今日最低價 > 昨日最高價
        if cur["Low"] > prev["High"]:
            gap_dates.append(dates[i])

    if len(gap_dates) >= REQUIRED_GAPS:
        return SignalResult(
            hit=True,
            detail=f"近{WINDOW}根K線中共 {len(gap_dates)} 根跳空向上: {', '.join(gap_dates)} => 單跳空成立",
            marks=gap_dates,
        )

    return SignalResult(
        hit=False,
        detail=f"近{WINDOW}根K線中僅 {len(gap_dates)} 根跳空向上 (需要{REQUIRED_GAPS}根)",
    )
