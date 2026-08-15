"""
scoring.py
=======
二階段過濾：訊號品質評分 / 優先追蹤排序 / 訊號追蹤紀錄。
※ 改版重點：原本綁在日/週KD、MACD、趨勢突破上的評分邏輯，
   已隨著這些訊號被移除而拿掉，改成依「本次觸發的訊號清單(含買/賣方向)」評分。
   下面的 SIGNAL_BASE_WEIGHT 可依實際回測結果自行調整每個訊號的權重，
   不影響其他評分邏輯，也不需要更動主程式。

※ 2026-08-10 依「進場指標濾網優化分析」回測結果校準（資料範圍2026-01~08，14,099筆交易）：
   1. SIGNAL_BASE_WEIGHT 改用各訊號回測EV(平均報酬率%)重新排序校準，
      原本雙漲停(18分,EV排名倒數第4)、三白兵(12分,EV排名最差)嚴重高估，KD高腳(12分,EV排名第1)明顯低估
   2. 訊號共振門檻修正：2個訊號同時觸發 vs 1個訊號 勝率幾乎無差異(41.0% vs 41.2%)，
      原本「2個+6分」沒有數據支持，拿掉；3個以上才開始有統計顯著提升(p=0.0025)，
      4個以上效果最強，新增獨立分級
   3. 新增「漲停」專屬濾網加分：MA10>MA20 且 成交量/昨量(VolRatioYesterday)<=0.95
      → 勝率46.5%→56.8%，跨月穩健性驗證100%通過(7個月全部正向,p=0.0025)，
      是這次分析中唯一通過完整跨市況驗證的訊號專屬條件。
      MA10/MA20/VolRatioYesterday 三個欄位已對應到 indicators.py 的
      add_indicators() 實際輸出欄位名稱（VolRatioYesterday 為新增欄位，
      計算方式為 Volume / Volume.shift(1)，需先更新 indicators.py 才有這個欄位）。
   其餘10個訊號（3K反轉/KD高腳/三白兵/下降趨勢線突破/周1K/單跳空/島狀反轉/巧妙點/
   布林縮窄突破/雙跳空/雙漲停）在單一濾網、組合濾網、三合一組合三個階段的回測中，
   都沒有找到能通過跨月穩健性驗證的MA/RSI/成交量條件，故不加專屬硬性gate，
   避免過度篩選(over-filtering)，僅靠下方通用評分項與訊號權重反映其相對品質。

※ 待確認事項（本檔案看不到來源，需你對照主程式/掃描器確認）：
   calc_signal_quality_score() 吃的 data dict 裡 ma_range/ma_trend/rs_raw/
   volume_lots/pct/volatility_pct 這幾個 key，在目前上傳的 indicators.py 和
   common_fubon.py 裡都找不到計算來源（indicators.py 只有 MA5/10/20/60 等
   欄位本身，沒有轉換成 ma_range 這種分類字串的邏輯）。這代表這段轉換邏輯
   寫在其他還沒上傳的檔案（可能是主程式/掃描器 app.py）裡。5.5 區塊新增的
   MA10/MA20/VolRatioYesterday 三個 key，請確認你那邊組 data dict 時，是直接
   把 indicators.py add_indicators() 那一列的 MA10/MA20/VolRatioYesterday
   原封不動放進去，而不是又轉換成其他分類字串，不然這段加分不會生效。
"""

import os

import pandas as pd
import streamlit as st

# ===== 二階段過濾 / 追蹤 Database 設定 =====
LOCAL_DATABASE_DIR = st.secrets.get("LOCAL_DATABASE_DIR", "Database")
TRACKING_FILE = os.path.join(LOCAL_DATABASE_DIR, "signal_tracking.csv")
SIGNAL_SCORE_MIN = float(st.secrets.get("SIGNAL_SCORE_MIN", 55))
PRIORITY_SCORE_MIN = float(st.secrets.get("PRIORITY_SCORE_MIN", 65))

