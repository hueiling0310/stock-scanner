"""
下降趨勢線突破 (Descending Trendline Breakout)

定義:
1. 下降趨勢線 = 由「高點(最高價)」與「高點(最高價)」兩點連成的直線，且線段之間所有K棒的
   最高價都不能超過(穿越)這條線 (可貼線/觸線，但不可突破)。
   => 以「上緣凸包 (Upper Convex Hull)」演算法找出所有合法的、未被穿越的高點連線，
      確保任兩根K棒之間絕不會有K棒的High超過線的內插值。
   => 兩個高點之間至少要間隔 MIN_ANCHOR_GAP_DAYS(預設3)個交易日，避免兩點太靠近
      導致斜率被少數幾根K棒放大、外推後線型失真(過陡)。
   => 較新的高點(anchor2)距離掃描日至少要間隔 MIN_BREAKOUT_GAP_DAYS(預設2)個交易日，
      避免高點才剛形成、線根本還沒被價格「測試」過一天，隔天隨便反彈一根就被誤判成突破
      (數學上: 若 anchor2 剛好是掃描日前一天，則「前一天延伸線值」= anchor2 當天的
      High，而「前一天收盤」必然 <= 當天 High，freshness 檢查形同虛設，必定判定為
      「本日新突破」，這是不合理的)。
2. 掃描日「收盤價」向上突破該延伸線 (今日收盤 > 延伸線今日價位)，才算突破 (只看收盤價，
   單純盤中最高價觸碰/穿越不算)。
   且前一日仍未突破 (前一日收盤 <= 延伸線前一日價位)，視為「本日新突破」。

三種等級 (以掃描日往前算，較舊的那個高點錨點(anchor1)距離掃描日的交易日數 day_back1 分類):
    短期   : day_back1 <= 6
    中短期 : 6  < day_back1 <= 23
    中長期 : 23 < day_back1 <= 66  (66個交易日約為一季，也是最常用的長期掃描區間)

只註冊「單一」訊號 key="trendline_breakout"，內部會同時檢查三個等級。
只要任一等級在掃描日「收盤」突破，該訊號即 hit=True，detail 會列出是哪個/哪些等級突破。
不論當日是否突破，只要該區間內找得到合法的下降趨勢線，皆會透過 marks 回傳三個等級各自的
錨點資訊，方便外部(如 app_simulator.py)把三條線都畫在圖上 (呈現「貼線觀察中」的效果)，
真正觸發突破的那個等級，才會在圖上額外標示突破箭頭。

marks 資料結構 (list):
    [
        ("short", anchor1_date_or_None, anchor2_date_or_None, tier_hit_bool),
        ("mid",   anchor1_date_or_None, anchor2_date_or_None, tier_hit_bool),
        ("long",  anchor1_date_or_None, anchor2_date_or_None, tier_hit_bool),
        ("scan", scan_date),
    ]
    找不到合法線的等級，anchor1/anchor2 會是 None。

需要 df 至少包含 High / Close 欄位。

效能備註 (2026-08-12)：改用 df.index 直接查找 (in / get_loc)，
取代原本每次呼叫都重新 df.index.tolist() + list.index() 的線性掃描。
這個訊號本身內部還要做上緣凸包運算，是眾訊號中單次呼叫成本較高的一個，
在回測逐日呼叫時，省下的日期定位開銷佔比也更明顯。
"""
from .base import SignalContext, SignalResult, register_signal

SHORT_MAX_DAYS = 6   # 短期下降趨勢線分類的錨點1距離掃描日交易日數上限
MID_MAX_DAYS = 23    # 中短期分類的錨點1距離掃描日交易日數上限
LONG_MAX_DAYS = 66  # 一季，最常用的長期掃描區間
MIN_ANCHOR_GAP_DAYS = 2  # 兩個高點錨點之間，中間至少要夾幾根K棒
MIN_BREAKOUT_GAP_DAYS = 2  # 錨點2(較新的高點)距離掃描日至少要幾個交易日，避免高點剛形成隔天就被判定「突破」

# (tier_key, day_back1下限(不含), day_back1上限(含), 顯示用標籤)
TIER_DEFS = [
    ("short", 0, SHORT_MAX_DAYS, "短期"),
    ("mid", SHORT_MAX_DAYS, MID_MAX_DAYS, "中短期"),
    ("long", MID_MAX_DAYS, LONG_MAX_DAYS, "中長期"),
]


def _cross(o, a, b):
    """外積: >0 代表 O->A->B 為逆時針(左轉)，<0 為順時針(右轉)，=0 共線"""
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _upper_hull(points):
    """
    points: [(x, y), ...]，需已依 x 由小到大排序 (交易日位置本身即遞增)
    回傳「上緣凸包」的頂點序列 (由左到右)，
    上緣凸包的定義保證：任兩相鄰頂點連線之間，其餘所有點的 y 值都 <= 連線內插值，
    也就是完全符合「連線之間不能有K棒穿越」的要求。
    """
    hull = []
    for p in points:
        while len(hull) >= 2 and _cross(hull[-2], hull[-1], p) >= 0:
            hull.pop()
        hull.append(p)
    return hull


