"""
app_logic.py — 화면(app.py)에서 쓰는 순수 로직 모음 (Streamlit 의존 없음 → 단독 테스트 가능)
카드 HTML 생성, 가격 포맷, 레버리지 상한 가이드, 주문 메모 텍스트 등.
"""
import html
import math
from typing import Dict, Optional, Tuple

import pandas as pd

REGIME_INFO = {
    "uptrend": {"emoji": "🟢", "title": "상승장", "color": "#16a34a", "bg": "rgba(22,163,74,.13)",
                "desc": "롱 위주. 급등 추격 말고 눌림목(되돌림 지정가)에서 진입"},
    "downtrend": {"emoji": "🔴", "title": "하락장", "color": "#dc2626", "bg": "rgba(220,38,38,.13)",
                  "desc": "숏 위주. 급락 추격 말고 반등 구간에서 진입"},
    "sideways": {"emoji": "🟡", "title": "횡보장", "color": "#d97706", "bg": "rgba(217,119,6,.13)",
                 "desc": "박스 경계에서만 진입. 방향은 기울기 점수로 판단하고 중간 구간은 관망"},
}
TREND_ICON = {"uptrend": "↑", "downtrend": "↓", "sideways": "→"}
TF_LABEL = {"4h": "4시간봉", "1h": "1시간봉"}

# bias → (방향, 한글 방향, 설명)
BIAS_INFO = {
    "long": ("long", "롱", "추세 눌림목 진입"),
    "short": ("short", "숏", "추세 반등 매도"),
    "wait_breakout_long": ("long", "롱", "박스 상단 돌파 · 봉 마감 직후 시장가"),
    "wait_breakout_short": ("short", "숏", "박스 하단 이탈 · 봉 마감 직후 시장가"),
    "range_fade_long": ("long", "롱", "박스 하단 반등(평균회귀)"),
    "range_fade_short": ("short", "숏", "박스 상단 저항(평균회귀)"),
    "donchian_long": ("long", "롱", "신고점 돌파(약 20일) · 시장가"),
    "donchian_short": ("short", "숏", "신저점 이탈(약 20일) · 시장가"),
    "st_long": ("long", "롱", "ATR 추적선 상승 전환 · 시장가"),
    "st_short": ("short", "숏", "ATR 추적선 하락 전환 · 시장가"),
    "retest_long": ("long", "롱", "🔁 돌파 후 되돌림(경계 지지 확인) · 시장가"),
    "retest_short": ("short", "숏", "🔁 이탈 후 되돌림(경계 저항 확인) · 시장가"),
    "div_long": ("long", "롱", "📐 위치 다이버전스(상승 추세 속 저점) · 시장가"),
    "div_short": ("short", "숏", "📐 위치 다이버전스(하락 추세 속 고점) · 시장가"),
    "pb_long": ("long", "롱", "📈 추세 눌림목(상승 추세 속 눌림 → 20EMA 위로 회복) · 시장가"),
    "pb_short": ("short", "숏", "📉 반등 숏(하락 추세 속 반등 → 20EMA 아래로 재마감) · 시장가"),
    "diag_long": ("long", "롱", "🧪 상승 빗각 지지(일봉 · 한국 09시 마감) · 시장가"),
    "wick_long": ("long", "롱", "🧪 아래꼬리 2연속(일봉 · 한국 09시 마감) · 시장가"),
}
FAMILY_TAG = {"돌파 후 되돌림": "🔁 돌파 후 되돌림", "위치 다이버전스": "📐 위치 다이버전스", "추세 눌림목": "📈 추세 눌림목",
              "빗각 지지": "🧪 빗각 지지", "아래꼬리 연속": "🧪 아래꼬리 연속"}

CSS = """
.block-container{padding-top:1.1rem;padding-bottom:3rem;max-width:720px}
.regime{border-radius:16px;padding:14px 16px;margin:6px 0 10px 0;border:1px solid rgba(128,128,128,.25)}
.regime .t{font-size:1.35rem;font-weight:800;line-height:1.3}
.regime .d{font-size:.88rem;opacity:.9;margin-top:8px;line-height:1.5}
.gauge{margin-top:12px}
.gauge-track{position:relative;height:8px;border-radius:999px;
  background:linear-gradient(90deg,#dc2626,#9ca3af,#16a34a)}
.gauge-mark{position:absolute;top:-4px;width:16px;height:16px;border-radius:50%;
  background:#fff;border:3px solid #111827;transform:translateX(-50%);box-shadow:0 1px 3px rgba(0,0,0,.4)}
.gauge-lbl{position:absolute;top:10px;font-size:.68rem;opacity:.65}
.gauge-lbl.left{left:0}.gauge-lbl.right{right:0}
.brk{margin-top:14px;font-size:.82rem;background:rgba(128,128,128,.12);border-radius:10px;padding:7px 10px}
.unver{margin-top:8px;font-size:.74rem;opacity:.65;font-style:italic}
.conf{margin-top:10px;font-size:.78rem;opacity:.85}
.conf .hint{opacity:.7;font-size:.72rem}
.regime .act{margin-top:6px;font-size:.92rem;font-weight:650;line-height:1.45}
.gauge-lbl.mid{left:50%;transform:translateX(-50%)}
.drow{margin-top:10px;padding:9px 11px;border-radius:12px;background:rgba(128,128,128,.08);line-height:1.5}
.drow .h{font-size:.78rem;opacity:.7;font-weight:700}
.drow .v{font-size:1.05rem;font-weight:800;margin-top:1px}
.drow .s{font-size:.82rem;opacity:.9;margin-top:3px}
.drow.main{background:rgba(128,128,128,.13);border:1px solid rgba(128,128,128,.25)}
.drow.muted .v{font-size:.92rem;font-weight:650;opacity:.85}
.rnote{margin-top:8px;font-size:.8rem}
.altv{margin-top:8px;font-size:.84rem;font-weight:600}
.flow{margin:6px 0 2px;font-size:.88rem;line-height:1.5;padding:8px 10px;border-radius:10px;background:rgba(128,128,128,.08)}
.allot{font-size:.86rem;line-height:1.6;padding:8px 10px;border-radius:10px;background:rgba(59,130,246,.08);margin-bottom:6px}
.valn{margin-top:6px;font-size:.78rem;opacity:.8}
.tf{display:flex;flex-direction:column;gap:6px}
.tfrow{display:flex;justify-content:space-between;align-items:center;gap:8px;
  background:rgba(128,128,128,.08);border-radius:10px;padding:7px 10px}
.tfl{font-size:.82rem}.tfw{display:block;font-size:.68rem;opacity:.6}
.tfv{font-size:.85rem;text-align:right}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:4px}
.chip{font-size:.75rem;padding:3px 9px;border-radius:999px;background:rgba(128,128,128,.16)}
.cc{border:1px solid rgba(128,128,128,.28);border-left:5px solid var(--c);border-radius:14px;
    padding:12px 14px;margin:10px 0 4px 0;background:rgba(128,128,128,.06)}
.cc.long{--c:#16a34a}.cc.short{--c:#dc2626}
.cc-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.pill{font-weight:700;font-size:.78rem;padding:2px 10px;border-radius:999px;color:#fff}
.pill.long{background:#16a34a}.pill.short{background:#dc2626}
.sym{font-size:1.15rem;font-weight:800}
.tag{font-size:.72rem;padding:1px 8px;border-radius:999px;background:rgba(245,158,11,.22)}
.sub{opacity:.75;font-size:.82rem;margin:3px 0 8px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.grid div{background:rgba(128,128,128,.11);border-radius:10px;padding:6px 8px}
.k{display:block;font-size:.68rem;opacity:.7}
.v{display:block;font-weight:650;font-size:.9rem;word-break:break-all}
.v.sl{color:#dc2626}.v.tp{color:#16a34a}
.foot{font-size:.8rem;opacity:.9;margin-top:8px;line-height:1.55}
.warn{margin-top:6px;font-size:.78rem;color:#d97706}
.status{font-size:.82rem;font-weight:650;margin-top:4px}
.plan{margin-top:8px;font-size:.8rem;line-height:1.5;background:rgba(59,130,246,.10);border-radius:10px;padding:6px 9px}
.over{margin-top:6px;font-size:.78rem;color:#dc2626;font-weight:650}
.perp{margin-top:4px;font-size:.78rem;background:rgba(245,158,11,.15);border-radius:8px;padding:4px 8px}
.tag.ct{background:rgba(147,51,234,.18)}
.tag.lab{background:rgba(59,130,246,.16)}
.tag.grp{background:rgba(16,185,129,.18);font-weight:650}
.dead{font-size:.85rem;padding:6px 2px;border-bottom:1px solid rgba(128,128,128,.2)}
.watch{font-size:.86rem;padding:7px 2px;border-bottom:1px solid rgba(128,128,128,.2);line-height:1.5}
.hot{margin-top:3px;font-size:.78rem;color:#dc2626;font-weight:650}
"""