# 各訊號基礎權重：買進型態給正分、賣出/風險型態給負分。
# key 為 signal_module 訊號的「label」(顯示名稱)，可自行調整。
#
# 買進型態權重依 2026/01-08 回測全資料 EV(平均報酬率%) 校準，正規化到 5~18 分區間，
# EV排名見下方註解；「漲幅達標」「廣義上升三法」不在本次回測範圍內，暫維持原值。
# 賣出/風險型態（跌停/移動停利/廣義下降三法/反向島狀）也不在本次回測範圍內，暫維持原值。
SIGNAL_BASE_WEIGHT = {
    "漲幅達標": 6,          # 不在本次回測範圍，暫維持原值
    "KD高腳": 18,           # EV +7.27（回測EV第1名） 12→18
    "3K反轉": 16,           # EV +6.30（第2名）        12→16
    "漲停": 15,             # EV +6.20（第3名，另有專屬濾網加分，見下方）
    "布林縮窄突破": 14,      # EV +6.12（第4名，n=319樣本較小，權重不宜衝太高） 15→14
    "巧妙點": 11,            # EV +5.16（第7名）        14→11
    "周1K": 10,              # EV +5.50（第6名，原本被高估最多的之一） 15→10
    "島狀反轉": 9,            # EV +4.61（第8名）        15→9
    "雙漲停": 8,              # EV +4.18（第9名，n=274樣本較小，原本18分嚴重高估） 18→8
    "單跳空": 7,              # EV +4.14（第10名）        6→7
    "雙跳空": 7,              # EV +3.88（第11名）       10→7
    "三白兵": 5,              # EV +2.78（回測EV最差，原本12分嚴重高估） 12→5
    "廣義上升三法": 15,        # 不在本次回測範圍，暫維持原值
    "跌停": -20,
    "移動停利": -15,
    "廣義下降三法": -18,
    "反向島狀": -18,
}


def ensure_local_database_dir():
    os.makedirs(LOCAL_DATABASE_DIR, exist_ok=True)


def safe_float(x, default=0):
    try:
        if x in ["-", "", None]:
            return default
        return float(x)
    except Exception:
        return default


def classify_signal_grade(score):
    score = safe_float(score, 0)
    if score >= 75:
        return "A強勢追蹤"
    elif score >= 65:
        return "B可追蹤"
    elif score >= 55:
        return "C觀察"
    return "D過濾"


# 漲停專屬濾網門檻（回測校準值，可調整）：
#   MA10 > MA20  且  VolRatioYesterday(成交量/昨量) <= RALLY_LIMIT_VOL_RATIO_MAX
#   → 全資料勝率 46.5%→56.8%，7個月walk-forward全部正向，配對t檢定 p=0.0025
RALLY_LIMIT_VOL_RATIO_MAX = 0.95
RALLY_LIMIT_BONUS = 15


