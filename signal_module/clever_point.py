"""
巧妙點

條件 (以掃描日為基準):
1. 掃描日「當天」同時符合:
   - 收盤價 > 60日均線(MA60)
   - 成交量 < 當日之前10日均量(VolMA10) 的 0.8 倍
   - 收窄線型: K線實體(|Close-Open|) 佔當日振幅(High-Low) 比例 <= 30%，
     且有上影線或下影線 (或兩者皆有)
   - 20MA乖離率(Bias20) < 26.5
   - 布林通道帶寬 (BB_BW) >= 30% (避免波動極端壓縮的死水區)
2. 往前 22 個交易日內 (含掃描日)，曾出現「訊號線型」(包含: 雙漲停、漲停、雙跳空、單跳空、三白兵) 訊號 (視為突破事件)
=> 代表突破後量縮整理、且掃描日當天正站穩均線之上且乖離不過大的整理甜蜜點，巧妙點成立

需要 SignalContext.df 已包含 MA60 / VolMA10 / Bias20 / BB_BW 欄位 (見 indicators.add_indicators)

效能備註 (2026-08-12)：改用 df.index 直接查找 (in / get_loc)，
取代原本每次呼叫都重新 df.index.tolist() + list.index() 的線性掃描。
本函式內部迴圈還會呼叫 5 個子訊號 (雙漲停/漲停/雙跳空/單跳空/三白兵)，
那些子訊號同樣套用了相同的效能修正，因此這裡的效益是疊加的。
"""
import pandas as pd
from .base import SignalContext, SignalResult, register_signal
from .three_white_soldiers import check_three_white_soldiers
from .double_gap import check_double_gap
from .single_gap import check_single_gap
from .double_limit_up import check_double_limit_up
from .limit_up import check_limit_up

LOOKBACK_DAYS = 22            # 往前搜尋「訊號線型」突破事件(雙漲停/漲停/雙跳空/單跳空/三白兵)的交易日數上限
BODY_RATIO_MAX = 30.0         # K線實體佔當日振幅比例上限 (%)，用來判定是否為窄幅整理K線
VOLUME_SHRINK_RATIO = 0.8     # 量縮比例：需低於前10日均量的此倍數
BIAS20_MAX_PCT = 26.5         # 20日乖離率上限 (%)
BB_BW_MIN_PCT = 30.0          # 布林通道帶寬下限 (%)


def _is_narrow_candle(df, i) -> bool:
    row = df.iloc[i]
    rng = row["High"] - row["Low"]
    if rng <= 0:
        return False
    body = abs(row["Close"] - row["Open"])
    ratio = body / rng * 100
    upper_shadow = row["High"] - max(row["Open"], row["Close"])
    lower_shadow = min(row["Open"], row["Close"]) - row["Low"]
    has_shadow = upper_shadow > 0 or lower_shadow > 0
    return ratio <= BODY_RATIO_MAX and has_shadow


@register_signal(
    key="clever_point",
    label="巧妙點",
    description="掃描日站上60MA、量縮0.8倍、窄幅K線、乖離<26.5且BBand帶寬>=30%，前22日內曾突破",
)
def check_clever_point(ctx: SignalContext) -> SignalResult:
    df = ctx.df
    dates = df.index

    if ctx.scan_date not in dates:
        return SignalResult(hit=False, detail="掃描日不在資料範圍內")

    # 確認所需欄位皆存在 (包含 BB_BW)
    if "MA60" not in df.columns or "VolMA10" not in df.columns or "Bias20" not in df.columns or "BB_BW" not in df.columns:
        return SignalResult(hit=False, detail="資料缺少 MA60 / VolMA10 / Bias20 / BB_BW 指標欄位")

    idx = df.index.get_loc(ctx.scan_date)
    if idx == 0:
        return SignalResult(hit=False, detail="資料不足，無法計算前10日均量")

    # 1. 掃描日「當天」是否符合各項條件
    today = df.iloc[idx]
    if today["Close"] <= today["MA60"]:
        return SignalResult(hit=False, detail=f"{ctx.scan_date} 收盤未站上60MA，不成立")

    prior_vol_avg = df["Volume"].iloc[max(0, idx - 10):idx].mean()
    threshold_vol = prior_vol_avg * VOLUME_SHRINK_RATIO

    if today["Volume"] >= threshold_vol:
        return SignalResult(
            hit=False,
            detail=f"{ctx.scan_date} 成交量({today['Volume']:.0f})未縮到前10日均量的0.8倍({threshold_vol:.0f})下，不成立",
        )

    if not _is_narrow_candle(df, idx):
        return SignalResult(hit=False, detail=f"{ctx.scan_date} 不屬於窄幅整理K線 (實體比例過大或無上下影線)，不成立")

    if today["Bias20"] >= BIAS20_MAX_PCT:
        return SignalResult(
            hit=False,
            detail=f"{ctx.scan_date} 20MA乖離率({today['Bias20']:.2f}%) >= {BIAS20_MAX_PCT}%，乖離過大不成立"
        )

    # 檢查布林通道帶寬是否大於等於門檻 (直接讀取 indicators.py 計算好的結果)
    if pd.isna(today["BB_BW"]) or today["BB_BW"] < BB_BW_MIN_PCT:
        return SignalResult(
            hit=False,
            detail=f"{ctx.scan_date} 布林帶寬({today['BB_BW']:.2f}%) < 30%，波動壓縮過度不觸發"
        )

    # 2. 往前 22 個交易日內 (含掃描日) 是否曾出現「訊號線型」突破
    lookback_start = max(1, idx - LOOKBACK_DAYS + 1)
    breakout_date, breakout_label = None, None
    for i in range(idx, lookback_start - 1, -1):
        d = dates[i]
        sub_ctx = SignalContext(code=ctx.code, name=ctx.name, df=df, scan_date=d)

        if check_double_limit_up(sub_ctx).hit:
            breakout_date, breakout_label = d, "雙漲停"
            break
        if check_limit_up(sub_ctx).hit:
            breakout_date, breakout_label = d, "漲停"
            break
        if check_double_gap(sub_ctx).hit:
            breakout_date, breakout_label = d, "雙跳空"
            break
        if check_single_gap(sub_ctx).hit:
            breakout_date, breakout_label = d, "跳空"
            break
        if check_three_white_soldiers(sub_ctx).hit:
            breakout_date, breakout_label = d, "三白兵"
            break

    if breakout_date is None:
        return SignalResult(
            hit=False,
            detail=f"{ctx.scan_date} 雖為站上60MA/量縮/窄幅整理K線且乖離合規，但前{LOOKBACK_DAYS}個交易日內未出現「訊號線型」突破，不成立",
        )

    return SignalResult(
        hit=True,
        detail=(
            f"{breakout_date} 出現「{breakout_label}」突破，"
            f"{ctx.scan_date}(掃描日) 站上60MA、量縮整理且乖離率合規，帶寬({today['BB_BW']:.2f}%) >= 30% => 巧妙點成立"
        ),
        marks=[breakout_date, ctx.scan_date],
    )