def fmt_price(x: Optional[float]) -> str:
    """가격 크기에 맞춰 소수점 자리수를 자동 조절."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    ax = abs(x)
    if ax >= 1000:
        return f"{x:,.2f}"
    if ax >= 10:
        return f"{x:,.3f}"
    if ax >= 1:
        return f"{x:.4f}"
    if ax >= 0.01:
        return f"{x:.5f}"
    return f"{x:.8f}"


def fmt_money(x: float) -> str:
    return f"{x:,.2f}" if abs(x) < 1000 else f"{x:,.0f}"


def leverage_guide(entry: float, sl: float, liq_multiple: float = 3.0, cap: int = 10) -> int:
    """격리마진 기준 레버리지 상한 가이드.
    청산까지의 거리(≈1/레버리지)가 손절까지의 거리보다 liq_multiple배 이상 멀도록 잡고, 상한은 cap배.
    (수수료·유지증거금은 무시한 근사치입니다. 레버리지가 높을수록 손절 전에 청산될 위험이 커집니다.)"""
    if not entry:
        return 1
    dist = abs(entry - sl) / entry
    if dist <= 0:
        return 1
    return int(max(1, min(cap, math.floor(1 / (liq_multiple * dist)))))


def side_info(bias: str) -> Tuple[str, str, str]:
    return BIAS_INFO.get(bias, ("long", bias, ""))


def sort_key(setup) -> Tuple[float, float]:
    """정렬: 연구실에서 '우선순위'로 통과한 표시가 많은 신호 먼저 → 알트 지수 대비 상대강도(없으면 BTC 대비).
    롱은 강할수록, 숏은 약할수록 위로."""
    from crypto_market_regime import PRIORITY_TAGS  # 지연 임포트
    pri = sum(t in PRIORITY_TAGS for t in (getattr(setup, "tags", None) or []))
    rs_alt = getattr(setup, "rs_alt", None)
    strength = rs_alt if rs_alt is not None else setup.rs
    return (pri, strength if side_info(setup.bias)[0] == "long" else -strength)


STATUS_INFO = {
    "ready": ("✅", "진입가 근처"), "chase": ("⏳", "추격 구간 · 지정가 대기"),
    "invalid": ("❌", "무효 · 손절선을 먼저 넘음"), "missed": ("⌛", "놓침 · 목표1에 먼저 도달"),
}


def exit_plan_text(setup, scale: float = 1.0) -> str:
    """청산 계획 한 줄. v27 규칙(목표1에서 팔지 않고 추적 손절로 전환) 또는 예전 규칙(목표1 절반 익절)."""
    from crypto_market_regime import exit_of  # 지연 임포트
    frac, trail_k, _ = exit_of(setup)
    side = side_info(setup.bias)[0]
    anchor = "최고가" if side == "long" else "최저가"
    trail = (f"{trail_k:g} ATR(≈{fmt_price(trail_k * setup.atr * scale)})"
             if getattr(setup, "atr", 0) else f"{trail_k:g} ATR")
    if frac >= 1:
        return "목표1에서 전량 익절"
    if frac <= 0:
        return f"목표1(추적 시작)에 닿아도 팔지 않음 → 손절을 진입가로 올림 → 이후 {anchor}에서 {trail} 되돌리면 전량 정리"
    return f"목표1에서 {int(round(frac * 100))}% 익절 → 남은 물량 손절을 진입가로 → 이후 {anchor}에서 {trail} 되돌리면 정리"


def tp1_label(setup) -> str:
    from crypto_market_regime import exit_of  # 지연 임포트
    return "목표1(추적 시작)" if exit_of(setup)[0] <= 0 else "목표1"


def market_price(setup) -> float:
    """v30.2: 수량·레버리지 계산 기준 가격. 시장가 신호는 지금 가격(진입가 표시는 신호 때 가격이라, 그사이 가격이
    움직였으면 손절까지 거리가 달라져 실제 손실이 계획한 리스크보다 커질 수 있었음), 지정가 신호는 진입가."""
    cp = getattr(setup, "current_price", None)
    if entry_label(setup) == "진입(시장가)" and cp:
        lg = side_info(setup.bias)[0] == "long"
        if (lg and cp > setup.sl) or (not lg and cp < setup.sl):
            return float(cp)
    return float(setup.entry_price)


def entry_label(setup) -> str:
    """신고점 돌파·즉시 진입은 시장가, 나머지는 지정가."""
    market = setup.bias.startswith(("donchian", "st_", "retest_", "div_", "pb_", "diag_", "wick_")) or "즉시 진입" in (setup.entry_note or "")
    return "진입(시장가)" if market else "진입(지정가)"


def perp_name(setup) -> str:
    ps = getattr(setup, "perp_symbol", "") or ""
    return (ps.split(":")[0] if ps else setup.symbol).replace("/", "")


def exec_rules(setup) -> list:
    """과거 검증과 똑같이 운용하기 위한 실행 규칙: 지정가 유효기간, 최대 보유 기한, 목표1 이후 트레일링 설정값."""
    import pandas as pd  # 지연 임포트
    from crypto_market_regime import TIMEFRAME, exit_of
    frac, trail_k, hold_bars = exit_of(setup)
    bar = pd.Timedelta(TIMEFRAME)
    now_kst = pd.Timestamp.now(tz="UTC").tz_localize(None) + pd.Timedelta(hours=9)
    hold_days = int(round(hold_bars * bar / pd.Timedelta("1D")))
    out = []
    sig = getattr(setup, "signal_ts", "") or ""
    if getattr(setup, "live_status", "") == "chase" and sig and entry_label(setup) != "진입(시장가)":
        cancel = pd.Timestamp(sig) + bar * 16 + pd.Timedelta(hours=9)  # 신호 봉 마감 + 15봉
        out.append(f"지정가 유효: {cancel:%m-%d %H}시까지 — 그때까지 안 닿으면 주문 취소")
        out.append(f"보유 기한: 체결 후 최대 {hold_days}일 — 그때까지 목표1·손절 모두 안 닿으면 정리")
    else:
        out.append(f"보유 기한: 지금 진입하면 {now_kst + pd.Timedelta(days=hold_days):%m-%d}까지 — "
                   f"목표1·손절 모두 안 닿으면 그날 정리")
    if getattr(setup, "atr", 0) and setup.tp1:
        cb = trail_k * setup.atr / setup.tp1 * 100
        if frac <= 0:
            out.append(f"진입 직후: 손절 주문 + 거래소 트레일링 주문(전량, 활성화 가격 = 목표1, 되돌림 {cb:.1f}%)을 미리 걸어두기")
            out.append("목표1 도달 시: 익절하지 말고 손절 주문만 진입가로 옮기기(본전) — 이후는 트레일링 주문이 정리")
        else:
            out.append(f"목표1 도달 시: {int(frac * 100)}% 익절 → 남은 물량 손절을 진입가로 → "
                       f"거래소 트레일링 주문(활성화 가격 = 목표1, 되돌림 {cb:.1f}%)")
    # 2026-10-16 추세 유효/깨짐 지표 10종 5년 검증: 눌림목은 진입 때 이미 추세가 깨져 보이는 게 정상(약 80%),
    # 그걸 이유로 미리 청산하면 거래당 +0.26R → +0.01~0.10R (3자료 모두). 박스·되돌림은 영향 거의 없음.
    from crypto_market_regime import SETUP_FAMILY
    if SETUP_FAMILY.get(getattr(setup, "bias", ""), "") == "빗각 지지":
        out.append("빗각 지지는 일봉 신호라 손절이 넓어요(진입가 − 2 일봉 ATR) — 수량은 이 손절폭 기준으로 계산했으니 레버리지만 지키기. "
                   "5년 검증: 승률 약 40%, 평균 보유 17일, 수익은 소수의 큰 상승(5R 넘는 거래)에서 나옴 — 작은 이익에 미리 팔지 않기")
    if SETUP_FAMILY.get(getattr(setup, "bias", ""), "") == "아래꼬리 연속":
        out.append("아래꼬리 연속은 일봉 신호라 손절이 넓어요(진입가 − 2 일봉 ATR) — 수량은 이 손절폭 기준으로 계산했으니 레버리지만 지키기. "
                   "5년 검증: 승률 약 43%, 평균 보유 17일, 5R에 닿은 뒤에야 최고가 − 3 ATR(4시간봉)로 따라 올리며, "
                   "수익은 소수의 큰 상승에서 나옴 — 작은 이익에 미리 팔지 않기")
    if SETUP_FAMILY.get(getattr(setup, "bias", ""), "") == "추세 눌림목":
        out.append("눌림목은 진입 때 이미 추세가 깨진 것처럼 보이는 게 정상(고점·저점 이탈·슈퍼트렌드 반대가 약 80%) — "
                   "그걸 이유로 손절·트레일링 전에 미리 청산하지 않기 (5년 검증: 그렇게 청산하면 거래당 +0.26R → +0.01~0.10R)")
    add = pyramid_line(setup)
    if add:
        out.append(add)
    return out


PYRAMID_SIZE = 0.5     # v32.7: 목표1에 닿으면 처음 수량의 0.5배 추가(5년 검증 전 신호 6/6 · 수동 6/6, 두 번 통과)


def pyramid_line(setup) -> Optional[str]:
    """불타기 안내(v32.7): 목표1에 처음 닿을 때 처음 수량의 0.5배 추가 — 추가분도 같은 추적 손절로 함께 정리."""
    e_, sl_, tp_ = getattr(setup, "entry_price", None), getattr(setup, "sl", None), getattr(setup, "tp1", None)
    if not e_ or not sl_ or not tp_ or e_ == sl_:
        return None
    from crypto_market_regime import SETUP_FAMILY, CHOP_FAMILIES, exit_of  # 지연 임포트
    if SETUP_FAMILY.get(getattr(setup, "bias", ""), "") not in CHOP_FAMILIES:
        return None
    lg = side_info(setup.bias)[0] == "long"
    m = int(getattr(setup, "perp_mult", 1) or 1)
    trail_k = exit_of(setup)[1]
    return (f"➕ 불타기: 목표1 {fmt_price(tp_ * m)}에 닿으면 처음 수량의 {PYRAMID_SIZE:g}배 추가 — 진입할 때 목표1 가격에 "
            f"{'매수' if lg else '매도'} 역지정가(조건부) 주문을 같이 걸어 두면 돼요. 추가분도 처음 물량과 같은 추적 손절"
            f"({'최고가 −' if lg else '최저가 +'} {trail_k:g} ATR, 진입가보다 불리해지지 않게)로 함께 정리")


def elapsed_note(setup):
    """시장가 진입 신호의 '신호 봉 마감 후 경과 시간' 안내 (2년·82개 코인 1시간봉 검증 기준).
    반환 (문구, 단계: ok / warn / stop) 또는 None."""
    import pandas as pd  # 지연 임포트
    from crypto_market_regime import TIMEFRAME, ELAPSED_WARN_H, ELAPSED_STOP_H
    sig = getattr(setup, "signal_ts", "") or ""
    if not sig or entry_label(setup) != "진입(시장가)":
        return None
    close = pd.Timestamp(sig) + pd.Timedelta(TIMEFRAME)
    h = (pd.Timestamp.now(tz="UTC").tz_localize(None) - close).total_seconds() / 3600
    if h < 0:
        return None
    t = f"{int(h * 60)}분" if h < 1 else f"{h:.1f}시간"
    if setup.bias.startswith("diag_"):  # v31 5년 검증(Bitget 선물): 09시 +0.51R · 13시 +0.50R · 17시 +0.49R · 21시 +0.44R
        if h >= 13:
            return (f"⛔ 일봉 마감(09시) 후 {t} — 검증해 본 범위(21시까지) 밖이라 건너뛰세요", "stop")
        return (f"⏱ 일봉 마감(09시) 후 {t} — 빗각 지지는 그날 안에 들어가도 괜찮아요"
                f"(5년 검증 거래당: 09시 +0.51R · 13시 +0.50R · 17시 +0.49R · 21시 +0.44R)", "ok")
    if setup.bias.startswith("wick_"):  # v32 5년 검증(Bitget 선물): 09시 +0.71R · 13시 +0.70R · 17시 +0.63R · 21시 +0.62R
        if h >= 13:
            return (f"⛔ 일봉 마감(09시) 후 {t} — 검증해 본 범위(21시까지) 밖이라 건너뛰세요", "stop")
        return (f"⏱ 일봉 마감(09시) 후 {t} — 아래꼬리 연속은 그날 안에 들어가도 괜찮아요"
                f"(5년 검증 거래당: 09시 +0.71R · 13시 +0.70R · 17시 +0.63R · 21시 +0.62R)", "ok")
    if setup.bias.startswith("pb_"):    # 5년 검증: 바로 +0.29R · 4시간 늦게 +0.31R · 8시간 늦게 +0.25R
        if h >= 12:
            return (f"⛔ 신호 후 {t} — 12시간이 넘었어요. 검증해 본 범위(8시간) 밖이라 건너뛰세요", "stop")
        if h >= 8:
            return (f"⚠️ 신호 후 {t} — 8시간 넘게 늦으면 엣지가 줄어요(5년 검증: 8시간 늦게 +0.25R). 리스크를 줄이거나 건너뛰세요", "warn")
        return (f"⏱ 신호 후 {t} — 추세 눌림목은 늦어도 괜찮아요(5년 검증: 바로 +0.29R · 4시간 늦게 +0.31R · 8시간 늦게 +0.25R)", "ok")
    if setup.bias.startswith("div_"):   # 늦은 진입에 강함 (24개월 검증)
        if h >= 12:
            return (f"⛔ 신호 후 {t} — 12시간이 넘었어요. 검증해 본 범위(8시간) 밖이라 건너뛰세요", "stop")
        if h >= 8:
            return (f"⚠️ 신호 후 {t} — 8시간 넘게 늦으면 엣지가 줄어요(검증: 8시간 늦게 +0.70R). 리스크를 줄이거나 건너뛰세요", "warn")
        return (f"⏱ 신호 후 {t} — 위치 다이버전스는 늦어도 괜찮아요(검증: 바로 +0.96R · 4시간 늦게 +0.87R · 8시간 늦게 +0.70R). "
                f"새벽 신호는 아침에 들어가도 돼요", "ok")
    if setup.bias.startswith("retest_"):
        if h >= ELAPSED_STOP_H:
            return (f"⛔ 신호 후 {t} — 진입 비권장. 되돌림 확인 뒤 늦게 들어가면 엣지가 줄어요(5년 검증: 바로 +0.48R → 4시간 늦게 +0.32R)", "stop")
        if h >= ELAPSED_WARN_H:
            return (f"⚠️ 신호 후 {t} — 엣지가 줄고 있어요(5년 검증: 바로 +0.48R → 4시간 늦게 +0.32R). 리스크를 줄이거나 건너뛰세요", "warn")
        return (f"⏱ 신호 후 {t} — 지금 진입이 가장 좋아요(5년 검증: 봉 마감 직후 +0.48R)", "ok")
    don = setup.bias.startswith("donchian")
    if don and h >= 1:
        return (f"⛔ 신호 후 {t} — 신고점 돌파는 1시간 안에 들어가야 해요(2시간 넘으면 검증 결과 마이너스). 건너뛰세요", "stop")
    if h >= ELAPSED_STOP_H:
        return (f"⛔ 신호 후 {t} — 진입 비권장. 늦게 따라 들어간 돌파는 엣지가 크게 줄어요(5년 검증: 바로 +0.51R → 4시간 늦게 +0.38R → 8시간 +0.30R)", "stop")
    if h >= ELAPSED_WARN_H:
        return (f"⚠️ 신호 후 {t} — 엣지가 줄었어요. 리스크를 줄이거나 건너뛰세요(5년 검증: 바로 +0.51R → 4시간 늦게 +0.38R)", "warn")
    if h >= 1:
        return (f"⏱ 신호 후 {t} — 아직 괜찮지만 엣지가 줄기 시작했어요(5년 검증: 늦을수록 줄어 4시간 늦으면 +0.38R)", "ok")
    return (f"⏱ 신호 후 {t} — 지금 진입이 가장 좋아요(5년 검증: 봉 마감 직후 +0.51R)", "ok")


def flow_summary_html(regime) -> str:
    """시장 흐름 한 줄 요약: 일봉 흐름 점수, 며칠째인지, 최근 7일 강해지는지·약해지는지, 지수별 변화."""
    f = getattr(regime, "flow", None) or {}
    if not f.get("score"):
        return ""
    sc = f["score"][-1]
    st_kr = "상승" if sc >= 0.25 else ("하락" if sc <= -0.25 else "횡보")
    color = "#16a34a" if sc >= 0.25 else ("#dc2626" if sc <= -0.25 else "#d97706")
    d7 = f.get("delta7")
    trend = ""
    if d7 is not None:
        trend = " · 최근 7일 " + ("강해지는 중 ▲" if d7 > 0.1 else ("약해지는 중 ▼" if d7 < -0.1 else "큰 변화 없음"))
    chips = []
    if f.get("btc"):
        chips.append(f"BTC {f['btc'][-1] - 100:+.0f}%")
    if f.get("alt"):
        chips.append(f"알트 지수 {f['alt'][-1] - 100:+.0f}%")
    if f.get("b50pct") and f["b50pct"][-1] is not None:
        chips.append(f"50일선 위 코인 {f['b50pct'][-1]:.0f}%")
    days = len(f["score"])
    return (f'<div class="flow"><b style="color:{color}">일봉 흐름 {st_kr} {sc:+.2f}</b> · {f.get("state_days", 1)}일째{trend}'
            f'<br><span class="k">최근 {days}일 변화: {" · ".join(chips)}</span></div>')


def order_memo(setup, sizing: Dict, lev: int) -> str:
    """Bitget 선물 주문용 메모. 1000배 단위로 표기되는 선물은 가격·수량을 선물 기준으로 환산."""
    side, side_kr, sub = side_info(setup.bias)
    m = int(getattr(setup, "perp_mult", 1) or 1)
    lines = [
        f"{perp_name(setup)}  {side_kr} ({sub})",
        f"{entry_label(setup)}: {fmt_price(setup.entry_price * m)}",
        f"손절: {fmt_price(setup.sl * m)}",
        (f"{tp1_label(setup)}: {fmt_price(setup.tp1 * m)}" if tp1_label(setup) != "목표1"
         else f"목표1: {fmt_price(setup.tp1 * m)}  /  목표2(참고): {fmt_price(setup.tp2 * m)}"),
        f"청산: {exit_plan_text(setup, m)}",
        f"수량: {sizing['size'] / m:.4f}  (명목 ${fmt_money(sizing['notional'])})",
        f"레버리지 상한 가이드: {lev}배 이하 (격리)",
    ] + [f"실행: {x}" for x in exec_rules(setup)]
    if m > 1:
        lines.append(f"※ Bitget 선물은 {m:,}배 단위 표기라 가격은 ×{m:,}, 수량은 ÷{m:,} 해서 적었어요")
    return "\n".join(lines)


def watch_line(w: Dict) -> str:
    """돌파 임박 관찰 목록 한 줄. 선물이 1000배 표기면 가격도 선물 기준으로 환산."""
    m = int(w.get("perp_mult", 1) or 1)
    name = (w.get("perp_symbol") or w["symbol"]).split(":")[0].replace("/", "")
    long_side = w["side"] == "long"
    what = "⬆ 상단 돌파 대기 (롱)" if long_side else "⬇ 하단 이탈 대기 (숏)"
    vol = w.get("vol_ratio")
    vol_txt = f" · 거래량 {vol:.1f}배" + (" ↑" if vol and vol >= 1.2 else "") if vol else ""
    hot = ('<div class="hot">🔥 지금 경계를 넘는 중 — 이 봉이 경계 밖에서 마감하면(거래량 1.5배 이상) 추천으로 올라와요</div>'
           if w.get("crossing") else "")
    chk = "✓ Bitget 선물" if w.get("perp_ok") else ("Bitget 선물" if w.get("perp_symbol") else "⚠ 선물 미확인")
    return (f'<div class="watch"><b>{html.escape(name)}</b> <span class="k">({chk})</span> {what}<br>'
            f'<span class="k">경계 {fmt_price(w["trigger"] * m)} · 현재가 {fmt_price(w["price"] * m)} · '
            f'남은 거리 {w["dist_atr"]:.1f} ATR{vol_txt} · 박스 {html.escape(w.get("touches", ""))}</span>{hot}</div>')


def paused_line(setup) -> str:
    """자동 중지된 신호 한 줄 (참고용)."""
    side, side_kr, sub = side_info(setup.bias)
    return (f'<div class="dead"><b>{html.escape(setup.symbol)}</b> {side_kr} · {html.escape(sub)} '
            f'<span class="k">(진입 {fmt_price(setup.entry_price)} · 손절 {fmt_price(setup.sl)})</span></div>')


PORT_ICON = {"신규 진입": "🟢", "비중 조정": "🔄", "정리": "🔴", "유지": "⚪"}


def portfolio_line(r: Dict) -> str:
    """추세 포트폴리오 한 줄. 선물이 1000배 표기면 가격·수량을 선물 기준으로 환산."""
    m = int(r.get("perp_mult", 1) or 1)
    name = (r.get("perp_symbol") or r["symbol"]).split(":")[0].replace("/", "")
    qty = (r.get("qty") or 0.0) / m
    dq = (r.get("delta_qty") or 0.0) / m
    if r["status"] == "정리":
        why = f" — {html.escape(r['reason'])}" if r.get("reason") else ""
        body = f"<b>전량 정리</b> (어제 목표 {r['prev_weight']:.0%} · ${fmt_money(r.get('prev_notional', 0.0))}){why}"
    elif r["status"] == "비중 조정":
        verb = "늘리기" if dq > 0 else "줄이기"
        body = (f"비중 {r['prev_weight']:.0%} → <b>{r['weight']:.0%}</b> · 목표 ${fmt_money(r['notional'])} "
                f"(수량 약 {qty:,.4g}개 · {abs(dq):,.4g}개 {verb})")
    elif r["status"] == "신규 진입":
        body = f"목표 비중 <b>{r['weight']:.0%}</b> · ${fmt_money(r['notional'])} (롱 수량 약 {qty:,.4g}개)"
    else:
        body = f"목표 비중 <b>{r['weight']:.0%}</b> · ${fmt_money(r['notional'])} (수량 약 {qty:,.4g}개)"
    mn = r.get("min_notional") or 5.0                     # 모르면 Bitget 최소 주문 금액 5 USDT로 가정
    small = r["status"] != "정리" and 0 < r.get("notional", 0.0) < mn
    if small:
        body += f'<br>⚠️ 목표 금액이 최소 주문(약 ${fmt_money(mn)})보다 작아요 — 이 코인은 건너뛰세요(배분을 늘리면 해결)'
    return (f'<div class="watch">{PORT_ICON.get(r["status"], "")} <b>{html.escape(name)}</b> {r["status"]} · {body}<br>'
            f'<span class="k">종가 {fmt_price(r["close"] * m)} · 50일선 {fmt_price(r["exit_line"] * m)}'
            f'{" · 30일 수익률 " + format(r["ret30"], "+.1%") if r.get("ret30") is not None else ""}'
            f'{" · 선물 ×" + format(m, ",") + " 표기" if m > 1 else ""}</span></div>')


def portfolio_gate_html(pf: Dict, gate_text: str) -> str:
    """시장 관문 상태 상자"""
    g = pf.get("gate") or {}
    if not g.get("enabled", True):
        return ""
    on = g.get("on")
    head = ("🟢 <b>시장 관문 켜짐</b> — 추세 코인을 보유하는 구간" if on
            else "🔴 <b>시장 관문 꺼짐</b> — 포트폴리오 전부 현금(정리) 구간")
    chg = " · <b>오늘 바뀜</b>" if g.get("changed") else f" · {g.get('days', 0)}일째"
    tail = ""
    if not on:
        need = []
        if not g.get("btc_ok") and g.get("btc_sma"):
            need.append(f"BTC 종가가 100일선({fmt_price(g['btc_sma'])}) 위로 마감")
        if not g.get("breadth_ok"):
            need.append(f"20일선 위 코인 {g.get('need_up', 0)}개 이상(지금 {g.get('n_up', 0)}개)")
        tail = "<br>켜지는 조건: " + " 그리고 ".join(need) + " — 둘 다 맞는 날 09시에 진입 목록이 떠요"
        if pf.get("waiting"):
            tail += "<br>켜지면 들어갈 코인(개별 추세 충족): " + ", ".join(
                html.escape(s.split("/")[0]) for s in pf["waiting"])
    return (f'<div class="allot">{head}{chg}<br><span class="k">{html.escape(gate_text)} · '
            f'최근 90일 중 켜진 날 {g.get("on_ratio_90d", 0):.0%}</span>{tail}</div>')


def portfolio_exec_rules(gross_cap: float, deadline_kst: str) -> str:
    """포트폴리오 주문 방법(마크다운)"""
    margin = gross_cap / 2
    return (f"- ⏰ **언제**: 매일 09시 일봉 마감 뒤 목록이 바뀌어요. **오늘 {deadline_kst}까지** 조정하세요. "
            "검증에서 12시간 안에 하면 효과가 유지됐고, 하루 늦게 하면 대부분 사라졌어요. 놓친 날은 넘기고 다음 날 목록을 따르세요.\n"
            "- 🎯 **얼마나**: 목록의 **목표 금액(명목)** 에 맞추세요. 실제 보유가 앱의 어제 목표와 달라도 목표 금액 기준으로 맞추면 돼요.\n"
            "- 🧾 **주문**: 지정가(Post-only)로 매수 1호가/매도 1호가에 걸고, 30~60분 안에 안 채워지면 시장가로 바꾸세요 "
            "(전부 지정가로 채워지면 비용이 연 3%p 정도 줄어요). 관문이 꺼진 날·정리는 그날 안에 전부 정리.\n"
            f"- 🛡 **증거금**: 격리 마진·**레버리지 2배 고정**. 청산가가 진입가에서 약 −50% 떨어져 있어 급락 꼬리에도 안전해요. "
            f"비중 합계 {gross_cap:.0%}면 증거금은 포트폴리오 배분의 최대 {margin:.0%}가 필요해요. 3배 이상은 급락 때 청산 위험.\n"
            "- 🚫 **손절 주문 없음**: 일봉 종가로만 판단해요(장중 손절은 검증에서 오히려 나빴어요). 대신 관문·50일선이 정리 신호예요.")


def dead_line(setup) -> str:
    """무효·놓침 추천을 한 줄로."""
    side, side_kr, _ = side_info(setup.bias)
    icon, label = STATUS_INFO.get(setup.live_status, ("", setup.live_status))
    return (f'<div class="dead"><b>{html.escape(setup.symbol)}</b> {side_kr} · {icon} {html.escape(label)} '
            f'<span class="k">(현재가 {fmt_price(setup.current_price)})</span></div>')


def _hold_text(bars: int) -> str:
    import pandas as pd  # 지연 임포트
    from crypto_market_regime import TIMEFRAME
    d = bars * pd.Timedelta(TIMEFRAME) / pd.Timedelta("1D")
    return f"{d:g}일" if d >= 1 else f"{bars}봉"


def card_html(setup, sizing: Dict, risk_pct: float, over_limit: bool = False) -> str:
    side, side_kr, sub = side_info(setup.bias)
    mkt = entry_label(setup) == "진입(시장가)"
    lev = leverage_guide(market_price(setup), setup.sl)
    e = html.escape
    tags = ""
    if setup.poc_confluence:
        tags += '<span class="tag">🎯 매물대 겹침</span>'
    if setup.sweep_confluence:
        tags += '<span class="tag">🩸 유동성 스윕</span>'
    grp = getattr(setup, "group", "")
    if grp:
        tags += f'<span class="tag grp">{e(grp)}</span>'
    vt = getattr(setup, "vol_tier", "")
    if vt:
        tags += f'<span class="tag">{e(vt.replace("거래량 ", ""))}</span>'
    if getattr(setup, "counter_trend", False):
        tags += '<span class="tag ct">↔ 시장 역행</span>'
    from crypto_market_regime import SETUP_FAMILY, exit_of  # 지연 임포트
    fam_ = SETUP_FAMILY.get(setup.bias, "")
    if fam_ in FAMILY_TAG:
        tags += f'<span class="tag lab">{e(FAMILY_TAG[fam_])}</span>'
    status = getattr(setup, "live_status", "") or ("chase" if setup.is_chase else "ready")
    s_icon, s_label = STATUS_INFO.get(status, ("", ""))
    from crypto_market_regime import TAG_LABELS, PRIORITY_TAGS, LAB_TAGS  # 지연 임포트
    for t in [t for t in (getattr(setup, "tags", None) or []) if t in PRIORITY_TAGS or t in LAB_TAGS]:
        star = "⭐ " if t in PRIORITY_TAGS else ""
        tags += f'<span class="tag lab">{star}{e(TAG_LABELS.get(t, t))}</span>'
    stats = f"BTC 대비 {setup.rs:+.1f}%"
    if getattr(setup, "rs_alt", None) is not None:
        stats += f" · 알트 대비 {setup.rs_alt:+.1f}%"
    warn = ""
    if status == "chase" and setup.entry_price and mkt:
        # v30.2: 시장가 신호(박스 돌파·되돌림·눌림목)는 '지정가 대기'가 검증에서 더 나빴음 → 지정가 안내 대신 시간 기준 안내
        s_icon, s_label = "⏳", "신호 때 가격에서 멀어짐 · 시장가 신호"
        gap = (setup.current_price - setup.entry_price) / setup.entry_price * 100
        warn = (f'<div class="warn">⏳ 현재가가 신호 때 가격보다 {abs(gap):.1f}% {"위" if gap > 0 else "아래"} — '
                f'시장가 신호라 지정가로 되돌림을 기다리지 마세요(5년 검증: 되돌아올 때만 체결되면 크게 가는 신호를 놓쳐 더 나빴어요). '
                f'신호 봉 마감 2시간 안이면 지금 가격으로 진입하고, 지났으면 건너뛰세요. 수량은 지금 가격 기준으로 계산했어요</div>')
    elif status == "chase" and setup.entry_price:
        gap = (setup.current_price - setup.entry_price) / setup.entry_price * 100
        warn = (f'<div class="warn">⏳ 현재가가 진입가보다 {abs(gap):.1f}% '
                f'{"위" if gap > 0 else "아래"} — 지정가만 걸어두고 기다리세요 (지금 시장가 진입은 추격)</div>')

    def cell(k: str, v: str, cls: str = "") -> str:
        return f'<div><span class="k">{e(k)}</span><span class="v {cls}">{e(v)}</span></div>'

    grid = "".join([
        cell("현재가", fmt_price(setup.current_price)),
        cell(entry_label(setup), fmt_price(setup.entry_price)),
        cell("손절", fmt_price(setup.sl), "sl"),
        cell(tp1_label(setup), fmt_price(setup.tp1), "tp"),
        (cell("최대 보유", _hold_text(exit_of(setup)[2])) if exit_of(setup)[0] <= 0
         else cell("목표2(참고)", fmt_price(setup.tp2), "tp")),
        cell("손익비", f"{setup.rr_ratio:.2f}"),
    ])
    foot = (f"수량 <b>{sizing['size']:.4f}</b> · 명목 ${fmt_money(sizing['notional'])} · "
            f"손절 시 손실 ${fmt_money(sizing['risk_amount'])} (계좌의 {risk_pct:g}%)<br>"
            f"레버리지 상한 가이드 <b>{lev}배 이하</b> (격리마진)")
    plan = (f'<div class="plan">🧭 청산: {e(exit_plan_text(setup))}'
            + "".join(f"<br>📅 {e(x)}" for x in exec_rules(setup)) + "</div>")
    m = int(getattr(setup, "perp_mult", 1) or 1)
    perp_note = (f'<div class="perp">Bitget 선물 표기 <b>{e(perp_name(setup))}</b> — 선물 주문 가격은 아래 값 ×{m:,} '
                 f'(주문 메모에는 환산해서 적었어요)</div>') if m > 1 else ""
    if over_limit:
        warn += '<div class="over">⛔ 총 리스크 상한 초과 — 지금은 참고만 (보유 포지션이 정리되면 검토)</div>'
    if sizing.get("capped"):
        warn += (f'<div class="over">💧 유동성 상한: 이 코인 24시간 거래대금의 작은 비율(${fmt_money(sizing["cap_notional"])})로 '
                 f'주문 금액을 줄였어요 — 큰 주문은 체결 비용이 커져서 검증 성과를 깎아요</div>')
    el = elapsed_note(setup)
    if el:
        cls = "over" if el[1] in ("warn", "stop") else "plan"
        warn += f'<div class="{cls}">{e(el[0])}</div>'
    if getattr(setup, "event_note", ""):
        warn += (f'<div class="over">📅 {e(setup.event_note)} — 크게 흔들리는 시간대라 권장 리스크를 절반으로 계산했어요. '
                 f'검증에서 이 시간대 진입은 평균 −0.25R이었으니 가능하면 건너뛰세요</div>')
    if getattr(setup, "drift_note", ""):     # v32.3 FOMC 선반영 참고(표시만)
        warn += f'<div class="plan">📐 {e(setup.drift_note)}</div>'
    if getattr(setup, "fixed_risk", None) and fam_ in ("빗각 지지", "아래꼬리 연속"):
        warn += (f'<div class="over">✅ {fam_} 승격 — 실전 30건 확인을 통과했지만 5년 계좌 모의를 0.5배로 해서 '
                 f'위험 {setup.fixed_risk:g}배까지만 써요. 수량은 이미 낮춰서 계산했어요</div>')
    elif getattr(setup, "fixed_risk", None):
        warn += (f'<div class="over">📉 추세 눌림목은 위험 {setup.fixed_risk:g}배로 고정이에요 — 거래당 수익이 박스 돌파·되돌림보다 작아, '
                 '5년 검증에서 비중을 낮춘 쪽이 계좌 효율(연수익÷낙폭)이 더 좋았어요. 수량은 이미 낮춰서 계산했어요</div>')
    if getattr(setup, "sunday_half", False):
        warn += ('<div class="over">🗓 일요일 진입 — 5년 검증에서 일요일에 들어간 박스 돌파·되돌림·눌림목은 손절이 잦아'
                 '(72~76%, 다른 날 56~58%) 거래당 손해였어요. 조용한 일요일 변동폭으로 잡은 손절이 월요일 오전 변동성에 걸리기 쉬워 '
                 '리스크를 절반으로 계산했어요</div>')
    if getattr(setup, "chop_half", False):
        warn += (f'<div class="over">🌊 시장 출렁임 지수 {getattr(setup, "chop_value", 0) or 0:.1f} (기준 52 초과) — 시장 전체가 방향 없이 출렁이는 구간이에요. '
                 '5년 검증에서 이런 때 박스 돌파·되돌림 성적이 크게 떨어져 리스크를 절반으로 계산했어요</div>')
    if getattr(setup, "half_start", False):
        warn += ('<div class="over">🌱 새 매매법 — 실전 기록이 쌓이기 전이라 리스크를 절반으로 낮춰 수량을 계산했어요 '
                 '(실전 추적 30건 평균이 플러스면 자동으로 정상 리스크)</div>')
    if getattr(setup, "trial", False) and fam_ == "빗각 지지":
        warn += ('<div class="over">🧪 시험 운용 신호(빗각 지지) — 5년 검증에서 거래당 성과는 좋았지만 사전 기준(사이클 교차·전 신호 계좌)을 '
                 '다 넘지 못해 리스크를 ¼로 낮춰 수량을 계산했어요. 실전 30건 평균이 플러스면 0.5배로 승격</div>')
    elif getattr(setup, "trial", False) and fam_ == "아래꼬리 연속":
        warn += ('<div class="over">🧪 시험 운용 신호(아래꼬리 연속) — 5년 검증의 사전 기준(3자료·사이클·플라시보·계좌)을 모두 통과했지만 '
                 '새 매매법이라 실전 30건을 확인할 때까지 리스크를 ¼로 낮춰 수량을 계산했어요. 실전 30건 평균이 플러스면 0.5배로 승격</div>')
    elif getattr(setup, "trial", False):
        warn += ('<div class="over">🧪 시험 운용 신호 — 리스크를 ¼로 낮춰 수량을 계산했어요 '
                 '(실전 30건이 과거 기준선 안에 들면 자동으로 정상 리스크로 승격)</div>')
    # v31: 작은 계좌에서 위험을 낮춘 신호(시험 운용 ¼·눌림목 ½)는 주문 금액이 거래소 최소 주문보다 작을 수 있음
    try:
        from crypto_market_regime import bitget_min_notional  # 지연 임포트
        mult_ = int(getattr(setup, "perp_mult", 1) or 1)
        mn_ = bitget_min_notional(getattr(setup, "perp_symbol", "") or "", market_price(setup) * mult_) or 5.0
    except Exception:
        mn_ = 5.0
    if sizing.get("notional") and 0 < sizing["notional"] < mn_:
        need_ = mn_ / sizing["notional"] * risk_pct
        tip_ = ("시험 운용 신호라 건너뛰고 결과만 지켜보는 걸 권해요(앱이 추천 결과를 자동으로 기록해요)"
                if getattr(setup, "trial", False) else "건너뛸지, 최소 금액으로 넣고 더 큰 위험을 감수할지 정하세요")
        warn += (f'<div class="over">🪙 주문 금액 ${fmt_money(sizing["notional"])}가 Bitget 최소 주문(약 ${fmt_money(mn_)})보다 작아요 — '
                 f'최소 금액으로 넣으면 손절 시 손실이 계좌의 약 {need_:.2f}%(계산한 위험 {risk_pct:g}%)가 돼요. {tip_}</div>')
    if getattr(setup, "health", "") == "caution":
        warn += ('<div class="over">🛡 자동 방어: 이 신호 유형의 최근 실전 성과가 기준 아래라 '
                 '권장 리스크를 절반으로 낮춰 수량을 계산했어요</div>')
    if getattr(setup, "already_tracked", ""):
        warn += (f'<div class="over">📌 같은 코인·같은 방향 추천({e(setup.already_tracked)})이 아직 진행 중이에요 — '
                 '검증 규칙은 코인당 한 포지션이라 이 추천은 실전 추적에 따로 남지 않아요. 앞 포지션을 들고 있으면 추가 진입하지 마세요</div>')
    if getattr(setup, "opposite_tracked", ""):
        warn += (f'<div class="over">↔️ 이 코인은 반대 방향 추천({e(setup.opposite_tracked)})이 아직 진행 중이에요 — '
                 '검증 규칙은 방향과 상관없이 코인당 한 포지션이라 이 추천은 건너뛰고 실전 추적에도 남지 않아요. '
                 '앞 포지션은 원래 손절·목표대로 두세요 (5년 검증에서 이런 경우는 자료마다 4~7번뿐이고, 무시·양쪽 보유·뒤집기의 결과 차이가 거의 없었어요)</div>')
    return (f'<div class="cc {side}">'
            f'<div class="cc-head"><span class="pill {side}">{side.upper()} {e(side_kr)}</span>'
            f'<span class="sym">{e(setup.symbol)}</span>{tags}</div>'
            f'<div class="status">{s_icon} {e(s_label)}</div>{perp_note}'
            f'<div class="sub">{e(sub)} · {e(stats)}</div>'
            f'<div class="grid">{grid}</div>'
            f'{plan}<div class="foot">{foot}</div>{warn}</div>')


BIG_FLOW = {"uptrend": "상승 쪽", "downtrend": "하락 쪽", "sideways": "중립"}


def macro_trade_line(mf: Optional[Dict]) -> str:
    """v29.7: 이 카드가 실제 추천에 주는 영향(거시 필터)을 한 줄로 — 색이 아니라 이 줄이 매매 규칙."""
    mf = mf or {}
    sc = mf.get("score")
    if sc is None:
        return "📌 매매 규칙: 일봉 흐름 점수를 아직 계산하지 못해 롱·숏 모두 추천해요"
    if sc >= 0.25:
        if mf.get("blocked_side") == "숏":
            return (f"📌 매매 규칙: 일봉 흐름 점수 {sc:+.2f} (+0.25 이상) → <b>숏 추천 제외, 롱만</b> "
                    "· 5년 검증에서 이 구간 숏은 평균 −0.08R(2021~23 −0.63R)")
        return (f"📌 일봉 흐름 점수 {sc:+.2f} (+0.25 이상)인데 거시 필터가 꺼져 있어 숏도 추천 중 "
                "· 5년 검증에서 이 구간 숏은 평균 −0.08R(2021~23 −0.63R)")
    if sc <= -0.25:
        if mf.get("blocked_side") == "롱":
            return (f"📌 매매 규칙: 일봉 흐름 점수 {sc:+.2f} (−0.25 이하) → <b>롱 추천 제외, 숏만</b> "
                    "· 5년 검증에서 이 구간 롱은 평균 −0.23R")
        return (f"📌 일봉 흐름 점수 {sc:+.2f} (−0.25 이하)인데 거시 필터가 꺼져 있어 롱도 추천 중 "
                "· 5년 검증에서 이 구간 롱은 평균 −0.23R")
    return (f"📌 매매 규칙: 일봉 흐름 점수 {sc:+.2f} (−0.25~+0.25 중립) → <b>롱·숏 모두 추천</b> "
            "· 5년 검증에서 신호 성과가 가장 좋았던 구간(롱 +0.41R · 숏 +0.82R)")


DIR_COLOR = {"down": ("#dc2626", "rgba(220,38,38,.10)"), "down_w": ("#f97316", "rgba(249,115,22,.09)"),
             "flat": ("#6b7280", "rgba(107,114,128,.10)"), "up_w": ("#65a30d", "rgba(101,163,13,.09)"),
             "up": ("#16a34a", "rgba(22,163,74,.10)")}
def _reason_txt(r: dict) -> str:
    """모델 근거 하나를 쉬운 말로: 무엇이 얼마인지 + 오를 확률을 높였는지·낮췄는지"""
    k, v = r["k"], r["v"]
    txt = {"r7": f"BTC 최근 7일 {v:+.0%}", "r30": f"BTC 30일 {v:+.0%}", "r90": f"BTC 90일 {v:+.0%}",
           "d_e50": f"BTC가 50일 평균보다 {abs(v):.0%} {'위' if v >= 0 else '아래'}",
           "d_s200": f"BTC가 200일선보다 {abs(v):.0%} {'위' if v >= 0 else '아래'}",
           "breadth": f"20일선 위 알트 {v:.0%}", "macro": f"일봉 흐름 점수 {v:+.2f}",
           "alt_r30": f"알트 지수 30일 {v:+.0%}",
           "altbtc_r30": f"알트가 30일간 BTC보다 {abs(v):.0%} {'더 오름' if v >= 0 else '덜 오름'}"}.get(k, f"{k} {v:+.2f}")
    return f"{txt} → 오를 확률 {'높임 ↑' if r['c'] > 0 else '낮춤 ↓'}"


def direction_html(d: dict, macro_filter: Optional[Dict] = None) -> str:
    """v30 메인 거시 카드 — 검증을 통과한 것만 위에, 약한 참고는 아래로.
    ① BTC 보유 판단(150일선, 5년 검증 통과) ② 추천 신호 규칙(거시 필터, 검증 통과)
    ③ 앞으로 2주 기울기(시간 순서 재검증에서 동전 수준 → 참고만) ④ 1~3일(어떤 지표도 50% 못 넘음 → 판단 안 함)"""
    e = html.escape
    mid, sh, hd = (d or {}).get("mid") or {}, (d or {}).get("short") or {}, (d or {}).get("hold") or {}
    from crypto_market_regime import BTC_HOLD_MA, BTC_HOLD_STATS
    sa, sb = BTC_HOLD_STATS["A"], BTC_HOLD_STATS["B"]
    proof = (f'5년 검증: 2021~23 연 {sa["cagr"]:+.0%}(그냥 보유 {sa["bh_cagr"]:+.0%}) · 최대 낙폭 {sa["mdd"]:.0%}({sa["bh_mdd"]:.0%}) | '
             f'2024~ 연 {sb["cagr"]:+.0%}({sb["bh_cagr"]:+.0%}) · 낙폭 {sb["mdd"]:.0%}({sb["bh_mdd"]:.0%}) · 수수료·펀딩 포함, 100·200일선도 비슷하게 통과')
    if hd:
        if hd["above"]:
            col, bg = "#16a34a", "rgba(22,163,74,.10)"
            head = f'✅ BTC 롱 보유 유지 — 일봉 종가가 {BTC_HOLD_MA}일선 위 ({hd["gap"]:+.1%}) · {hd["days"]}일째'
            todo = (f'할 일: 들고 있기. 매일 오전 9시(일봉 마감) 종가가 {BTC_HOLD_MA}일선(지금 {hd["ma"]:,.0f}) 아래로 마감하면 청산')
        else:
            col, bg = "#6b7280", "rgba(107,114,128,.10)"
            head = f'⏸ BTC 청산·대기 — 일봉 종가가 {BTC_HOLD_MA}일선 아래 ({hd["gap"]:+.1%}) · {hd["days"]}일째'
            todo = (f'할 일: 현금으로 기다리기. 종가가 {BTC_HOLD_MA}일선(지금 {hd["ma"]:,.0f}) 위로 마감하면 다시 롱. '
                    '숏은 하지 않기 — 이평선 아래 숏은 2024년 이후 손실이라 검증 탈락')
        hold = (f'<div class="drow main"><div class="h">BTC 보유 판단 · 검증 통과</div>'
                f'<div class="v" style="color:{col}">{e(head)}</div><div class="s">{e(todo)}</div>'
                f'<div class="s" style="opacity:.75">{e(proof)} · {e(hd.get("asof", ""))} 일봉 기준</div></div>')
    else:
        col, bg = "#6b7280", "rgba(107,114,128,.10)"
        hold = ('<div class="drow main"><div class="h">BTC 보유 판단 · 검증 통과</div><div class="v">계산 못 함</div>'
                '<div class="s">BTC 일봉을 받지 못했어요 — 다음 분석 때 다시 계산해요</div></div>')
    rule = f'<div class="act" style="margin-top:10px">{macro_trade_line(macro_filter)}</div>'
    if mid.get("p") is not None:
        b = mid["bin"]
        two = (f'<div class="drow muted"><div class="h">앞으로 2주 · 참고(예측력 확인 안 됨)</div>'
               f'<div class="v">{e(mid["label"])} · BTC 오를 확률 {mid["p"]:.0%}</div>'
               f'<div class="s">과거 이런 확률일 때 BTC 오른 비율 {b["p_up"]:.0%}(평균 {b["mean"]:+.1%}) · 알트 지수 {b["alt_p_up"]:.0%}({b["alt_mean"]:+.1%}). '
               f'근거: {" · ".join(e(_reason_txt(r)) for r in mid.get("reasons", []))}</div>'
               f'<div class="s" style="opacity:.7">실제로 쓸 때처럼 시간 순서로 다시 시험하면(2022-08~2026-09, 매달 다시 학습) 적중 45~51%·AUC 0.49로 동전 수준이었어요. 위 \'과거 비율\'은 미래 자료가 섞인 시험에서 나온 값이라 실제보다 좋게 보여요 — 참고로만 보고 매매엔 쓰지 않아요</div></div>')
    else:
        two = (f'<div class="drow muted"><div class="h">앞으로 2주 · 참고(예측력 확인 안 됨)</div><div class="v">계산 못 함</div>'
               f'<div class="s">{e(mid.get("error", "자료 부족"))}</div></div>')
    r3 = sh.get("r3d")
    short = (f'<div class="drow muted"><div class="h">앞으로 1~3일</div><div class="v">판단 안 함</div>'
             f'<div class="s">5년 검증에서 어떤 지표도 50%(동전 던지기)를 넘지 못했어요'
             + (f' · 사실: 최근 3일 BTC {r3:+.1%}' if r3 is not None else "") + '</div></div>')
    return (f'<div class="regime" style="background:{bg};border-left:5px solid {col}">'
            f'<div class="t">🧭 거시 방향 · 지금 할 일</div>{hold}{rule}{two}{short}'
            f'<div class="unver">지지·저항선, 추세선, 매물대, 오더블록, 피보나치, 라운드 넘버는 5년 검증(31개 코인)에서 아무 가격선보다 '
            f'반등을 더 잘 하지 않았어요 — 선에서의 반등을 미리 믿고 들어가지 마세요.</div></div>')


def regime_html(regime, macro_hours: float = 0.0, macro_filter: Optional[Dict] = None, compact: bool = False) -> str:
    """메인 거시 방향 카드: 한 줄 결론 → 행동 가이드 → 방향 게이지 → 근거 설명 → (횡보) 전환 기준선 → 신뢰도.
    v29.7: 색(큰 흐름 점수)과 글(BTC 일봉·4시간 추세 조합)이 달라 '초록불인데 횡보'처럼 헷갈려서, 제목에 둘을 나눠 쓰고
    실제 추천에 영향을 주는 규칙(거시 필터)과 '색은 진입 신호가 아님'을 함께 보여줌."""
    info = REGIME_INFO[regime.overall]
    score = max(-1.0, min(1.0, regime.score))
    pos_pct = (score + 1) / 2 * 100
    conf_color = {"높음": "#16a34a", "보통": "#d97706", "낮음": "#6b7280"}.get(regime.confidence_label, "#6b7280")
    e = html.escape

    breakout = ""
    if regime.breakout_up and regime.breakout_down:
        from crypto_market_regime import fmt_range  # 지연 임포트(app_logic 단독 임포트 가능하게)
        breakout = (f'<div class="brk">📏 BTC 최근 3일(4시간봉 20개) 범위: 고점 <b>{e(fmt_range(regime.breakout_up))}</b> · '
                    f'저점 <b>{e(fmt_range(regime.breakout_down))}</b><br><span class="hint">4시간마다 새로 계산돼서 '
                    f'계속 바뀌는 참고값이에요. 새 저점이 나오면 그 값으로 내려가고, 오래된 저점이 3일 밖으로 빠지면 다시 올라가요. '
                    f'진입 신호나 전환 기준이 아니에요.</span></div>')
    alt_line = (f'<div class="altv">🪙 {e(regime.alt_view)}</div>' if getattr(regime, "alt_view", "") else "")
    val_line = (f'<div class="valn">📏 {e(regime.validation_note)}</div>' if getattr(regime, "validation_note", "") else "")
    notes = ""
    if regime.shock:
        notes += '<div class="rnote">⚡ BTC 급변 감지 — 국면을 즉시 반영했어요</div>'
    if regime.transition_pending:
        notes += '<div class="rnote">⏳ 새 방향이 나왔지만 아직 확인 중 — 다음 봉 마감 때 확정돼요</div>'
    if regime.lean_verified is False:
        notes += '<div class="unver">ⓘ 횡보 중 방향 판단(기울기)은 아직 실측 검증 전이라 참고용이에요</div>'

    return (
        f'<div class="regime" style="background:{info["bg"]};border-left:5px solid {info["color"]}">'
        f'<div class="t">{info["emoji"]} 큰 흐름 {BIG_FLOW[regime.overall]} · 지금 {e(regime.headline)}</div>'
        + ("" if compact else f'<div class="act">{macro_trade_line(macro_filter)}</div>') +
        f'<div class="act">▶ {e(regime.action)}</div>'
        f'<div class="gauge"><div class="gauge-track">'
        f'<span class="gauge-lbl left">하락</span><span class="gauge-lbl mid">중립</span>'
        f'<span class="gauge-lbl right">상승</span>'
        f'<div class="gauge-mark" style="left:{pos_pct:.1f}%"></div></div></div>'
        f'<div class="d">{e(regime.explanation)}</div>'
        f'{alt_line}{val_line}'
        f'{breakout}{notes}'
        f'<div class="conf">판단 신뢰도 <b style="color:{conf_color}">{e(regime.confidence_label)}</b>'
        f' <span class="hint">· 근거들이 같은 방향을 가리키는 정도</span></div>'
        + ("" if compact else
           f'<div class="unver">🚦 색은 큰 흐름 참고용이지 진입 신호가 아니에요. 일봉 중심이라 하루 이틀 하락으로는 잘 안 바뀌고, '
           f'바뀌어도 봉 2개 마감까지 확인 후 바뀌어요. 5년 검증: 일봉 흐름이 강한 상승(+0.25 이상)일 때 앱의 롱 신호도 '
           f'평균 +0.02R로 이득이 거의 없었어요 — 진입은 추천 카드만.</div>') +
        f'</div>'
    )


def regime_detail_html(regime) -> str:
    """'판단 근거 자세히' 안에 넣는 근거별 정리 (메인 카드에는 결론만)."""
    e = html.escape
    comps = getattr(regime, "components", None)
    if comps:
        rows_ = []
        for c in comps:
            v = c["value"]
            color = "#16a34a" if v > 0.1 else ("#dc2626" if v < -0.1 else "#d97706")
            arrow = "↑" if v > 0.1 else ("↓" if v < -0.1 else "→")
            rows_.append(f'<div class="tfrow"><span class="tfl">{e(c["name"])}<span class="tfw">비중 {c["weight"]:.0%}</span></span>'
                         f'<span class="tfv"><b style="color:{color}">{arrow} {e(c["text"])}</b></span></div>')
        rows_.append(f'<div class="tfrow"><span class="tfl">종합 점수<span class="tfw">−1 ~ +1</span></span>'
                     f'<span class="tfv"><b>{regime.score:+.2f}</b> (±0.25 넘으면 추세)</span></div>')
        return f'<div class="tf">{"".join(rows_)}</div>'
    kr = {"uptrend": "상승", "downtrend": "하락", "sideways": "횡보"}
    color = {"uptrend": "#16a34a", "downtrend": "#dc2626", "sideways": "#d97706"}

    def row(label: str, weight: str, value_html: str) -> str:
        return (f'<div class="tfrow"><span class="tfl">{e(label)}<span class="tfw">{e(weight)}</span></span>'
                f'<span class="tfv">{value_html}</span></div>')

    def trend(t: str) -> str:
        return f'<b style="color:{color[t]}">{TREND_ICON[t]} {kr[t]}</b>'

    rows = [
        row("큰 흐름 · BTC 일봉", "비중 35%", trend(regime.daily_trend)),
        row(f"중기 흐름 · BTC {TF_LABEL.get(getattr(regime, 'timeframe', '4h'), '4시간봉')}", "비중 30%",
            trend(regime.btc_trend)),
    ]
    if regime.breadth_up_pct is not None:
        rows.append(row(f"시장 참여 · 코인 {regime.breadth_n}개", "비중 25%",
                        f'<b style="color:#16a34a">상승 {regime.breadth_up_pct:.0f}%</b> · '
                        f'<b style="color:#dc2626">하락 {regime.breadth_down_pct:.0f}%</b>'))
    else:
        rows.append(row("시장 참여 · 스캔 코인", "비중 25%", "이번엔 스캔 전이라 미반영"))
    span = (regime.snapshot or {}).get("macro_span_hours", 0.0)
    if span >= 24:
        rows.append(row("도미넌스·TOTAL", "비중 10%",
                        f"BTC.D {TREND_ICON[regime.btc_d_trend]} · USDT.D {TREND_ICON[regime.usdt_d_trend]} · "
                        f"TOTAL2 {TREND_ICON[regime.total2_trend]} · TOTAL3 {TREND_ICON[regime.total3_trend]}"))
    else:
        rows.append(row("도미넌스·TOTAL", "비중 10%", f"기록 {span:.0f}시간 — 24시간 이후 반영"))
    rows.append(row("종합 점수", "−1 ~ +1", f"<b>{regime.score:+.2f}</b> (±0.25 넘으면 추세)"))
    return f'<div class="tf">{"".join(rows)}</div>'


# ---------------------------------------------------------------- v29.6 보유 중 포지션 관리
HOLD_EVIDENCE = ("5년 검증(Bitget 선물, 같은 매매법): **규칙대로 들고 있기가 가장 나았어요.** "
                 "물타기는 롱·숏 각각 72가지(자리 0.33·0.5·0.75R × 크기 × 손절 × 청산 × 위험)를 3개 거래소 5년으로 시험해도 "
                 "Bitget 두 기간을 모두 나아지게 한 방식이 하나도 없었어요. 0.5R 넘게 밀린 거래는 롱 약 80%·숏 약 75%가 결국 손절로 끝났고, "
                 "추가 물량은 수익보다 낙폭을 더 키워요. 손절을 넓히고 물타면 더 나빴어요. "
                 "불리할 때 일찍 정리하는 것도 기간마다 엇갈려 이득이 없었어요. "
                 "불타기(목표1에 닿을 때 처음 수량의 0.5배 추가, 같은 추적 손절로 함께 정리)는 사전 기준 두 번 통과 — v32.7부터 적용.")


def held_position_card(t: dict) -> tuple:
    """실전 추적 '보유' 기록 하나 → (제목, 할 일 줄 목록). 가격은 선물 표기(1000PEPE 등은 ×배수)."""
    lv = t.get("live") or {}
    m = float(t.get("mult") or 1)
    p = lambda x: fmt_price(None if x is None else float(x) * m)
    long_ = t.get("direction") == "long"
    coin = t["symbol"].split("/")[0] if m == 1 else f"{int(m)}{t['symbol'].split('/')[0]}"
    sig_kst = (pd.Timestamp(t["signal_ts"]) + pd.Timedelta(hours=4 + 9)).strftime("%m-%d %H:%M")
    title = f"{coin} {'롱' if long_ else '숏'} · {t.get('family', '')} · 신호 {sig_kst} 마감"
    if not lv:
        return title, ["상태 계산 전이에요. 잠시 뒤 다시 확인돼요."]
    rn = lv.get("R_now")
    lines = [f"진입 {p(lv['entry'])} → 마지막 4시간봉 종가 {p(lv['last_close'])}"
             + (f" (**{rn:+.2f}R**)" if rn is not None else "") + f" · 보유 {lv['held']}/{lv['hmax']}봉"]
    if lv["stage"] == 0:
        lines.append(f"🛑 손절 {p(lv['sl'])} 그대로. 목표1 {p(lv['tp1'])}에 닿으면 손절을 **진입가 {p(lv['entry'])}**로 올리기 "
                     "(트레일링 주문을 걸어 뒀다면 자동)")
    else:
        lines.append(f"🛑 목표1 도달 → 손절은 본전 이상. 다음 4시간 손절선 **{p(lv['stop_next'])}** "
                     f"({'최고가 −' if long_ else '최저가 +'} {lv['trail']:g} ATR, 봉마다 유리한 쪽으로만 옮김). 트레일링 주문이 있으면 그대로 두기")
    pyr = lv.get("pyramid") or {}
    sz_ = pyr.get("size", 0.5)
    if pyr.get("first_idx") is None:
        lines.append(f"➕ 불타기: 목표1 {p(pyr.get('level'))}에 닿으면 처음 수량의 {sz_:g}배 추가 — 목표1 가격에 "
                     f"{'매수' if long_ else '매도'} 역지정가 주문을 걸어 두면 자동. 추가분도 같은 추적 손절로 함께 정리")
    elif pyr.get("now"):
        lines.append(f"➕ 불타기: ✅ 방금 마감한 봉에서 목표1에 닿음 — 주문이 체결됐으면 추가분 손절도 **{p(pyr.get('add_stop'))}**부터 "
                     f"같은 추적 손절로 함께. 주문을 안 걸어 뒀다면 지금 가격이 목표1 근처일 때만 {sz_:g}배 추가, 멀어졌으면 건너뛰기")
    else:
        fk = (pd.Timestamp(pyr["first_ts"]) + pd.Timedelta(hours=4 + 9)).strftime("%m-%d %H:%M")
        lines.append(f"➕ 불타기: {fk} 마감 봉에서 목표1에 이미 닿음 — 추가했으면 추가분도 다음 4시간 손절선 {p(lv['stop_next'])}로 함께, "
                     "놓쳤으면 건너뛰기(늦게 추가하면 검증 밖)")
    if rn is not None and rn <= -0.3:
        lines.append(f"🚫 물타기·손절 {'내리기' if long_ else '올리기'}: 하지 않기 — 이만큼 밀린 {'롱' if long_ else '숏'}은 "
                     f"5년 검증에서 대부분 손절로 끝났어요(0.5R 넘게 밀리면 약 {80 if long_ else 75}%). 물타기 72가지 중 나아진 방식이 없었어요")
    else:
        lines.append(f"🚫 물타기·손절 {'내리기' if long_ else '올리기'}: 하지 않기")
    left = max(0, lv["hmax"] - lv["held"])
    lines.append(f"⏱ 남은 시간 {left}봉(약 {left * 4 / 24:.1f}일) — 그때까지 안 끝나면 그 봉 종가에 정리")
    return title, lines