def calc_signal_quality_score(data, signal_types, signal_kinds=None):
    """
    data: compute_indicators() 回傳的 dict
          (需含 ma_range / ma_trend / rs_raw / volume_lots / pct / volatility_pct
           ——這幾個 key 的計算來源不在 indicators.py/common_fubon.py 裡，
           應該在你主程式裡把 add_indicators() 的欄位轉換成這些分類值；
           若要套用「漲停」專屬濾網加分，另需含 MA10 / MA20 / VolRatioYesterday
           這三個欄位，直接對應 indicators.py add_indicators() 的輸出欄位名稱，
           不是分類過的字串，是原始數值——請確認你組 data dict 時有把這三欄
           原封不動放進去；缺任一欄位時這段加分會自動略過，不影響其他評分邏輯)
    signal_types: 本次觸發的訊號「名稱」清單 (例如 ["KD高腳", "雙跳空"])
    signal_kinds: {訊號名稱: "buy"/"sell"}，用於買賣方向修正 (可不傳，僅影響下方第6、7項)
    """
    signal_kinds = signal_kinds or {}
    score = 0

    # 1. 價格位置：避免接刀
    ma_range = data.get("ma_range", "")
    ma_trend = data.get("ma_trend", "")

    if ma_range == ">MA5":
        score += 15
    elif ma_range == "MA5~10":
        score += 10
    elif ma_range == "MA10~20":
        score += 5
    elif ma_range == "<MA20":
        score -= 15

    if ma_trend == "多頭":
        score += 15
    elif ma_trend == "糾結":
        score += 5
    elif ma_trend == "空頭":
        score -= 10

    # 2. 相對強度：優先抓比大盤/族群強的股票
    rs = safe_float(data.get("rs_raw", 0))
    if rs >= 5:
        score += 20
    elif rs >= 2:
        score += 12
    elif rs >= 0:
        score += 5
    else:
        score -= 10

    # 3. 量能確認
    volume_lots = safe_float(data.get("volume_lots", 0))
    if volume_lots >= 3000:
        score += 8
    elif volume_lots >= 1000:
        score += 4

    # 4. 避免暴衝過熱 / 波動過大
    pct = safe_float(data.get("pct", 0))
    volatility = safe_float(data.get("volatility_pct", 0))
    if pct >= 8:
        score -= 8
    elif 1 <= pct <= 5:
        score += 5

    if volatility >= 15:
        score -= 8
    elif volatility <= 8:
        score += 4

    # 5. 訊號本身的基礎權重 (買進型態加分、賣出/風險型態扣分)
    for sig in signal_types:
        score += SIGNAL_BASE_WEIGHT.get(sig, 0)

    # 5.5 漲停專屬濾網加分：MA10>MA20 且 量縮(VolRatioYesterday<=0.95)
    #     這是三個階段回測中唯一通過完整跨月穩健性驗證的訊號專屬條件，
    #     其餘訊號目前沒有數據支持的專屬gate，故不比照辦理。
    #     MA10/MA20/VolRatioYesterday 直接對應 indicators.py add_indicators()
    #     的輸出欄位，不是分類字串，是當日原始數值。
    if "漲停" in signal_types:
        ma10 = safe_float(data.get("MA10"), default=None)
        ma20 = safe_float(data.get("MA20"), default=None)
        vol_ratio_yest = safe_float(data.get("VolRatioYesterday"), default=None)
        if ma10 is not None and ma20 is not None and vol_ratio_yest is not None:
            if ma10 > ma20 and vol_ratio_yest <= RALLY_LIMIT_VOL_RATIO_MAX:
                score += RALLY_LIMIT_BONUS

    # 6. 訊號共振：多個「買進型態」訊號同時出現，比單一訊號可靠
    #    回測顯示 2個訊號 vs 1個訊號 勝率幾乎無差異(41.0% vs 41.2%)，故不再對2個訊號加分；
    #    3個以上才開始有統計顯著提升(整體41.1%→47.0%, p=0.0025, 8個月全部正向)，
    #    4個以上效果最強，獨立分級。
    buy_signals = {s for s in signal_types if signal_kinds.get(s, "buy") == "buy"}
    if len(buy_signals) >= 4:
        score += 18
    elif len(buy_signals) == 3:
        score += 12

    # 7. 買賣訊號同時出現：代表當下同時有出場/風險警示，降低追蹤分數
    sell_signals = {s for s in signal_types if signal_kinds.get(s) == "sell"}
    if sell_signals and buy_signals:
        score -= 10

    return max(0, min(100, round(score, 1)))


def build_priority_rows(all_signal_rows, min_score=None):
    if min_score is None:
        min_score = PRIORITY_SCORE_MIN
    if not all_signal_rows:
        return []
    priority_rows = []
    for row in all_signal_rows:
        score = safe_float(row.get("訊號分數", 0))
        if score >= float(min_score):
            priority_rows.append(row.copy())
    return sorted(
        priority_rows,
        key=lambda r: (
            safe_float(r.get("訊號分數", 0)),
            safe_float(r.get("RS加權報酬%", 0)),
            safe_float(r.get("成交量(張)", 0)),
        ),
        reverse=True,
    )


def append_signal_tracking(row, scan_date, tracking_file=TRACKING_FILE):
    ensure_local_database_dir()
    base_cols = [
        "scan_date", "代碼", "股票名稱", "entry_price",
        "訊號類型", "訊號方向", "訊號分數", "追蹤等級",
        "RS加權報酬%", "MA位置", "MA排列", "成交量(張)",
        "status",
    ]
    new_record = {
        "scan_date": scan_date,
        "代碼": row.get("代碼"),
        "股票名稱": row.get("股票名稱"),
        "entry_price": row.get("價格"),
        "訊號類型": row.get("訊號類型"),
        "訊號方向": row.get("訊號方向"),
        "訊號分數": row.get("訊號分數"),
        "追蹤等級": row.get("追蹤等級"),
        "RS加權報酬%": row.get("RS加權報酬%"),
        "MA位置": row.get("MA位置"),
        "MA排列": row.get("MA排列"),
        "成交量(張)": row.get("成交量(張)"),
        "status": "tracking",
    }

    if os.path.exists(tracking_file):
        df = pd.read_csv(tracking_file, encoding="utf-8-sig")
    else:
        df = pd.DataFrame(columns=base_cols)

    if df.empty:
        should_append = True
    else:
        key = (
            (df["scan_date"].astype(str) == str(scan_date))
            & (df["代碼"].astype(str) == str(row.get("代碼")))
        )
        should_append = not key.any()

    if should_append:
        df = pd.concat([df, pd.DataFrame([new_record])], ignore_index=True)
        df.to_csv(tracking_file, index=False, encoding="utf-8-sig")
