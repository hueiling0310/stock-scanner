"""
signals/gap.py
===============
掃描條件：跳空（今天最低價 > 昨天最高價）。
"""

import pandas as pd

from .context import SignalContext

ENABLE_GAP_SIGNAL = True


def check_gap_signal(ctx: SignalContext, params: dict) -> dict:
    """跳空：今天最低價 > 昨天最高價"""
    if not ENABLE_GAP_SIGNAL:
        return {"labels": [], "fields": {"跳空訊號": "-"}, "extra": None}
    today_low = float(ctx.low.iloc[-1])
    yesterday_high = float(ctx.high.iloc[-2])
    triggered = pd.notna(today_low) and pd.notna(yesterday_high) and today_low > yesterday_high
    label = "跳空" if triggered else "-"
    return {"labels": ["跳空"] if triggered else [], "fields": {"跳空訊號": label}, "extra": None}