def _hull_edges(df, end_idx, lookback, min_gap):
    """
    取 [end_idx-lookback, end_idx) 這段(不含掃描日本身)的 High 建立上緣凸包，
    回傳所有「下降」且「兩端點之間至少夾了 min_gap 根K棒」的相鄰頂點線段 (x1, x2, y1, y2)。
    注意: 兩端點之間夾的K棒數 = (x2 - x1 - 1)，所以要求 (x2 - x1) > min_gap，
    而不是 (x2 - x1) >= min_gap (後者只保證夾了 min_gap - 1 根)。
    """
    start_pos = max(0, end_idx - lookback)
    positions = list(range(start_pos, end_idx))
    if len(positions) < 2:
        return []

    points = [(pos, float(df["High"].iloc[pos])) for pos in positions]
    hull = _upper_hull(points)

    edges = []
    for i in range(len(hull) - 1):
        (x1, y1), (x2, y2) = hull[i], hull[i + 1]
        if y2 < y1 and (x2 - x1) > min_gap:
            edges.append((x1, x2, y1, y2))
    return edges


def _select_edge(edges, end_idx, min_days, max_days):
    """
    在候選線段中，篩選出:
    1.「較舊的錨點(x1)」距離掃描日的交易日數落在 (min_days, max_days] 區間者
    2.「較新的錨點(x2)」距離掃描日至少要 MIN_BREAKOUT_GAP_DAYS 個交易日 (避免高點剛形成、
       線根本還沒被價格測試過，隔天隨便一根紅K就被誤判成「突破」)
    並取其中離掃描日最近(x2最大，代表目前最新、最可能正在被測試)的一段。
    """
    candidates = [
        e for e in edges
        if min_days < (end_idx - e[0]) <= max_days
        and (end_idx - e[1]) >= MIN_BREAKOUT_GAP_DAYS
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda e: e[1])


def _evaluate_tier(df, end_idx, min_days, max_days):
    """回傳單一等級的評估結果 dict，若找不到合法線則回傳 None"""
    edges = _hull_edges(df, end_idx, LONG_MAX_DAYS, MIN_ANCHOR_GAP_DAYS)
    edge = _select_edge(edges, end_idx, min_days, max_days)
    if edge is None:
        return None

    x1, x2, y1, y2 = edge
    slope = (y2 - y1) / (x2 - x1)
    intercept = y1 - slope * x1

    today_val = slope * end_idx + intercept
    today_close = float(df["Close"].iloc[end_idx])

    prev_idx = end_idx - 1
    prev_val = slope * prev_idx + intercept
    prev_close = float(df["Close"].iloc[prev_idx])

    # 只看「收盤價」是否突破延伸線；且前一日收盤仍未突破，才算「本日新突破」
    hit = (today_close > today_val) and not (prev_close > prev_val)

    return {
        "x1": x1, "x2": x2, "y1": y1, "y2": y2,
        "today_val": today_val, "today_close": today_close, "hit": hit,
    }


@register_signal(
    key="trendline_breakout",
    label="下降趨勢線突破",
    description=(
        "同時檢查短期(≤6日)/中短期(6~23日)/中長期(23~66日)三種下降趨勢線(高點間隔至少"
        f"{MIN_ANCHOR_GAP_DAYS}個交易日、中間未被K棒穿越)，任一等級收盤價向上突破即成立"
    ),
)
def check_trendline_breakout(ctx: SignalContext) -> SignalResult:
    df = ctx.df
    dates = df.index

    if ctx.scan_date not in dates:
        return SignalResult(hit=False, detail="掃描日不在資料範圍內")

    end_idx = df.index.get_loc(ctx.scan_date)
    if end_idx < 2:
        return SignalResult(hit=False, detail="資料筆數不足，無法建立趨勢線")

    marks = []
    detail_lines = []
    hit_labels = []
    any_hit = False

    for tier_key, min_days, max_days, tier_label in TIER_DEFS:
        info = _evaluate_tier(df, end_idx, min_days, max_days)

        if info is None:
            marks.append((tier_key, None, None, False))
            detail_lines.append(
                f"【{tier_label}】往前{min_days}~{max_days}個交易日內，找不到符合條件的下降趨勢線"
                f"(需兩個依序遞減、間隔至少{MIN_ANCHOR_GAP_DAYS}個交易日的高點，且中間K棒未穿越)"
            )
            continue

        a1_date, a2_date = dates[info["x1"]], dates[info["x2"]]
        marks.append((tier_key, a1_date, a2_date, info["hit"]))

        if info["hit"]:
            any_hit = True
            hit_labels.append(tier_label)
            detail_lines.append(
                f"【{tier_label}】✅收盤突破 {a1_date}(高{info['y1']:.2f})→{a2_date}(高{info['y2']:.2f})，"
                f"{ctx.scan_date}收盤{info['today_close']:.2f} > 延伸線{info['today_val']:.2f}"
            )
        else:
            detail_lines.append(
                f"【{tier_label}】{a1_date}(高{info['y1']:.2f})→{a2_date}(高{info['y2']:.2f})，"
                f"延伸至{ctx.scan_date}約{info['today_val']:.2f}，收盤{info['today_close']:.2f} 尚未(或非本日新)突破"
            )

    marks.append(("scan", ctx.scan_date))

    if any_hit:
        summary = "、".join(hit_labels)
        detail = f"{ctx.scan_date} 突破：{summary}\n" + "\n".join(detail_lines)
    else:
        detail = f"{ctx.scan_date} 尚無任何等級之下降趨勢線突破\n" + "\n".join(detail_lines)

    return SignalResult(hit=any_hit, detail=detail, marks=marks)
