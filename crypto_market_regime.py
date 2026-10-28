"""
crypto_market_regime.py
========================
멀티거래소(Binance / OKX / Bitget) 실시간 데이터 기반
크립토 시장 국면(상승/횡보/하락) 판단 + 코인 스크리너 + TP/SL 제안 도구

⚠️ 중요 전제 (반드시 읽고 사용하세요)
--------------------------------
1. 이 스크립트는 "수익을 보장하는 시그널 생성기"가 아닙니다.
   - 시장 국면을 객관적 지표로 요약하고, 조건에 맞는 후보를 걸러주는 "의사결정 보조 도구"입니다.
   - 여기서 나온 TP/SL/방향은 참고용이며, 최종 진입/청산 판단과 책임은 사용자 본인에게 있습니다.
2. 실전 투입 전 반드시 아래 순서를 거치세요:
   a) 최소 3~6개월 페이퍼 트레이딩(모의투자)으로 신호 품질 검증
   b) backtest_wfo() 로 워크포워드 검증 (과거 특정 구간에만 맞춰진 과최적화 여부 확인)
   c) 실전 투입 시 레버리지는 규칙 기반으로 상한을 강제 (이 스크립트는 레버리지 추천을 하지 않습니다)
3. 네트워크가 막힌 환경(샌드박스)에서는 실행되지 않습니다. 로컬/서버에서 다음을 설치 후 실행하세요:
   pip install ccxt requests pandas numpy

작성 방식: 단일 파일, 모듈형 함수 구성. main() 에서 전체 파이프라인을 한 번에 실행합니다.
"""

import time
import json
import re
import os
import threading
import math
import statistics
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Literal, Tuple

import requests
import pandas as pd
import numpy as np

try:
    import ccxt
except ImportError:
    ccxt = None  # 실행 시 pip install ccxt 안내

# --------------------------------------------------------------------------
# 0. 설정
# --------------------------------------------------------------------------

APP_VERSION = "2026-10-28 v32.7"          # 화면·검증 결과에 표시 — 새 파일이 반영됐는지 확인용
EXCHANGES = ["bitget", "okx", "binance"]          # 앞쪽일수록 우선 사용(Bitget = 실제 거래 거래소). 일부 거래소는 서버 지역에 따라 차단될 수 있음
QUOTE = "USDT"
TOP_N_BY_VOLUME = 100                             # 스캔 코인 수 = 합산 거래량 순위 상위 N개 (2년 검증: 1~100위 모든 구간 플러스)
TRACK_FILE = "signal_tracking.json"               # 실전 추천 자동 추적 기록
LAB_REF_FILE = "lab_reference.json"               # 자동 방어 기준선(연구실 실행 때마다 신호 유형별 과거 성과 저장)
AUTO_DEFENSE = True                               # 실전 성과가 기준선 아래로 떨어진 신호 유형을 자동 감축·중지
FALLBACK_LOW_R = -0.3                             # 연구실 기준선이 없을 때 쓰는 보수적 고정 기준(최근 평균 R)
ENABLED_FAMILIES = {"돌파", "돌파 후 되돌림", "추세 눌림목", "빗각 지지", "아래꼬리 연속"}   # 추천할 신호 유형 (v29 3가지 + 🧪 시험 운용 v31 빗각 지지 · v32 아래꼬리 연속)
TIMEFRAME = "4h"                                  # 스윙 트레이딩 기준 봉
OHLCV_LIMIT = 600                                 # 4h 캔들 개수 (EMA200이 제대로 계산되려면 수백 개 필요 — 200개면 첫 봉 비중이 13%나 남음)
HTF_LIMIT = 300                                   # 일봉 캔들 개수 (일봉 EMA200용)
ADX_TREND_MIN = 25.0                              # 추세 판정: Wilder ADX 최소값
ER_TREND_MIN = 0.20                               # 추세 판정: 효율비율(50봉 순이동 ÷ 총이동) 최소값
MIN_QUOTE_VOLUME_USDT = 3_000_000                 # 24h 거래대금 최소 — 유동성 얇아 휘둘리기 쉬운 코인 제외
STOP_BUFFER_ATR = 0.3                             # 손절을 뻔한 구조 레벨(오더블록·박스 끝)보다 이만큼 더 바깥에
MIN_STOP_ATR = 1.0                                # 손절폭 최소값 — 너무 좁은 손절은 노이즈·손절 사냥에 취약
CHASE_ATR = 0.5                                   # 현재가가 진입가에서 이만큼(ATR 배수) 넘게 벗어나면 '추격 구간'
PARTIAL_TP_FRACTION = 0.5                         # (예전 규칙·신고점 돌파 등) 목표1에서 이 비율만큼 익절
TRAIL_ATR = 2.5                                   # (예전 규칙) 나머지는 최고가(롱)/최저가(숏)에서 이만큼 떨어지면 정리
# v27 청산 규칙 (24개월·83개 코인·약 196만 조합 탐색 + 기간·코인 교차 검증으로 고름)
# - 목표1에 닿으면 팔지 않고(frac 0) 손절을 진입가로 올린 뒤, 최고가(롱)/최저가(숏)에서 trail ATR 되돌리면 전량 정리
# - stop_atr: 기준선(박스 경계·되돌림 저점·다이버전스 저점) 너머 몇 ATR에 손절 (진입가에서 최소 MIN_STOP_ATR)
# - tp_box: 목표1 = 기준선 + 박스 높이 × tp_box / tp_R: 목표1 = 진입가 + 손절폭 × tp_R
# - hold: 최대 보유 봉 수 (4시간봉 30봉 = 5일, 60봉 = 10일)
# v29 (5년·2021.10~2026.10, 사이클 A 2021-23 ↔ B 2024-26 교차 검증): 한 사이클에서 고른 설정이 다른 사이클에서도 통한 것만
EXIT_RULES = {
    "돌파": {"stop_atr": 0.5, "tp_box": 1.0, "frac": 0.0, "trail": 1.0, "hold": 60},
    "돌파 후 되돌림": {"stop_atr": 0.0, "tp_box": 2.0, "frac": 0.0, "trail": 0.75, "hold": 60},
    "추세 눌림목": {"stop_atr": 0.0, "tp_R": 4.0, "frac": 0.0, "trail": 0.75, "hold": 60},
    "위치 다이버전스": {"stop_atr": 0.0, "tp_R": 3.0, "frac": 0.0, "trail": 0.75, "hold": 60},   # v29 기본 꺼짐(5년 교차 검증 탈락)
    "빗각 지지": {"stop_atr": 0.0, "tp_R": 5.0, "frac": 0.0, "trail": 1.5, "hold": 180},   # v31 시험 운용(손절 = 진입가 − 2 일봉 ATR)
    "아래꼬리 연속": {"stop_atr": 0.0, "tp_R": 5.0, "frac": 0.0, "trail": 3.0, "hold": 180},   # v32 시험 운용(손절 = 진입가 − 2 일봉 ATR)
}
# 새로 넣은 매매법은 위험 절반으로 시작 → 실전 추적 30건 평균이 플러스(기준선 안)면 자동으로 정상 위험
NEW_FAMILY_RISK = {"돌파 후 되돌림": 0.5, "위치 다이버전스": 0.5}
# v30.3: 승격 없이 고정하는 위험 비중. 추세 눌림목은 거래당 수익(+0.21~0.34R)이 박스 돌파·되돌림(+0.57~1.36R)보다 작아,
# 5년·3자료(Bitget 선물·현물·Binance 선물)×2사이클 계좌 모의에서 ½로 두는 쪽이 전 신호 6칸 모두 연수익÷낙폭이 높았음
# (균등 1·1·1 대비, 2026-10-14 검증) → 실전 30건 뒤에도 1로 올리지 않음
FIXED_FAMILY_RISK = {"추세 눌림목": 0.5}
# v31 🧪 빗각 지지 롱(시험 운용) — 2026-10-17 사용자 차트 방식 빗각 5년 검증(diag4):
#   일봉 꼭짓점 좌우 10일, 상승 각도 = 주요 저점 A → A~다음 주요 고점 사이 몸통이 가장 큰 양봉의 고가, 그 각도를 B 뒤 확정된
#   주요 꼭짓점 최근 6곳에 평행 복사. 직전 종가는 선 위 · 저가가 선 + 0.5 일봉 ATR 안 · 종가 선 위 · 종가 > 200일 평균 → 09시 시장가.
#   손절 진입가 − 2 일봉 ATR, 목표 5R 뒤 최고가 − 1.5 ATR(4시간봉) 추적, 최대 30일(180봉).
#   거래당 Bitget 선물 +0.88/+0.38R(2021~23/2024~), 현물 +0.65/+0.40R, Binance +0.92/+0.39R · 가짜 빗각보다 나은 칸 89% ·
#   수동 시각 계좌 6칸 모두 개선. 사이클 교차(t 0.9)·전 신호 계좌(6칸 중 2칸 낮아짐) 미달 → 위험 ¼ 시험 운용, 승격해도 0.5배
DIAG_K, DIAG_COPIES, DIAG_ZONE_ATR = 10, 6, 0.5
DIAG_STOP_ATR_D, DIAG_TP_R = 2.0, 5.0
DIAG_DAILY_LIMIT = 500                            # 일봉 개수 (200일 평균 + 꼭짓점·각도용)
FIXED_TRIAL_FAMILIES = {"빗각 지지", "아래꼬리 연속"}   # 연구실 설정을 초기화·적용해도 시험 운용으로 남는 유형
TRIAL_PROMOTED_RISK = {"빗각 지지": 0.5, "아래꼬리 연속": 0.5}   # 실전 30건 뒤 승격해도 이 배수(5년 계좌 모의를 0.5배로 했음)
STATIC_FAMILY_REF = {"빗각 지지": (0.50, 2.35), "아래꼬리 연속": (0.71, 2.42)}   # 자동 방어 기준선(5년 Bitget 선물 거래당 평균·표준편차) — 연구실 기준선이 없을 때
# v31.1 사용자 결정(2026-10-17): 🧪 빗각 지지는 BTC 대상승 국면(일봉 종가 > 200일 평균, 60일 수익률 ≥ +20%)일 때만.
#   5년 확인(판정 기준 아님): 대상승 거래당 Bitget +0.75/+0.60R(52/100건) vs 전체 +0.88/+0.38R(63/192건).
#   지금 3종 + 0.5배 같은 낙폭 연수익은 관문 없을 때보다 전 신호·수동 모두 6칸 중 2칸만 높음. v32.2: 사용자 결정으로 항상 켬(사이드바 설정 없앰).
DIAG_BULL_ONLY = True
# v32 🧪 아래꼬리 연속 롱(시험 운용) — 2026-10-17 캔들 꼬리 5년 검증(사전 등록 wx_prereg.txt, 254만 규칙):
#   일봉 아래꼬리(min(시가,종가) − 저가)가 0.5 일봉 ATR 이상이고 위꼬리보다 긴 봉이 정확히 2번째 연속으로 마감 ·
#   BTC 일봉 종가 > 200일 평균 · 코인 종가 > EMA20(일봉) → 한국 09시 시장가. 손절 진입가 − 2 일봉 ATR,
#   목표 5R 뒤 최고가 − 3 ATR(4시간봉) 추적, 최대 30일(180봉).
#   거래당 Bitget 선물 +0.96/+0.60R(2021~23/2024~, 90/215건), 현물 +0.98/+0.76R, Binance +0.81/+0.65R ·
#   날짜 밀기 19회 최대보다 큼(월 +3.7R vs +1.7R) · 탐색 전체 플라시보 5회보다 큼 · 일봉 같은 방향 연속 묶음 안 사이클 교차 통과 ·
#   지금 3종 + 0.5배 같은 낙폭 연수익 전 신호 6칸·수동 6칸 모두 높음 → 사전 기준 통과 → 위험 ¼ 시험 운용, 승격해도 0.5배
WICK_ATR, WICK_RUN = 0.5, 2
WICK_STOP_ATR_D, WICK_TP_R = 2.0, 5.0
# v32.1 사용자 결정(2026-10-18): 일요일(한국 시각, 신호 봉 마감 기준) 진입은 지금 3종 위험 ½.
#   5년 검증(cal_prereg.txt): 일요일 진입 거래당 Bitget −0.43/−0.25R · 현물 −0.29/−0.22R · Binance −0.18/−0.12R(2021~23/2024~),
#   다른 요일은 모두 +. 손절 비율 72~76%(다른 날 56~58%) — 조용한 일요일 ATR로 잡은 좁은 손절이 월요일 오전 변동성에 걸림.
#   같은 낙폭 계좌: 수동 6칸 모두 높음 · 전 신호 5/6칸(Binance 2024~ −2%p) → 사전 기준(전 신호 6/6)엔 한 칸 모자라 사용자 결정으로 적용.
SUNDAY_HALF = True
SUNDAY_HALF_FAMILIES = {"돌파", "돌파 후 되돌림", "추세 눌림목"}
SUNDAY_RISK_MULT = 0.5
# v32.6 사용자 결정(2026-10-28): 시장 출렁임 지수(스캔한 코인들의 4시간봉 Choppiness Index 28봉 중앙값)가 52를 넘으면
#   지금 3종 신규 진입 위험 ½ (키우는 쪽은 없음).
#   검증(ext_prereg.txt 3부 + 강건성): 사전 기준 CHOP 14봉 0.5·1·1.5배 = 전 신호 5/6 · 수동 6/6, 무작위 섞기 20번 0번.
#   28봉 '상위 1/3만 ½' = 전 신호 6/6 · 수동 6/6, 같은 낙폭 연수익 평균 +52%p(전 신호)·+21%p(수동), 무작위 섞기 20번 모두 이김.
#   경계 52 = 5년 3자료 거래 기준 상위 1/3(2021~23 53.1~53.8, 2024~ 51.5~51.9). 결과를 본 뒤 고른 기간이라 사용자 결정으로 적용.
CHOP_HALF = True
CHOP_N = 28
CHOP_LIMIT = 52.0
CHOP_RISK_MULT = 0.5
CHOP_FAMILIES = SUNDAY_HALF_FAMILIES
LONG_BIASES = ("long", "wait_breakout_long", "range_fade_long", "donchian_long", "st_long", "retest_long", "div_long", "pb_long",
               "diag_long", "wick_long")
# v29 박스 돌파 진입: 직전 90봉(15일) 박스 · 거래량 20봉 평균의 2배 이상 · 경계 밖 1 ATR 이내 마감(멀리 튄 봉은 추격 안 함)
BOX_LOOKBACK = 90
BOX_VOL_MIN = 2.0
BOX_EXT_ATR = 1.0

# 거래 비용 (Bitget USDT-M 선물 일반 등급 기준. 등급이 다르면 여기서 바꾸세요)
MAKER_FEE_PCT = 0.02                              # 지정가 진입·목표1 지정가 익절
TAKER_FEE_PCT = 0.06                              # 시장가 진입·손절·추적손절
SLIPPAGE_BY_TIER = {"거래량 1~30위": 0.03, "거래량 31~60위": 0.05, "거래량 61~100위": 0.08}
DEFAULT_SLIPPAGE_PCT = 0.05                       # 시장가 체결 시 슬리피지(%), 거래량 작을수록 큼

# 신고점 돌파(돈치안): 직전 N봉 최고가를 종가로 넘으면 시장가 진입. N은 약 20일
DONCHIAN_N = {"4h": 120, "1h": 480}
DONCHIAN_VARIANT = "atr"   # atr(손절 2ATR·목표 3ATR) | structure(구조 기반) | weekly | volume | tsm (연구실에서 정함)
DONCHIAN_VARIANT_LABEL = {"atr": "기본(손절 2ATR·목표 3ATR)", "structure": "구조 기반(돌파 고점 아래 손절·다음 저항 목표)",
                          "weekly": "주봉 추세 일치", "volume": "거래량 동반", "tsm": "포트폴리오 보유 코인"}
TRIAL_FAMILIES: set = set(FIXED_TRIAL_FAMILIES)   # 🧪 시험 운용 중인 신호 유형 (리스크 ¼로 추천, 실전 30건이 기준선 안이면 자동 승격)
TSM_TRIAL = False             # 추세 포트폴리오 시험 운용 (비중 ¼)
TRIAL_RISK_MULT = 0.25
HOLD_MIN_R = 0.10             # 2차 연구실부터: 확인 구간 평균 +0.1R 이상이어야 함

# 전략 연구실에서 '통과'한 항목을 실전 추천에 반영하는 설정 (앱의 '이 설정 적용' 버튼이 바꿈)
BREAKOUT_ENTRY = "immediate"                      # immediate(봉 마감 직후 시장가, 기본) | retest(리테스트 지정가 대기)
# 2년·82개 코인 정밀 검증(1시간봉): 봉 마감 직후 시장가 +0.49R → 1시간 뒤 +0.31R → 2시간 +0.22R, 리테스트 지정가는 +0.09R 이하
MACRO_FILTER = True                               # 거시 흐름과 반대 방향 신호 제외 (검증: 역방향 개발 −0.15R·확인 −0.50R)
ELAPSED_WARN_H, ELAPSED_STOP_H = 2, 4             # 신호 봉 마감 후 경과 시간 경고 (시간)
# 코인 그룹 (2년 검증): BTC는 어떤 차트 규칙도 엣지가 없어 매매 신호에서 제외(거시 지표로만),
# 메이저는 추세가 잘 이어져 신고점 돌파·일봉 추세 포트폴리오가 통하고, 일반 알트는 박스 돌파가 가장 강함
BOX_RR_MAX = 5.0     # 박스 돌파: 봉 마감 가격 기준 손익비 5 이상은 제외 (2년·두 데이터 모두 개발·확인 구간 마이너스 —
                     # 박스가 손절폭보다 지나치게 넓어, 단단한 박스가 아니라 넓게 출렁인 구간인 경우가 많음)
LIQ_CAP_PCT = 0.10   # 주문 금액 상한 = 그 코인 24시간 거래대금의 0.1% (계좌가 커질 때 체결 비용이 검증 가정을 넘지 않게)
MAJORS = {"ETH", "XRP", "BNB", "SOL", "DOGE", "ADA", "TRX", "LINK", "AVAX", "BCH", "LTC", "DOT", "XLM", "SUI", "HBAR", "TON"}


def coin_group(symbol: str) -> str:
    base = symbol.split("/")[0]
    return "BTC" if base == "BTC" else ("메이저" if base in MAJORS else "일반 알트")
PRIORITY_TAGS: set = set()                        # 이 표시가 붙은 신호를 먼저 보여줌
FILTER_TAGS: set = set()                          # 이 표시가 모두 붙은 돌파 신호만 추천
LAB_CONFIG_FILE = "lab_config.json"
TAG_LABELS = {"squeeze": "변동성 수축", "htf_align": "일봉 방향 일치", "alt_strong": "알트 지수 대비 강함",
              "btc_strong": "BTC 대비 강함", "strong_close": "강한 마감", "obv_accum": "거래량 흐름(OBV) 매집"}
ST_PERIOD, ST_MULT = 10, 3.0   # ATR 추적선(슈퍼트렌드): 트레이딩뷰 기본값과 같음

# 신호 봉 모드. 1시간봉은 '비교용' — 상대강도 기간은 시간으로 환산(≈1·2·4주 유지), 상위추세는 4시간봉.
# 나머지 기준(박스 60봉, 임펄스 30봉 등)은 봉 개수 그대로라 기간이 4분의 1로 짧아지는 '더 빠른 전략'이 됩니다.
TIMEFRAME_PRESETS = {
    "4h": {"limit": 600, "htf": "1d", "rs_windows": [42, 84, 168], "bars_per_8h": 2, "label": "4시간봉"},
    "1h": {"limit": 700, "htf": "4h", "rs_windows": [168, 336, 672], "bars_per_8h": 8, "label": "1시간봉"},
}
HTF_TIMEFRAME = "1d"
RS_WINDOWS = [42, 84, 168]
BARS_PER_8H = 2


def set_timeframe(tf: str) -> None:
    """신호 봉 모드 전환(4h 기본 / 1h 비교용). 모듈 전역 설정을 바꿉니다."""
    global TIMEFRAME, OHLCV_LIMIT, HTF_TIMEFRAME, RS_WINDOWS, BARS_PER_8H
    p = TIMEFRAME_PRESETS[tf]
    TIMEFRAME, OHLCV_LIMIT, HTF_TIMEFRAME = tf, p["limit"], p["htf"]
    RS_WINDOWS, BARS_PER_8H = list(p["rs_windows"]), p["bars_per_8h"]


def tf_label(tf: Optional[str] = None) -> str:
    return TIMEFRAME_PRESETS.get(tf or TIMEFRAME, {}).get("label", tf or TIMEFRAME)
HISTORY_FILE = "market_regime_history.csv"        # BTC.D / USDT.D / TOTAL2,3 스냅샷 누적 저장 (트렌드 판단용)
REGIME_LOG_FILE = "regime_confirmation_log.csv"   # 국면 whipsaw 방지용 확정 이력

CoinGeckoGlobalURL = "https://api.coingecko.com/api/v3/global"
CoinPaprikaGlobalURL = "https://api.coinpaprika.com/v1/global"
CoinPaprikaTickerURL = "https://api.coinpaprika.com/v1/tickers/{coin_id}"

RegimeType = Literal["uptrend", "downtrend", "sideways"]


@dataclass
class RiskConfig:
    """계좌 단위 리스크 관리 설정. '얼마나 걸지'는 신호와 완전히 분리해서 관리해야 합니다."""
    account_balance: float          # 계좌 총 잔고 (USDT 기준)
    risk_per_trade_pct: float = 1.0  # 트레이드 1건당 허용 손실 (계좌 대비 %). 권장 0.5~2%
    max_correlated_exposure_pct: float = 3.0  # BTC 방향에 동조된 포지션들의 합산 리스크 상한(%)
    max_concurrent_setups: int = 12  # 같은 방향(롱 또는 숏) 동시 보유 최대 개수
    max_total_risk_pct: float = 12.0  # 동시에 들고 있는 모든 포지션의 손절 시 합산 손실 상한(계좌 대비 %)
    open_positions: int = 0          # 지금 거래소에서 이미 보유 중인 포지션 수(직접 입력 — 프로그램은 계좌를 모름)


def available_slots(risk_cfg: "RiskConfig") -> int:
    """총 리스크 상한 안에서 새로 잡을 수 있는 포지션 수.
    예) 상한 5%, 건당 1%, 보유 2개 → 3개. 알트코인은 BTC와 같이 움직여서, 같은 방향 여러 개는
    한 번에 같이 손절될 수 있으므로 '합계'로 묶어 제한합니다."""
    r = max(risk_cfg.risk_per_trade_pct, 1e-9)
    left = risk_cfg.max_total_risk_pct - risk_cfg.open_positions * r
    return max(0, int(np.floor(left / r + 1e-9)))


def vol_tier_label(rank: int) -> str:
    for label, a_, b_ in (("거래량 1~30위", 0, 30), ("거래량 31~60위", 30, 60), ("거래량 61~100위", 60, 100)):
        if a_ <= rank < b_:
            return label
    return "거래량 101위~"


def apply_liquidity_cap(sizing: Dict, vol24h: Optional[float], cap_pct: Optional[float] = None) -> Dict:
    """주문 금액이 24시간 거래대금의 cap_pct%를 넘으면 수량·손실 금액을 같은 비율로 줄임 (작은 코인 체결 비용 방지)."""
    cap_pct = LIQ_CAP_PCT if cap_pct is None else cap_pct
    if not vol24h or vol24h <= 0 or not sizing or not sizing.get("notional"):
        return sizing
    cap = vol24h * cap_pct / 100
    if sizing["notional"] <= cap:
        return {**sizing, "capped": False, "cap_notional": cap}
    k = cap / sizing["notional"]
    return {**sizing, "size": sizing["size"] * k, "notional": cap, "risk_amount": sizing["risk_amount"] * k,
            "capped": True, "cap_notional": cap}


def calculate_position_size(entry: float, sl: float, risk_cfg: RiskConfig) -> Dict:
    """RR이 아무리 좋아도 '얼마를 걸지'는 항상 이 공식으로만 결정합니다.
    포지션 크기 = (계좌잔고 * 리스크%) / |entry - sl|
    → SL에 닿아도 계좌 손실이 risk_per_trade_pct를 넘지 않도록 강제."""
    risk_amount = risk_cfg.account_balance * (risk_cfg.risk_per_trade_pct / 100)
    per_unit_risk = abs(entry - sl)
    if per_unit_risk <= 0:
        return {"size": 0, "risk_amount": risk_amount, "notional": 0}
    size = risk_amount / per_unit_risk
    notional = size * entry
    return {"size": size, "risk_amount": risk_amount, "notional": notional}


def cap_correlated_exposure(setups: List["CoinSetup"], risk_cfg: RiskConfig,
                             assumed_correlation: float = 0.6) -> List["CoinSetup"]:
    """같은 방향(롱/숏) 알트코인들은 BTC와 0.5~0.8 수준으로 동조화되는 경우가 흔해서
    (여러 개 들고 있어도 사실상 '하나의 큰 베팅'과 비슷) 두 단계로 제한합니다:

    1) 개수 제한: RR 상위 max_concurrent_setups개만 남김
    2) 상관조정: '유효 독립 베팅 수' = n / (1 + (n-1) * 평균상관계수) 공식으로
       실제 분산 효과가 얼마나 되는지 계산해서 함께 출력 (n=1이면 전혀 분산 안 된 것)
    """
    def _long_rank(s):
        return (s.rs, s.asymmetry if s.asymmetry is not None else 0.0, s.rr_ratio)

    def _short_rank(s):
        return (-s.rs, -(s.asymmetry if s.asymmetry is not None else 0.0), s.rr_ratio)

    long_like = sorted([s for s in setups if s.bias in LONG_BIASES],
                        key=_long_rank, reverse=True)[:risk_cfg.max_concurrent_setups]
    short_like = sorted([s for s in setups if s.bias not in LONG_BIASES],
                         key=_short_rank, reverse=True)[:risk_cfg.max_concurrent_setups]

    for group, label in [(long_like, "롱"), (short_like, "숏")]:
        n = len(group)
        if n > 1:
            n_eff = n / (1 + (n - 1) * assumed_correlation)
            print(f"[상관관계] {label} {n}개 동시 보유 → 유효 독립 베팅 수 ≈ {n_eff:.1f}개 "
                  f"(가정 상관계수 {assumed_correlation}) — 실제 분산 효과는 숫자보다 훨씬 작습니다.")

    return long_like + short_like


# --------------------------------------------------------------------------
# 1. 거시 지표: BTC.D, USDT.D, TOTAL2, TOTAL3
# --------------------------------------------------------------------------

def _fetch_snapshot_coingecko() -> Optional[Dict]:
    """1차 공급처. 실패 시 None (예외를 던지지 않음 — 호출부가 다음 공급처로 넘어감)."""
    try:
        resp = requests.get(CoinGeckoGlobalURL, timeout=10)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"[warn] CoinGecko 조회 실패: {e}")
        return None
    data = resp.json()["data"]
    total_mcap = data["total_market_cap"]["usd"]
    btc_pct = data["market_cap_percentage"].get("btc", 0)
    eth_pct = data["market_cap_percentage"].get("eth", 0)
    usdt_pct = data["market_cap_percentage"].get("usdt", 0)
    return {
        "total_mcap": total_mcap, "btc_d": btc_pct, "usdt_d": usdt_pct, "eth_d": eth_pct,
        "total2": total_mcap * (1 - btc_pct / 100),
        "total3": total_mcap * (1 - btc_pct / 100 - eth_pct / 100),
    }


def _fetch_snapshot_coinpaprika() -> Optional[Dict]:
    """2차 공급처(CoinGecko 실패 시). CoinGecko와 완전히 다른 회사·서버라 같은 이유로
    동시에 막힐 가능성이 낮습니다. 월 2만 회 무료, API 키 불필요.
    ⚠️ ETH/USDT 개별 시가총액을 구하려 티커를 2번 더 호출합니다(그래도 무료 한도에 넉넉히 여유)."""
    try:
        g = requests.get(CoinPaprikaGlobalURL, timeout=10)
        g.raise_for_status()
        gd = g.json()
        total_mcap = gd["market_cap_usd"]
        btc_pct = gd["bitcoin_dominance_percentage"]

        eth = requests.get(CoinPaprikaTickerURL.format(coin_id="eth-ethereum"), timeout=10)
        eth.raise_for_status()
        eth_mcap = eth.json()["quotes"]["USD"]["market_cap"]

        usdt = requests.get(CoinPaprikaTickerURL.format(coin_id="usdt-tether"), timeout=10)
        usdt.raise_for_status()
        usdt_mcap = usdt.json()["quotes"]["USD"]["market_cap"]
    except (requests.exceptions.RequestException, KeyError) as e:
        print(f"[warn] CoinPaprika 조회 실패: {e}")
        return None

    eth_pct = eth_mcap / total_mcap * 100
    usdt_pct = usdt_mcap / total_mcap * 100
    return {
        "total_mcap": total_mcap, "btc_d": btc_pct, "usdt_d": usdt_pct, "eth_d": eth_pct,
        "total2": total_mcap * (1 - btc_pct / 100),
        "total3": total_mcap * (1 - btc_pct / 100 - eth_pct / 100),
    }


def fetch_global_snapshot() -> Optional[Dict]:
    """BTC.D, USDT.D, TOTAL2, TOTAL3 스냅샷. CoinGecko를 1차로 시도하고, 실패하면(429 등)
    완전히 별도 회사·서버인 CoinPaprika로 넘어갑니다 — 같은 원인으로 둘 다 막힐 가능성은 낮습니다.
    둘 다 실패하면 None을 반환해서, 호출부(determine_overall_regime)가 이번 회차는
    새 스냅샷 없이 기존에 쌓인 기록으로만 판단하도록 합니다.
    (CoinGecko/CoinPaprika 무료 API는 '현재 스냅샷'만 주고 과거 시계열은 안 줘서, 추세는
    HISTORY_FILE에 스냅샷을 직접 누적해서 계산합니다 — 이 스크립트를 자주 돌릴수록 정확해집니다.)
    """
    fields = _fetch_snapshot_coingecko()
    source = "coingecko"
    if fields is None:
        fields = _fetch_snapshot_coinpaprika()
        source = "coinpaprika"
    if fields is None:
        return None

    snapshot = {"timestamp": int(time.time()), "source": source, **fields}
    _append_history(snapshot)
    return snapshot


def _append_history(snapshot: Dict) -> None:
    df_new = pd.DataFrame([snapshot])
    if os.path.exists(HISTORY_FILE):
        df_old = pd.read_csv(HISTORY_FILE)
        df = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df = df_new
    df.to_csv(HISTORY_FILE, index=False)


def macro_history_span_hours(window_days: float = 7.0) -> float:
    """BTC.D/USDT.D/TOTAL2·3 스냅샷이 몇 시간 분량 쌓였는지 (추세 판단 가능 여부 확인용)."""
    if not os.path.exists(HISTORY_FILE):
        return 0.0
    df = pd.read_csv(HISTORY_FILE)
    if df.empty or "timestamp" not in df.columns:
        return 0.0
    df = df[df["timestamp"] >= df["timestamp"].max() - window_days * 86400]
    if len(df) < 2:
        return 0.0
    return float((df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]) / 3600)


_MACRO_THRESHOLD_PCT = {"btc_d": 1.0, "usdt_d": 1.0, "total2": 5.0, "total3": 5.0}


def macro_trend_from_history(column: str, window_days: float = 7.0,
                              min_span_hours: float = 24.0) -> RegimeType:
    """누적 스냅샷으로 지표(btc_d, usdt_d, total2, total3)의 추세를 '시간' 기준으로 판단.
    (실행 빈도와 무관하게 일관되도록 '몇 개 쌓였나'가 아니라 '최근 window_days일 변화율'을 봅니다.)
    - 기록 기간이 min_span_hours 미만이면 판단 근거 부족 → 'sideways'
    - 변화율 기준: 도미넌스는 ±1%, TOTAL2/3는 ±5% (상대 변화율)"""
    if not os.path.exists(HISTORY_FILE):
        return "sideways"
    df = pd.read_csv(HISTORY_FILE)
    if df.empty or column not in df.columns or "timestamp" not in df.columns:
        return "sideways"
    df = df[df["timestamp"] >= df["timestamp"].max() - window_days * 86400]
    if len(df) < 2:
        return "sideways"
    span_hours = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]) / 3600
    if span_hours < min_span_hours:
        return "sideways"
    first, last = float(df[column].iloc[0]), float(df[column].iloc[-1])
    if first == 0:
        return "sideways"
    pct_change = (last - first) / first * 100
    th = _MACRO_THRESHOLD_PCT.get(column, 2.0)
    if pct_change > th:
        return "uptrend"
    if pct_change < -th:
        return "downtrend"
    return "sideways"


# --------------------------------------------------------------------------
# 2. BTC 가격 추세 (거시 국면의 핵심 축)
# --------------------------------------------------------------------------

_EX_CACHE: Dict = {}


def _get_ex(exchange_id: str):
    """거래소 연결을 한 번만 만들어 재사용. (호출마다 새로 만들면 시장 목록을 매번 다시 받아
    매우 느려지고 API 차단 위험이 커집니다.)"""
    if ccxt is None:
        raise RuntimeError("ccxt가 설치되어 있지 않습니다. `pip install ccxt` 후 재시도하세요.")
    if exchange_id not in _EX_CACHE:
        _EX_CACHE[exchange_id] = getattr(ccxt, exchange_id)({"enableRateLimit": True, "timeout": 15000})
    return _EX_CACHE[exchange_id]


def _mkt_symbol(ex, symbol: str) -> str:
    """조회에 쓸 실제 심볼. v29.2: 이 거래소에 그 현물이 없거나 거래 중지인데 같은 이름의 USDT 무기한 선물이 있으면 선물 심볼.
    (해외 서버에선 Bitget 현물 목록이 지역 제한으로 비어 있어서, 선물 시세로 분석하도록 보완. 현물이 정상이면 그대로 현물)"""
    if ":" in symbol:
        return symbol
    try:
        mk = ex.markets or ex.load_markets() or {}
    except Exception:
        return symbol
    m = mk.get(symbol)
    if m and m.get("active") is not False:
        return symbol
    sw = f"{symbol}:{QUOTE}"
    return sw if sw in mk else symbol


# v29.4: 분석 차트 = Bitget 선물. 5년 비교(현물 분석 vs 선물 분석, 둘 다 선물로 체결)에서 선물 분석이 나쁘지 않았고
# (수동 시각 거래당 +0.35R → +0.40R, 우연 범위 안), 실제 거래 상품을 분석하는 게 원칙이라 PC·폰 모두 선물 차트로 통일.
# 가격은 현물 단위로 맞춰서(1000PEPE 선물 → 1000으로 나눔) 나머지 계산·표시는 그대로. 연구용 과거 자료 내보내기는 현물 그대로.
ANALYZE_ON_PERP = True
_LAST_PERPS: set = set()


def _route(ex, exchange_id: str, symbol: str):
    """(실제 조회 심볼, 가격 배수). 선물 분석이면 Bitget 거래소 조회를 그 코인의 USDT 무기한 선물로 보냄."""
    if ":" in symbol:
        return symbol, 1
    if ANALYZE_ON_PERP and exchange_id == "bitget":
        perps = _LAST_PERPS
        if not perps:
            try:
                perps = bitget_perp_symbols()
            except Exception:
                perps = set()
        if perps:
            p, m = perp_match(symbol, perps)
            if p:
                return p, m
    return _mkt_symbol(ex, symbol), 1


def _last_prices(ex_id: str, syms: List[str]) -> Dict[str, float]:
    """한 거래소의 여러 심볼 현재가 {심볼: 가격}. 현물·선물(위 _route로 바뀐 것)을 나눠 한 번씩 조회. 가격은 현물 단위."""
    ex = _get_ex(ex_id)
    routed = {s: _route(ex, ex_id, s) for s in syms}
    real = {s: r for s, (r, _) in routed.items()}
    groups: Dict[bool, List[str]] = {}
    for r in dict.fromkeys(real.values()):
        groups.setdefault(":" in r, []).append(r)
    tickers: Dict = {}
    for is_swap, g in groups.items():
        try:
            tickers.update(ex.fetch_tickers(g) or {})
        except Exception:
            params = ({"type": "swap", "productType": "USDT-FUTURES"} if ex_id == "bitget" else {"type": "swap"}) if is_swap else {}
            tickers.update(ex.fetch_tickers(None, params) or {})
    out: Dict[str, float] = {}
    for s, (r, m) in routed.items():
        t = tickers.get(r) or {}
        last = t.get("last") or t.get("close")
        if last:
            out[s] = float(last) / m
    return out


def fetch_btc_df() -> pd.DataFrame:
    """BTC 4h 캔들. 한 거래소가 지역 차단/장애여도 다음 거래소로 넘어가도록 순차 시도."""
    for ex_id in EXCHANGES:
        df = fetch_ohlcv(ex_id, f"BTC/{QUOTE}")
        if df is not None and len(df) > 0:
            return df
    raise RuntimeError("모든 거래소에서 BTC 데이터를 가져오지 못했습니다 (네트워크/지역 차단/거래소 장애 확인)")


def _fetch_ohlcv_paged(ex, symbol: str, timeframe: str, total: int, max_calls: int = 8) -> List:
    """거래소마다 한 번에 주는 캔들 수가 달라서(예: OKX 300개, Bitget·Binance 1000개),
    필요한 개수(total)를 채울 때까지 과거→현재 방향으로 나눠 받습니다."""
    tf_ms = ex.parse_timeframe(timeframe) * 1000
    now = ex.milliseconds()
    since = now - total * tf_ms
    lim = min(total, 1000)
    long_mode = max_calls > 12  # 긴 과거 수집(연구용 내보내기·검증)일 때만 상장 시점 탐색·빈 구간 건너뛰기
    rows: List = []

    def _get(s: int) -> List:
        if not long_mode:
            return ex.fetch_ohlcv(symbol, timeframe=timeframe, since=s, limit=lim)
        try:
            return ex.fetch_ohlcv(symbol, timeframe=timeframe, since=s, limit=lim) or []
        except Exception:
            return []

    batch = _get(since)
    calls, empty_run = 1, 0
    if not batch and long_mode:
        # v26.1: 요청 시작점이 상장 전이면 OKX 등은 '시작점부터 일정 구간' 안에 봉이 없어 빈 결과를 줌.
        # 예전엔 여기서 멈춰 그 거래소에선 상장 기간이 요청보다 짧은 코인이 통째로 빠졌음 → 상장 시점을 이분 탐색(약 7회).
        # 빈 지점 lo와 봉이 있는 지점 hi의 간격이 100봉 이하면 멈춤(거래소가 한 번에 100봉 이상 주면 앞부분 손실 없음).
        lo, hi, found = since, now, None
        while hi - lo > 100 * tf_ms and calls < max_calls - 2:
            mid = lo + ((hi - lo) // 2 // tf_ms) * tf_ms
            b = _get(mid)
            calls += 1
            if b:
                hi, found = mid, b
            else:
                lo = mid
        if found:
            since, batch = hi, found
    while True:
        if batch:
            empty_run = 0
            rows += batch
            next_since = batch[-1][0] + tf_ms
            if len(rows) >= total or next_since <= since or next_since > now:
                break
            since = next_since
        else:
            # 받는 도중 빈 구간(거래 중단 등)을 만나면 긴 수집에서만 100봉씩 건너뛰며 최대 5번 이어 받기
            empty_run += 1
            if not long_mode or not rows or empty_run > 5 or since + 100 * tf_ms > now:
                break
            since += 100 * tf_ms
        if calls >= max_calls:
            break
        batch = _get(since)
        calls += 1
    return rows


def fetch_ohlcv(exchange_id: str, symbol: str, timeframe: Optional[str] = None,
                 limit: Optional[int] = None) -> Optional[pd.DataFrame]:
    """캔들 조회. 마지막 봉은 아직 진행 중일 수 있으니, 신호 계산 전에 split_live()로 분리하세요."""
    timeframe = timeframe or TIMEFRAME
    limit = limit or OHLCV_LIMIT
    if ccxt is None:
        raise RuntimeError("ccxt가 설치되어 있지 않습니다. `pip install ccxt` 실행 후 재시도하세요.")
    try:
        ex = _get_ex(exchange_id)
        msym, mult = _route(ex, exchange_id, symbol)
        try:
            raw = _fetch_ohlcv_paged(ex, msym, timeframe, limit)
        except Exception:
            raw = []
        if len(raw) < 60:  # 나눠 받기가 안 되는 거래소면 한 번에 받을 수 있는 만큼이라도
            raw = ex.fetch_ohlcv(msym, timeframe=timeframe, limit=min(limit, 1000))
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df = df.drop_duplicates(subset="ts").sort_values("ts").tail(limit).reset_index(drop=True)
        if exchange_id == "bitget" and ":" in msym and timeframe in _PERP_GRAN:
            # v29.4: ccxt 버전에 따라 선물 캔들 중간이 빠지거나 덜 오는 경우 → 거래소 원본 API로 다시 받기
            tf_ms = ex.parse_timeframe(timeframe) * 1000
            gappy = len(df) > 1 and bool((np.diff(df["ts"].to_numpy(np.int64)) != tf_ms).any())
            if gappy or len(df) < 0.9 * limit:
                try:
                    alt = _bitget_perp_history(msym, timeframe, int(limit * tf_ms / 86_400_000) + 2)
                    if len(alt) and (gappy or len(alt) > len(df)):
                        alt = alt[["ts", "open", "high", "low", "close", "volume"]].copy()
                        alt["ts"] = (alt["ts"] - pd.Timestamp("1970-01-01")) // pd.Timedelta(milliseconds=1)
                        live = df[df["ts"] > alt["ts"].iloc[-1]] if len(df) else df
                        df = pd.concat([alt, live], ignore_index=True).drop_duplicates(subset="ts") \
                            .sort_values("ts").tail(limit).reset_index(drop=True)
                except Exception as e:
                    print(f"[warn] {msym} 선물 캔들 보완 실패: {e}")
        if mult != 1:   # 선물 표기(예: 1000PEPE) → 현물 단위
            df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].astype(float) / mult
            df["volume"] = df["volume"].astype(float) * mult
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        return df
    except Exception as e:
        print(f"[warn] {exchange_id} {symbol} OHLCV 조회 실패: {e}")
        return None


def drop_unclosed(df: Optional[pd.DataFrame], timeframe: Optional[str] = None) -> Optional[pd.DataFrame]:
    """아직 마감되지 않은 마지막 봉을 제거. 신호는 완성된 봉으로만 계산해야
    몇 분 사이에 신호가 생겼다 사라지는 일이 없고, 백테스트와도 같은 조건이 됩니다."""
    if df is None or df.empty:
        return df
    timeframe = timeframe or TIMEFRAME
    now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    if df["ts"].iloc[-1] + pd.Timedelta(timeframe) > now:
        return df.iloc[:-1].reset_index(drop=True)
    return df


def split_live(df: pd.DataFrame, timeframe: Optional[str] = None):
    """(완성봉만 남긴 df, 실시간 현재가). 현재가는 진행 중 봉의 종가(=가장 최근 체결가)."""
    return drop_unclosed(df, timeframe), float(df["close"].iloc[-1])


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _adx_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    alpha = 1 / period
    atr_w = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_w
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_w
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    return dx.ewm(alpha=alpha, adjust=False).mean()


def trend_series(df: pd.DataFrame) -> np.ndarray:
    """백테스트 속도용: 전체 구간에 대해 봉별 국면(classify_price_trend와 같은 규칙)을 한 번에 계산.
    라이브는 최근 600봉 창으로 계산하는데, 600봉이면 EMA 초기값 영향이 0.3% 미만이라 결과가 사실상 같습니다."""
    c = df["close"]
    adx_s = _adx_series(df).to_numpy()
    er = ((c - c.shift(50)).abs() / c.diff().abs().rolling(50).sum()).to_numpy()
    e50, e200 = ema(c, 50).to_numpy(), ema(c, 200).to_numpy()
    net = (c - c.shift(50)).to_numpy()
    with np.errstate(invalid="ignore"):
        strong = (adx_s >= ADX_TREND_MIN) & (er >= ER_TREND_MIN)
        up = strong & (e50 > e200) & (net > 0)
        down = strong & (e50 < e200) & (net < 0)
    reg = np.full(len(df), "sideways", dtype=object)
    reg[up], reg[down] = "uptrend", "downtrend"
    reg[:60] = "sideways"
    return reg


def adx(df: pd.DataFrame, period: int = 14) -> float:
    """추세 강도(ADX), Wilder 표준 방식(지수 평활).
    - 한 봉에서 +DM/-DM은 상호 배타적(더 크게 움직인 쪽만 인정)
    - 예전 단순평균 방식은 박스권의 77%를 추세로 오판해서 교체했습니다."""
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    alpha = 1 / period
    atr_w = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_w
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_w
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    val = dx.ewm(alpha=alpha, adjust=False).mean().iloc[-1] if not dx.empty else 0.0
    return float(val) if pd.notna(val) else 0.0


def efficiency_ratio(df: pd.DataFrame, n: int = 50) -> float:
    """효율비율 = |n봉 동안 순이동| ÷ n봉 동안 움직인 총거리 (0~1).
    한 방향으로 곧게 가면 1에 가깝고, 위아래로 오가기만 하면 0에 가까움."""
    c = df["close"].to_numpy()
    n = min(n, len(c) - 1)
    if n < 5:
        return 0.0
    seg = c[-n - 1:]
    path = np.abs(np.diff(seg)).sum()
    return float(abs(seg[-1] - seg[0]) / path) if path > 0 else 0.0


def classify_price_trend(df: pd.DataFrame, adx_threshold: float = ADX_TREND_MIN,
                          er_threshold: float = ER_TREND_MIN) -> RegimeType:
    """상승/하락/횡보 분류.
    - 강도: Wilder ADX ≥ 25 그리고 효율비율(50봉) ≥ 0.20 — 둘 다 만족해야 '추세'
      (무작위 실험에서 박스권 오판율 77% → 5%, 뚜렷한 추세 인식률 약 80%)
    - 방향: EMA50 vs EMA200 배열과 최근 50봉 순이동 방향이 '일치'해야 함.
      장기 배열과 최근 움직임이 엇갈리면 전환 구간으로 보고 '횡보' 처리."""
    if df is None or len(df) < 60:
        return "sideways"
    if adx(df) < adx_threshold or efficiency_ratio(df) < er_threshold:
        return "sideways"
    ema_fast = ema(df["close"], 50).iloc[-1]
    ema_slow = ema(df["close"], min(200, len(df) - 1)).iloc[-1]
    n = min(50, len(df) - 1)
    net = df["close"].iloc[-1] - df["close"].iloc[-n - 1]
    if ema_fast > ema_slow and net > 0:
        return "uptrend"
    if ema_fast < ema_slow and net < 0:
        return "downtrend"
    return "sideways"


# --------------------------------------------------------------------------
# 3. 전체 국면 종합 판단
# --------------------------------------------------------------------------

@dataclass
class MarketRegime:
    btc_trend: RegimeType
    btc_d_trend: RegimeType
    usdt_d_trend: RegimeType
    total2_trend: RegimeType
    total3_trend: RegimeType
    overall: RegimeType
    snapshot: Dict = field(default_factory=dict)
    headline: str = ""            # 한 줄 결론 (예: "횡보 후 상승 우세")
    score: float = 0.0            # -1(강한 하락)~+1(강한 상승) 종합 방향 점수
    confidence_label: str = ""    # "높음"/"보통"/"낮음" — 근거들이 서로 얼마나 일치하는지
    explanation: str = ""         # 근거를 풀어 쓴 설명 문장
    breakout_up: Optional[float] = None    # 횡보일 때: 최근 20개 4시간봉(약 3일) BTC 최고가 — 참고값(4시간마다 다시 계산)
    breakout_down: Optional[float] = None  # 횡보일 때: 최근 20개 4시간봉(약 3일) BTC 최저가 — 참고값
    lean_verified: Optional[bool] = None   # 횡보 기울기 근거가 검증됐는지(=validate_lean_auto 결과 반영 여부)
    daily_trend: RegimeType = "sideways"   # 큰 흐름: BTC 일봉 추세
    action: str = ""                       # 행동 가이드 한 줄
    breadth_up_pct: Optional[float] = None    # 스캔 코인 중 상승 추세 비율(%)
    breadth_down_pct: Optional[float] = None  # 스캔 코인 중 하락 추세 비율(%)
    breadth_n: int = 0
    shock: bool = False                    # BTC 급변 감지(국면 전환 즉시 반영)
    transition_pending: bool = False       # 새 방향이 나왔지만 아직 확정 전(다음 봉 마감 때 확정)
    timeframe: str = "4h"                  # 신호 봉 모드
    components: List[Dict] = field(default_factory=list)  # 판단 근거별 {name, weight, value(-1~+1), text}
    alt_view: str = ""                     # 알트 시장 해석 한 줄
    validation_note: str = ""              # 과거 검증에서 같은 판정 뒤 실제 흐름
    flow: Dict = field(default_factory=dict)  # 최근 흐름(일별 거시 점수·BTC·알트 지수·50일선 위 비율)


def fetch_btc_daily_trend() -> RegimeType:
    """BTC 일봉(HTF) 추세. 한 거래소가 막혀도 다음 거래소로 순차 시도."""
    for ex_id in EXCHANGES:
        try:
            return get_htf_trend(ex_id, f"BTC/{QUOTE}", "1d")
        except Exception:
            continue
    return "sideways"


def _confidence_label(agreement: float) -> str:
    if agreement >= 0.6:
        return "높음"
    if agreement >= 0.3:
        return "보통"
    return "낮음"


def fmt_range(x: float) -> str:
    if x >= 1000:
        return f"{x:,.0f}"
    if x >= 1:
        return f"{x:,.4g}"
    return f"{x:.6g}"


def _is_btc_shock(btc_df: pd.DataFrame, atr_mult: float = 2.5) -> bool:
    """최근 완성봉 1~2개 동안 BTC가 ATR의 2.5배 이상 움직였으면 '급변'으로 판단."""
    if btc_df is None or len(btc_df) < 20:
        return False
    a = atr(btc_df)
    c = btc_df["close"]
    move = max(abs(c.iloc[-1] - c.iloc[-2]), abs(c.iloc[-1] - c.iloc[-3]))
    return bool(a > 0 and move >= atr_mult * a)


def collect_market_inputs() -> Dict:
    """시장 전체 판단에 필요한 데이터를 한 번에 수집 (API 호출은 여기서만)."""
    snap = fetch_global_snapshot()
    if snap is None:
        print("[warn] 이번 회차는 새 거시 스냅샷 없이 기존 기록만으로 판단합니다 (BTC.D 등 값 갱신 안 됨)")
        snap = {}
    btc_closed, btc_live = split_live(fetch_btc_df())
    span_h = macro_history_span_hours()
    snap["macro_span_hours"] = span_h
    return {
        "snap": snap, "btc_df": btc_closed, "btc_live": btc_live,
        "btc_trend": classify_price_trend(btc_closed),
        "daily_trend": fetch_btc_daily_trend(),
        "macro": {k: macro_trend_from_history(k) for k in ("btc_d", "usdt_d", "total2", "total3")},
        "span_h": span_h,
        "candle_ts": str(btc_closed["ts"].iloc[-1]),
        "shock": _is_btc_shock(btc_closed),
    }


_TREND_NUM = {"uptrend": 1.0, "downtrend": -1.0, "sideways": 0.0}
_TREND_KR = {"uptrend": "상승", "downtrend": "하락", "sideways": "횡보"}


MACRO_VAL_FILE = "macro_validation.json"
MACRO_WEIGHTS = {"daily": 0.25, "h4": 0.15, "alt": 0.20, "altbtc": 0.10, "ethbtc": 0.05,
                 "b50": 0.15, "breadth": 0.05, "macro": 0.05}


def close_trend_series(close: pd.Series, fast: int = 20, slow: int = 50, er_n: int = 30,
                       er_min: float = 0.2) -> np.ndarray:
    """종가만 있는 지수(알트 지수·비율 차트 등)의 봉별 추세.
    상승: EMA20 > EMA50 이고 30봉 순이동이 플러스이며 효율비율 ≥ 0.2 (하락은 반대). 나머지는 횡보."""
    c = close.astype(float)
    ef, es = ema(c, fast), ema(c, slow)
    net = c - c.shift(er_n)
    er = net.abs() / c.diff().abs().rolling(er_n).sum()
    up = ((ef > es) & (net > 0) & (er >= er_min)).to_numpy()
    dn = ((ef < es) & (net < 0) & (er >= er_min)).to_numpy()
    out = np.full(len(c), "sideways", dtype=object)
    out[up], out[dn] = "uptrend", "downtrend"
    out[:slow] = "sideways"
    return out


def _daily_closes(df: pd.DataFrame) -> pd.Series:
    """완성된 봉들을 일봉 종가로 (하루치 봉이 다 모인 날만)."""
    per_day = max(1, int(pd.Timedelta("1D") / pd.Timedelta(TIMEFRAME)))
    g = df.set_index("ts")["close"].resample("1D")
    last, cnt = g.last(), g.count()
    return last[cnt >= per_day].dropna()


def _breadth50(closes: Dict[str, pd.Series]) -> tuple:
    """50일선 위 코인 비율 − 아래 비율 (−1~+1), 위 비율(%), 코인 수."""
    above = below = 0
    for c in closes.values():
        if len(c) >= 55:
            e = ema(c, 50).iloc[-1]
            above += c.iloc[-1] > e
            below += c.iloc[-1] < e
    n = above + below
    return ((above - below) / n if n else None), (above / n * 100 if n else None), n


def macro_extras_from_loaded(loaded: Dict[str, tuple], btc_df: pd.DataFrame) -> Dict:
    """스캔에서 이미 받은 캔들로 알트 지수·알트/BTC·ETH/BTC·50일선 참여도 계산 (추가 API 호출 없음)."""
    btc_d = _daily_closes(btc_df)
    closes = {sym: _daily_closes(d) for sym, (_, d) in loaded.items() if sym.split("/")[0] != "BTC"}
    closes = {k: v for k, v in closes.items() if len(v) >= 20}
    out: Dict = {}
    alt_frames = [pd.DataFrame({"ts": v.index, "close": v.to_numpy()}) for k, v in closes.items()
                  if k.split("/")[0] != "ETH"]
    alt = build_alt_index(alt_frames)
    if alt is not None and len(alt) >= 60:
        a = alt.set_index("ts")["close"]
        out["alt"] = close_trend_series(a)[-1]
        out["alt_ret30"] = float(a.iloc[-1] / a.iloc[-31] - 1) if len(a) > 31 else None
        ratio = (a / btc_d.reindex(a.index)).dropna()
        if len(ratio) >= 60:
            out["altbtc"] = close_trend_series(ratio)[-1]
            out["altbtc_ret30"] = float(ratio.iloc[-1] / ratio.iloc[-31] - 1) if len(ratio) > 31 else None
    eth = closes.get(f"ETH/{QUOTE}")
    if eth is not None:
        r = (eth / btc_d.reindex(eth.index)).dropna()
        if len(r) >= 60:
            out["ethbtc"] = close_trend_series(r)[-1]
    b50, above_pct, n50 = _breadth50(closes)
    if b50 is not None and n50 >= 10:
        out.update(b50=b50, b50_above_pct=above_pct, b50_n=n50)
    try:
        out["hist"] = _macro_history(btc_df, alt, closes)
    except Exception as e:
        print(f"[warn] 시장 흐름 계산 실패: {e}")
    return out


def _macro_history(btc_df: pd.DataFrame, alt: Optional[pd.DataFrame], closes: Dict[str, pd.Series],
                   days: int = 45) -> Dict:
    """최근 흐름: 일봉 근거(BTC 일봉·알트 지수·알트/BTC·ETH/BTC·50일선 위 비율)로 날마다 거시 점수를 다시 계산.
    4시간봉 근거와 도미넌스 기록은 일별로 재현할 수 없어 빠짐(메인 판정의 약 75% 근거)."""
    per_day = max(1, int(pd.Timedelta("1D") / pd.Timedelta(TIMEFRAME)))
    g = btc_df.set_index("ts").resample("1D")
    btc_ohlc = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                             "close": g["close"].last(), "volume": g["volume"].sum(), "n": g["close"].count()})
    btc_ohlc = btc_ohlc[btc_ohlc["n"] >= per_day].drop(columns="n").dropna()
    dates = btc_ohlc.index
    comp: Dict[str, pd.Series] = {"daily": pd.Series([_TREND_NUM[x] for x in trend_series(btc_ohlc.reset_index())],
                                                     index=dates)}
    alt_c = None
    if alt is not None and len(alt) >= 60:
        alt_c = alt.set_index("ts")["close"].reindex(dates).ffill()
        a_ = alt_c.dropna()
        comp["alt"] = pd.Series([_TREND_NUM[x] for x in close_trend_series(a_)], index=a_.index)
        ratio = (alt_c / btc_ohlc["close"]).dropna()
        comp["altbtc"] = pd.Series([_TREND_NUM[x] for x in close_trend_series(ratio)], index=ratio.index)
    eth = closes.get(f"ETH/{QUOTE}")
    if eth is not None:
        er_ = (eth.reindex(dates) / btc_ohlc["close"]).dropna()
        if len(er_) >= 60:
            comp["ethbtc"] = pd.Series([_TREND_NUM[x] for x in close_trend_series(er_)], index=er_.index)
    mat = pd.DataFrame(closes).reindex(dates)
    e50 = mat.ewm(span=50, adjust=False, min_periods=50).mean()
    ab, be = (mat > e50).sum(axis=1), (mat < e50).sum(axis=1)
    valid = (ab + be) >= 10
    comp["b50"] = ((ab - be) / (ab + be).replace(0, np.nan)).where(valid)
    df_ = pd.DataFrame(comp).reindex(dates)
    w = pd.Series({k: MACRO_WEIGHTS[k] for k in df_.columns})
    score = (df_.fillna(0) * w).sum(axis=1) / (df_.notna() * w).sum(axis=1).replace(0, np.nan)
    score = score.iloc[55:].tail(days)  # 이동평균이 자리 잡은 뒤부터
    if len(score) < 5:
        return {}
    idx = score.index
    btc_c = btc_ohlc["close"].reindex(idx)
    out = {"date": [d.strftime("%Y-%m-%d") for d in idx], "score": [round(float(v), 3) for v in score],
           "btc": [round(float(v / btc_c.iloc[0] * 100), 2) for v in btc_c]}
    if alt_c is not None:
        a2 = alt_c.reindex(idx)
        out["alt"] = [round(float(v / a2.iloc[0] * 100), 2) for v in a2]
    above_pct = (ab / (ab + be).replace(0, np.nan) * 100).reindex(idx)
    out["b50pct"] = [None if pd.isna(v) else round(float(v), 1) for v in above_pct]
    states = [_macro_state(v) for v in score]
    run = 1
    for k in range(len(states) - 2, -1, -1):
        if states[k] != states[-1]:
            break
        run += 1
    out["state_days"] = run
    out["delta7"] = float(score.iloc[-1] - score.iloc[-8]) if len(score) >= 8 else None
    return out


def load_macro_validation() -> Optional[Dict]:
    try:
        with open(MACRO_VAL_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _macro_state(score: float) -> str:
    return "uptrend" if score >= 0.25 else ("downtrend" if score <= -0.25 else "sideways")


def validate_macro(n_coins: int = 20, days: int = 730, horizon: int = 7, progress_cb=None) -> Dict:
    """거시 방향 판단의 과거 검증: 2년치 일봉으로 매일의 판정(일봉 근거만)을 다시 계산하고,
    그날 이후 horizon일 동안 알트 지수·BTC가 실제로 어떻게 움직였는지 판정 상태별로 집계.
    (4시간봉 근거와 도미넌스 기록은 과거 데이터로 재현할 수 없어 제외 — 실제 판정의 약 75% 근거를 검증)"""
    total = days + 260
    ex_id, btc = None, None
    for cand in EXCHANGES:
        try:
            b_ = fetch_extended_ohlcv(cand, f"BTC/{QUOTE}", "1d", total)
        except Exception:
            continue
        if len(b_) >= 300 and (btc is None or len(b_) > len(btc)):
            ex_id, btc = cand, b_
        if btc is not None and len(btc) >= 0.9 * total:
            break
    if ex_id is None:
        raise RuntimeError("BTC 일봉을 받을 수 있는 거래소가 없어요")
    perps = bitget_perp_symbols() if BITGET_ONLY else set()
    uni = build_universe(60, perps or None)
    ranked = [x for x in uni if x != f"BTC/{QUOTE}"]
    idx = np.linspace(0, len(ranked) - 1, num=min(n_coins, len(ranked))).round().astype(int)
    syms = list(dict.fromkeys([ranked[i] for i in idx] + ([f"ETH/{QUOTE}"] if f"ETH/{QUOTE}" in uni else [])))
    closes: Dict[str, pd.Series] = {}
    for k, sym in enumerate(syms):
        if progress_cb:
            progress_cb(k, len(syms), sym)
        for src in dict.fromkeys([ex_id, uni.get(sym, {}).get("src", ex_id)]):
            try:
                d = fetch_extended_ohlcv(src, sym, "1d", total)
            except Exception:
                d = None
            if d is not None and len(d) >= 120:
                closes[sym] = d.set_index("ts")["close"]
                break
    if progress_cb:
        progress_cb(1, 1, "")
    btc_c = btc.set_index("ts")["close"]
    dates = btc_c.index
    comp: Dict[str, pd.Series] = {"daily": pd.Series([_TREND_NUM[x] for x in trend_series(btc)], index=dates)}
    alt_frames = [pd.DataFrame({"ts": v.index, "close": v.to_numpy()}) for k, v in closes.items() if k.split("/")[0] != "ETH"]
    alt = build_alt_index(alt_frames)
    if alt is None:
        raise RuntimeError("알트 지수를 만들 코인 데이터가 부족해요")
    alt_c = alt.set_index("ts")["close"].reindex(dates).ffill()
    comp["alt"] = pd.Series([_TREND_NUM[x] for x in close_trend_series(alt_c.dropna())], index=alt_c.dropna().index)
    ratio = (alt_c / btc_c).dropna()
    comp["altbtc"] = pd.Series([_TREND_NUM[x] for x in close_trend_series(ratio)], index=ratio.index)
    if f"ETH/{QUOTE}" in closes:
        er_ = (closes[f"ETH/{QUOTE}"].reindex(dates) / btc_c).dropna()
        comp["ethbtc"] = pd.Series([_TREND_NUM[x] for x in close_trend_series(er_)], index=er_.index)
    mat = pd.DataFrame(closes).reindex(dates)
    e50 = mat.ewm(span=50, adjust=False, min_periods=50).mean()
    ab, be = (mat > e50).sum(axis=1), (mat < e50).sum(axis=1)
    comp["b50"] = ((ab - be) / (ab + be).replace(0, np.nan)).where((ab + be) >= 5)
    df_ = pd.DataFrame(comp).reindex(dates)
    w = pd.Series({k: MACRO_WEIGHTS[k] for k in df_.columns})
    avail = df_.notna()
    score = (df_.fillna(0) * w).sum(axis=1) / (avail * w).sum(axis=1).replace(0, np.nan)
    fwd_alt = alt_c.shift(-horizon) / alt_c - 1
    fwd_btc = btc_c.shift(-horizon) / btc_c - 1
    ev = pd.DataFrame({"score": score, "alt": fwd_alt, "btc": fwd_btc}).iloc[120:].dropna()
    ev = ev[ev.index >= ev.index.max() - pd.Timedelta(days=days)]
    ev["state"] = ev["score"].map(_macro_state)

    def table(e: pd.DataFrame) -> List[Dict]:
        rows = []
        for st_ in ("uptrend", "sideways", "downtrend"):
            v = e[e["state"] == st_]
            if len(v):
                rows.append({"state": st_, "days": int(len(v)), "alt_mean": float(v["alt"].mean()),
                             "alt_up": float((v["alt"] > 0).mean()), "btc_mean": float(v["btc"].mean()),
                             "btc_up": float((v["btc"] > 0).mean())})
        rows.append({"state": "all", "days": int(len(e)), "alt_mean": float(e["alt"].mean()),
                     "alt_up": float((e["alt"] > 0).mean()), "btc_mean": float(e["btc"].mean()),
                     "btc_up": float((e["btc"] > 0).mean())})
        return rows

    cut = ev.index.min() + (ev.index.max() - ev.index.min()) * 2 / 3
    res = {"horizon": horizon, "exchange": ex_id, "coins": list(closes), "days": int(len(ev)),
           "start": str(ev.index.min().date()), "end": str(ev.index.max().date()),
           "all": table(ev), "recent": table(ev[ev.index >= cut]), "recent_from": str(cut.date()),
           "made_at": str(utc_now())}
    try:
        with open(MACRO_VAL_FILE, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False)
    except Exception as e:
        print(f"[warn] 거시 검증 저장 실패: {e}")
    return res


_STATE_KR = {"uptrend": "상승 판정", "sideways": "횡보 판정", "downtrend": "하락 판정", "all": "전체 날짜"}


def macro_validation_lines(v: Dict) -> List[str]:
    """과거 검증 결과를 판정 문장으로."""
    rows = {r["state"]: r for r in v["all"]}
    base = rows.get("all", {})
    up, dn, sd = rows.get("uptrend"), rows.get("downtrend"), rows.get("sideways")
    out = []
    for r in v["all"]:
        out.append(f"{_STATE_KR[r['state']]} {r['days']}일: 이후 {v['horizon']}일 알트 지수 평균 {r['alt_mean']:+.1%}, "
                   f"오른 비율 {r['alt_up']:.0%} · BTC 평균 {r['btc_mean']:+.1%}")
    # 평균 수익 차이 0.3%p 이상 + 오른 비율 차이 5%p 이상이어야 '구분력 있음'으로 인정
    good_up = bool(up and up["days"] >= 20 and up["alt_mean"] >= base["alt_mean"] + 0.003
                   and up["alt_up"] >= base["alt_up"] + 0.05)
    good_dn = bool(dn and dn["days"] >= 20 and dn["alt_mean"] <= base["alt_mean"] - 0.003
                   and dn["alt_up"] <= base["alt_up"] - 0.05)
    order = up and dn and up["alt_mean"] > (sd["alt_mean"] if sd else base["alt_mean"]) > dn["alt_mean"]
    if good_up and good_dn and order:
        out.append("✅ 판정이 이후 흐름과 잘 맞았어요: 상승 판정 뒤엔 평균보다 잘 오르고, 하락 판정 뒤엔 평균보다 잘 빠졌어요.")
    elif good_up or good_dn:
        out.append("🟡 일부만 맞았어요: " + ("상승 판정은 쓸 만하고" if good_up else "상승 판정은 구분력이 약하고")
                   + (", 하락 판정도 쓸 만해요." if good_dn else ", 하락 판정은 구분력이 약해요."))
    else:
        out.append("❌ 판정이 이후 흐름을 뚜렷하게 구분하지 못했어요. 거시 방향은 참고로만 쓰세요.")
    rr = {r["state"]: r for r in v.get("recent", [])}
    if rr.get("uptrend") or rr.get("downtrend"):
        parts = [f"{_STATE_KR[k]} {rr[k]['days']}일 평균 {rr[k]['alt_mean']:+.1%}" for k in ("uptrend", "downtrend") if rr.get(k)]
        out.append(f"최근 3분의 1 기간({v['recent_from']}~): " + " · ".join(parts))
    return out


def compose_market_regime(inputs: Dict, breadth: Optional[Dict] = None, commit: bool = True,
                          extras: Optional[Dict] = None) -> MarketRegime:
    """시장 전체 방향을 하나의 점수(-1~+1)와 한 줄 결론으로 종합.
    - 큰 흐름: BTC 일봉 추세 (가중치 0.35)
    - 중기 흐름: BTC 4시간봉 추세 (0.30)
    - 시장 참여: 스캔한 코인 중 상승 추세 비율 − 하락 추세 비율 (0.25) — 스캔 후에만 반영
    - 도미넌스·TOTAL: 기록이 24시간 이상 쌓였을 때만 (0.10)
    점수 ≥ +0.25 상승 / ≤ −0.25 하락 / 그 사이 횡보. 같은 4시간봉 안에서는 결과가 흔들리지 않고,
    BTC 급변 때만 즉시 전환합니다."""
    btc_df, btc_trend, daily = inputs["btc_df"], inputs["btc_trend"], inputs["daily_trend"]
    macro, span_h = inputs["macro"], inputs["span_h"]

    W = MACRO_WEIGHTS
    ex = extras or {}
    parts = {"daily": (W["daily"], _TREND_NUM[daily]), "h4": (W["h4"], _TREND_NUM[btc_trend])}
    comps = [{"name": "BTC 일봉 추세", "weight": W["daily"], "value": _TREND_NUM[daily], "text": _TREND_KR[daily]},
             {"name": f"BTC {tf_label()} 추세", "weight": W["h4"], "value": _TREND_NUM[btc_trend], "text": _TREND_KR[btc_trend]}]
    if ex.get("alt"):
        parts["alt"] = (W["alt"], _TREND_NUM[ex["alt"]])
        r30 = f" (30일 {ex['alt_ret30']:+.0%})" if ex.get("alt_ret30") is not None else ""
        comps.append({"name": "알트 지수 일봉 추세", "weight": W["alt"], "value": _TREND_NUM[ex["alt"]],
                      "text": _TREND_KR[ex["alt"]] + r30})
    if ex.get("altbtc"):
        parts["altbtc"] = (W["altbtc"], _TREND_NUM[ex["altbtc"]])
        r30 = f" (30일 {ex['altbtc_ret30']:+.0%})" if ex.get("altbtc_ret30") is not None else ""
        comps.append({"name": "알트/BTC 비율 (알트 시즌 여부)", "weight": W["altbtc"], "value": _TREND_NUM[ex["altbtc"]],
                      "text": _TREND_KR[ex["altbtc"]] + r30})
    if ex.get("ethbtc"):
        parts["ethbtc"] = (W["ethbtc"], _TREND_NUM[ex["ethbtc"]])
        comps.append({"name": "ETH/BTC 비율", "weight": W["ethbtc"], "value": _TREND_NUM[ex["ethbtc"]],
                      "text": _TREND_KR[ex["ethbtc"]]})
    if ex.get("b50") is not None:
        parts["b50"] = (W["b50"], float(ex["b50"]))
        comps.append({"name": f"50일선 위 코인 비율 ({ex['b50_n']}개)", "weight": W["b50"], "value": float(ex["b50"]),
                      "text": f"{ex['b50_above_pct']:.0f}%"})
    up_pct = down_pct = None
    n = 0
    if breadth and breadth.get("n", 0) >= 10:
        n = breadth["n"]
        up_pct, down_pct = breadth["uptrend"] / n * 100, breadth["downtrend"] / n * 100
        parts["breadth"] = (W["breadth"], (up_pct - down_pct) / 100)
        comps.append({"name": f"{tf_label()} 추세 코인 비율 ({n}개)", "weight": W["breadth"],
                      "value": (up_pct - down_pct) / 100, "text": f"상승 {up_pct:.0f}% · 하락 {down_pct:.0f}%"})
    macro_ready = span_h >= 24
    if macro_ready:
        mv = [-_TREND_NUM[macro["btc_d"]], -_TREND_NUM[macro["usdt_d"]],
              _TREND_NUM[macro["total2"]], _TREND_NUM[macro["total3"]]]
        parts["macro"] = (W["macro"], float(np.mean(mv)))
        comps.append({"name": "도미넌스·TOTAL 기록", "weight": W["macro"], "value": float(np.mean(mv)),
                      "text": f"BTC.D {_TREND_KR[macro['btc_d']]} · USDT.D {_TREND_KR[macro['usdt_d']]}"})

    wsum = sum(w for w, _ in parts.values())
    score = float(np.clip(sum(w * v for w, v in parts.values()) / wsum, -1, 1))
    raw = "uptrend" if score >= 0.25 else ("downtrend" if score <= -0.25 else "sideways")
    overall = _apply_regime_hysteresis(raw, inputs["candle_ts"], inputs["shock"], commit=commit)

    # 신뢰도: 결론 방향과 같은 쪽을 가리키는 근거의 가중 비율
    breakout_up = breakout_down = None
    lean_verified = None
    if raw != "sideways":
        sign = 1 if raw == "uptrend" else -1
        agreement = sum(w for w, v in parts.values() if v * sign > 0) / wsum
    else:
        agreement = 0.0

    # 한 줄 결론: 큰 흐름(일봉)과 중기 흐름(4시간)의 조합으로 표현
    if btc_trend == "sideways":
        lean = compute_sideways_lean(btc_df, daily)
        agreement = min(abs(lean["score"]) / 0.6, 1.0)
        lean_word = "상승 우세" if lean["score"] > 0.3 else ("하락 우세" if lean["score"] < -0.3 else "방향 대기")
        prefix = {"uptrend": "상승 추세 속 횡보", "downtrend": "하락 추세 속 횡보", "sideways": "횡보 후"}[daily]
        headline = f"{prefix} {lean_word}" if daily == "sideways" and lean_word != "방향 대기" else \
                   ("횡보 · 방향 대기" if daily == "sideways" else f"{prefix} · {lean_word}")
        breakout_up = float(btc_df["high"].tail(20).max())
        breakout_down = float(btc_df["low"].tail(20).min())
        lean_verified = False
        # v29.8: 예전 문구('위로 돌파 시 롱 쪽, 아래로 이탈 시 숏 쪽으로 무게')는 검증 안 된 감 매매 안내였고,
        # 기준선이 최근 20봉 최고·최저라 4시간마다 따라 움직여 '계속 롱'처럼 읽혔음 → 진입은 추천 카드만으로 안내
        action = "BTC가 큰 방향 없이 오르내리는 중이에요. 진입은 추천 카드만 — 바로 위 매매 규칙 줄이 실제로 적용되는 규칙이에요"
    elif daily == btc_trend:
        strong = abs(score) >= 0.6
        if btc_trend == "uptrend":
            headline = "강한 상승장" if strong else "상승장"
            action = "롱 위주. 눌림목 지정가에서만 진입하고, 숏은 개별적으로 약한 코인만 짧게"
        else:
            headline = "강한 하락장" if strong else "하락장"
            action = "숏 위주. 반등 지정가에서만 진입하고, 롱은 개별적으로 강한 코인만 짧게"
    elif daily == "sideways":
        headline = f"단기 {_TREND_KR[btc_trend]} (큰 흐름은 중립)"
        action = "큰 흐름이 불분명해요. 목표는 짧게(목표1 위주), 비중은 평소보다 작게"
    elif btc_trend == "downtrend":
        headline = "상승 추세 속 조정"
        action = "큰 흐름은 상승. 조정이 끝나는 지지 자리에서 롱을 준비하고, 숏은 짧게만"
    else:
        headline = "하락 추세 속 반등"
        action = "큰 흐름은 하락. 반등이 끝나는 저항 자리에서 숏을 준비하고, 롱은 짧게만"

    # 설명 문장
    parts_txt = [f"큰 흐름(BTC 일봉)은 {_TREND_KR[daily]}, 중기 흐름(BTC {tf_label()})은 {_TREND_KR[btc_trend]}"]
    if up_pct is not None:
        parts_txt.append(f"스캔한 {n}개 코인 중 {up_pct:.0f}%가 상승 추세, {down_pct:.0f}%가 하락 추세예요")
        if btc_trend == "uptrend" and up_pct < 35:
            parts_txt.append("BTC만 강하고 알트코인 다수는 따라오지 못하고 있어 알트 롱은 선별이 필요해요")
        elif btc_trend == "downtrend" and up_pct >= 50:
            parts_txt.append("BTC는 약하지만 알트코인 다수가 버티는 중이에요")
    if macro_ready:
        if macro["usdt_d"] == "uptrend":
            parts_txt.append("현금성 자금(USDT) 비중이 늘고 있어 위험 회피 분위기예요")
        elif macro["usdt_d"] == "downtrend":
            parts_txt.append("현금성 자금(USDT) 비중이 줄고 있어 위험자산 선호 분위기예요")
    else:
        parts_txt.append(f"도미넌스·TOTAL 지표는 기록이 {span_h:.0f}시간 쌓여 아직 반영 전이에요")
    if inputs["shock"]:
        parts_txt.append("BTC 급변이 감지되어 국면을 즉시 반영했어요")
    explanation = ". ".join(parts_txt) + "."
    alt_t, ab_t = ex.get("alt"), ex.get("altbtc")
    if alt_t == "uptrend" and ab_t == "uptrend":
        alt_view = "알트 지수와 알트/BTC 비율이 함께 올라 알트 강세장이에요"
    elif alt_t == "downtrend" and ab_t == "downtrend":
        alt_view = "알트가 떨어지면서 BTC보다도 약해요 — 알트 롱은 비중을 줄이세요"
    elif daily == "uptrend" and ab_t == "downtrend":
        alt_view = "BTC가 주도하고 알트는 상대적으로 약해요 — 알트 롱은 강한 코인만"
    elif daily == "downtrend" and ab_t == "uptrend":
        alt_view = "BTC 약세 속에서도 알트가 상대적으로 버티고 있어요"
    elif alt_t:
        alt_view = f"알트 지수는 {_TREND_KR[alt_t]}" + (f", 알트/BTC 비율은 {_TREND_KR[ab_t]}" if ab_t else "") + "이에요"
    else:
        alt_view = ""
    if ex.get("b50_above_pct") is not None:
        alt_view += (" · " if alt_view else "") + f"코인 {ex['b50_above_pct']:.0f}%가 50일선 위"
    vnote = ""
    mv_ = load_macro_validation()
    if mv_:
        row = next((r for r in mv_.get("all", []) if r["state"] == overall), None)
        base_ = next((r for r in mv_.get("all", []) if r["state"] == "all"), None)
        if row and base_:
            vnote = (f"과거 검증({mv_['start'][:7]}~): 같은 판정 {row['days']}일 뒤 {mv_['horizon']}일간 알트 지수 평균 "
                     f"{row['alt_mean']:+.1%}, 오른 비율 {row['alt_up']:.0%} (전체 평균 {base_['alt_up']:.0%})")

    return MarketRegime(
        btc_trend, macro["btc_d"], macro["usdt_d"], macro["total2"], macro["total3"], overall,
        dict(inputs["snap"]), headline=headline, score=score,
        confidence_label=_confidence_label(agreement), explanation=explanation,
        breakout_up=breakout_up, breakout_down=breakout_down, lean_verified=lean_verified,
        daily_trend=daily, action=action, breadth_up_pct=up_pct, breadth_down_pct=down_pct,
        breadth_n=n, shock=inputs["shock"], transition_pending=(raw != overall), timeframe=TIMEFRAME,
        components=comps, alt_view=alt_view, validation_note=vnote, flow=ex.get("hist", {}),
    )


def determine_overall_regime() -> MarketRegime:
    """시장 전체 국면(스캔 없이 BTC·거시 지표만). 콘솔 실행(main) 등에서 사용."""
    return compose_market_regime(collect_market_inputs(), None, commit=True)


def _apply_regime_hysteresis(raw_regime: RegimeType, candle_ts: str, shock: bool = False,
                             commit: bool = True, confirm_count: int = 2) -> RegimeType:
    """국면 전환 확정 규칙 (시간 기준).
    - 같은 4시간봉 안에서는 몇 번을 새로 분석해도 한 번으로 셈 (버튼 연타로 국면이 바뀌지 않음)
    - 완성된 4시간봉 기준 confirm_count번 연속 같은 결과가 나와야 전환 인정
    - BTC 급변(shock)이면 즉시 인정
    commit=False면 기록을 남기지 않고 결과만 계산 (스캔 전 임시 판단용)."""
    cols = ["candle_ts", "raw_regime", "confirmed_regime"]
    path = REGIME_LOG_FILE if TIMEFRAME == "4h" else REGIME_LOG_FILE.replace(".csv", f"_{TIMEFRAME}.csv")
    log = pd.read_csv(path) if os.path.exists(path) else pd.DataFrame(columns=cols)
    if "candle_ts" not in log.columns:  # 예전 형식(호출 횟수 기준) 기록은 버리고 새로 시작
        log = pd.DataFrame(columns=cols)
    log = log.astype(object)
    if len(log) and str(log["candle_ts"].iloc[-1]) == str(candle_ts):
        log.loc[log.index[-1], "raw_regime"] = raw_regime
    else:
        log = pd.concat([log, pd.DataFrame([{"candle_ts": str(candle_ts), "raw_regime": raw_regime,
                                             "confirmed_regime": None}])], ignore_index=True)
    prior = log.iloc[:-1]
    prev = prior["confirmed_regime"].dropna() if len(prior) else pd.Series(dtype=object)
    prev_conf = prev.iloc[-1] if len(prev) else None
    recent = log["raw_regime"].tail(confirm_count).tolist()
    if shock or prev_conf is None or (len(recent) >= confirm_count and len(set(recent)) == 1):
        confirmed = raw_regime
    else:
        confirmed = prev_conf
    log.loc[log.index[-1], "confirmed_regime"] = confirmed
    if commit:
        log.tail(500).to_csv(path, index=False)
    return confirmed


# --------------------------------------------------------------------------
# 4. 코인 스크리너 (거래량 상위 + 상대강도 + 오더블록 + 변동성)
# --------------------------------------------------------------------------

@dataclass
class CoinSetup:
    symbol: str
    exchange: str
    bias: Literal["long", "short", "wait_breakout_long", "wait_breakout_short",
                  "range_fade_long", "range_fade_short", "donchian_long", "donchian_short",
                  "st_long", "st_short"]
    entry_note: str
    entry_price: float   # 추격이 아닌, 되돌림 지정가(limit) 진입가
    current_price: float # 참고용 현재가 (추격 여부 비교용)
    tp1: float
    tp2: float
    sl: float
    rr_ratio: float
    is_chase: bool = False  # True면 아직 되돌림 전(=추격 구간)이라는 경고 플래그
    poc_confluence: bool = False  # True면 진입가가 POC/Value Area와도 겹치는 고신뢰 구간
    rs: float = 0.0  # 지수(BTC) 대비 상대강도(%) — 클수록 시장 대비 강함
    asymmetry: Optional[float] = None  # 상승포착률-하락포착률 — 클수록 '오를 때 크게 빠질 때 작게'
    bitget_perp: Optional[bool] = None  # Bitget USDT 무기한 선물 거래 가능 여부(None=확인 불가)
    counter_trend: bool = False  # True면 이 코인의 개별 국면이 시장 전체 국면과 반대 방향
    sweep_confluence: bool = False  # True면 진입 자리에서 유동성 스윕(손절 사냥 후 반전)이 확인됨
    coin_regime: str = ""  # 이 코인 자체의 국면 (시장 전체와 비교해 역행 여부 판단)
    atr: float = 0.0       # 신호 계산 시점의 ATR (추격 판단·추적 손절 폭 계산용)
    live_status: str = ""  # ready(진입가 근처) / chase(추격 구간) / invalid(손절선 먼저 이탈) / missed(목표 먼저 도달)
    perp_symbol: str = ""  # Bitget 선물 심볼 (예: 1000PEPE/USDT:USDT)
    perp_mult: int = 1     # 선물 가격 = 현물 가격 × perp_mult
    rs_alt: Optional[float] = None  # 알트 지수 대비 상대강도(%) — 알트끼리 비교한 힘
    tags: List[str] = field(default_factory=list)  # 돌파 신호의 보조 표시 (TAG_LABELS 참고)
    signal_ts: str = ""        # 신호가 나온 완성봉 시각 (실전 추적용)
    market_entry: bool = False  # 시장가 진입 신호(신고점 돌파·즉시 진입) 여부
    health: str = ""            # 자동 방어 상태: "" 정상·판단 전 / caution 주의(리스크 절반) / paused 자동 중지
    risk_mult: float = 1.0      # 권장 리스크 배수 (주의 상태면 0.5, 시험 운용이면 ×0.25)
    trial: bool = False         # 🧪 시험 운용 중인 신호
    group: str = ""             # BTC / 메이저 / 일반 알트
    vol_tier: str = ""          # 거래량 순위 구간 (거래량 1~30위 / 31~60위 / 61~100위 / 101위~)
    vol24h: Optional[float] = None  # 24시간 거래대금(USDT) — 유동성 상한 계산용
    level: Optional[float] = None   # v27: 손절 기준선 (박스 경계 / 되돌림 저점·고점 / 다이버전스 저점·고점)
    box_h: Optional[float] = None   # v27: 목표 계산용 높이 (박스 높이)
    rr_cap: Optional[float] = None  # 박스 돌파 손익비 상한 판정값 (예전 정의: 목표 경계+높이 · 손절 경계−1ATR)
    exit_frac: Optional[float] = None   # v27: 목표1에서 익절 비율 (0 = 익절 없이 추적 손절로 전환)
    exit_trail: Optional[float] = None  # v27: 추적 손절 폭(ATR 배수)
    max_hold: Optional[int] = None      # v27: 최대 보유 봉 수
    half_start: bool = False            # v27: 새 매매법 — 위험 절반으로 시작
    fixed_risk: Optional[float] = None  # v30.3: 승격 없이 고정한 위험 비중(추세 눌림목 0.5)
    sunday_half: bool = False           # v32.1: 일요일 진입 — 위험 ½
    chop_half: bool = False             # v32.6: 시장 출렁임 지수 52 초과 — 위험 ½
    chop_value: Optional[float] = None  # v32.6: 신호 봉의 시장 출렁임 지수
    already_tracked: str = ""           # v30.2: 같은 코인·방향 추적 기록이 진행 중이면 그 설명
    opposite_tracked: str = ""          # v32.5: 같은 코인·반대 방향 추적 기록이 진행 중이면 그 설명 (코인당 한 포지션 → 새 추천 건너뜀)


_VOL_DIAG: Dict[str, Dict] = {}   # v29.2: 거래소별 현물 목록 상태(진단 표시용)


def _ticker_qv(t: Optional[Dict]) -> float:
    """24h 거래대금(USDT). v29.2: quoteVolume이 비어 있으면 거래량×가격, 그것도 없으면 거래소 원본 필드로.
    (ccxt 버전·서버 지역에 따라 quoteVolume이 빠져 모든 코인이 0으로 보이던 문제 대비)"""
    if not t:
        return 0.0
    try:
        v = t.get("quoteVolume")
        if v:
            return float(v)
        bv, px = t.get("baseVolume"), t.get("last") or t.get("close")
        if bv and px:
            return float(bv) * float(px)
        info = t.get("info") or {}
        if isinstance(info, dict):
            for k in ("usdtVolume", "quoteVolume", "turnover24h", "volCcy24h", "quoteVol"):
                v = info.get(k)
                if v not in (None, "", "0"):
                    return float(v)
    except Exception:
        pass
    return 0.0


def _volume_list(exchange_id: str, top_n: int) -> List[tuple]:
    """(심볼, 24h 거래대금) 목록, 거래대금 순. 거래 가능 여부가 비어 있으면(None) 가능으로 처리."""
    ex = _get_ex(exchange_id)
    markets = ex.load_markets()
    tickers = ex.fetch_tickers() or {}
    usdt_all = [s_ for s_ in markets if s_.endswith(f"/{QUOTE}") and markets[s_].get("spot") is not False]
    usdt_pairs = [s_ for s_ in usdt_all if markets[s_].get("active") is not False]
    vols = {s_: _ticker_qv(tickers.get(s_)) for s_ in usdt_pairs}

    def qv(sym: str) -> float:
        return vols.get(sym, 0.0)

    ranked = sorted(usdt_pairs, key=qv, reverse=True)
    n_vol = sum(1 for sym in ranked if qv(sym) > 0)
    _VOL_DIAG[exchange_id] = {"pairs": len(usdt_all), "active": len(usdt_pairs), "tickers": len(tickers),
                              "with_vol": n_vol, "btc": f"BTC/{QUOTE}" in usdt_pairs}
    # 거래대금 정보를 주지 않는 거래소면 필터를 건너뜀 (전부 0으로 보여 통째로 빠지는 것 방지)
    if n_vol >= 10:
        ranked = [sym for sym in ranked if qv(sym) >= MIN_QUOTE_VOLUME_USDT]
    return [(sym, float(qv(sym))) for sym in ranked[:top_n]]


def get_top_volume_symbols(exchange_id: str, top_n: Optional[int] = None) -> List[str]:
    return [sym for sym, _ in _volume_list(exchange_id, top_n or TOP_N_BY_VOLUME)]


def relative_strength_vs_btc(coin_df: pd.DataFrame, btc_df: pd.DataFrame,
                              windows: Optional[List[int]] = None) -> float:
    """지수(BTC) 대비 상대강도를 여러 구간(4시간봉 42/84/168개 ≈ 1주·2주·4주)의 평균으로 계산.
    - 예전(약 2·3·8일)처럼 너무 짧으면 '추세가 강한 코인'이 아니라 '단기 과열 코인'을 고르게 됨
      (크립토는 1주 이내 짧은 기간에 오히려 되돌림이 나타난다는 연구들이 있음)
    - 한 구간만 보면 우연한 단기 스파이크에 흔들리기 쉬워서, 여러 구간을 평균내
      '꾸준히 지수보다 강한' 코인을 더 정확히 골라내도록 함
    - 값이 클수록 같은 기간 동안 BTC보다 더 많이 오르고(하락장이면 덜 빠지고) 있다는 뜻
    - 반환값(%): 각 구간별 (코인 수익률 - BTC 수익률)의 단순 평균"""
    windows = windows or RS_WINDOWS
    scores = []
    for w in windows:
        n = min(len(coin_df), len(btc_df), w)
        if n < 2:
            continue
        coin_ret = coin_df["close"].iloc[-1] / coin_df["close"].iloc[-n] - 1
        btc_ret = btc_df["close"].iloc[-1] / btc_df["close"].iloc[-n] - 1
        scores.append((coin_ret - btc_ret) * 100)
    return float(np.mean(scores)) if scores else 0.0


def detect_order_block(df: pd.DataFrame, lookback: int = 30) -> Dict:
    """단순화된 오더블록 탐지:
    - 강한 상승 임펄스 직전의 마지막 음봉 = 불리시 오더블록
    - 강한 하락 임펄스 직전의 마지막 양봉 = 베어리시 오더블록
    엄밀한 스마트머니 컨셉 정의와는 차이가 있는 '실전 근사치'입니다."""
    recent = df.tail(lookback)
    o, c = recent["open"].to_numpy(), recent["close"].to_numpy()
    h, l = recent["high"].to_numpy(), recent["low"].to_numpy()
    avg_body = np.abs(c - o).mean()
    bullish_ob, bearish_ob = None, None
    for i in range(1, len(recent) - 1):
        if (c[i] - o[i]) > 2 * avg_body and c[i - 1] < o[i - 1]:
            bullish_ob = {"low": float(l[i - 1]), "high": float(h[i - 1])}
        if (o[i] - c[i]) > 2 * avg_body and c[i - 1] > o[i - 1]:
            bearish_ob = {"low": float(l[i - 1]), "high": float(h[i - 1])}
    return {"bullish_ob": bullish_ob, "bearish_ob": bearish_ob}


def get_htf_trend(exchange_id: str, symbol: str, htf_timeframe: Optional[str] = None) -> RegimeType:
    """상위 타임프레임(HTF) 추세 필터. 진입 타임프레임(4h) 대비 6배 비율(1d)을 사용.
    ⚠️ 반드시 '마감된 봉'만 사용 — 마지막 봉은 아직 진행 중일 수 있으므로 제외합니다
    (미래참조/look-ahead 오류 방지)."""
    htf_timeframe = htf_timeframe or HTF_TIMEFRAME
    df = fetch_ohlcv(exchange_id, symbol, timeframe=htf_timeframe, limit=HTF_LIMIT)
    closed_df = drop_unclosed(df, htf_timeframe)  # 진행 중인 오늘 일봉 제외
    if closed_df is None or len(closed_df) < 60:
        return "sideways"
    return classify_price_trend(closed_df)


def get_spread_pct(exchange_id: str, symbol: str) -> Optional[float]:
    """호가 스프레드(%) 조회 — 스프레드가 넓으면 그만큼 즉시 손실을 안고 시작하는 셈이라
    슬리피지 리스크가 큰 종목을 걸러내는 데 사용."""
    try:
        ex = _get_ex(exchange_id)
        ticker = ex.fetch_ticker(_route(ex, exchange_id, symbol)[0])
        bid, ask = ticker.get("bid"), ticker.get("ask")
        if not bid or not ask:
            return None
        return (ask - bid) / bid * 100
    except Exception:
        return None


# --------------------------------------------------------------------------
# 4.1 서킷브레이커 (일일/주간 손실 한도) — 실제 체결 결과를 기록해두면
#     이 한도를 넘었을 때 스크립트가 신규 신호를 아예 막아버립니다.
# --------------------------------------------------------------------------

TRADE_LOG_FILE = "trade_results_log.csv"


def log_trade_result(pnl_usdt: float, symbol: str = "", note: str = "") -> None:
    """실제 체결 후 손익을 여기에 직접 기록하세요 (수동). 이 로그가 쌓여야
    서킷브레이커와, 나중에 Kelly 기반 사이징으로 넘어갈 때의 승률/손익비 계산이 가능합니다."""
    entry = pd.DataFrame([{"ts": int(time.time()), "pnl_usdt": pnl_usdt, "symbol": symbol, "note": note}])
    if os.path.exists(TRADE_LOG_FILE):
        log = pd.concat([pd.read_csv(TRADE_LOG_FILE), entry], ignore_index=True)
    else:
        log = entry
    log.to_csv(TRADE_LOG_FILE, index=False)


def circuit_breaker_triggered(risk_cfg: RiskConfig, max_daily_loss_pct: float = 5.0,
                               max_weekly_loss_pct: float = 10.0) -> Optional[str]:
    """오늘/이번 주 실현 손실이 한도를 넘었으면 신규 진입을 전면 차단.
    이유: 손실 중 감정적으로 만회하려는 시도(revenge trading)가 계좌를 가장 크게
    파괴하는 패턴이므로, 규칙 기반으로 강제 중단하는 것이 중요합니다."""
    if not os.path.exists(TRADE_LOG_FILE):
        return None
    log = pd.read_csv(TRADE_LOG_FILE)
    if log.empty:
        return None

    # v30.2: 한국 날짜 기준으로 통일 (예전엔 기록은 UTC, '오늘'은 서버 시계라 PC에선 09시 전 손실이 전날로 잡혔음)
    log["dt"] = pd.to_datetime(log["ts"], unit="s") + pd.Timedelta(hours=9)
    now = utc_now() + pd.Timedelta(hours=9)

    today_pnl = log[log["dt"].dt.date == now.date()]["pnl_usdt"].sum()
    week_pnl = log[log["dt"] >= now - pd.Timedelta(days=7)]["pnl_usdt"].sum()

    today_loss_pct = -today_pnl / risk_cfg.account_balance * 100
    week_loss_pct = -week_pnl / risk_cfg.account_balance * 100

    if today_loss_pct >= max_daily_loss_pct:
        return f"일일 손실 한도 초과 ({today_loss_pct:.1f}% ≥ {max_daily_loss_pct}%) — 오늘 신규 진입 중단"
    if week_loss_pct >= max_weekly_loss_pct:
        return f"주간 손실 한도 초과 ({week_loss_pct:.1f}% ≥ {max_weekly_loss_pct}%) — 이번 주 신규 진입 중단"
    return None


def get_funding_rate(exchange_id: str, symbol: str) -> Optional[float]:
    """USDT-M 무기한 선물(예: 'SOL/USDT:USDT')의 현재 펀딩비 조회.
    (현물 심볼로 조회하면 항상 실패해서 필터가 무력화되므로 반드시 선물 심볼을 사용)
    선물이 없거나 조회 실패 시 None → 이 경우 필터를 건너뜁니다.
    ⚠️ 연환산은 8시간 정산 기준 근사치입니다(코인/거래소에 따라 정산 주기가 다를 수 있음)."""
    try:
        ex = _get_ex(exchange_id)
        ex.load_markets()
        swap_symbol = symbol if ":" in symbol else f"{symbol}:{QUOTE}"
        if swap_symbol not in ex.markets:
            return None
        fr = ex.fetch_funding_rate(swap_symbol)
        return fr.get("fundingRate")
    except Exception:
        return None


def funding_rate_ok(exchange_id: str, symbol: str, bias: str,
                     max_annualized_pct: float = 20.0) -> bool:
    """펀딩비가 과열(쏠림)된 방향으로는 진입하지 않도록 걸러냄.
    - 8시간마다 정산 → 하루 3회 → 연 1095회
    - 롱인데 펀딩비가 크게 플러스(롱 과열, 내가 숏에게 계속 돈을 냄) → 제외
    - 숏인데 펀딩비가 크게 마이너스(숏 과열, 내가 롱에게 계속 돈을 냄) → 제외
    데이터가 없으면(현물 등) 통과시킴 — 무기한 선물이 아니면 해당 없음."""
    rate = get_funding_rate(exchange_id, symbol)
    if rate is None:
        return True
    annualized_pct = rate * 1095 * 100

    if "long" in bias and annualized_pct > max_annualized_pct:
        return False
    if "short" in bias and annualized_pct < -max_annualized_pct:
        return False
    return True


def atr(df: pd.DataFrame, period: int = 14) -> float:
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def calculate_volume_profile(df: pd.DataFrame, num_bins: int = 50, lookback: int = 100) -> Dict:
    """POC(Point of Control)·Value Area 근사 계산.
    - 각 캔들의 거래량을 그 캔들의 고가~저가 구간에 균등 분산시켜 가격대별 거래량을 누적
    - 거래량이 가장 많이 쌓인 구간 = POC (매물대 핵심, '가격 자석' 역할)
    - POC를 중심으로 누적거래량 70%에 도달할 때까지 확장한 상/하단 = Value Area High/Low
      (그 안쪽은 '시장이 공정가로 받아들인 가격대', 바깥쪽은 '거부된 가격대'로 해석)
    ⚠️ 캔들(OHLCV) 데이터 기반 근사치입니다. 틱/오더북 데이터 기반 진짜 볼륨프로파일보다는
    정밀도가 떨어지지만, 실전에서 널리 쓰이는 근사 방식입니다."""
    recent = df.tail(lookback)
    price_min, price_max = recent["low"].min(), recent["high"].max()
    if price_max <= price_min:
        return {"poc": None, "vah": None, "val": None}

    bins = np.linspace(price_min, price_max, num_bins + 1)
    lo_idx = np.maximum(np.searchsorted(bins, recent["low"].to_numpy(), side="right") - 1, 0)
    hi_idx = np.minimum(np.searchsorted(bins, recent["high"].to_numpy(), side="right") - 1, num_bins - 1)
    span = np.maximum(hi_idx - lo_idx + 1, 1)
    per = recent["volume"].to_numpy() / span
    valid = lo_idx <= hi_idx
    diff = np.zeros(num_bins + 1)
    np.add.at(diff, lo_idx[valid], per[valid])
    np.add.at(diff, hi_idx[valid] + 1, -per[valid])
    vol_by_bin = np.cumsum(diff)[:num_bins]

    poc_idx = int(np.argmax(vol_by_bin))
    poc_price = (bins[poc_idx] + bins[poc_idx + 1]) / 2

    total_vol = vol_by_bin.sum()
    target = total_vol * 0.7
    lo, hi = poc_idx, poc_idx
    acc = vol_by_bin[poc_idx]
    while acc < target and (lo > 0 or hi < num_bins - 1):
        expand_lo = vol_by_bin[lo - 1] if lo > 0 else -1
        expand_hi = vol_by_bin[hi + 1] if hi < num_bins - 1 else -1
        if expand_hi >= expand_lo:
            hi = min(hi + 1, num_bins - 1)
            acc += vol_by_bin[hi]
        else:
            lo = max(lo - 1, 0)
            acc += vol_by_bin[lo]

    return {"poc": float(poc_price), "val": float(bins[lo]), "vah": float(bins[hi + 1])}


def detect_liquidity_sweep(df: pd.DataFrame, lookback: int = 20) -> Dict:
    """최근 완결봉이 직전 lookback봉의 스윙 고점/저점을 꼬리(wick)로 살짝 넘었다가
    종가는 다시 그 안으로 들어온 '유동성 스윕(손절 사냥 후 반전)' 패턴 탐지.
    - bullish_sweep: 직전 스윙 저점을 저가로 이탈했다가 종가는 그 위로 복귀 → 매수세 유입(반전 상승 신호)
    - bearish_sweep: 직전 스윙 고점을 고가로 이탈했다가 종가는 그 아래로 복귀 → 매도세 유입(반전 하락 신호)
    ⚠️ 4시간봉 기준입니다. 원래 이 컨셉은 1~15분봉처럼 훨씬 짧은 타임프레임에서 정밀 진입용으로
    쓰이는 경우가 많아, 4시간봉에서는 일반적인 변동성과 뚜렷이 구분되지 않을 수 있습니다.
    여기서는 '있으면 신뢰도를 살짝 높여주는 보조 컨플루언스'로만 쓰고, 진입 조건 자체를 바꾸지
    않습니다 — 이렇게 해야 이 신호가 실제로 도움이 되는지 나중에 백테스트로 따로 검증할 수 있습니다."""
    if len(df) < lookback + 2:
        return {"bullish_sweep": None, "bearish_sweep": None}
    recent = df.tail(lookback + 1)
    prior, last = recent.iloc[:-1], recent.iloc[-1]
    prior_low, prior_high = prior["low"].min(), prior["high"].max()

    bullish_sweep = None
    if last["low"] < prior_low and last["close"] > prior_low:
        bullish_sweep = {"swept_level": float(prior_low), "close": float(last["close"])}

    bearish_sweep = None
    if last["high"] > prior_high and last["close"] < prior_high:
        bearish_sweep = {"swept_level": float(prior_high), "close": float(last["close"])}

    return {"bullish_sweep": bullish_sweep, "bearish_sweep": bearish_sweep}


def near_level(price: float, level: Optional[float], tolerance_atr: float, a: float) -> bool:
    if level is None or a <= 0:
        return False
    return abs(price - level) <= tolerance_atr * a


def compute_sideways_lean(df: pd.DataFrame, htf_trend: RegimeType, lookback: int = 30,
                           weights: tuple = (0.5, 0.3, 0.2)) -> Dict:
    """횡보장에서 다음 방향에 대한 '확률적 기울기'를 계산.
    ⚠️ 이건 확정 예측이 아니라 여러 객관적 근거를 가중평균한 확률적 기울기입니다.
    score는 -1(하락 우세)~+1(상승 우세), |score| < 0.3이면 '중립'으로 판단해 방향을 강제하지 않습니다."""
    recent = df.tail(lookback)
    htf_component = {"uptrend": 1.0, "downtrend": -1.0, "sideways": 0.0}[htf_trend]

    half = len(recent) // 2
    first_half, second_half = recent.iloc[:half], recent.iloc[half:]
    if len(first_half) and len(second_half):
        low_diff = second_half["low"].min() - first_half["low"].min()    # 저점 상승폭
        high_diff = second_half["high"].max() - first_half["high"].max()  # 고점 상승폭
        rng = max(recent["high"].max() - recent["low"].min(), 1e-9)
        structure_component = float(np.clip(((low_diff - high_diff) / 2) / rng, -1, 1))
    else:
        structure_component = 0.0

    up_bars = recent[recent["close"] > recent["open"]]
    down_bars = recent[recent["close"] < recent["open"]]
    if len(up_bars) and len(down_bars):
        up_vol, down_vol = up_bars["volume"].mean(), down_bars["volume"].mean()
        accum_component = float(np.clip((up_vol - down_vol) / max(up_vol + down_vol, 1e-9), -1, 1))
    else:
        accum_component = 0.0

    w_htf, w_struct, w_accum = weights
    score = w_htf * htf_component + w_struct * structure_component + w_accum * accum_component
    if score > 0.3:
        label = "상승쪽 우세"
    elif score < -0.3:
        label = "하락쪽 우세"
    else:
        label = "중립(방향성 불명확)"

    return {"score": score, "label": label, "htf": htf_component,
            "structure": structure_component, "accumulation": accum_component}


def find_recent_impulse(df: pd.DataFrame, direction: str, lookback: int = 30,
                         vol_multiple: float = 2.0, body_atr: float = 1.0) -> Optional[float]:
    """최근 lookback봉 안에 '가격을 실제로 밀어낸' 임펄스 봉이 있었는지 확인.
    - 방향이 맞는 봉(상승이면 양봉, 하락이면 음봉)
    - 거래량이 그 봉 직전 20봉 평균의 vol_multiple배(기본 2배) 이상
    - 몸통이 그 시점 ATR의 body_atr배(기본 1배) 이상 — 거래량만 많고 가격은 안 움직인 봉 제외
    있으면 그중 가장 큰 거래량 배수를, 없으면 None을 반환.
    (예전 기준 '거래량 1.3배, 몸통 조건 없음'은 무작위 데이터에서도 98%가 통과해 필터 역할을 못 했음)"""
    if len(df) < 40:
        return None
    w = df.tail(lookback + 35)
    vol_avg = w["volume"].rolling(20).mean().shift(1)
    tr = pd.concat([w["high"] - w["low"], (w["high"] - w["close"].shift()).abs(),
                    (w["low"] - w["close"].shift()).abs()], axis=1).max(axis=1)
    atr_s = tr.rolling(14).mean().shift(1)
    body = w["close"] - w["open"]
    dir_ok = body > 0 if direction == "up" else body < 0
    rel = w["volume"] / vol_avg
    ok = (dir_ok & (rel >= vol_multiple) & (body.abs() >= body_atr * atr_s)).tail(lookback)
    matched = rel.tail(lookback)[ok]
    return float(matched.max()) if len(matched) else None


def detect_box(df: pd.DataFrame, a: float, lookback: int = 60, min_height_atr: float = 2.0,
               max_height_atr: float = 10.0, touch_tol_atr: float = 0.5, min_touches: int = 2,
               min_gap: int = 3) -> Dict:
    """박스권 판정. 예전에는 '최근 20봉 고점·저점'을 그냥 박스로 봐서 어느 차트에나 박스가 있었음.
    이제는 마지막 완성봉을 뺀 최근 lookback봉(기본 60봉 ≈ 10일)에서
    - 박스 높이가 ATR의 2~10배 사이이고
    - 위·아래 경계를 각각 서로 떨어진 시점에 2번 이상 터치했을 때만 유효한 박스로 인정."""
    if len(df) < lookback + 2 or not a or a <= 0:
        return {"valid": False}
    prior = df.iloc[-lookback - 1:-1]
    hi, lo = float(prior["high"].max()), float(prior["low"].min())
    height = hi - lo

    def touches(mask) -> int:
        count, last = 0, -10 ** 6
        for i, m in enumerate(mask):
            if m:
                if i - last > min_gap:
                    count += 1
                last = i
        return count

    th = touches((prior["high"] >= hi - touch_tol_atr * a).tolist())
    tl = touches((prior["low"] <= lo + touch_tol_atr * a).tolist())
    valid = (min_height_atr * a <= height <= max_height_atr * a) and th >= min_touches and tl >= min_touches
    return {"valid": bool(valid), "high": hi, "low": lo, "height": height, "touch_high": th, "touch_low": tl}


_REJECTS: Dict[str, int] = {}
REJECT_LABELS = {
    "not_perp": "Bitget 선물 미지원", "data_short": "데이터 부족", "error": "조회/분석 오류",
    "no_impulse": "최근 임펄스 없음", "weak_rs": "상대강도 방향 불일치", "no_room": "목표까지 여유 없음", "rr_too_far": "목표가 너무 멂(손익비 5 이상 · 넓게 출렁인 박스)", "perp_untradable": "Bitget 선물 거래 불가(시세 없음)", "macro_against": "거시 흐름과 반대 방향", "no_st_flip": "추적선 전환 없음",
    "invalid_price": "이미 손절선을 넘음(무효)", "target_reached": "이미 목표가 도달(놓침)",
    "no_box": "유효한 박스 아님", "lean_against": "횡보 기울기와 반대", "mid_box": "박스 중간(관망)",
    "htf_against": "일봉 추세와 반대", "wide_spread": "스프레드 넓음", "funding_hot": "펀딩비 과열",
    "low_rr": "손익비 부족", "family_off": "꺼둔 신호 유형", "no_donchian": "신고점 돌파 없음",
    "tag_filter": "필수 표시 없음(연구실 설정)", "donchian_filter": "신고점 돌파 조건 미충족(연구실 설정)",
    "phase_gate": "BTC 대상승 아님(빗각 지지는 대상승일 때만)",
    "wick_gate": "BTC 200일선 아래 또는 코인 EMA20 아래(아래꼬리 연속 조건)",
}


_REJ_SYM: Dict[str, List[str]] = {}   # v29.5: 코인별 제외 사유 (PC·폰 비교용 '코인 찾기')
_CUR_SYM = {"sym": None}


def _rej(reason: str) -> None:
    _REJECTS[reason] = _REJECTS.get(reason, 0) + 1
    if _CUR_SYM["sym"]:
        _REJ_SYM.setdefault(_CUR_SYM["sym"], []).append(reason)


def _order_targets(direction: str, tp1: float, tp2: float, a: float):
    """목표2가 목표1보다 불리해지지 않도록 순서 보장."""
    if direction == "long":
        return tp1, max(tp2, tp1 + 0.5 * a)
    return tp1, min(tp2, tp1 - 0.5 * a)


def _signal_time_problem(direction: str, price: float, sl: float, tp1: float) -> Optional[str]:
    """신호가 뜨는 시점에 이미 무효(손절선 통과)이거나 이미 목표에 도달한 셋업은 추천하지 않음."""
    if direction == "long":
        if price <= sl:
            return "invalid_price"
        if price >= tp1:
            return "target_reached"
    else:
        if price >= sl:
            return "invalid_price"
        if price <= tp1:
            return "target_reached"
    return None


def breakout_tags(df: pd.DataFrame, direction: str, htf_trend: RegimeType,
                  rs_alt: Optional[float], rs_btc: Optional[float]) -> List[str]:
    """돌파 신호의 보조 표시. 진입 조건은 바꾸지 않고, 전략 연구실에서 '붙은 신호 vs 안 붙은 신호'를 비교하는 용도.
    - squeeze: 돌파 직전 20봉 평균 변동폭이 100봉 평균의 80% 이하 (변동성이 눌렸다가 터짐)
    - htf_align: 일봉(상위 봉) 추세와 같은 방향
    - alt_strong / btc_strong: 알트 지수·BTC 대비 상대강도가 진입 방향과 일치
    - strong_close: 돌파봉이 봉 범위 상단 25%(숏은 하단 25%)에서 마감"""
    tags: List[str] = []
    long_side = direction == "long"
    tr = pd.concat([df["high"] - df["low"], (df["high"] - df["close"].shift()).abs(),
                    (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
    if len(tr) >= 102:
        a20, a100 = tr.iloc[-21:-1].mean(), tr.iloc[-101:-1].mean()
        if a100 > 0 and a20 / a100 <= 0.8:
            tags.append("squeeze")
    if (long_side and htf_trend == "uptrend") or (not long_side and htf_trend == "downtrend"):
        tags.append("htf_align")
    if rs_alt is not None and ((long_side and rs_alt > 0) or (not long_side and rs_alt < 0)):
        tags.append("alt_strong")
    if rs_btc is not None and ((long_side and rs_btc > 0) or (not long_side and rs_btc < 0)):
        tags.append("btc_strong")
    last = df.iloc[-1]
    rng = last["high"] - last["low"]
    if rng > 0:
        pos = (last["close"] - last["low"]) / rng
        if (long_side and pos >= 0.75) or (not long_side and pos <= 0.25):
            tags.append("strong_close")
    flow = obv_flow(df)
    if flow is not None and ((long_side and flow >= 0.10) or (not long_side and flow <= -0.10)):
        tags.append("obv_accum")
    return tags


def obv_flow(df: pd.DataFrame, n: int = 30) -> Optional[float]:
    """OBV(거래량 흐름) 기울기를 −1~+1로: 직전 n봉(마지막 봉 제외) 동안 (상승봉 거래량 − 하락봉 거래량) ÷ 전체 거래량.
    +0.1 이상이면 매집(사는 쪽 거래량 우위), −0.1 이하면 분산."""
    if len(df) < n + 2:
        return None
    w = df.iloc[-n - 1:-1]
    sign = np.sign(w["close"].diff().fillna(0).to_numpy())
    vol = w["volume"].to_numpy()
    tot = vol.sum()
    return float((sign * vol).sum() / tot) if tot > 0 else None


def build_alt_index(coin_dfs: List[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """직접 만든 알트 지수: 코인들의 봉별 수익률을 같은 비중으로 평균낸 뒤 누적 (BTC·ETH·스테이블코인은
    넣지 않음). TOTAL3는 스테이블코인이 섞여 움직임이 무뎌지고 과거 데이터를 무료로 받을 수 없어서 대신 사용."""
    rets = [d.set_index("ts")["close"].pct_change() for d in coin_dfs if d is not None and len(d) > 10]
    if len(rets) < 3:
        return None
    m = pd.concat(rets, axis=1).sort_index().mean(axis=1, skipna=True).fillna(0.0).clip(-0.5, 0.5)
    idx = (1 + m).cumprod() * 100
    return pd.DataFrame({"ts": idx.index, "close": idx.to_numpy()}).reset_index(drop=True)


def align_to(df: pd.DataFrame, bench: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """벤치마크(알트 지수 등)를 코인 캔들 시각에 맞춤 — 상대강도를 같은 봉끼리 비교하기 위해."""
    if bench is None:
        return None
    out = df[["ts"]].merge(bench, on="ts", how="left")
    out["close"] = out["close"].ffill()
    return out if out["close"].notna().sum() > 10 else None


def _nearest_swing(arr: np.ndarray, lo_i: int, hi_i: int, above: Optional[float], below: Optional[float],
                   k: int = 3) -> Optional[float]:
    """구간 [lo_i, hi_i)의 스윙 고점(above 지정 시)·스윙 저점(below 지정 시) 중 현재가에 가장 가까운 것.
    스윙 = 좌우 k봉보다 높은(낮은) 봉."""
    best = None
    for j in range(max(lo_i + k, k), min(hi_i, len(arr) - k)):
        win = arr[j - k:j + k + 1]
        if above is not None and arr[j] == win.max() and arr[j] > above:
            best = arr[j] if best is None else min(best, arr[j])
        if below is not None and arr[j] == win.min() and arr[j] < below:
            best = arr[j] if best is None else max(best, arr[j])
    return float(best) if best is not None else None


def _donchian_levels(h: np.ndarray, l: np.ndarray, c: np.ndarray, a: float, i: int, n: int,
                     direction: str, exits: str):
    """신고점 돌파의 손절·목표1·목표2.
    - atr: 손절 진입가−2ATR, 목표1 +3ATR (손익비 항상 1.5)
    - structure: 손절은 돌파한 레벨 아래 0.5ATR(최소 1ATR), 목표1은 90일 안에서 '가장 가까운 위쪽 스윙 고점'
      (숏은 아래쪽 스윙 저점). 그런 저항이 없으면(신고가 영역) 손절폭의 2배. 목표1은 손절폭의 4배를 넘지 않게 제한
      → 코인별 차트에 따라 손익비가 달라지고, 1.5 미만(바로 위에 저항이 있어 여유 없음)이면 None"""
    e = float(c[i])
    if exits == "atr":
        return (e - 2 * a, e + 3 * a, e + 6 * a) if direction == "long" else (e + 2 * a, e - 3 * a, e - 6 * a)
    lookback = int(90 * pd.Timedelta("1D") / pd.Timedelta(TIMEFRAME))
    lo_i = max(0, i - lookback)
    if direction == "long":
        level = float(np.max(h[i - n:i]))
        sl = min(level - 0.5 * a, e - 1.0 * a)
        risk = e - sl
        res = _nearest_swing(h, lo_i, i - n, above=e + 0.5 * a, below=None)  # 지금 돌파한 구간 이전의 저항만
        tp1 = min(res, e + 4 * risk) if res is not None else e + 2 * risk
        rr = (tp1 - e) / risk
        tp2 = e + max(3 * risk, (tp1 - e) + 1.0 * a)
    else:
        level = float(np.min(l[i - n:i]))
        sl = max(level + 0.5 * a, e + 1.0 * a)
        risk = sl - e
        sup = _nearest_swing(l, lo_i, i - n, above=None, below=e - 0.5 * a)
        tp1 = max(sup, e - 4 * risk) if sup is not None else e - 2 * risk
        rr = (e - tp1) / risk
        tp2 = e - max(3 * risk, (e - tp1) + 1.0 * a)
    return (sl, tp1, tp2) if rr >= 1.5 else None


def _daily_flags(daily: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """일봉 기준 필터: 주봉 추세(100일선·12주 수익률), 포트폴리오 보유 조건(50일선·30일 수익률)."""
    if daily is None or len(daily) < 110:
        return None
    d = daily.set_index("ts")["close"]
    e100, e50 = ema(d, 100), ema(d, 50)
    r84, r30 = d / d.shift(84) - 1, d / d.shift(30) - 1
    return pd.DataFrame({"weekly_long": (d > e100) & (r84 > 0), "weekly_short": (d < e100) & (r84 < 0),
                         "tsm_on": (d > e50) & (r30 > 0)}, index=d.index)


def _flags_at(flags: Optional[pd.DataFrame], bar_ts) -> Optional[pd.Series]:
    """4시간봉(bar_ts 시작)이 끝난 시점까지 '마감된' 일봉의 필터 값 (미래 참조 방지)."""
    if flags is None:
        return None
    cutoff = pd.Timestamp(bar_ts) + pd.Timedelta(TIMEFRAME)
    closed = flags[flags.index + pd.Timedelta("1D") <= cutoff]
    return closed.iloc[-1] if len(closed) else None


def _donchian_filter_ok(variant: str, direction: str, vol_ok: bool, fl: Optional[pd.Series]) -> bool:
    if variant == "volume":
        return vol_ok
    if variant == "weekly":
        return fl is not None and bool(fl["weekly_long" if direction == "long" else "weekly_short"])
    if variant == "tsm":
        return direction == "long" and fl is not None and bool(fl["tsm_on"])
    return True


def _breakout_volume_ok(df: pd.DataFrame, i: int, direction: str) -> bool:
    """돌파봉 거래량 ≥ 직전 20봉 평균의 2배, 그리고 봉 상단(숏은 하단) 25% 안에서 마감."""
    if i < 21:
        return False
    v = df["volume"].to_numpy()
    avg = v[i - 20:i].mean()
    h, l, c = df["high"].iloc[i], df["low"].iloc[i], df["close"].iloc[i]
    pos = (c - l) / (h - l) if h > l else 0.5
    return bool(avg > 0 and v[i] >= 2 * avg and (pos >= 0.75 if direction == "long" else pos <= 0.25))


def supertrend(df: pd.DataFrame, period: int = ST_PERIOD, mult: float = ST_MULT):
    """ATR 추적선(슈퍼트렌드). 완성봉만으로 계산해 리페인팅 없음. 반환: (추적선 값, 방향 +1/−1) 배열."""
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    n = len(c)
    tr = np.maximum(h - l, np.maximum(np.abs(h - np.roll(c, 1)), np.abs(l - np.roll(c, 1))))
    tr[0] = h[0] - l[0]
    atr_ = pd.Series(tr).ewm(alpha=1 / period, adjust=False).mean().to_numpy()
    hl2 = (h + l) / 2
    ub, lb = hl2 + mult * atr_, hl2 - mult * atr_
    fub, flb = ub.copy(), lb.copy()
    d = np.ones(n, dtype=int)
    for i in range(1, n):
        fub[i] = ub[i] if (ub[i] < fub[i - 1] or c[i - 1] > fub[i - 1]) else fub[i - 1]
        flb[i] = lb[i] if (lb[i] > flb[i - 1] or c[i - 1] < flb[i - 1]) else flb[i - 1]
        if d[i - 1] == 1 and c[i] < flb[i]:
            d[i] = -1
        elif d[i - 1] == -1 and c[i] > fub[i]:
            d[i] = 1
        else:
            d[i] = d[i - 1]
    return np.where(d == 1, flb, fub), d


def _st_levels(e: float, line: float, a: float, direction: str):
    """추적선 전환 진입의 손절(추적선, 최소 1ATR)·목표1(손절폭 2배)·목표2(4배)."""
    if direction == "long":
        sl = min(line, e - 1.0 * a)
        r = e - sl
        return sl, e + 2 * r, e + 4 * r
    sl = max(line, e + 1.0 * a)
    r = sl - e
    return sl, e - 2 * r, e - 4 * r


def build_supertrend_setup(symbol: str, exchange_id: str, df: pd.DataFrame, btc_df: pd.DataFrame,
                           current_price: Optional[float] = None, alt_df: Optional[pd.DataFrame] = None,
                           htf_trend: RegimeType = "sideways") -> Optional[CoinSetup]:
    """마지막 완성봉에서 ATR 추적선 방향이 바뀌면 그 방향으로 시장가 진입 (연구실 통과 시에만 추천)."""
    if df is None or len(df) < 60:
        _rej("data_short")
        return None
    line, d = supertrend(df)
    if d[-1] == d[-2]:
        _rej("no_st_flip")
        return None
    a = atr(df)
    if not a or np.isnan(a) or a <= 0:
        _rej("data_short")
        return None
    direction = "long" if d[-1] == 1 else "short"
    last = float(df["close"].iloc[-1])
    price = float(current_price) if current_price is not None else last
    sl, tp1, tp2 = _st_levels(last, float(line[-1]), a, direction)
    problem = _signal_time_problem(direction, price, sl, tp1)
    if problem:
        _rej(problem)
        return None
    is_chase = price > last + CHASE_ATR * a if direction == "long" else price < last - CHASE_ATR * a
    rs = relative_strength_vs_btc(df, btc_df)
    rs_alt = relative_strength_vs_btc(df, alt_df) if alt_df is not None else None
    note = (f"ATR 추적선이 {'상승' if direction == 'long' else '하락'}으로 전환 → "
            f"{'⏳ 이미 많이 움직임(추격 주의)' if is_chase else '✅ 시장가 진입 가능'}")
    return CoinSetup(symbol, exchange_id, "st_long" if direction == "long" else "st_short", note, last, price,
                     tp1, tp2, sl, 2.0, is_chase, False, rs, None, atr=a,
                     live_status="chase" if is_chase else "ready", rs_alt=rs_alt,
                     tags=breakout_tags(df, direction, htf_trend, rs_alt, rs))


def build_donchian_setup(symbol: str, exchange_id: str, df: pd.DataFrame, btc_df: pd.DataFrame,
                         current_price: Optional[float] = None, alt_df: Optional[pd.DataFrame] = None,
                         htf_trend: RegimeType = "sideways") -> Optional[CoinSetup]:
    """신고점 돌파(돈치안): 마지막 완성봉 종가가 직전 N봉(약 20일) 최고가를 넘으면 롱, 최저가를 깨면 숏.
    시장가 진입 기준이며 손절은 진입가에서 2 ATR, 목표1은 3 ATR, 이후 추적손절(전략 연구실에서 검증된 경우에만 추천)."""
    n = DONCHIAN_N.get(TIMEFRAME, 120)
    if df is None or len(df) < n + 20:
        _rej("data_short")
        return None
    a = atr(df)
    if not a or np.isnan(a) or a <= 0:
        _rej("data_short")
        return None
    last = float(df["close"].iloc[-1])
    prior_hi = float(df["high"].iloc[-n - 1:-1].max())
    prior_lo = float(df["low"].iloc[-n - 1:-1].min())
    price = float(current_price) if current_price is not None else last
    if last > prior_hi:
        direction, bias = "long", "donchian_long"
    elif last < prior_lo:
        direction, bias = "short", "donchian_short"
    else:
        _rej("no_donchian")
        return None
    variant = DONCHIAN_VARIANT
    i_last = len(df) - 1
    if variant in ("weekly", "tsm"):  # 일봉 필터는 돌파가 났을 때만 조회 (API 절약)
        daily = drop_unclosed(fetch_ohlcv(exchange_id, symbol, "1d", 200), "1d") if exchange_id != "backtest" else None
        if not _donchian_filter_ok(variant, direction, False, _flags_at(_daily_flags(daily), df["ts"].iloc[-1])):
            _rej("donchian_filter")
            return None
    elif variant == "volume" and not _breakout_volume_ok(df, i_last, direction):
        _rej("donchian_filter")
        return None
    lv = _donchian_levels(df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(), a, i_last, n,
                          direction, "structure" if variant == "structure" else "atr")
    if lv is None:
        _rej("no_room")
        return None
    sl, tp1, tp2 = lv
    problem = _signal_time_problem(direction, price, sl, tp1)
    if problem:
        _rej(problem)
        return None
    is_chase = price > last + CHASE_ATR * a if direction == "long" else price < last - CHASE_ATR * a
    rs = relative_strength_vs_btc(df, btc_df)
    rs_alt = relative_strength_vs_btc(df, alt_df) if alt_df is not None else None
    days = n * pd.Timedelta(TIMEFRAME) / pd.Timedelta("1D")
    note = (f"직전 {n}봉(약 {days:.0f}일) {'최고가 돌파' if direction == 'long' else '최저가 이탈'} → "
            f"{'⏳ 이미 많이 움직임(추격 주의)' if is_chase else '✅ 시장가 진입 가능'}")
    rr_ = (tp1 - last) / (last - sl) if direction == "long" else (last - tp1) / (sl - last)
    if variant != "atr":
        note += f" · {DONCHIAN_VARIANT_LABEL.get(variant, variant)}"
    return CoinSetup(symbol, exchange_id, bias, note, last, price, tp1, tp2, sl, rr_, is_chase,
                     False, rs, None, atr=a, live_status="chase" if is_chase else "ready", rs_alt=rs_alt,
                     tags=breakout_tags(df, direction, htf_trend, rs_alt, rs))


# ==========================================================================
# v27 청산 규칙 도우미 + 새 매매법 2가지 (돌파 후 되돌림 · 위치 다이버전스)
# 24개월(2024.10~2026.10)·83개 코인에서 약 196만 조합을 탐색하고, 고르는 데 안 쓴 기간·코인으로 다시 확인한 규칙.
# 신호는 '그 봉 마감까지의 데이터'만 사용 (데이터를 중간에서 잘라도 그 이전 신호가 같은지 검사함)
# ==========================================================================
def set_exit_rule(x: "CoinSetup", fam: str) -> None:
    r = EXIT_RULES.get(fam)
    if r:
        x.exit_frac, x.exit_trail, x.max_hold = r["frac"], r["trail"], r["hold"]


def exit_of(x) -> tuple:
    """(목표1 익절 비율, 추적 손절 ATR 배수, 최대 보유 봉 수) — 값이 없으면 예전 규칙."""
    get = (lambda k: x.get(k)) if isinstance(x, dict) else (lambda k: getattr(x, k, None))
    frac = get("exit_frac") if get("exit_frac") is not None else get("frac")
    trail = get("exit_trail") if get("exit_trail") is not None else get("trail")
    hold = get("max_hold") if get("max_hold") is not None else get("hold")
    return (PARTIAL_TP_FRACTION if frac is None else float(frac), TRAIL_ATR if trail is None else float(trail),
            60 if hold is None else int(hold))


def v27_levels(fam: str, direction: str, entry: float, ref: float, box_h: Optional[float], a: float) -> tuple:
    """진입가 기준 손절·목표1. 손절 = 기준선 너머 stop_atr ATR(진입가에서 최소 1 ATR),
    목표1 = 기준선 + 박스 높이 × tp_box 또는 진입가 + 손절폭 × tp_R."""
    xr = EXIT_RULES[fam]
    long_side = direction == "long"
    if long_side:
        sl = min(ref - xr["stop_atr"] * a, entry - MIN_STOP_ATR * a)
    else:
        sl = max(ref + xr["stop_atr"] * a, entry + MIN_STOP_ATR * a)
    if "tp_box" in xr:
        tp1 = ref + xr["tp_box"] * box_h if long_side else ref - xr["tp_box"] * box_h
    else:
        sd = abs(entry - sl)
        tp1 = entry + xr["tp_R"] * sd if long_side else entry - xr["tp_R"] * sd
    return float(sl), float(tp1)


def _atr_array(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> np.ndarray:
    pc = np.r_[np.nan, c[:-1]]
    tr = np.nanmax(np.vstack([h - l, np.abs(h - pc), np.abs(l - pc)]), axis=0)
    return pd.Series(tr).rolling(period).mean().to_numpy()


def _rsi_array(c: np.ndarray, n: int = 14) -> np.ndarray:
    s = pd.Series(c); d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean(); dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return (100 - 100 / (1 + up / dn.replace(0, np.nan))).fillna(50).to_numpy()


def _box_arrays(h: np.ndarray, l: np.ndarray, a: np.ndarray, L: int, tol: float, min_gap: int = 3):
    """봉마다 detect_box와 같은 박스(직전 L봉 고가·저가, 경계 터치 횟수 — 간격 min_gap봉 초과)를 한 번에 계산."""
    from numpy.lib.stride_tricks import sliding_window_view as swv
    n = len(h)
    hi = np.full(n, np.nan); lo = np.full(n, np.nan); th = np.zeros(n, int); tl = np.zeros(n, int)
    if n <= L + 1:
        return hi, lo, th, tl
    Wh = swv(h, L)[:-1]; Wl = swv(l, L)[:-1]
    hiw, low = Wh.max(1), Wl.min(1); ai = a[L:]
    with np.errstate(invalid="ignore"):
        mh = Wh >= (hiw - tol * ai)[:, None]; ml = Wl <= (low + tol * ai)[:, None]

    def clusters(m):
        start = m.copy()
        for g in range(1, min_gap + 1):
            start[:, g:] &= ~m[:, :-g]
        return start.sum(1)
    hi[L:] = hiw; lo[L:] = low; th[L:] = clusters(mh); tl[L:] = clusters(ml)
    return hi, lo, th, tl


RETEST_PARENT = {"lookback": 90, "tol": 0.8, "touches": 2, "hmin": 2.0, "hmax": 8.0, "vol": 2.0, "ext": 1.0}
RETEST_WINDOW, RETEST_TOUCH_ATR, RETEST_INVALID_ATR = 3, 0.3, 0.5   # v29: 경계 0.3 ATR 안까지 오면 닿은 것으로
RETEST_TRIG = "rev"     # v29: 경계 위 양봉 + 직전 봉 고가 위 마감(롱) — 'bull'(양봉만)은 2021-23에서 약했음
DIV_K, DIV_GAP, DIV_FIRST_RSI, DIV_LEVEL_BARS = 2, (5, 60), 40.0, 60


def retest_signals(df: pd.DataFrame, reg: Optional[np.ndarray] = None) -> Dict[int, Dict]:
    """돌파 후 되돌림(지지·저항 전환). 반환 {진입 봉: 신호}.
    1) 품질 박스 돌파: 직전 90봉 박스(경계 터치 허용 0.8 ATR, 위·아래 각 2번 이상, 높이 2~8 ATR), 횡보 국면,
       거래량 20봉 평균의 2배 이상, 경계 밖 1 ATR 이내 마감
    2) 그 뒤 3봉 안에 가격이 경계선 0.3 ATR 안까지 되돌아오고(롱: 저가 ≤ 경계 + 0.3 ATR) 경계 위 양봉이면서
       직전 봉 고가 위로 마감 → 그 봉 종가에 진입
       (그 사이 경계 안쪽으로 0.5 ATR 넘게 마감하면 무효)
    기준선(손절 기준) = 돌파 뒤 되돌림 저점(롱)/고점(숏), 목표 높이 = 박스 높이"""
    df = df.reset_index(drop=True)
    o, h, l, c, v = (df[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume"))
    n = len(c)
    if n < RETEST_PARENT["lookback"] + 30:
        return {}
    a = _atr_array(h, l, c)
    reg = trend_series(df) if reg is None else reg
    P = RETEST_PARENT
    hi, lo, th, tl = _box_arrays(h, l, a, P["lookback"], P["tol"])
    va = pd.Series(v).shift(1).rolling(20).mean().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        rv = np.where(va > 0, v / va, 1.0)
        hg = hi - lo
        ok = (~np.isnan(hi) & (hg >= P["hmin"] * a) & (hg <= P["hmax"] * a) & (th >= P["touches"]) & (tl >= P["touches"])
              & (rv >= P["vol"]) & (np.asarray(reg) == "sideways"))
        up = ok & (c > hi) & (c - hi <= P["ext"] * a)
        dn = ok & (c < lo) & (lo - c <= P["ext"] * a)
    out: Dict[int, Dict] = {}
    for b in np.flatnonzero(up | dn):
        dr = 1 if up[b] else -1
        E_ = hi[b] if dr > 0 else lo[b]
        touched, ext = False, np.inf
        for t in range(b + 1, min(n, b + RETEST_WINDOW + 1)):
            if dr > 0:
                if c[t] < E_ - RETEST_INVALID_ATR * a[b]:
                    break
                ext = min(ext, l[t]); touched |= l[t] <= E_ + RETEST_TOUCH_ATR * a[b]
                fire = touched and c[t] > E_ and c[t] > o[t] and (RETEST_TRIG == "bull" or c[t] > h[t - 1])
            else:
                if c[t] > E_ + RETEST_INVALID_ATR * a[b]:
                    break
                ext = min(ext, -h[t]); touched |= h[t] >= E_ - RETEST_TOUCH_ATR * a[b]
                fire = touched and c[t] < E_ and c[t] < o[t] and (RETEST_TRIG == "bull" or c[t] < l[t - 1])
            if fire:
                if t not in out:
                    out[t] = {"direction": "long" if dr > 0 else "short", "edge": float(E_),
                              "ref": float(ext if dr > 0 else -ext), "box_h": float(hg[b]), "atr": float(a[t]),
                              "parent": int(b), "box_hi": float(hi[b]), "box_lo": float(lo[b])}
                break
    return out


PB_RSI_DIP, PB_VOL_MIN, PB_REF_BARS = 40.0, 1.2, 10


def pullback_signals(df: pd.DataFrame) -> Dict[int, Dict]:
    """v29 추세 눌림목·반등 숏. 반환 {진입 봉: 신호}.
    롱: 50EMA > 200EMA 이고 종가 > 200EMA(상승 추세) · 직전 3봉 중 RSI(14) 최저 < 40(눌림) ·
        종가가 20EMA를 아래에서 위로 넘어 마감 · 거래량 20봉 평균의 1.2배 이상 → 그 봉 종가에 진입
    숏: 반대(하락 추세에서 RSI 60 넘게 반등했다가 20EMA 아래로 다시 마감). 기준선(손절) = 최근 10봉 최저가/최고가"""
    df = df.reset_index(drop=True)
    h, l, c, v = (df[k].to_numpy(float) for k in ("high", "low", "close", "volume"))
    n = len(c)
    if n < 210:
        return {}
    a = _atr_array(h, l, c)
    cs = pd.Series(c)
    e20, e50, e200 = (cs.ewm(span=k_, adjust=False).mean().to_numpy() for k_ in (20, 50, 200))
    r = _rsi_array(c, 14)
    va = pd.Series(v).shift(1).rolling(20).mean().to_numpy()
    rmin3 = pd.Series(r).shift(1).rolling(3).min().to_numpy(); rmax3 = pd.Series(r).shift(1).rolling(3).max().to_numpy()
    lo10 = pd.Series(l).rolling(PB_REF_BARS).min().to_numpy(); hi10 = pd.Series(h).rolling(PB_REF_BARS).max().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        rv = np.where(va > 0, v / va, 1.0)
        up = ((e50 > e200) & (c > e200) & (rmin3 < PB_RSI_DIP) & (c > e20) & np.r_[False, c[:-1] <= e20[:-1]]
              & (rv >= PB_VOL_MIN))
        dn = ((e50 < e200) & (c < e200) & (rmax3 > 100 - PB_RSI_DIP) & (c < e20) & np.r_[False, c[:-1] >= e20[:-1]]
              & (rv >= PB_VOL_MIN))
    out: Dict[int, Dict] = {}
    for i in np.flatnonzero(up | dn):
        lg = bool(up[i])
        out[int(i)] = {"direction": "long" if lg else "short", "ref": float(lo10[i] if lg else hi10[i]), "atr": float(a[i]),
                       "rsi_ext": float(rmin3[i] if lg else rmax3[i]), "rel_vol": float(rv[i])}
    return out


def divergence_signals(df: pd.DataFrame) -> Dict[int, Dict]:
    """위치 다이버전스. 반환 {진입 봉: 신호}.
    롱: 스윙 저점(좌우 2봉) 두 개(간격 5~60봉)에서 두 번째 저점이 더 낮은데 RSI(14)는 2 이상 높고,
        첫 저점의 RSI < 40, 두 번째 저점이 최근 60봉 최저(이전 저점들을 쓸어냄)
        → 저점 확정 봉(저점 + 2봉) 마감에 진입, 단 종가가 200EMA 위(상승 추세 속 눌림)
    숏: 반대 (200EMA 아래에서 고점 다이버전스). 기준선(손절) = 두 번째 저점/고점"""
    df = df.reset_index(drop=True)
    h, l, c = (df[k].to_numpy(float) for k in ("high", "low", "close"))
    n = len(c)
    if n < 120:
        return {}
    a = _atr_array(h, l, c)
    r = _rsi_array(c, 14)
    e200 = ema(df["close"], 200).to_numpy()
    k, (gmin, gmax) = DIV_K, DIV_GAP
    out: Dict[int, Dict] = {}
    for dr, L_, C_, R_, E_ in ((1, l, c, r, e200), (-1, -h, -c, 100 - r, -e200)):
        piv = np.flatnonzero(L_ == pd.Series(L_).rolling(2 * k + 1, center=True).min().to_numpy())
        for q, j in enumerate(piv):
            cand = [p for p in piv[max(0, q - 30):q] if gmin <= j - p <= gmax]
            if not cand:
                continue
            j0 = cand[-1]
            if not (L_[j] < L_[j0] and R_[j] > R_[j0] + 2 and R_[j0] < DIV_FIRST_RSI):
                continue
            if not L_[j] <= L_[max(0, j - DIV_LEVEL_BARS):j + 1].min():
                continue
            i = int(j + k)
            if i >= n or not C_[i] > E_[i] or i in out:
                continue
            out[i] = {"direction": "long" if dr > 0 else "short", "ref": float(dr * L_[j]), "atr": float(a[i]),
                      "pivot": int(j), "pivot0": int(j0), "rsi0": float(r[j0]), "rsi1": float(r[j])}
    return out


def _v27_setup(fam: str, bias: str, s: Dict, symbol: str, exchange_id: str, df: pd.DataFrame, btc_df: pd.DataFrame,
               price: float, alt_df: Optional[pd.DataFrame], note: str) -> Optional["CoinSetup"]:
    a = s["atr"]
    if not a or np.isnan(a) or a <= 0:
        _rej("data_short")
        return None
    direction = s["direction"]
    sl, tp1 = v27_levels(fam, direction, price, s["ref"], s.get("box_h"), a)
    problem = _signal_time_problem(direction, price, sl, tp1)
    if problem:
        _rej(problem)
        return None
    long_side = direction == "long"
    risk = (price - sl) if long_side else (sl - price)
    rr = ((tp1 - price) if long_side else (price - tp1)) / risk if risk > 0 else 0.0
    rs = relative_strength_vs_btc(df, btc_df)
    rs_alt = relative_strength_vs_btc(df, alt_df) if alt_df is not None else None
    tp2 = tp1 + (tp1 - price) if long_side else tp1 - (price - tp1)
    x = CoinSetup(symbol, exchange_id, f"{bias}_{'long' if long_side else 'short'}",
                  f"{note} · 봉 마감 직후 시장가 즉시 진입", price, price, tp1, tp2, sl, rr, False, False, rs, None,
                  atr=a, live_status="ready", rs_alt=rs_alt)
    x.level, x.box_h, x.market_entry = s["ref"], s.get("box_h"), True
    set_exit_rule(x, fam)
    return x


def build_retest_setup(symbol: str, exchange_id: str, df: pd.DataFrame, btc_df: pd.DataFrame, price: float,
                       alt_df: Optional[pd.DataFrame] = None, reg: Optional[np.ndarray] = None) -> Optional["CoinSetup"]:
    s = retest_signals(df, reg).get(len(df) - 1)
    if not s:
        return None
    side = "상단 돌파" if s["direction"] == "long" else "하단 이탈"
    note = (f"박스({fmt_range(s['box_lo'])}~{fmt_range(s['box_hi'])}) {side} 뒤 경계 {fmt_range(s['edge'])}까지 되돌림 → "
            f"경계 {'위' if s['direction'] == 'long' else '아래'} 마감 확인")
    return _v27_setup("돌파 후 되돌림", "retest", s, symbol, exchange_id, df, btc_df, price, alt_df, note)


def build_pullback_setup(symbol: str, exchange_id: str, df: pd.DataFrame, btc_df: pd.DataFrame, price: float,
                         alt_df: Optional[pd.DataFrame] = None) -> Optional["CoinSetup"]:
    s = pullback_signals(df).get(len(df) - 1)
    if not s:
        return None
    lg = s["direction"] == "long"
    note = (f"{'상승' if lg else '하락'} 추세(50·200EMA) 속 {'눌림' if lg else '반등'}(RSI {s['rsi_ext']:.0f}) 뒤 "
            f"20EMA {'위로' if lg else '아래로'} 다시 마감 · 거래량 {s['rel_vol']:.1f}배 · 손절 기준 최근 10봉 {'저점' if lg else '고점'}")
    return _v27_setup("추세 눌림목", "pb", s, symbol, exchange_id, df, btc_df, price, alt_df, note)


def build_divergence_setup(symbol: str, exchange_id: str, df: pd.DataFrame, btc_df: pd.DataFrame, price: float,
                           alt_df: Optional[pd.DataFrame] = None) -> Optional["CoinSetup"]:
    s = divergence_signals(df).get(len(df) - 1)
    if not s:
        return None
    lw = "저점" if s["direction"] == "long" else "고점"
    note = (f"{lw} 다이버전스(RSI {s['rsi0']:.0f}→{s['rsi1']:.0f}) · 두 번째 {lw}이 최근 60봉 "
            f"{'최저' if s['direction'] == 'long' else '최고'} · 200EMA {'위' if s['direction'] == 'long' else '아래'}")
    return _v27_setup("위치 다이버전스", "div", s, symbol, exchange_id, df, btc_df, price, alt_df, note)


# --------------------------------------------------------------------------
# v31 🧪 빗각 지지 롱 (일봉) — 연구 코드 diag4_lib.build / signals(L1, k=10, B2, 200일선, 닿음 0.5 ATR)와 같은 계산
# --------------------------------------------------------------------------
_DAILY_CACHE: Dict[tuple, tuple] = {}     # (거래소, 심볼) → (오늘 UTC 날짜, 마감 일봉 df) — 일봉은 하루 한 번만 바뀜


def _diag_pivots(x: np.ndarray, k: int, hi: bool) -> np.ndarray:
    """좌우 k봉 중 최고(저) = 꼭짓점. 확정에는 오른쪽 k봉이 필요(p + k < n)."""
    xs = pd.Series(x)
    ext = (xs.rolling(2 * k + 1, center=True).max() if hi else xs.rolling(2 * k + 1, center=True).min()).to_numpy()
    p = np.flatnonzero(x == ext)
    return p[(p >= k) & (p + k < len(x))]


def diag_support_signal(d: pd.DataFrame, k: int = DIAG_K, m_copies: int = DIAG_COPIES) -> Optional[Dict]:
    """마감된 일봉(UTC 00시 기준) 마지막 봉에 '상승 빗각 지지' 신호가 있으면 정보를, 없으면 None.
    상승 각도: 확정된 주요 고점 B마다 직전 주요 저점 A → A~B 사이 몸통(종가−시가)이 가장 큰 양봉의 고가. 기울기 = 가격차 ÷ 일수.
    선: 가장 최근에 확정된 각도를 B 뒤(B 포함) 확정된 주요 꼭짓점(고점·저점) 최근 6곳에 평행 복사.
    신호: 어제도 같은 각도였고 · 복사 꼭짓점이 어제까지 확정 · 어제 종가 > 어제 선 값 · 오늘 저가 ≤ 선 + 0.5 ATR · 오늘 종가 > 선 ·
    오늘 종가 > 200일 단순평균."""
    if d is None or len(d) < 210:
        return None
    d = d.reset_index(drop=True)
    o, h, l, c = (d[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    n = len(c); t = n - 1
    PH, PL = _diag_pivots(h, k, True), _diag_pivots(l, k, False)
    if not len(PH) or not len(PL):
        return None
    piv = np.r_[PH, PL]; ppx = np.r_[h[PH], l[PL]]
    o_ = np.argsort(piv, kind="stable"); piv, ppx = piv[o_], ppx[o_]
    ang = []                                   # (확정 시각, B 위치, 기울기, A 위치, A 저가, 각도 고점 위치)
    for b in PH:
        aa = PL[PL < b]
        if not len(aa):
            continue
        a = int(aa[-1]); seg = np.arange(a + 1, b + 1); body = c[seg] - o[seg]
        if body.max() <= 0:
            continue
        bb = int(seg[np.argmax(body)])
        if bb <= a:
            continue
        s = (h[bb] - l[a]) / (bb - a)
        if s <= 0:
            continue
        ang.append((int(b) + k, int(b), float(s), a, float(l[a]), bb))
    if not ang:
        return None
    cts = np.array([x[0] for x in ang])
    ai = int(np.searchsorted(cts, t, side="right") - 1); ai_prev = int(np.searchsorted(cts, t - 1, side="right") - 1)
    if ai < 0 or ai_prev != ai:
        return None
    _, b0, s, a_idx, a_px, bb = ang[ai]
    lo = int(np.searchsorted(piv, b0)); hi = int(np.searchsorted(piv, t - k, side="right"))
    if hi <= lo:
        return None
    sel = np.arange(max(lo, hi - m_copies), hi)[::-1]
    pp, px = piv[sel], ppx[sel]
    V = px + s * (t - pp); VP = px + s * (t - 1 - pp); okp = pp + k <= t - 1
    a_d = float(_atr_array(h, l, c)[t]); sma200 = float(np.mean(c[t - 199:t + 1]))
    if not np.isfinite(a_d) or a_d <= 0 or not c[t] > sma200:
        return None
    q = okp & (c[t - 1] > VP) & (l[t] <= V + DIAG_ZONE_ATR * a_d) & (c[t] > V)
    if not q.any():
        return None
    j = int(np.argmin(np.where(q, np.abs(V - c[t]), np.inf)))
    return {"line": float(V[j]), "slope": float(s), "atr_d": a_d, "sma200": sma200, "pivot_idx": int(pp[j]),
            "pivot_ts": d["ts"].iloc[int(pp[j])], "A_ts": d["ts"].iloc[a_idx], "A_px": a_px,
            "B_ts": d["ts"].iloc[bb], "B_px": float(h[bb]), "day": d["ts"].iloc[t], "close_d": float(c[t])}


def closed_daily(exchange_id: str, symbol: str) -> Optional[pd.DataFrame]:
    """마감된 일봉(UTC 00시 기준). 하루 한 번만 거래소에서 받고 나머지는 저장해 둔 것을 씀."""
    key = (exchange_id, symbol); today = utc_now().floor("1D")
    got = _DAILY_CACHE.get(key)
    if got and got[0] == today:
        return got[1]
    d = drop_unclosed(fetch_ohlcv(exchange_id, symbol, timeframe="1d", limit=DIAG_DAILY_LIMIT), "1d")
    if d is not None and len(d) and not (pd.to_datetime(d["ts"]).dt.hour == 0).all():
        print(f"[warn] {symbol} 일봉이 UTC 00시 기준이 아니라 빗각 계산에서 제외")
        d = None
    if utc_now() - today >= pd.Timedelta(minutes=5):    # 마감 직후 몇 분은 거래소 반영 전일 수 있어 저장하지 않음
        _DAILY_CACHE[key] = (today, d)
    return d


def btc_phase_on(exchange_id: str, day) -> Optional[int]:
    """마감된 BTC 일봉 기준 그날의 국면 번호(0 대상승 · 1 상승·횡보 · 2 하락·횡보 · 3 대하락). 모르면 None.
    연구 코드 regime5.regime_daily와 같은 계산: 일봉 d의 국면 = d일 종가(다음 날 UTC 00시)까지의 정보."""
    for ex_id in [exchange_id] + [e for e in EXCHANGES if e != exchange_id]:
        try:
            d = closed_daily(ex_id, f"BTC/{QUOTE}")
        except Exception:
            d = None
        if d is None or len(d) < 260:
            continue
        b = d.set_index("ts")["close"].astype(float).sort_index()
        sma = b.rolling(200, min_periods=200).mean(); r60 = b / b.shift(60) - 1
        lab = pd.Series(np.where(b > sma, np.where(r60 >= 0.20, 0, 1), np.where(r60 <= -0.20, 3, 2)), index=b.index)[sma.notna()]
        v = lab.get(pd.Timestamp(day))
        return None if v is None else int(v)
    return None


def build_diag_setup(symbol: str, exchange_id: str, df: pd.DataFrame, btc_df: pd.DataFrame, price: float,
                     alt_df: Optional[pd.DataFrame] = None) -> Optional["CoinSetup"]:
    """🧪 빗각 지지 롱. 일봉 신호는 한국 09시(UTC 00시) 마감 → 진입 기준 = 그 마감 4시간봉(UTC 20시 시작) 종가.
    다음 일봉 마감 전까지는 같은 신호를 보여줌(5년 검증: 13·17·21시에 늦게 들어가도 거래당 성과 거의 같음)."""
    d = closed_daily(exchange_id, symbol)
    if d is None or len(d) < 210:
        return None
    sig = diag_support_signal(d)
    if not sig:
        return None
    if DIAG_BULL_ONLY and btc_phase_on(exchange_id, sig["day"]) != 0:     # v31.1 사용자 결정: 대상승일 때만
        _rej("phase_gate")
        return None
    bar_ts = pd.Timestamp(sig["day"]) + pd.Timedelta(hours=20)       # 이 일봉 마감과 같이 끝나는 4시간봉
    df_ = df.reset_index(drop=True)
    hit = df_.index[df_["ts"] == bar_ts]
    if not len(hit) or TIMEFRAME != "4h":
        _rej("data_short")
        return None
    i = int(hit[0])
    h4, l4, c4 = (df_[x].to_numpy(float) for x in ("high", "low", "close"))
    a4 = float(_atr_array(h4, l4, c4)[i]); e = float(c4[i])
    if not np.isfinite(a4) or a4 <= 0:
        _rej("data_short")
        return None
    sl = e - DIAG_STOP_ATR_D * sig["atr_d"]
    if sl <= 0 or e - sl < 0.3 * a4:
        _rej("data_short")
        return None
    tp1 = e + DIAG_TP_R * (e - sl)
    if price <= sl or price >= tp1:
        _rej("invalid_price" if price <= sl else "target_reached")
        return None
    rs = relative_strength_vs_btc(df_.iloc[:i + 1], btc_df)
    rs_alt = relative_strength_vs_btc(df_.iloc[:i + 1], alt_df) if alt_df is not None else None
    note = (f"상승 빗각 지지(일봉): 저점 {pd.Timestamp(sig['A_ts']):%y-%m-%d} {fmt_range(sig['A_px'])} → "
            f"큰 양봉 고가 {pd.Timestamp(sig['B_ts']):%y-%m-%d} {fmt_range(sig['B_px'])} 각도를 "
            f"{pd.Timestamp(sig['pivot_ts']):%y-%m-%d} 꼭짓점에 복사한 선 {fmt_range(sig['line'])}에 닿고 위에서 마감 · "
            f"200일선 {fmt_range(sig['sma200'])} 위{' · BTC 대상승 국면' if DIAG_BULL_ONLY else ''} · 손절 = 진입가 − 2 일봉 ATR")
    x = CoinSetup(symbol, exchange_id, "diag_long", note + " · 한국 09시 일봉 마감 시장가 진입", e, price, tp1, tp1 + (tp1 - e), sl,
                  (tp1 - e) / (e - sl), False, False, rs, None, atr=a4, live_status="ready", rs_alt=rs_alt)
    x.level, x.market_entry, x.signal_ts = sig["line"], True, str(bar_ts)
    set_exit_rule(x, "빗각 지지")
    return x



# --------------------------------------------------------------------------
# v32 🧪 아래꼬리 연속 롱 (일봉) — 연구 코드 wx_lib(꼬리 ≥ 0.5 ATR, 같은 방향 2연속, 완성 봉 진입, BTC200위 + EMA20같은쪽)와 같은 계산
# --------------------------------------------------------------------------
def wick_support_signal(d: pd.DataFrame) -> Optional[Dict]:
    """마감된 일봉(UTC 00시 기준) 마지막 봉이 '아래꼬리 정확히 2연속째'면 정보를, 아니면 None.
    아래꼬리 봉: 아래꼬리(min(시가,종가) − 저가) ≥ 0.5 × 그 봉의 일봉 ATR(14) · 아래꼬리 > 위꼬리 · 고가 > 저가.
    신호: 오늘·어제 아래꼬리 봉, 그저께는 아님(3연속째는 신호 아님) · 오늘 종가 > EMA20(일봉 종가)."""
    if d is None or len(d) < 81:
        return None
    d = d.reset_index(drop=True)
    o, h, l, c = (d[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    n = len(c); t = n - 1
    a = _atr_array(h, l, c)
    lw = np.fmin(o, c) - l; uw = h - np.fmax(o, c); rg = h - l
    with np.errstate(invalid="ignore"):
        L = (lw >= WICK_ATR * a) & (lw > uw) & (rg > 0) & np.isfinite(a)
    if not (L[t] and L[t - 1] and not L[t - 2]):
        return None
    ema20 = float(pd.Series(c).ewm(span=20, adjust=False, min_periods=20).mean().iloc[t])
    a_d = float(a[t])
    if not np.isfinite(a_d) or a_d <= 0 or not np.isfinite(ema20):
        return None
    return {"atr_d": a_d, "ema20": ema20, "ema_ok": bool(c[t] > ema20), "low2": float(min(l[t], l[t - 1])),
            "wick1": float(lw[t - 1]), "wick2": float(lw[t]), "day": d["ts"].iloc[t], "close_d": float(c[t])}


def build_wick_setup(symbol: str, exchange_id: str, df: pd.DataFrame, btc_df: pd.DataFrame, price: float,
                     alt_df: Optional[pd.DataFrame] = None) -> Optional["CoinSetup"]:
    """🧪 아래꼬리 연속 롱. 일봉 신호는 한국 09시(UTC 00시) 마감 → 진입 기준 = 그 마감 4시간봉(UTC 20시 시작) 종가.
    다음 일봉 마감 전까지는 같은 신호를 보여줌(5년 검증: 13·17·21시에 늦게 들어가도 거래당 +0.70·+0.63·+0.62R, 09시 +0.71R)."""
    d = closed_daily(exchange_id, symbol)
    if d is None or len(d) < 81:
        return None
    sig = wick_support_signal(d)
    if not sig:
        return None
    ph = btc_phase_on(exchange_id, sig["day"])
    if not sig["ema_ok"] or ph is None or ph not in (0, 1):            # BTC 200일선 위(대상승·상승·횡보) + 코인 EMA20 위
        _rej("wick_gate")
        return None
    bar_ts = pd.Timestamp(sig["day"]) + pd.Timedelta(hours=20)       # 이 일봉 마감과 같이 끝나는 4시간봉
    df_ = df.reset_index(drop=True)
    hit = df_.index[df_["ts"] == bar_ts]
    if not len(hit) or TIMEFRAME != "4h":
        _rej("data_short")
        return None
    i = int(hit[0])
    h4, l4, c4 = (df_[x].to_numpy(float) for x in ("high", "low", "close"))
    a4 = float(_atr_array(h4, l4, c4)[i]); e = float(c4[i])
    if not np.isfinite(a4) or a4 <= 0:
        _rej("data_short")
        return None
    sl = e - WICK_STOP_ATR_D * sig["atr_d"]
    if sl <= 0 or e - sl < 0.3 * a4:
        _rej("data_short")
        return None
    tp1 = e + WICK_TP_R * (e - sl)
    if price <= sl or price >= tp1:
        _rej("invalid_price" if price <= sl else "target_reached")
        return None
    rs = relative_strength_vs_btc(df_.iloc[:i + 1], btc_df)
    rs_alt = relative_strength_vs_btc(df_.iloc[:i + 1], alt_df) if alt_df is not None else None
    note = (f"일봉 아래꼬리 2연속(어제 {fmt_range(sig['wick1'])} · 오늘 {fmt_range(sig['wick2'])}, 기준 0.5 일봉 ATR = "
            f"{fmt_range(WICK_ATR * sig['atr_d'])}) · 두 봉 최저가 {fmt_range(sig['low2'])} · BTC 200일선 위 · "
            f"EMA20 {fmt_range(sig['ema20'])} 위 마감 · 손절 = 진입가 − 2 일봉 ATR")
    x = CoinSetup(symbol, exchange_id, "wick_long", note + " · 한국 09시 일봉 마감 시장가 진입", e, price, tp1, tp1 + (tp1 - e), sl,
                  (tp1 - e) / (e - sl), False, False, rs, None, atr=a4, live_status="ready", rs_alt=rs_alt)
    x.level, x.market_entry, x.signal_ts = sig["low2"], True, str(bar_ts)
    set_exit_rule(x, "아래꼬리 연속")
    return x

def build_setup(symbol: str, exchange_id: str, df: pd.DataFrame, btc_df: pd.DataFrame,
                regime: RegimeType, htf_trend: RegimeType = "sideways",
                current_price: Optional[float] = None,
                alt_df: Optional[pd.DataFrame] = None) -> Optional[CoinSetup]:
    """완성된 봉(df)으로 신호를 계산하고, 현재가(current_price)로 추격·무효 여부를 판단.
    - 상승/하락: 최근 임펄스 → 눌림목(오더블록 또는 EMA20)에 지정가, 목표는 직전 고점/저점(구조적 목표)
    - 횡보: 유효한 박스에서만 (A) 돌파 후 리테스트 또는 (B) 조용한 경계 역매매
    - 손절: 구조 레벨보다 0.3 ATR 바깥, 그리고 최소 1 ATR (좁은 손절은 사냥당하기 쉬움)
    - 추격: 현재가가 진입가에서 0.5 ATR 넘게 벗어나면 '대기'"""
    if df is None or len(df) < 60:
        _rej("data_short")
        return None
    a = atr(df)
    if not a or np.isnan(a) or a <= 0:
        _rej("data_short")
        return None
    price = float(current_price) if current_price is not None else float(df["close"].iloc[-1])
    vol_avg20 = df["volume"].iloc[-21:-1].mean()
    rel_vol = float(df["volume"].iloc[-1] / vol_avg20) if vol_avg20 else 1.0  # 마지막 완성봉 거래량 배수
    rs = relative_strength_vs_btc(df, btc_df)
    rs_alt = relative_strength_vs_btc(df, alt_df) if alt_df is not None else None

    def _extras():
        """조건을 통과한 경우에만 계산하는 지표들 (매물대·유동성 스윕)."""
        return calculate_volume_profile(df), detect_liquidity_sweep(df), None, ""

    hi20, lo20 = float(df["high"].tail(20).max()), float(df["low"].tail(20).min())
    hi50, lo50 = float(df["high"].tail(50).max()), float(df["low"].tail(50).min())

    if regime in ("uptrend", "downtrend"):
        long_side = regime == "uptrend"
        impulse = find_recent_impulse(df, "up" if long_side else "down")
        if impulse is None:
            _rej("no_impulse")
            return None
        if (long_side and rs <= 0) or (not long_side and rs >= 0):
            _rej("weak_rs")
            return None
        ob = detect_order_block(df)
        ema20 = float(ema(df["close"], 20).iloc[-1])
        vp, sweep, asym, asym_tag = _extras()
        if long_side:
            zone = ob["bullish_ob"]
            entry = zone["high"] if zone else ema20
            structural_sl = zone["low"] - STOP_BUFFER_ATR * a if zone else entry - 1.5 * a
            sl = min(structural_sl, entry - MIN_STOP_ATR * a)
            tp1 = hi20
            if tp1 <= entry + 0.5 * a:
                _rej("no_room")
                return None
            tp1, tp2 = _order_targets("long", tp1, hi50, a)
            rr = (tp1 - entry) / (entry - sl)
            is_chase = price > entry + CHASE_ATR * a
            poc_conf = near_level(entry, vp["poc"], 0.5, a) or near_level(entry, vp["val"], 0.5, a)
            sweep_conf = sweep["bullish_sweep"] is not None
            direction, bias = "long", "long"
        else:
            zone = ob["bearish_ob"]
            entry = zone["low"] if zone else ema20
            structural_sl = zone["high"] + STOP_BUFFER_ATR * a if zone else entry + 1.5 * a
            sl = max(structural_sl, entry + MIN_STOP_ATR * a)
            tp1 = lo20
            if tp1 >= entry - 0.5 * a:
                _rej("no_room")
                return None
            tp1, tp2 = _order_targets("short", tp1, lo50, a)
            rr = (entry - tp1) / (sl - entry)
            is_chase = price < entry - CHASE_ATR * a
            poc_conf = near_level(entry, vp["poc"], 0.5, a) or near_level(entry, vp["vah"], 0.5, a)
            sweep_conf = sweep["bearish_sweep"] is not None
            direction, bias = "short", "short"
        problem = _signal_time_problem(direction, price, sl, tp1)
        if problem:
            _rej(problem)
            return None
        tags = (" + 매물대 겹침" if poc_conf else "") + (" + 유동성 스윕" if sweep_conf else "")
        where = "오더블록" if zone else "EMA20"
        note = (f"상대강도 {rs:+.1f}%, 임펄스(거래량 {impulse:.1f}배) 후 {where} "
                f"{'눌림목' if long_side else '반등'}{tags}{asym_tag} → "
                f"{'⏳ 지정가 대기(지금은 추격)' if is_chase else '✅ 진입가 근처'}")
        return CoinSetup(symbol, exchange_id, bias, note, entry, price, tp1, tp2, sl, rr, is_chase,
                         poc_conf, rs, asym, sweep_confluence=sweep_conf, atr=a,
                         live_status="chase" if is_chase else "ready", rs_alt=rs_alt)

    # ---------------- 횡보: 유효한 박스에서만
    box = detect_box(df, a, lookback=BOX_LOOKBACK)
    if not box["valid"]:
        _rej("no_box")
        return None
    vp, sweep, asym, _ = _extras()
    lean = compute_sideways_lean(df, htf_trend)
    lean_tag = f" [기울기: {lean['label']} ({lean['score']:+.2f})]"
    hi, lo, h = box["high"], box["low"], box["height"]
    last_close = float(df["close"].iloc[-1])
    box_tag = f"박스({fmt_range(lo)}~{fmt_range(hi)}, 위 {box['touch_high']}회·아래 {box['touch_low']}회 터치)"

    def _make(direction, bias, entry, sl, tp1, tp2, note_core, poc_conf=False, sweep_conf=False):
        tp1, tp2 = _order_targets(direction, tp1, tp2, a)
        problem = _signal_time_problem(direction, price, sl, tp1)
        if problem:
            _rej(problem)
            return None
        if direction == "long":
            rr = (tp1 - entry) / (entry - sl)
            is_chase = price > entry + CHASE_ATR * a
        else:
            rr = (entry - tp1) / (sl - entry)
            is_chase = price < entry - CHASE_ATR * a
        tags = (" + 매물대 겹침" if poc_conf else "") + (" + 유동성 스윕" if sweep_conf else "")
        note = (f"{note_core}{tags}{lean_tag} → "
                f"{'⏳ 지정가 대기(지금은 추격)' if is_chase else '✅ 진입가 근처'}")
        tg = breakout_tags(df, direction, htf_trend, rs_alt, rs) if bias.startswith("wait_breakout") else []
        return CoinSetup(symbol, exchange_id, bias, note, entry, price, tp1, tp2, sl, rr, is_chase,
                         poc_conf, rs, asym, sweep_confluence=sweep_conf, atr=a,
                         live_status="chase" if is_chase else "ready", rs_alt=rs_alt, tags=tg)

    # (A) 돌파: 마지막 완성봉이 박스 밖에서 마감 + 거래량 2배 이상 + 경계 밖 1 ATR 이내 → 봉 마감 직후 시장가
    #     v29: 직전 90봉 박스 · 손절 = 경계 너머 0.5 ATR(진입가에서 최소 1 ATR) · 목표1 = 경계 + 박스 높이 1배
    #     (닿으면 팔지 않고 손절을 진입가로 올린 뒤 1 ATR 추적) · 최대 60봉(10일)
    xr = EXIT_RULES["돌파"]
    if last_close > hi and rel_vol >= BOX_VOL_MIN and last_close - hi <= BOX_EXT_ATR * a:
        s_ = _make("long", "wait_breakout_long", hi, hi - xr["stop_atr"] * a, hi + xr["tp_box"] * h,
                   hi + (xr["tp_box"] + 1.0) * h, f"{box_tag} 상단 돌파(거래량 {rel_vol:.1f}배) 후 리테스트")
        if s_:
            s_.level, s_.box_h = hi, h
            set_exit_rule(s_, "돌파")
        return s_
    if last_close < lo and rel_vol >= BOX_VOL_MIN and lo - last_close <= BOX_EXT_ATR * a:
        s_ = _make("short", "wait_breakout_short", lo, lo + xr["stop_atr"] * a, lo - xr["tp_box"] * h,
                   lo - (xr["tp_box"] + 1.0) * h, f"{box_tag} 하단 이탈(거래량 {rel_vol:.1f}배) 후 리테스트")
        if s_:
            s_.level, s_.box_h = lo, h
            set_exit_rule(s_, "돌파")
        return s_

    # (B) 박스 안, 거래량이 잠잠한 상태에서 경계 근처 → 역매매(평균회귀)
    if lo <= last_close <= hi and rel_vol <= 1.1:
        if last_close <= lo + 1.0 * a:
            if lean["score"] < -0.3:
                _rej("lean_against")
                return None
            entry = lo + 0.25 * a
            sl = min(lo - STOP_BUFFER_ATR * a, entry - MIN_STOP_ATR * a)
            poc = vp["poc"]
            tp1 = poc if poc and poc > entry + 0.5 * a else lo + h / 2
            return _make("long", "range_fade_long", entry, sl, tp1, hi - 0.2 * a,
                         f"{box_tag} 하단 지지, 거래량 잠잠({rel_vol:.1f}배)",
                         near_level(entry, vp["val"], 0.5, a), sweep["bullish_sweep"] is not None)
        if last_close >= hi - 1.0 * a:
            if lean["score"] > 0.3:
                _rej("lean_against")
                return None
            entry = hi - 0.25 * a
            sl = max(hi + STOP_BUFFER_ATR * a, entry + MIN_STOP_ATR * a)
            poc = vp["poc"]
            tp1 = poc if poc and poc < entry - 0.5 * a else hi - h / 2
            return _make("short", "range_fade_short", entry, sl, tp1, lo + 0.2 * a,
                         f"{box_tag} 상단 저항, 거래량 잠잠({rel_vol:.1f}배)",
                         near_level(entry, vp["vah"], 0.5, a), sweep["bearish_sweep"] is not None)

    _rej("mid_box")
    return None


BITGET_ONLY = True  # True면 Bitget USDT 무기한 선물이 있는 코인만 추천 (Bitget에서 거래하므로)
_EXCLUDED_BASES = {"USDC", "FDUSD", "TUSD", "DAI", "USDE", "USDD", "PYUSD", "BUSD", "USD1", "USDP", "EUR"}


def _is_excluded_symbol(symbol: str) -> bool:
    """스테이블코인·레버리지 토큰은 추천 대상에서 제외."""
    base = symbol.split("/")[0]
    return base in _EXCLUDED_BASES or base.endswith(("3L", "3S", "5L", "5S"))


PERPS_CACHE_FILE = "bitget_perps_cache.json"
PERPS_SOURCE = "none"   # live(방금 조회) / cache(조회 실패 → 최근 저장본) / none(확인 불가)
_BAD_PERP_STATUS = {"off", "offline", "maintain", "restrictedapi", "limit_open", "delisted", "suspend", "halt"}


def _perp_tradable(m: Dict) -> bool:
    """정상 거래 가능한 USDT 무기한 선물인지 (상장 폐지·거래 중지·청산만 가능 상태 제외)."""
    if not (m.get("swap") and m.get("linear") and m.get("quote") == QUOTE) or m.get("active") is False:
        return False
    info = m.get("info") or {}
    for key in ("symbolStatus", "status", "symbolType"):
        v = str(info.get(key, "")).strip().lower()
        if v in _BAD_PERP_STATUS:
            return False
    off = info.get("offTime") or info.get("deliveryTime")
    try:
        if off and str(off) not in ("-1", "0", "") and int(off) <= int(utc_now().timestamp() * 1000):
            return False   # 이미 내려간(만기·폐지) 선물
    except Exception:
        pass
    return True


def bitget_perp_symbols() -> set:
    """Bitget USDT-M 무기한 선물 중 정상 거래 가능한 심볼 집합(예: 'SOL/USDT:USDT').
    조회에 성공하면 파일로 저장하고, 실패하면 7일 이내 저장본을 씀. 둘 다 없으면 빈 집합(PERPS_SOURCE='none')."""
    global PERPS_SOURCE
    try:
        ex = _get_ex("bitget")
        ex.load_markets()
        perps = {m["symbol"] for m in ex.markets.values() if _perp_tradable(m)}
        if perps:
            PERPS_SOURCE = "live"
            _LAST_PERPS.clear()
            _LAST_PERPS.update(perps)
            try:
                with open(PERPS_CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump({"saved": str(utc_now()), "perps": sorted(perps)}, f)
            except Exception:
                pass
            return perps
    except Exception as e:
        print(f"[warn] Bitget 선물 목록 조회 실패: {e}")
    try:
        with open(PERPS_CACHE_FILE, encoding="utf-8") as f:
            c = json.load(f)
        if utc_now() - pd.Timestamp(c["saved"]) <= pd.Timedelta("7D") and c.get("perps"):
            PERPS_SOURCE = "cache"
            print("[warn] Bitget 선물 목록 조회 실패 → 최근 저장본 사용")
            _LAST_PERPS.clear()
            _LAST_PERPS.update(c["perps"])
            return set(c["perps"])
    except Exception:
        pass
    PERPS_SOURCE = "none"
    return set()


def verify_bitget_tradable(perp_symbols: List[str]) -> Optional[set]:
    """화면에 내보내기 직전 확인: Bitget 선물 시세를 한 번에 조회해 실제 가격·거래량이 있는 심볼만 반환.
    조회 자체가 실패하면 None(확인 못 함 → 거르지 않음)."""
    syms = sorted({p for p in perp_symbols if p})
    if not syms:
        return set()
    try:
        ex = _get_ex("bitget")
        try:
            tk = ex.fetch_tickers(syms)
        except Exception:
            tk = ex.fetch_tickers(syms, params={"productType": "USDT-FUTURES"})
        ok = set()
        for sym in syms:
            t = tk.get(sym) or {}
            if (t.get("last") or t.get("close")) and (t.get("quoteVolume") or t.get("baseVolume") or 0) > 0:
                ok.add(sym)
        return ok
    except Exception as e:
        print(f"[warn] Bitget 선물 시세 확인 실패: {e}")
        return None


_PERP_MULTS = (1, 1000, 10000, 1000000)
UNIVERSE_DIAG: Dict = {}


def bitget_perp_volumes() -> Dict[str, float]:
    """v29.2: Bitget USDT-M 선물 24h 거래대금(USDT) {선물 심볼: 금액}. 실패하면 빈 dict.
    현물 거래대금을 못 받는 환경(해외 서버 등)에서 순위를 매기는 데 씀 — 실제로 거래하는 상품의 거래대금이라 기준으로도 자연스러움."""
    try:
        ex = _get_ex("bitget")
        ex.load_markets()
    except Exception as e:
        print(f"[warn] Bitget 시장 목록 조회 실패: {e}")
        return {}
    tk: Dict = {}
    for params in ({"type": "swap", "productType": "USDT-FUTURES"}, {"productType": "USDT-FUTURES"}):
        try:
            tk = ex.fetch_tickers(None, params) or {}
            if tk:
                break
        except Exception as e:
            print(f"[warn] Bitget 선물 시세 조회 실패: {e}")
    swap_keys = [s for s in tk if ":" in s]
    out: Dict[str, float] = {}
    for s in (swap_keys or list(tk)):
        p = s if ":" in s else f"{s}:{QUOTE}"
        if p.endswith(f"/{QUOTE}:{QUOTE}"):
            out[p] = max(out.get(p, 0.0), _ticker_qv(tk[s]))
    return out


def _is_rwa_perp(perp: str) -> bool:
    """주식·원자재 등 코인이 아닌 선물(거래소 정보에 RWA 표시가 있는 경우) — 코인으로 검증한 매매법이라 제외."""
    try:
        info = ((_get_ex("bitget").markets or {}).get(perp) or {}).get("info") or {}
        return any(str(info.get(k, "")).strip().upper() in ("YES", "TRUE", "1") for k in ("isRwa", "isRWA", "rwa"))
    except Exception:
        return False


def _perp_ranked_universe(perps: set, pvol: Dict[str, float]) -> List[tuple]:
    """v29.2: Bitget 선물 거래대금 순위로 고른 후보 [(심볼, info)].
    캔들: Bitget 현물이 정상이면 현물(PC와 같은 데이터), 없으면 그 선물 자체(_mkt_symbol이 자동으로 선물로 조회)."""
    bg = _EX_CACHE.get("bitget")
    mk = (bg.markets or {}) if bg is not None else {}

    def spot_ok(s: str) -> bool:
        m = mk.get(s)
        return bool(m) and m.get("spot") is not False and m.get("active") is not False

    out, seen = [], set()
    for p, pv in sorted(pvol.items(), key=lambda kv: -kv[1]):
        if p not in perps or pv < MIN_QUOTE_VOLUME_USDT or _is_rwa_perp(p):
            continue
        base_p = p.split("/")[0]
        sym, m = f"{base_p}/{QUOTE}", 1               # 기본: 선물 이름 그대로(현물 없으면 선물 캔들)
        if ANALYZE_ON_PERP:
            # v29.5: 선물로 분석하므로 현물 목록과 상관없이 같은 규칙 → PC·폰에서 코인 이름·가격 단위가 같음
            # (1000PEPE → PEPE·1000배. 단, 'PEPE' 이름의 다른 선물이 따로 있으면 헷갈리지 않게 그대로 둠)
            for mm in sorted(_PERP_MULTS[1:], reverse=True):
                pref = str(mm)
                rest = base_p[len(pref):]
                if base_p.startswith(pref) and rest and not rest[0].isdigit():
                    if f"{rest}/{QUOTE}:{QUOTE}" not in perps:
                        sym, m = f"{rest}/{QUOTE}", mm
                    break
        elif not spot_ok(sym):
            for mm in sorted(_PERP_MULTS[1:], reverse=True):   # 1000PEPE → 현물 PEPE(가격 1000배 표기)가 있으면 그 현물로
                pref = str(mm)
                rest = base_p[len(pref):]
                if base_p.startswith(pref) and rest and not rest[0].isdigit():
                    if spot_ok(f"{rest}/{QUOTE}"):
                        sym, m = f"{rest}/{QUOTE}", mm
                    break
        if _is_excluded_symbol(sym) or sym in seen:
            continue
        seen.add(sym)
        out.append((sym, {"src": "bitget", "vol": float(pv), "perp": p, "mult": m,
                          "candle": "선물" if ANALYZE_ON_PERP or not spot_ok(sym) else "현물"}))
    return out


def perp_match(symbol: str, perps: set):
    """현물 심볼에 맞는 Bitget 선물 심볼과 가격 배수. 예) PEPE/USDT → 1000PEPE/USDT:USDT, 1000배.
    (선물은 가격이 너무 작은 코인을 1000배 단위로 표기해서, 그대로 찾으면 '선물 없음'으로 빠졌음)"""
    base = symbol.split("/")[0]
    for m in _PERP_MULTS:
        cand = f"{'' if m == 1 else m}{base}/{QUOTE}:{QUOTE}"
        if cand in perps:
            return cand, m
    return None, 1


def build_universe(top_n: Optional[int] = None, perps: Optional[set] = None, rank_by: str = "auto") -> Dict[str, Dict]:
    """스캔할 코인 상위 top_n개 {심볼: {src, vol, perp, mult}}.
    v29.5: 선물 분석(ANALYZE_ON_PERP)이면 Bitget 선물 24h 거래대금 순위로 고름 → PC·폰이 같은 Bitget 응답 하나로
    같은 코인 목록을 만듦 (예전엔 PC는 현물 거래대금, 폰은 선물 거래대금 순위라 목록이 달랐음).
    rank_by="spot"(연구용 과거 자료 내보내기)이거나 선물 거래대금 조회가 실패하면 예전 방식(현물 거래대금 순위)."""
    top_n = top_n or TOP_N_BY_VOLUME
    if perps and ANALYZE_ON_PERP and rank_by == "auto":
        pvol = bitget_perp_volumes()
        kept = _perp_ranked_universe(perps, pvol) if pvol else []
        if len(kept) >= min(top_n, 10):
            UNIVERSE_DIAG.clear()
            _VOL_DIAG.clear()
            out = dict(kept[:top_n])
            UNIVERSE_DIAG.update({"순위 기준": "Bitget 선물 거래대금(PC·폰 공통)", "분석 차트": "Bitget 선물",
                                  "Bitget 선물": f"{len(perps)}개({PERPS_SOURCE}) · 거래대금 300만$ 이상 "
                                                 f"{sum(1 for v in pvol.values() if v >= MIN_QUOTE_VOLUME_USDT)}",
                                  "배수 표기(1000PEPE 등)": sum(1 for i in out.values() if i["mult"] > 1),
                                  "스캔 대상": len(out)})
            return out
        print("[warn] Bitget 선물 거래대금 조회 실패 → 현물 거래대금 순위로 대신")
    out = _build_universe_spot(top_n, perps, perp_src=bool(perps and ANALYZE_ON_PERP and rank_by == "auto"))
    if perps and ANALYZE_ON_PERP and rank_by == "auto":
        UNIVERSE_DIAG["순위 기준"] = "현물 거래대금(선물 거래대금 조회 실패 → 대신)"
    return out


def _build_universe_spot(top_n: int, perps: Optional[set], perp_src: bool) -> Dict[str, Dict]:
    """(v29.4까지의 방식) 3개 거래소 거래량 목록을 합쳐 '합산 거래량 순위' 상위 top_n개를 반환 {심볼: {src, vol, perp, mult}}.
    - 캔들은 EXCHANGES 순서상 먼저 나온 거래소(기본 Bitget)에서 받음
    - 순위는 거래소들 중 가장 큰 24h 거래대금 기준
    - perps를 주면 Bitget 선물로 거래 가능한 코인만 남긴 뒤 순위를 자름 → N은 '실제 거래 가능한 N개'"""
    merged: Dict[str, Dict] = {}
    # v29.1: 선물 목록이 있으면 거래대금 목록을 자르지 않고 받음. 거래소 현물 상위권에 선물 없는 종목(토큰화 주식 등)이
    # 많아서, 2배만 받으면 다른 거래소 목록을 못 받는 환경(예: 해외 서버의 Streamlit Cloud)에서 N개보다 적게 남았음
    want = 100_000 if perps else max(top_n, 30) * 2
    src_status: Dict[str, str] = {}
    _VOL_DIAG.clear()
    for exchange_id in EXCHANGES:
        try:
            lst = _volume_list(exchange_id, want)
            src_status[exchange_id] = f"{len(lst)}개"
        except Exception as e:
            print(f"[warn] {exchange_id} 거래량 목록 조회 실패: {e}")
            src_status[exchange_id] = "실패(" + (re.search(r"\b(4\d\d|5\d\d)\b", str(e)) or [str(e)[:30]])[0] + ")"
            continue
        for sym, vol in lst:
            if _is_excluded_symbol(sym):
                continue
            if sym not in merged:
                merged[sym] = {"src": exchange_id, "vol": vol}
            else:
                merged[sym]["vol"] = max(merged[sym]["vol"], vol)
    ranked = sorted(merged.items(), key=lambda kv: -kv[1]["vol"])
    UNIVERSE_DIAG.clear()
    UNIVERSE_DIAG["거래량 목록"] = " · ".join(f"{k} {v}" for k, v in src_status.items())
    UNIVERSE_DIAG["합산 후보"] = len(ranked)
    bd = _VOL_DIAG.get("bitget")
    if bd:
        UNIVERSE_DIAG["Bitget 현물"] = (f"목록 {bd['pairs']} · 거래 가능 {bd['active']} · 시세 {bd['tickers']} · "
                                       f"거래대금 있음 {bd['with_vol']} · BTC {'있음' if bd['btc'] else '없음'}")
    if perps:
        matched = [(sym, info, *perp_match(sym, perps)) for sym, info in ranked]
        n_spot_ok = sum(1 for _, info, p, _ in matched if p is not None and info["vol"] > 0)
        # 선물 거래대금은 필요할 때만 조회: 현물 거래대금이 비어 있는 코인이 있거나, 현물 기준으로 N개를 크게 못 채울 때
        need_pvol = any(p is not None and info["vol"] <= 0 for _, info, p, _ in matched) or n_spot_ok < 0.8 * top_n
        pvol = bitget_perp_volumes() if need_pvol else {}
        kept, dropped, mult_n, miss, covered = [], 0, 0, [], set()
        n_fill = n_thin = n_dup = 0
        for sym, info, p, m in matched:
            if p is None:
                dropped += 1
                miss.append((sym, info["src"]))
                continue
            if p in covered:            # v29.2: 같은 선물에 현물 두 개(예: SATS·1000SATS)가 붙으면 거래대금 큰 쪽만 (같은 추천 두 번 방지)
                n_dup += 1
                continue
            if info["vol"] <= 0 and pvol:   # 현물 거래대금을 안 준 거래소 → 선물 거래대금으로 대신, 기준 미달이면 제외
                pv = pvol.get(p, 0.0)
                if pv < MIN_QUOTE_VOLUME_USDT:
                    n_thin += 1
                    continue
                info["vol"] = pv
                n_fill += 1
            info.update(perp=p, mult=m)
            covered.add(p)
            mult_n += m > 1
            kept.append((sym, info))
        kept.sort(key=lambda kv: -kv[1]["vol"])
        basis = "현물 거래대금"
        if len(kept) < 0.8 * top_n and pvol:
            # v29.2: 해외 서버(Streamlit Cloud 등)는 지역 제한으로 현물 목록이 비거나 코인이 빠져 있어서 N개보다 훨씬 적게 남았음
            # → Bitget 선물 거래대금 순위로 바로 고름. PC처럼 현물 목록이 정상이면 이 경로를 타지 않음(결과 그대로)
            alt = _perp_ranked_universe(perps, pvol)
            if len(alt) > len(kept):
                kept, basis = alt, "Bitget 선물 거래대금(현물 목록 부족 → 보완)"
                UNIVERSE_DIAG["선물 캔들로 분석"] = sum(1 for _, i in kept[:top_n] if i.get("candle") == "선물")
        if perp_src:     # v29.4: 모든 코인을 Bitget 선물 차트로 분석 (fetch_ohlcv가 선물로 보냄)
            for _, info in kept:
                info["src"] = "bitget"
                info["candle"] = "선물"
            UNIVERSE_DIAG["분석 차트"] = "Bitget 선물(v29.4)"
            UNIVERSE_DIAG.pop("선물 캔들로 분석", None)
        ranked = kept
        by_src: Dict[str, int] = {}
        for _, src in miss:
            by_src[src] = by_src.get(src, 0) + 1
        UNIVERSE_DIAG.update({"순위 기준": basis,
                              "Bitget 선물": f"{len(perps)}개({PERPS_SOURCE})"
                                             + (f" · 거래대금 300만$ 이상 {sum(1 for v in pvol.values() if v >= MIN_QUOTE_VOLUME_USDT)}"
                                                if pvol else (" · 거래대금 조회 실패" if need_pvol else "")),
                              "Bitget 선물 없음": dropped, "배수 표기로 매칭(1000PEPE 등)": mult_n,
                              "선물 없음 출처": "·".join(f"{k} {v}" for k, v in by_src.items()) or "-",
                              "선물 없음 예시(거래량 큰 순)": " ".join(x.split("/")[0] for x, _ in miss[:15]) or "-",
                              "선물 목록 예시": " ".join(sorted(x.split("/")[0] for x in perps)[:8])})
        if n_fill or n_thin or n_dup:
            UNIVERSE_DIAG["현물 거래대금 없음→선물 기준"] = f"{n_fill}개 사용 · 기준 미달 {n_thin}개 제외 · 중복 {n_dup}개 제외"
    else:
        for sym, info in ranked:
            info.update(perp=f"{sym}:{QUOTE}", mult=1)
    out = dict(ranked[:top_n])
    UNIVERSE_DIAG["스캔 대상"] = len(out)
    return out


LAST_SCAN_STATS: Dict = {}
LAST_LOADED: Dict[str, tuple] = {}  # 마지막 스캔에서 받은 완성봉 {심볼: (거래소, df)} — 실전 추적에 재사용


def watch_candidate(df: pd.DataFrame, htf_trend: RegimeType, live: float) -> Optional[Dict]:
    """'돌파 임박' 관찰 후보: 유효한 박스 안에서 종가가 상단(또는 하단)에서 1 ATR 이내.
    아직 진입 신호는 아니고, 거래량과 함께 박스를 벗어나 봉이 마감되면 추천으로 올라옴."""
    a = atr(df)
    if not a or np.isnan(a) or a <= 0:
        return None
    box = detect_box(df, a, lookback=BOX_LOOKBACK)
    if not box["valid"]:
        return None
    last = float(df["close"].iloc[-1])
    hi, lo = box["high"], box["low"]
    if hi - 1.0 * a <= last <= hi:
        side, trigger = "long", hi
    elif lo <= last <= lo + 1.0 * a:
        side, trigger = "short", lo
    else:
        return None
    lean = compute_sideways_lean(df, htf_trend)   # v27: 표시만 (돌파 신호에서 기울기 필터를 뺐으므로 관찰 목록도 거르지 않음)
    prev = df["volume"].iloc[-23:-3].mean()
    return {"side": side, "trigger": float(trigger), "box_high": hi, "box_low": lo, "atr": float(a),
            "last_close": last, "price": float(live), "dist_atr": abs(trigger - last) / a,
            "vol_ratio": float(df["volume"].iloc[-3:].mean() / prev) if prev else None,
            "lean": lean["label"], "touches": f"위 {box['touch_high']}회·아래 {box['touch_low']}회"}


def _fetch_last_prices(by_ex: Dict[str, List[str]]) -> Dict[tuple, float]:
    """거래소별로 한 번에 현재가 조회 {(거래소, 심볼): 가격}."""
    prices: Dict[tuple, float] = {}
    for ex_id, syms in by_ex.items():
        try:
            for sym, p in _last_prices(ex_id, syms).items():
                prices[(ex_id, sym)] = p
        except Exception as e:
            print(f"[warn] {ex_id} 현재가 조회 실패: {e}")
    return prices


def refresh_watch_prices(items: List[Dict]) -> None:
    """관찰 목록 현재가 갱신. 현재가가 경계를 넘으면 '돌파 진행 중'(봉 마감 때 신호 확정) 표시."""
    by_ex: Dict[str, List[str]] = {}
    for w in items:
        by_ex.setdefault(w["exchange"], []).append(w["symbol"])
    prices = _fetch_last_prices(by_ex)
    for w in items:
        p = prices.get((w["exchange"], w["symbol"]))
        if p is None:
            continue
        w["price"] = p
        w["crossing"] = p > w["trigger"] if w["side"] == "long" else p < w["trigger"]


def entry_kst(setup: "CoinSetup") -> pd.Timestamp:
    """진입 기준 시각(한국): 신호 봉 마감. 신호 시각이 없으면 지금."""
    try:
        if getattr(setup, "signal_ts", None):
            return pd.Timestamp(setup.signal_ts) + pd.Timedelta(TIMEFRAME) + pd.Timedelta(hours=9)
    except Exception:
        pass
    return utc_now() + pd.Timedelta(hours=9)


def calendar_risk_mult(setup: "CoinSetup") -> float:
    """v32.1: 지금 3종의 일요일(한국) 진입은 위험 ½ — 5년 검증에서 일요일 진입만 3자료·두 사이클 모두 손해(손절 72~76%)."""
    if not SUNDAY_HALF or SETUP_FAMILY.get(getattr(setup, "bias", ""), "") not in SUNDAY_HALF_FAMILIES:
        return 1.0
    return SUNDAY_RISK_MULT if entry_kst(setup).weekday() == 6 else 1.0


def chop_index(df: pd.DataFrame, n: int = CHOP_N) -> pd.Series:
    """Choppiness Index = 100 × log10(n봉 진폭 합 ÷ n봉 최고−최저) ÷ log10(n). 높을수록 출렁임(방향 없음), 낮을수록 추세."""
    h, l, c = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    rng = h.rolling(n).max() - l.rolling(n).min()
    return 100 * np.log10(tr.rolling(n).sum() / rng) / np.log10(n)


def market_chop_info(loaded: Dict[str, tuple], n: int = CHOP_N, tail: int = 90) -> Optional[Dict]:
    """v32.6: 스캔한 코인(BTC 제외)의 봉마다 Choppiness Index 중앙값 → 시장 출렁임 지수. 완성봉만 사용."""
    cols = {}
    for sym, got in (loaded or {}).items():
        try:
            if sym.startswith("BTC/") or not got:
                continue
            df = got[1]
            if df is None or len(df) < n + 5:
                continue
            ts = pd.to_datetime(df["ts"])
            cols[sym] = pd.Series(chop_index(df, n).to_numpy(), index=ts).iloc[-tail:]
        except Exception:
            continue
    if len(cols) < 10:
        return None
    med = pd.DataFrame(cols).median(axis=1, skipna=True).dropna()
    if med.empty:
        return None
    last = float(med.iloc[-1])
    return {"value": last, "limit": CHOP_LIMIT, "n": n, "coins": len(cols), "ts": str(med.index[-1]),
            "avg7d": float(med.iloc[-42:].mean()), "on": bool(CHOP_HALF and last > CHOP_LIMIT),
            "series": {str(k): float(v) for k, v in med.iloc[-60:].items()}}


def chop_risk_mult(setup: "CoinSetup", info: Optional[Dict]) -> Tuple[float, Optional[float]]:
    """v32.6: 신호 봉 시각의 시장 출렁임 지수가 52 초과면 지금 3종 위험 ½. (배수, 그 봉의 지수)"""
    if not info or not CHOP_HALF:
        return 1.0, None
    ser = info.get("series") or {}
    v = ser.get(str(pd.Timestamp(setup.signal_ts))) if getattr(setup, "signal_ts", "") else None
    v = float(info["value"]) if v is None else float(v)
    if SETUP_FAMILY.get(getattr(setup, "bias", ""), "") not in CHOP_FAMILIES:
        return 1.0, v
    return (CHOP_RISK_MULT if v > CHOP_LIMIT else 1.0), v


def chop_text(info: Optional[Dict]) -> str:
    """화면 한 줄 설명"""
    if not info:
        return ""
    v, lim = info["value"], info["limit"]
    lvl = "출렁임 큼" if v > lim else ("추세 쪽(곧게 움직임)" if v < 47.9 else "보통")
    act = "박스 돌파·되돌림·눌림목 신규 진입 위험 ½ 적용 중" if info.get("on") else "정상 위험"
    return (f"🌊 시장 출렁임 지수 {v:.1f} ({info['n']}봉 · 코인 {info['coins']}개 중앙값, 최근 7일 평균 {info['avg7d']:.1f}) — "
            f"{lvl} → {act} (기준 {lim:g} 초과 시 ½)")


def live_status_of(setup: "CoinSetup", price: float) -> str:
    """현재가 기준 상태. 손절선을 먼저 넘으면 무효, 목표1에 먼저 닿으면 놓침."""
    tol = CHASE_ATR * setup.atr if setup.atr else setup.entry_price * 0.005
    if setup.bias.startswith(("diag_", "wick_")):   # v31·v32: 일봉 신호 — 13·17·21시 늦은 진입도 검증에서 거의 같아 '추격' 표시 안 함
        tol = float("inf")
    if setup.bias in LONG_BIASES:
        if price <= setup.sl:
            return "invalid"
        if price >= setup.tp1:
            return "missed"
        return "chase" if price > setup.entry_price + tol else "ready"
    if price >= setup.sl:
        return "invalid"
    if price <= setup.tp1:
        return "missed"
    return "chase" if price < setup.entry_price - tol else "ready"


def refresh_live_status(setups: List["CoinSetup"]) -> int:
    """전체 스캔 없이 현재가만 받아 추천들의 상태를 갱신 (거래소당 한 번의 일괄 조회, 보통 몇 초).
    한 번 무효·놓침이 된 추천은 다음 전체 스캔까지 그 상태를 유지합니다.
    ⚠️ 갱신 사이(예: 30초)에 잠깐 손절선을 찍고 돌아온 경우는 잡지 못할 수 있습니다."""
    by_ex: Dict[str, List[str]] = {}
    for x in setups:
        by_ex.setdefault(x.exchange, []).append(x.symbol)
    prices: Dict = {}
    for ex_id, syms in by_ex.items():
        try:
            for sym, p in _last_prices(ex_id, syms).items():
                prices[(ex_id, sym)] = p
        except Exception as e:
            print(f"[warn] {ex_id} 실시간 가격 조회 실패: {e}")
    updated = 0
    for x in setups:
        p = prices.get((x.exchange, x.symbol))
        if p is None:
            continue
        x.current_price = p
        updated += 1
        if x.live_status in ("invalid", "missed"):
            continue
        x.live_status = live_status_of(x, p)
        x.is_chase = x.live_status == "chase"
    return updated


def utc_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC").tz_localize(None)


def current_candle_start(timeframe: Optional[str] = None) -> pd.Timestamp:
    """지금 진행 중인 봉의 시작 시각(UTC). 거래소 4시간봉은 UTC 00·04·08·12·16·20시에 시작."""
    return utc_now().floor(pd.Timedelta(timeframe or TIMEFRAME))


def next_candle_close(timeframe: Optional[str] = None) -> pd.Timestamp:
    return current_candle_start(timeframe) + pd.Timedelta(timeframe or TIMEFRAME)


def needs_full_rescan(last_scan_utc: Optional[pd.Timestamp], timeframe: Optional[str] = None,
                      grace_sec: int = 60) -> bool:
    """마지막 전체 스캔 뒤에 새 봉이 마감됐으면 True (마감 직후 거래소 반영을 위해 1분 여유)."""
    if last_scan_utc is None:
        return True
    start = current_candle_start(timeframe)
    return last_scan_utc < start and (utc_now() - start).total_seconds() >= grace_sec


def apply_breakout_entry(x: CoinSetup) -> None:
    """박스 돌파 신호를 '봉 마감 직후 현재가(시장가) 진입'으로 바꿈 (기본). 리테스트 지정가는 검증에서 불리했음:
    되돌아와서 체결되는 돌파일수록 실패하는 돌파일 가능성이 높고, 강한 돌파는 되돌아오지 않음."""
    if BREAKOUT_ENTRY != "immediate" or not x.bias.startswith("wait_breakout"):
        return
    long_side = x.bias in LONG_BIASES
    x.entry_price = x.current_price
    if x.level is not None and x.box_h is not None and x.atr:
        e_, edge, a_ = x.entry_price, x.level, x.atr
        # 손익비 상한 판정은 검증한 예전 정의 그대로 (목표 = 경계 ± 박스 높이, 손절 = 경계 ∓ 1 ATR)
        old_sl, old_tp = (edge - a_, edge + x.box_h) if long_side else (edge + a_, edge - x.box_h)
        x.rr_cap = abs(old_tp - e_) / abs(e_ - old_sl) if abs(e_ - old_sl) > 0 else 99.0
        # v27 손절: 경계 너머 0.5 ATR, 진입가에서 최소 1 ATR
        x.sl, x.tp1 = v27_levels("돌파", "long" if long_side else "short", e_, edge, x.box_h, a_)
    risk = (x.entry_price - x.sl) if long_side else (x.sl - x.entry_price)
    if risk <= 0:
        return
    x.rr_ratio = ((x.tp1 - x.entry_price) if long_side else (x.entry_price - x.tp1)) / risk
    x.is_chase, x.live_status = False, "ready"
    x.entry_note = x.entry_note.replace(" 후 리테스트", "").rstrip() + " · 봉 마감 직후 시장가 즉시 진입"


def screen_market(market_regime: RegimeType, progress_cb=None,
                  btc_df: Optional[pd.DataFrame] = None) -> List[CoinSetup]:
    """코인마다 자기 차트의 '완성된 봉'으로 개별 국면과 신호를 계산하고, 현재가로 추격·무효 여부를 판단.
    1차로 모든 코인의 캔들을 받아 알트 지수를 만든 뒤(알트 지수 대비 상대강도용), 2차로 코인별 신호를 계산.
    스캔이 끝나면 LAST_SCAN_STATS에 필터별 제외 개수와 시장 참여도(상승/하락 추세 코인 수)를 남깁니다."""
    _REJECTS.clear()
    _REJ_SYM.clear()
    _CUR_SYM["sym"] = None
    if btc_df is None:
        btc_df, _ = split_live(fetch_btc_df())
    perps = bitget_perp_symbols() if BITGET_ONLY else set()
    if BITGET_ONLY and not perps:
        print("[warn] Bitget 선물 목록을 못 가져와 '선물 거래 가능 여부'를 확인하지 못했어요.")
    universe = build_universe(TOP_N_BY_VOLUME, perps or None)
    fund_ex = "bitget" if perps else None
    breadth = {"uptrend": 0, "downtrend": 0, "sideways": 0, "n": 0}
    items = list(universe.items())
    n_items = len(items)
    rank_of = {sym: k for k, sym in enumerate(universe)}   # 합산 거래량 순위(0부터)

    loaded = []  # 1차: 캔들 받기
    watch: List[Dict] = []
    for idx, (symbol, info) in enumerate(items):
        _CUR_SYM["sym"] = symbol
        if progress_cb:
            progress_cb(idx, 2 * n_items, f"데이터 {symbol}")
        try:
            full = fetch_ohlcv(info["src"], symbol)
            if full is None or len(full) < 61:
                _rej("data_short")
                continue
            df, live = split_live(full)
            loaded.append((symbol, info, df, live))
        except Exception as e:
            _rej("error")
            print(f"[warn] {symbol} 캔들 조회 실패: {e}")
    alt = build_alt_index([d for sym, _, d, _ in loaded if sym.split("/")[0] not in ("BTC", "ETH")]) \
        if len(loaded) >= 5 else None

    setups: List[CoinSetup] = []
    for idx, (symbol, info, df, live) in enumerate(loaded):  # 2차: 신호 계산
        _CUR_SYM["sym"] = symbol
        exchange_id = info["src"]
        if progress_cb:
            progress_cb(n_items + idx, 2 * n_items, symbol)
        try:
            coin_regime = classify_price_trend(df)
            breadth[coin_regime] += 1
            breadth["n"] += 1
            grp = coin_group(symbol)
            if grp == "BTC":          # BTC는 매매 신호 없이 거시 판단에만 사용
                continue
            alt_al = align_to(df, alt)
            # 일봉은 횡보 기울기 계산에 먼저 필요, 추세 신호는 신호가 난 뒤에만 조회(API 절약)
            htf_trend = get_htf_trend(exchange_id, symbol) if coin_regime == "sideways" else None
            setup = build_setup(symbol, exchange_id, df, btc_df, coin_regime, htf_trend or "sideways",
                                current_price=live, alt_df=alt_al)
            if setup and SETUP_FAMILY.get(setup.bias) not in ENABLED_FAMILIES:
                _rej("family_off")
                setup = None
            if setup is None and "신고점 돌파" in ENABLED_FAMILIES and grp == "메이저":
                setup = build_donchian_setup(symbol, exchange_id, df, btc_df, live, alt_al, htf_trend or "sideways")
                if setup is not None and htf_trend is None:  # 표시(일봉 방향 일치) 계산을 위해 신호가 났을 때만 조회
                    htf_trend = get_htf_trend(exchange_id, symbol)
                    setup.tags = breakout_tags(df, "long" if setup.bias in LONG_BIASES else "short",
                                               htf_trend, setup.rs_alt, setup.rs)
            if setup is None and "추적선 전환" in ENABLED_FAMILIES:
                setup = build_supertrend_setup(symbol, exchange_id, df, btc_df, live, alt_al, htf_trend or "sideways")
            # v27 새 매매법: 박스 돌파와 별개로 같은 코인에서 함께 나올 수 있음 (진입은 코인당 하나 — 추적·계좌 모의와 같은 규칙)
            extra: List[CoinSetup] = []
            if "돌파 후 되돌림" in ENABLED_FAMILIES:
                x_ = build_retest_setup(symbol, exchange_id, df, btc_df, live, alt_al)
                if x_:
                    extra.append(x_)
            if "추세 눌림목" in ENABLED_FAMILIES:
                x_ = build_pullback_setup(symbol, exchange_id, df, btc_df, live, alt_al)
                if x_:
                    extra.append(x_)
            if "위치 다이버전스" in ENABLED_FAMILIES:
                x_ = build_divergence_setup(symbol, exchange_id, df, btc_df, live, alt_al)
                if x_:
                    extra.append(x_)
            if "빗각 지지" in ENABLED_FAMILIES:          # v31 🧪 일봉 신호(하루 한 번 일봉을 받아 저장)
                x_ = build_diag_setup(symbol, exchange_id, df, btc_df, live, alt_al)
                if x_:
                    extra.append(x_)
            if "아래꼬리 연속" in ENABLED_FAMILIES:      # v32 🧪 일봉 신호
                x_ = build_wick_setup(symbol, exchange_id, df, btc_df, live, alt_al)
                if x_:
                    extra.append(x_)
            if not setup and coin_regime == "sideways" and "돌파" in ENABLED_FAMILIES:
                wc = watch_candidate(df, htf_trend or "sideways", live)
                if wc:
                    wc.update(symbol=symbol, exchange=exchange_id, perp_symbol=info.get("perp", ""),
                              perp_mult=int(info.get("mult", 1)))
                    watch.append(wc)
            cands_ = ([setup] if setup else []) + extra
            if not cands_:
                continue
            spread, spread_done, fund_ok = None, False, {}
            for setup in cands_:
                fam = SETUP_FAMILY.get(setup.bias)
                if FILTER_TAGS and fam in ("돌파", "신고점 돌파") and not FILTER_TAGS <= set(setup.tags):
                    _rej("tag_filter")
                    continue
                setup.coin_regime = coin_regime
                setup.counter_trend = coin_regime in ("uptrend", "downtrend") and coin_regime != market_regime

                if setup.bias in ("long", "short"):
                    if htf_trend is None:
                        htf_trend = get_htf_trend(exchange_id, symbol)
                    if (setup.bias == "long" and htf_trend == "downtrend") or \
                       (setup.bias == "short" and htf_trend == "uptrend"):
                        _rej("htf_against")
                        print(f"[skip] {symbol}: 일봉 추세와 반대 방향 신호라 제외")
                        continue

                if not spread_done:
                    spread, spread_done = get_spread_pct(exchange_id, symbol), True
                if spread is not None and spread > 0.3:
                    _rej("wide_spread")
                    print(f"[skip] {symbol}: 스프레드 {spread:.2f}% — 슬리피지 위험으로 제외")
                    continue

                side_ = "long" if setup.bias in LONG_BIASES else "short"
                if side_ not in fund_ok:
                    fund_ok[side_] = funding_rate_ok(fund_ex or exchange_id, info.get("perp") or symbol, setup.bias)
                if not fund_ok[side_]:
                    _rej("funding_hot")
                    print(f"[skip] {symbol}: 펀딩비 과열 방향이라 제외 ({setup.bias})")
                    continue

                setup.signal_ts = setup.signal_ts or str(df["ts"].iloc[-1])   # v31: 일봉 신호는 그 일봉 마감 봉
                setup.bitget_perp = True if perps else None
                setup.perp_symbol, setup.perp_mult = info.get("perp", ""), int(info.get("mult", 1))
                setup.group = grp
                setup.vol_tier = vol_tier_label(rank_of.get(symbol, 999))
                setup.vol24h = info.get("vol")
                setups.append(setup)
        except Exception as e:  # 코인 하나의 실패가 전체 스캔을 멈추지 않도록
            _rej("error")
            print(f"[warn] {symbol} 분석 실패: {e}")

    if progress_cb:
        progress_cb(2 * n_items, 2 * n_items, "")

    # 손익비 최소 기준 — 매물대 겹침 또는 유동성 스윕이 있으면 1.3, 없으면 1.5
    # (신고점 돌파는 손절 2 ATR·목표 3 ATR로 고정된 추세 추종 규칙이라 제외 — 연구실 검증 기준 그대로)
    # (v27 새 매매법도 제외 — 손절·목표가 검증한 규칙으로 정해져 있어 손익비 필터 없이 검증했음)
    passed = []
    for x in setups:
        _CUR_SYM["sym"] = x.symbol
        if SETUP_FAMILY.get(x.bias) in ("신고점 돌파", "추적선 전환", "돌파 후 되돌림", "위치 다이버전스", "추세 눌림목", "빗각 지지", "아래꼬리 연속") or \
                x.rr_ratio >= (1.3 if (x.poc_confluence or x.sweep_confluence) else 1.5):
            apply_breakout_entry(x)
            cap_ = x.rr_cap if x.rr_cap is not None else x.rr_ratio
            if x.bias.startswith("wait_breakout") and cap_ >= BOX_RR_MAX:
                _rej("rr_too_far")
                continue
            x.market_entry = x.bias.startswith(("donchian", "retest_", "div_", "pb_", "diag_", "wick_")) or "즉시 진입" in x.entry_note
            passed.append(x)
        else:
            _rej("low_rr")
    passed.sort(key=lambda x: x.rr_ratio, reverse=True)

    # 화면에 내보내기 직전: Bitget 선물 시세로 실제 거래 가능 여부 최종 확인 (상장 폐지·거래 중지 코인 제거)
    perp_verified = None
    if BITGET_ONLY and perps:
        targets = [x.perp_symbol for x in passed] + [w.get("perp_symbol", "") for w in watch]
        ok = verify_bitget_tradable(targets)
        if ok is not None:
            perp_verified = True
            dropped = [x for x in passed if x.perp_symbol and x.perp_symbol not in ok]
            dropped_w = [w for w in watch if w.get("perp_symbol") and w["perp_symbol"] not in ok]
            if dropped or dropped_w:
                _REJECTS["perp_untradable"] = len(dropped) + len(dropped_w)
                print(f"[skip] Bitget 선물 시세 없음(거래 불가): {[x.symbol for x in dropped] + [w['symbol'] for w in dropped_w]}")
            for x in dropped:
                _REJ_SYM.setdefault(x.symbol, []).append("perp_untradable")
            passed = [x for x in passed if x not in dropped]
            watch = [w for w in watch if w not in dropped_w]
            for w in watch:
                w["perp_ok"] = True
        else:
            perp_verified = False
    _CUR_SYM["sym"] = None
    # v29.5: 코인별 결과 (스캔 순위 · 추천/관찰/제외 사유) — PC와 폰 결과를 코인 단위로 비교
    watch_syms = {w["symbol"] for w in watch}
    passed_syms: Dict[str, List[str]] = {}
    for x in passed:
        passed_syms.setdefault(x.symbol, []).append(SETUP_FAMILY.get(x.bias, x.bias))
    coin_status: Dict[str, Dict] = {}
    for k, (sym, info) in enumerate(items):
        if sym in passed_syms:
            res_ = "후보 통과: " + "·".join(passed_syms[sym])
        elif _REJ_SYM.get(sym):
            res_ = "제외: " + "·".join(dict.fromkeys(REJECT_LABELS.get(r, r) for r in _REJ_SYM[sym]))
        elif coin_group(sym) == "BTC":
            res_ = "BTC는 시장 판단에만 사용"
        else:
            res_ = "신호 없음 (이번 봉에 조건을 만족한 매매법 없음)"
        if sym in watch_syms:
            res_ += " · 돌파 임박 관찰 중"
        coin_status[sym] = {"rank": k + 1, "perp": info.get("perp", ""), "status": res_}
    LAST_SCAN_STATS.clear()
    LAST_SCAN_STATS.update({"coin_status": coin_status,
                            "universe_diag": dict(UNIVERSE_DIAG), "rejects": dict(_REJECTS), "breadth": dict(breadth),
                            "universe": n_items, "passed": len(passed), "alt_index": alt is not None,
                            "watchlist": sorted(watch, key=lambda w: w["dist_atr"]),
                            "perp_source": PERPS_SOURCE if BITGET_ONLY else "off", "perp_verified": perp_verified})
    LAST_LOADED.clear()
    LAST_LOADED.update({sym: (info["src"], d) for sym, info, d, _ in loaded})
    summary = ", ".join(f"{REJECT_LABELS.get(k, k)} {v}" for k, v in sorted(_REJECTS.items(), key=lambda kv: -kv[1]))
    print(f"[필터 통과율] 대상 {n_items}개 → 최종 {len(passed)}개 | 제외: {summary or '없음'}")
    return passed


ACTIVE_TRACK = ("대기", "보유")


# ==========================================================================
# 클라우드 영구 저장 (GitHub Gist): Streamlit Cloud는 재시작하면 파일이 지워져서 실전 추적·연구실 설정이 초기화됨
# → 비공개 Gist에 저장했다가 시작할 때 불러옴. 토큰·Gist ID는 Streamlit Secrets에만 둠(코드·GitHub에 올리지 않기)
# ==========================================================================
CLOUD = {"token": None, "gist": None, "enabled": False, "last_sync": None, "last_error": None, "config_error": None,
         "hashes": {}, "remote_hashes": {}, "last_try": 0.0, "restored": None}
_TRACK_RANK = {"대기": 0, "chase": 0, "보유": 1, "종료": 2, "미체결": 2, "확인 불가": 2}
# v30.2: 기록 파일 읽기·합치기·쓰기를 한 번에 하나씩 (여러 화면·기기 동기화가 동시에 돌 때 기록이 덮어써져 사라지던 문제)
_FILE_LOCK = threading.RLock()


def cloud_files() -> List[str]:
    return [TRACK_FILE, LAB_REF_FILE, LAB_CONFIG_FILE, MACRO_VAL_FILE, TRADE_LOG_FILE, HISTORY_FILE, PORT_LOG_FILE,
            LT_LOG_FILE]


def _check_cloud_config(tok: Optional[str], gid: Optional[str]) -> Optional[str]:
    """Secrets 값 검사: 예시 문구가 그대로 남았거나 형식이 틀리면 이유를 돌려줌 (정상이면 None)."""
    if not tok and not gid:
        return None
    if not tok or not gid:
        return "GITHUB_TOKEN과 GIST_ID 두 줄이 모두 필요해요."
    if not tok.isascii() or not gid.isascii():
        return ("토큰이나 Gist ID에 한글이 들어 있어요. 안내의 예시 문구('복사한토큰' 등)가 그대로 남은 것 같아요. "
                "실제 값(영문·숫자)으로 바꿔 주세요.")
    if not (tok.startswith("ghp_") or tok.startswith("github_pat_")) or len(tok) < 30:
        return "토큰 형식이 아니에요. GitHub에서 만든 'ghp_'로 시작하는 토큰 전체를 넣어 주세요."
    if "gist.github.com" in gid or "/" in gid:
        return "GIST_ID에는 주소 전체가 아니라 주소 맨 끝의 영문·숫자 부분만 넣어 주세요."
    if not re.fullmatch(r"[0-9a-fA-F]{20,40}", gid):
        return "Gist ID 형식이 아니에요. Gist 주소 맨 끝의 영문·숫자(보통 32자)를 넣어 주세요."
    return None


def configure_cloud_store(token: Optional[str], gist_id: Optional[str]) -> None:
    tok, gid = (token or "").strip().strip('"').strip("'") or None, (gist_id or "").strip().strip('"').strip("'") or None
    if tok != CLOUD["token"] or gid != CLOUD["gist"]:
        CLOUD.update(token=tok, gist=gid, hashes={}, remote_hashes={}, last_error=None, restored=None)
    CLOUD["config_error"] = _check_cloud_config(tok, gid)
    CLOUD["enabled"] = bool(tok and gid) and CLOUD["config_error"] is None


def _gist_req(method: str, payload: Optional[Dict] = None) -> Dict:
    r = requests.request(method, f"https://api.github.com/gists/{CLOUD['gist']}", json=payload, timeout=20,
                         headers={"Authorization": f"Bearer {CLOUD['token']}", "Accept": "application/vnd.github+json",
                                  "X-GitHub-Api-Version": "2022-11-28"})
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub {r.status_code}: {r.text[:120]}")
    return r.json()


def _gist_files() -> Dict[str, str]:
    out = {}
    for name, f in (_gist_req("GET").get("files") or {}).items():
        content = f.get("content") or ""
        if f.get("truncated") and f.get("raw_url"):
            content = requests.get(f["raw_url"], timeout=20, headers={"Authorization": f"Bearer {CLOUD['token']}"}).text
        out[name] = content
    return out


def _sha(text: str) -> str:
    import hashlib
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _merge_tracks(local_txt: str, remote_txt: str) -> str:
    """두 곳(예: PC 앱·클라우드 앱)의 실전 추적 기록 합치기: 같은 신호는 더 진행된 상태(종료 > 보유 > 대기)를 남김."""
    def _load(t):
        try:
            v = json.loads(t) if t and t.strip() else []
            return v if isinstance(v, list) else []
        except Exception:
            return []
    merged: Dict[str, Dict] = {}
    for t in _load(remote_txt) + _load(local_txt):
        k = t.get("key")
        if k is None:
            continue
        cur = merged.get(k)
        rt, rc = _TRACK_RANK.get(t.get("status"), 0), (_TRACK_RANK.get(cur.get("status"), 0) if cur else -1)
        # 더 진행된 상태가 이김. 같은 단계면 더 최근에 확인한 기록(v29.6 'checked')이 이김 — 없으면 예전처럼 나중 것
        if cur is None or rt > rc or (rt == rc and str(t.get("checked", "")) >= str(cur.get("checked", ""))):
            merged[k] = t
    return json.dumps(sorted(merged.values(), key=lambda t: str(t.get("created", ""))), ensure_ascii=False)


def cloud_restore() -> str:
    """시작할 때 Gist의 기록을 내려받음. 실전 추적은 합치고, 나머지는 이 컴퓨터에 파일이 없을 때만 받음."""
    if not CLOUD["enabled"]:
        return "off"
    locked = False
    try:
        files = _gist_files()
        n = 0
        _FILE_LOCK.acquire()
        locked = True
        for path in cloud_files():
            name = os.path.basename(path)
            remote = files.get(name, "")
            if not remote.strip():
                continue
            CLOUD["remote_hashes"][name] = _sha(remote)
            local = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
            if path == TRACK_FILE and local.strip():
                text = _merge_tracks(local, remote)
            elif path == PORT_LOG_FILE and local.strip():
                text = _merge_port_log(local, remote)
            elif path == LT_LOG_FILE and local.strip():
                text = _merge_lt_log(local, remote)
            elif local.strip():
                CLOUD["hashes"][path] = None          # 이 컴퓨터 파일 유지 → 다음 저장 때 올림
                continue
            else:
                text = remote
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            CLOUD["hashes"][path] = _sha(text) if text == remote else None
            n += 1
        CLOUD["restored"], CLOUD["last_error"] = utc_now(), None
        return f"불러옴 {n}개"
    except Exception as e:
        CLOUD["last_error"] = str(e)[:150]
        return "error"
    finally:
        if locked:
            _FILE_LOCK.release()


def cloud_pull(min_interval: float = 120.0) -> str:
    """v29.6: 다른 기기(PC↔폰)가 올린 실전 추적·포트폴리오 기록을 주기적으로 당겨와 합침.
    (예전엔 앱이 처음 켜질 때 한 번만 받아서, 폰이 PC의 '종료'를 모르고 계속 '보유'로 보여줬음)"""
    if not CLOUD["enabled"]:
        return "off"
    now = time.time()
    if now - CLOUD.get("last_pull", 0.0) < min_interval:
        return "skip"
    CLOUD["last_pull"] = now
    try:
        files = _gist_files()
        n = 0
        for path, fn in ((TRACK_FILE, _merge_tracks), (PORT_LOG_FILE, _merge_port_log), (LT_LOG_FILE, _merge_lt_log)):
            name = os.path.basename(path)
            remote = files.get(name, "")
            if not remote.strip() or CLOUD["remote_hashes"].get(name) == _sha(remote):
                continue
            with _FILE_LOCK:
                local = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
                text = fn(local, remote) if local.strip() else remote
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
            CLOUD["remote_hashes"][name] = _sha(remote)
            CLOUD["hashes"][path] = _sha(text) if text == remote else None   # 합친 결과가 다르면 다음 저장 때 올림
            n += 1
        CLOUD["last_pulled"] = utc_now()
        return f"받음 {n}개"
    except Exception as e:
        CLOUD["last_error"] = str(e)[:150]
        return "error"


def refresh_tracks(min_interval: float = 600.0, force: bool = False) -> str:
    """v29.6: 전체 분석을 기다리지 않고 진행 중인 추적 기록만 다시 계산 (다른 기기 기록 당겨오기 → 계산 → 저장·올리기).
    화면을 열 때마다 부르되 10분에 한 번만 실제로 계산."""
    now = time.time()
    if not force and now - CLOUD.get("last_track_refresh", 0.0) < min_interval:
        return "skip"
    CLOUD["last_track_refresh"] = now
    cloud_pull(0 if force else 120.0)
    tracks = load_tracks()
    if not any(t["status"] in ACTIVE_TRACK for t in tracks):
        return "none"
    update_tracks(tracks, LAST_LOADED)
    _save_tracks(tracks)
    cloud_sync(force=True)
    return "updated"


def cloud_sync(force: bool = False, min_interval: float = 60.0) -> str:
    """바뀐 기록 파일만 Gist에 올림(최대 1분에 한 번). 실전 추적은 올리기 전에 Gist 쪽 기록과 합침."""
    if not CLOUD["enabled"]:
        return "off"
    now = time.time()
    if not force and now - CLOUD["last_try"] < min_interval:
        return "skip"
    CLOUD["last_try"] = now
    changed = {}
    for path in cloud_files():
        if os.path.exists(path):
            text = open(path, encoding="utf-8").read()
            if text.strip() and CLOUD["hashes"].get(path) != _sha(text):
                changed[path] = text
    if not changed:
        return "same"
    try:
        merge_fns = {TRACK_FILE: _merge_tracks, PORT_LOG_FILE: _merge_port_log, LT_LOG_FILE: _merge_lt_log}
        if any(p in changed for p in merge_fns):
            gfiles = _gist_files()
            for path, fn in merge_fns.items():
                if path not in changed:
                    continue
                tname = os.path.basename(path)
                remote = gfiles.get(tname, "")
                if remote.strip():
                    # v30.2: Gist가 '마지막으로 본 것과 같아 보여도' 항상 합침 — 이 컴퓨터 파일이 그사이 오래된 목록으로
                    # 덮어써졌으면, 합치지 않고 올릴 때 다른 기기 기록이 Gist에서 지워졌음 (합치기는 몇 번 해도 결과가 같음)
                    with _FILE_LOCK:
                        cur = open(path, encoding="utf-8").read() if os.path.exists(path) else changed[path]
                        merged = fn(cur, remote)
                        with open(path, "w", encoding="utf-8") as f:
                            f.write(merged)
                    changed[path] = merged
        _gist_req("PATCH", {"files": {os.path.basename(p): {"content": t} for p, t in changed.items()}})
        for p, t in changed.items():
            CLOUD["hashes"][p] = _sha(t)
            CLOUD["remote_hashes"][os.path.basename(p)] = _sha(t)
        CLOUD["last_sync"], CLOUD["last_error"] = utc_now(), None
        return f"저장 {len(changed)}개"
    except Exception as e:
        CLOUD["last_error"] = str(e)[:150]
        return "error"


def load_tracks() -> List[Dict]:
    """실전 추적 기록 읽기. 다른 버전·다른 기기에서 합쳐진 기록에 항목이 빠져 있어도 앱이 멈추지 않게 정리."""
    try:
        with open(TRACK_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return []
    out = []
    for t in raw if isinstance(raw, list) else []:
        # 가격·시각이 없는 기록은 성과를 계산할 수 없어 버림 (깨진 기록·형식이 다른 기록)
        if not isinstance(t, dict) or any(t.get(k) in (None, "") for k in ("key", "symbol", "signal_ts", "entry", "sl", "tp1")):
            continue
        t.setdefault("bias", "wait_breakout_long" if t.get("direction") == "long" else "wait_breakout_short")
        t.setdefault("family", SETUP_FAMILY.get(t["bias"], "돌파"))
        t.setdefault("direction", "long" if t["bias"] in LONG_BIASES else "short")
        for k, v in (("status", "대기"), ("R", None), ("created", ""), ("exchange", "bitget"), ("atr", None),
                     ("is_chase", False), ("market", False), ("tags", []), ("tier", "")):
            t.setdefault(k, v)
        t.setdefault("group", coin_group(t["symbol"]))
        out.append(t)
    return out


def _save_tracks(tracks: List[Dict]) -> None:
    """v30.2: 파일에 있는 기록과 합쳐서 저장 — 분석이 도는 사이 다른 화면·기기가 더한 기록을 지우지 않게.
    같은 신호는 더 진행된 상태(종료 > 보유 > 대기)·더 최근에 확인한 쪽이 남음. 기록을 지우는 기능은 없음."""
    try:
        with _FILE_LOCK:
            mine = json.dumps(tracks, ensure_ascii=False, default=str)
            cur = open(TRACK_FILE, encoding="utf-8").read() if os.path.exists(TRACK_FILE) else ""
            text = _merge_tracks(mine, cur) if cur.strip() else mine
            merged = json.loads(text)
            with open(TRACK_FILE, "w", encoding="utf-8") as f:
                json.dump(merged[-600:], f, ensure_ascii=False, default=str)
    except Exception as e:
        print(f"[warn] 추적 기록 저장 실패: {e}")


def track_new_setups(setups: List[CoinSetup], tracks: List[Dict]) -> int:
    """새 추천을 추적 기록에 추가 (같은 신호는 한 번만, 같은 코인·방향이 진행 중이면 추가 안 함)."""
    keys = {t["key"] for t in tracks}
    # v32.5: 방향과 상관없이 코인당 한 포지션 (5년 검증·계좌 모의와 같은 규칙). 예전엔 같은 방향만 막아서
    # 롱 진행 중에 같은 코인 숏 추천이 나오면 둘 다 기록됐음(양방향 동시 보유)
    active = {t["symbol"] for t in tracks if t["status"] in ACTIVE_TRACK}
    added = 0
    for x in setups:
        if not x.signal_ts:
            continue
        direction = "long" if x.bias in LONG_BIASES else "short"
        key = f"{x.symbol}|{x.bias}|{x.signal_ts}"
        if key in keys or x.symbol in active:
            continue
        tracks.append({"key": key, "symbol": x.symbol, "exchange": x.exchange, "bias": x.bias,
                       "family": SETUP_FAMILY.get(x.bias, x.bias), "direction": direction,
                       "signal_ts": x.signal_ts, "entry": x.entry_price, "sl": x.sl, "tp1": x.tp1,
                       "atr": x.atr, "is_chase": bool(x.is_chase), "market": bool(x.market_entry),
                       "tags": list(x.tags), "created": str(utc_now()), "status": "대기", "R": None,
                       "group": x.group or coin_group(x.symbol), "tier": x.vol_tier or "",
                       # v27: 이 추천에 적용한 청산 규칙 (예전 기록에는 없음 → 예전 규칙으로 계산)
                       "frac": x.exit_frac, "trail": x.exit_trail, "hold": x.max_hold,
                       "perp": x.perp_symbol or "", "mult": int(x.perp_mult or 1),
                       "risk_mult": float(getattr(x, "risk_mult", 1.0) or 1.0)})
        active.add(x.symbol)
        added += 1
    return added


def update_tracks(tracks: List[Dict], loaded: Dict[str, tuple]) -> None:
    """진행 중인 추적 기록의 결과를 실제 완성봉으로 다시 계산 (과거 검증과 같은 체결·청산 규칙).
    '추천대로 모두 진입했다면'을 가정한 결과라 실제 체결과는 조금 다를 수 있음."""
    for t in tracks:
        if t["status"] not in ACTIVE_TRACK:
            continue
        try:
            got = loaded.get(t["symbol"])
            df = got[1] if got and got[0] == "bitget" else None
            if df is None and ANALYZE_ON_PERP:
                # v29.6: 추천 당시 거래소(OKX·Binance 현물)와 상관없이 Bitget 선물로 계산 → PC·폰 결과가 같음
                # (폰 서버는 Binance가 막혀 예전 기록을 계산 못 하고 '보유'에 멈춰 있었음)
                df = drop_unclosed(fetch_ohlcv("bitget", t["symbol"]))
            if (df is None or df.empty) and got:
                df = got[1]
            if df is None or df.empty:
                df = drop_unclosed(fetch_ohlcv(t.get("exchange") or "bitget", t["symbol"]))
            # v30.2: 오래도록 계산할 수 없는 기록(상장 폐지·이름 변경·차트 범위 밖)은 '확인 불가'로 끝냄.
            # 예전엔 '대기/보유'에 영원히 남아, 같은 코인·같은 방향의 새 추천이 추적에 안 들어갔음
            age_bars = (utc_now() - pd.Timestamp(t["signal_ts"])) / pd.Timedelta(TIMEFRAME)
            give_up = age_bars > int(t.get("hold") or 60) + 30
            if df is None or df.empty:
                if give_up:
                    t.update(status="확인 불가", reason="차트를 받을 수 없음(상장 폐지·거래 중지 등)", checked=str(utc_now()))
                continue
            df = df.reset_index(drop=True)
            hit = df.index[df["ts"] == pd.Timestamp(t["signal_ts"])]
            if len(hit) == 0:
                if give_up:
                    t.update(status="확인 불가", reason="신호 봉을 차트에서 찾을 수 없음", checked=str(utc_now()))
                continue
            idx = int(hit[0])
            sig = {idx: {"direction": t["direction"], "bias": t["bias"], "entry": t["entry"], "sl": t["sl"],
                         "tp1": t["tp1"], "atr": t["atr"], "is_chase": t["is_chase"], "market": t["market"],
                         "tags": t.get("tags", []), "frac": t.get("frac"), "trail": t.get("trail"), "hold": t.get("hold")}}
            st_: Dict = {}
            tr = simulate_exits(df.iloc[:], sig, "partial_trail", entry_mode="retest", open_state=st_)
            t["checked"] = str(utc_now())
            if tr:
                x = tr[0]
                t.update(status="종료", R=round(x["R"], 3), gross_R=round(x["gross_R"], 3), reason=x["reason"],
                         exit_ts=str(df["ts"].iloc[x["exit_idx"]]))
                try:  # v32.7 불타기(목표1 0.5배) 결과: 목표1에 닿았으면 추가분 R ≈ 0.5 × (본 거래 R − 목표1까지 R), 처음 위험 기준
                    long_t = t["direction"] == "long"; ex_ = int(x["exit_idx"])
                    seg_ = (df["high"] if long_t else df["low"]).to_numpy(float)[idx + 1:ex_ + 1]
                    tp1_R = abs(float(t["tp1"]) - float(t["entry"])) / abs(float(t["entry"]) - float(t["sl"]))
                    hit_ = bool(len(seg_) and (((seg_ >= float(t["tp1"])) if long_t else (seg_ <= float(t["tp1"]))).any()))
                    t.update(pyr_hit=hit_, pyr_R=round(PYRAMID_SIZE * (x["R"] - tp1_R), 3) if hit_ else 0.0)
                except Exception:
                    pass
                t.pop("live", None)
                continue
            if st_:
                if not t.get("mult") and _LAST_PERPS:      # 예전 기록: 선물 배수(1000PEPE 등) 채워서 선물 가격으로 보여주기
                    p_, m_ = perp_match(t["symbol"], _LAST_PERPS)
                    t["perp"], t["mult"] = p_ or "", int(m_ or 1)
                t["live"] = _live_state(st_, df, t)
            after = df.iloc[idx + 1:idx + 16]
            long_side = t["direction"] == "long"
            if (t["market"] or not t["is_chase"]) and not st_:
                # v30.2: 진입 시점 가격이 이미 손절선 너머·목표 너머라 규칙상 진입하지 않은 경우(예전엔 '보유'로 멈춰 있었음)
                t.update(status="미체결", reason="진입 조건 불충족(신호 봉 종가가 손절·목표 밖)")
            elif t["market"] or not t["is_chase"]:
                t["status"] = "보유"
            elif len(after) and ((after["low"] <= t["entry"]).any() if long_side else (after["high"] >= t["entry"]).any()):
                t["status"] = "보유"
            elif len(after) and ((after["close"] < t["sl"]).any() if long_side else (after["close"] > t["sl"]).any()):
                t["status"] = "미체결"
            elif len(df) - 1 - idx >= 15:
                t["status"] = "미체결"
        except Exception as e:
            print(f"[warn] 추적 갱신 실패 {t.get('symbol')}: {e}")


def find_recent_signals(symbol: str, lookback: int = 18) -> List[Dict]:
    """v30.2: 추천받아 들어갔는데 실전 추적에 안 보일 때 — 최근 lookback봉(4시간봉 18개 = 3일) 안에서
    지금 켜둔 매매법 규칙으로 나왔던 신호를 Bitget 선물 차트로 다시 찾음. 최신순.
    (그때의 거시 필터·동시 추천 개수 제한·스프레드·펀딩비 걸러내기는 다시 확인할 수 없어서 빼고 찾음 — 사용자가 고름)"""
    sym = symbol.strip().upper()
    sym = sym if "/" in sym else f"{sym}/{QUOTE}"
    sym = sym.split(":")[0]
    df = drop_unclosed(fetch_ohlcv("bitget", sym))
    if df is None or len(df) < 120:
        return []
    df = df.reset_index(drop=True)
    n = len(df)
    lo = max(n - lookback, 60)
    found = []
    reg = trend_series(df)
    for fam in ("돌파 후 되돌림", "추세 눌림목"):
        if fam in ENABLED_FAMILIES:
            found += [(fam, i, s) for i, s in v27_family_signals(df, fam, reg).items() if i >= lo]
    if "돌파" in ENABLED_FAMILIES:
        try:
            bt = drop_unclosed(fetch_ohlcv("bitget", f"BTC/{QUOTE}"))
            bt = bt.set_index("ts").reindex(df["ts"]).ffill().bfill().reset_index()
            for i, s in generate_signals(df, bt, None, warmup=lo, families={"돌파"}).items():
                if s.get("rr_cap", 0.0) < BOX_RR_MAX:
                    found.append(("돌파", i, s))
        except Exception as e:
            print(f"[warn] 박스 돌파 신호 다시 찾기 실패 {sym}: {e}")
    perps = _LAST_PERPS or set()
    p_, m_ = perp_match(sym, perps) if perps else ("", 1)
    out = []
    for fam, i, s in sorted(found, key=lambda z: -z[1]):
        ts = pd.Timestamp(df["ts"].iloc[i])
        xr = EXIT_RULES.get(fam, {})
        out.append({"symbol": sym, "family": fam, "bias": s["bias"], "direction": s["direction"], "signal_ts": str(ts),
                    "close_kst": f"{ts + pd.Timedelta(TIMEFRAME) + pd.Timedelta(hours=9):%m-%d %H:%M}",
                    "entry": float(df["close"].iloc[i]), "sl": float(s["sl"]), "tp1": float(s["tp1"]), "atr": float(s["atr"]),
                    "frac": s.get("frac", xr.get("frac")), "trail": s.get("trail", xr.get("trail")), "hold": s.get("hold", xr.get("hold")),
                    "perp": p_ or "", "mult": int(m_ or 1)})
    return out


def add_recovered_track(sig: Dict) -> str:
    """v30.2: find_recent_signals로 찾은 신호를 실전 추적에 넣음(같은 신호가 이미 있으면 그대로). 바로 계산·저장·클라우드 올림."""
    tracks = load_tracks()
    key = f"{sig['symbol']}|{sig['bias']}|{sig['signal_ts']}"
    if any(t["key"] == key for t in tracks):
        return "이미 추적 중인 신호예요"
    rec = {"key": key, "symbol": sig["symbol"], "exchange": "bitget", "bias": sig["bias"], "family": sig["family"],
           "direction": sig["direction"], "signal_ts": sig["signal_ts"], "entry": sig["entry"], "sl": sig["sl"],
           "tp1": sig["tp1"], "atr": sig["atr"], "is_chase": False, "market": True, "tags": ["직접 추가"],
           "created": str(utc_now()), "status": "대기", "R": None, "group": coin_group(sig["symbol"]), "tier": "",
           "frac": sig.get("frac"), "trail": sig.get("trail"), "hold": sig.get("hold"),
           "perp": sig.get("perp", ""), "mult": int(sig.get("mult") or 1), "risk_mult": 1.0, "recovered": True}
    update_tracks([rec], {})
    _save_tracks([rec])
    cloud_sync(force=True)
    return f"추가했어요 · 상태 {rec['status']}"


# v32.7 사용자 결정(2026-10-28): 불타기 = 목표1에 처음 닿을 때 처음 수량의 0.5배 추가, 추가분도 같은 추적 손절로 함께 정리.
#   검증: 같은 최대 낙폭 연수익 전 신호 6/6 · 수동 6/6(두 번 독립 통과, 거래당 +0.43 → +0.51R). 같은 개수를 무작위 시점에 넣으면 3/6 · 1/6.
#   예전 '+1R 종가에 0.5% 추가'는 5/6 · 5/6이라 미통과 → 바꿈.
PYRAMID_SIZE = 0.5


def _live_state(st_: Dict, df: pd.DataFrame, t: Dict) -> Dict:
    """보유 중 포지션의 지금 상태: 실제 진입가·지금 손절선(본전·추적 반영)·다음 봉 손절선·불타기 조건·남은 봉 수."""
    long_ = st_["direction"] == "long"
    d = 1 if long_ else -1
    e, sl, atr = float(st_["entry"]), float(st_["sl"]), float(st_["atr"] or 0)
    risk = abs(e - sl)
    trail = float(st_.get("trail") or TRAIL_ATR)
    hmax = int(st_.get("hold") or 60)
    i0, last = int(st_["entry_idx"]), int(st_["last_idx"])
    closes = df["close"].to_numpy(float)
    if st_["stage"] == 0:
        stop_now = sl
        nxt = sl
    else:
        best = float(st_["best"])
        stop_now = float(st_["stop"])
        nxt = max(stop_now, best - trail * atr) if long_ else min(stop_now, best + trail * atr)
    lvl = float(st_["tp1"])                             # v32.7: 불타기 = 목표1에 처음 닿을 때
    ext = (df["high"] if long_ else df["low"]).to_numpy(float)[i0 + 1:last + 1]
    hit = np.flatnonzero((ext - lvl) * d >= 0) if len(ext) else np.array([], int)
    pyr = {"level": lvl, "size": PYRAMID_SIZE, "first_idx": int(i0 + 1 + hit[0]) if len(hit) else None}
    if pyr["first_idx"] is not None:
        pyr["first_ts"] = str(df["ts"].iloc[pyr["first_idx"]])
        pyr["now"] = pyr["first_idx"] == last          # 방금 마감한 봉에서 처음 닿음
        pyr["add_stop"] = max(e, lvl - trail * atr) if long_ else min(e, lvl + trail * atr)
    return {"entry": e, "entry_ts": str(df["ts"].iloc[i0]), "stage": int(st_["stage"]), "stop_now": stop_now,
            "stop_next": nxt, "sl": sl, "tp1": float(st_["tp1"]), "risk": risk, "trail": trail, "atr": atr,
            "held": last - i0, "hmax": hmax, "last_close": float(closes[last]),
            "last_ts": str(df["ts"].iloc[last]), "R_now": d * (float(closes[last]) - e) / risk if risk else None,
            "pyramid": pyr}


def tracking_summary(tracks: List[Dict], ref: Optional[Dict] = None) -> tuple:
    """신호 유형별 실전 추적 요약 + 과거 검증 기준선과 비교."""
    rows, notes = [], []
    health = family_health(tracks, ref)
    for fam in sorted({t["family"] for t in tracks}):
        v = [t for t in tracks if t["family"] == fam]
        closed = [t["R"] for t in v if t["status"] == "종료" and t.get("R") is not None][-30:]
        row = {"신호 유형": fam, "종료": len(closed), "보유": sum(t["status"] == "보유" for t in v),
               "대기": sum(t["status"] == "대기" for t in v), "미체결": sum(t["status"] == "미체결" for t in v),
               "승률": f"{np.mean([r > 0 for r in closed]):.0%}" if closed else "-",
               "평균 R": round(float(np.mean(closed)), 2) if closed else None,
               "합계 R": round(float(np.sum(closed)), 1) if closed else None}
        h = health.get(fam, {})
        row["자동 방어"] = HEALTH_LABEL.get(h.get("state", "unknown"), "-")
        if fam in TRIAL_FAMILIES:
            up_ = (f"✅ 승격(위험 {TRIAL_PROMOTED_RISK[fam]:g}배)" if fam in TRIAL_PROMOTED_RISK else "✅ 승격(정상 리스크)")
            row["단계"] = up_ if family_promoted(tracks, ref, fam) else f"🧪 시험 운용(리스크 ¼, {len(closed)}/30건)"
        elif fam in FIXED_FAMILY_RISK:
            row["단계"] = f"📉 위험 {FIXED_FAMILY_RISK[fam]:g}배 고정(5년 검증)"
        elif fam in NEW_FAMILY_RISK:
            row["단계"] = "✅ 정상 리스크" if family_promoted(tracks, ref, fam) else f"🌱 위험 절반으로 시작({len(closed)}/30건)"
        rows.append(row)
        if h:
            exp = f", 과거 검증 예상 {h['expected']:+.2f}R" if h.get("expected") is not None else ""
            notes.append(f"{HEALTH_LABEL.get(h['state'], '')} {fam}: {h['reason']} ({h['basis']}{exp})")
    by_grp = {}
    for t in tracks:
        if t["status"] == "종료" and t.get("R") is not None:
            by_grp.setdefault(t.get("group") or coin_group(t["symbol"]), []).append(t["R"])
    pyr_ = [t for t in tracks if t["status"] == "종료" and "pyr_hit" in t]
    if pyr_:
        hits_ = [t for t in pyr_ if t.get("pyr_hit")]
        notes.append(f"➕ 불타기(목표1 0.5배, v32.7) 실전 계산: 종료 {len(pyr_)}건 중 목표1 도달 {len(hits_)}건 · "
                     f"추가분 합계 {sum(t.get('pyr_R') or 0 for t in hits_):+.2f}R (처음 위험 기준, 추천대로 모두 넣었다고 가정)")
    if by_grp:
        notes.append("그룹별 종료 거래: " + " · ".join(f"{g} {len(v)}건 평균 {np.mean(v):+.2f}R" for g, v in sorted(by_grp.items())))
    by_tier = {}
    for t in tracks:
        if t["status"] == "종료" and t.get("R") is not None and t.get("tier"):
            by_tier.setdefault(t["tier"], []).append(t["R"])
    if by_tier:
        notes.append("거래량 구간별 종료 거래: " + " · ".join(
            f"{k.replace('거래량 ', '')} {len(v)}건 평균 {np.mean(v):+.2f}R" for k, v in sorted(by_tier.items()))
            + " (구간마다 20건 이상 쌓이면 스캔 범위 판단에 활용)")
    return rows, notes


def lab_reference_from(lab: Dict) -> Dict:
    """연구실 결과에서 자동 방어 기준선(진입 방식별 거래당 R의 평균·표준편차)을 뽑음."""
    ref = {"made_at": str(utc_now()), "version": APP_VERSION, "timeframe": lab.get("timeframe"),
           "per_coin_month": lab.get("per_coin_month", {})}
    var = (lab.get("config") or {}).get("donchian_variant", "atr")
    don_key = next((k for k, v in DON_KEYS.items() if v == var), "donchian")
    for key, src in (("base", "base"), ("immediate", "immediate"), ("donchian", don_key), ("supertrend", "supertrend"),
                     ("retest", "retest"), ("pb", "pb"), ("div", "div")):
        r = [x["R"] for x in lab["trades"].get(src, [])]
        if len(r) >= 20:
            ref[key] = {"mu": float(np.mean(r)), "sd": float(np.std(r)), "n": len(r), "R": [round(x, 4) for x in r[-1000:]]}
    return ref


def save_lab_reference(ref: Dict) -> None:
    try:
        with open(LAB_REF_FILE, "w", encoding="utf-8") as f:
            json.dump(ref, f, ensure_ascii=False)
    except Exception as e:
        print(f"[warn] 기준선 저장 실패: {e}")


def load_lab_reference() -> Optional[Dict]:
    try:
        with open(LAB_REF_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _family_ref(ref: Optional[Dict], fam: str):
    if fam in STATIC_FAMILY_REF and not (ref or {}).get(fam):   # v31: 연구실이 계산하지 않는 유형은 5년 검증 기준선
        return STATIC_FAMILY_REF[fam]
    if not ref:
        return None
    key = {"돌파": "base", "신고점 돌파": "donchian",
           "추적선 전환": "supertrend", "돌파 후 되돌림": "retest", "위치 다이버전스": "div", "추세 눌림목": "pb"}.get(fam)
    v = ref.get(key) if key else None
    return (v["mu"], v["sd"]) if v else None


def family_promoted(tracks: List[Dict], ref: Optional[Dict], fam: str) -> bool:
    """시험 운용 승격: 실전 추적 종료 30건 이상, 최근 30건 평균이 플러스이면서 기준선 정상 범위 안."""
    closed = [t["R"] for t in tracks if t["family"] == fam and t["status"] == "종료" and t.get("R") is not None]
    if len(closed) < 30:
        return False
    m = float(np.mean(closed[-30:]))
    mr = _family_ref(ref, fam)
    return m > 0 and (mr is None or m >= mr[0] - 2 * mr[1] / np.sqrt(30))


HEALTH_LABEL = {"normal": "✅ 정상", "caution": "⚠️ 주의 · 리스크 절반", "paused": "⏸ 자동 중지", "unknown": "ⓘ 판단 전"}


def family_health(tracks: List[Dict], ref: Optional[Dict] = None) -> Dict[str, Dict]:
    """신호 유형별 자동 방어 상태 (최근 종료된 실전 추적 결과 기준).
    - 주의: 최근 15건 평균이 기준선 정상 범위(평균 − 2×표준오차) 아래 → 권장 리스크 절반
    - 자동 중지: 최근 30건 평균까지 정상 범위 아래 → 진입 대상에서 제외(참고용 표시, 추적은 계속)
    - 기준선(연구실 결과)이 없으면 최근 평균 −0.3R 미만을 기준으로 씀
    - 성과가 범위 안으로 돌아오면 자동 복귀. 좋아졌다고 리스크를 올리는 일은 없음"""
    out: Dict[str, Dict] = {}
    for fam in sorted({t["family"] for t in tracks}):
        closed = [t["R"] for t in tracks if t["family"] == fam and t["status"] == "종료" and t.get("R") is not None]
        mr = _family_ref(ref, fam)

        def low_line(k: int) -> float:
            return mr[0] - 2 * mr[1] / np.sqrt(k) if mr else FALLBACK_LOW_R

        info = {"n": len(closed), "expected": mr[0] if mr else None, "basis": "연구실 기준선" if mr else "고정 기준(−0.3R)"}
        if len(closed) >= 30 and np.mean(closed[-30:]) < low_line(30):
            state, avg, line = "paused", float(np.mean(closed[-30:])), low_line(30)
        elif len(closed) >= 15 and np.mean(closed[-15:]) < low_line(15):
            state, avg, line = "caution", float(np.mean(closed[-15:])), low_line(15)
        elif len(closed) >= 15:
            state, avg, line = "normal", float(np.mean(closed[-15:])), low_line(15)
        else:
            state, avg, line = "unknown", float(np.mean(closed)) if closed else None, None
        if not AUTO_DEFENSE and state in ("paused", "caution"):
            state = "normal"
        info.update(state=state, avg=avg, line=line)
        if state == "paused":
            info["reason"] = f"최근 30건 평균 {avg:+.2f}R < 기준 {line:+.2f}R — 진입 대상에서 자동 제외(추적은 계속)"
        elif state == "caution":
            info["reason"] = f"최근 15건 평균 {avg:+.2f}R < 기준 {line:+.2f}R — 권장 리스크 자동 절반"
        elif state == "normal":
            info["reason"] = f"최근 15건 평균 {avg:+.2f}R (기준 {line:+.2f}R 이상)"
        else:
            info["reason"] = f"종료된 추천 {len(closed)}건 — 15건부터 판단"
        out[fam] = info
    return out


def run_analysis(risk_cfg: Optional[RiskConfig] = None, progress_cb=None) -> Dict:
    """전체 파이프라인: 시장 데이터 수집 → (임시 국면) → 코인 스캔 → 시장 참여도까지 넣어 국면 최종 확정.
    (웹 화면(app.py)이 사용. main()은 같은 내용을 콘솔에 출력하는 버전)"""
    if risk_cfg is None:
        risk_cfg = RiskConfig(account_balance=1000, risk_per_trade_pct=1.0, max_concurrent_setups=10)
    inputs = collect_market_inputs()
    prelim = compose_market_regime(inputs, None, commit=False)
    breaker = circuit_breaker_triggered(risk_cfg)
    all_setups: List[CoinSetup] = []
    stats: Dict = {}
    if not breaker:
        all_setups = screen_market(prelim.overall, progress_cb, btc_df=inputs["btc_df"])
        stats = dict(LAST_SCAN_STATS)
    extras: Dict = {}
    try:
        extras = macro_extras_from_loaded(LAST_LOADED, inputs["btc_df"]) if LAST_LOADED else {}
    except Exception as e:
        print(f"[warn] 거시 지수 계산 실패: {e}")
    regime = compose_market_regime(inputs, stats.get("breadth"), commit=True, extras=extras)
    hist_scores = (extras.get("hist") or {}).get("score") or []
    macro_now = hist_scores[-1] if hist_scores else None
    macro_info = {"score": macro_now, "blocked_side": None, "excluded": 0}
    pre_macro = list(all_setups)
    if MACRO_FILTER and macro_now is not None and abs(macro_now) >= 0.25:
        blocked_long = macro_now <= -0.25
        macro_info["blocked_side"] = "롱" if blocked_long else "숏"
        keep = [x for x in all_setups if (x.bias in LONG_BIASES) != blocked_long]
        macro_info["excluded"] = len(all_setups) - len(keep)
        if macro_info["excluded"]:
            _REJECTS["macro_against"] = _REJECTS.get("macro_against", 0) + macro_info["excluded"]
            if stats.get("rejects") is not None:
                stats["rejects"]["macro_against"] = macro_info["excluded"]
        all_setups = keep
    for x in all_setups:  # 최종 국면 기준으로 역행 표시 다시 계산
        x.counter_trend = x.coin_regime in ("uptrend", "downtrend") and x.coin_regime != regime.overall
    health: Dict[str, Dict] = {}
    tracks: List[Dict] = []
    try:  # 실전 추적: 다른 기기 기록 당겨와 합치기 → 진행 중인 기록 결과 갱신 → 자동 방어 상태 계산
        cloud_pull(0)
        tracks = load_tracks()
        update_tracks(tracks, LAST_LOADED)
        health = family_health(tracks, load_lab_reference())
    except Exception as e:
        print(f"[warn] 실전 추적 갱신 실패: {e}")
    ref_ = load_lab_reference()
    promoted = {fam: family_promoted(tracks, ref_, fam) for fam in set(TRIAL_FAMILIES) | set(NEW_FAMILY_RISK)}
    chop_info = None
    try:  # v32.6 시장 출렁임 지수(위험 ½ 규칙)
        chop_info = market_chop_info(LAST_LOADED)
    except Exception as e:
        print(f"[warn] 시장 출렁임 계산 실패: {e}")
    # v30.2: 같은 코인·방향 기록이 진행 중이면 새 추천은 추적에 안 들어감(코인당 한 포지션) → 카드에 알려줌
    act_ = {(t["symbol"], t["direction"]): t for t in tracks if t["status"] in ACTIVE_TRACK}
    for x in all_setups:
        prev = act_.get((x.symbol, "long" if x.bias in LONG_BIASES else "short"))
        x.already_tracked = (f"{prev['family']} · 신호 봉 마감 "
                             f"{pd.Timestamp(prev['signal_ts']) + pd.Timedelta(TIMEFRAME) + pd.Timedelta(hours=9):%m-%d %H:%M}"
                             if prev and prev["key"] != f"{x.symbol}|{x.bias}|{x.signal_ts}" else "")
        # v32.5: 반대 방향 기록이 진행 중이면 알려줌 — 검증 규칙(코인당 한 포지션)상 새 추천은 건너뜀
        opp = act_.get((x.symbol, "short" if x.bias in LONG_BIASES else "long"))
        x.opposite_tracked = (f"{'롱' if opp['direction'] == 'long' else '숏'} · {opp['family']} · 신호 봉 마감 "
                              f"{pd.Timestamp(opp['signal_ts']) + pd.Timedelta(TIMEFRAME) + pd.Timedelta(hours=9):%m-%d %H:%M}"
                              if opp else "")
    for x in all_setups:
        fam = SETUP_FAMILY.get(x.bias, "")
        st_ = health.get(fam, {}).get("state", "")
        x.health = st_ if st_ in ("caution", "paused") else ""
        x.risk_mult = 0.5 if x.health == "caution" else 1.0
        if fam in TRIAL_FAMILIES and not promoted.get(fam):
            x.trial = True
            x.risk_mult *= TRIAL_RISK_MULT
        elif fam in TRIAL_FAMILIES and fam in TRIAL_PROMOTED_RISK:   # v31: 승격해도 검증한 비중(0.5배)까지만
            x.fixed_risk = TRIAL_PROMOTED_RISK[fam]
            x.risk_mult *= TRIAL_PROMOTED_RISK[fam]
        if fam in FIXED_FAMILY_RISK:                           # v30.3: 승격 없는 고정 비중
            x.fixed_risk = FIXED_FAMILY_RISK[fam]
            x.risk_mult *= FIXED_FAMILY_RISK[fam]
        elif fam in NEW_FAMILY_RISK and not promoted.get(fam):   # v27 새 매매법: 실전 30건 확인 전까지 위험 절반
            x.half_start = True
            x.risk_mult *= NEW_FAMILY_RISK[fam]
        cm_ = calendar_risk_mult(x)                            # v32.1: 일요일 진입 위험 ½(사용자 결정)
        if cm_ != 1.0:
            x.sunday_half = True
            x.risk_mult *= cm_
        chm_, chv_ = chop_risk_mult(x, chop_info)              # v32.6: 시장 출렁임 52 초과면 위험 ½(사용자 결정)
        x.chop_value = chv_
        if chm_ != 1.0:
            x.chop_half = True
            x.risk_mult *= chm_
    active = [x for x in all_setups if x.health != "paused"]
    paused = [x for x in all_setups if x.health == "paused"]
    setups = cap_correlated_exposure(active, risk_cfg) if active else []
    # v29.5: 코인별 최종 결과 + 비교용 지문 (PC·폰에서 결과가 다를 때 어디서 갈렸는지 바로 보이게)
    cs = stats.get("coin_status") or {}
    fin_ids = {id(x) for x in setups}
    kept_ids = {id(x) for x in all_setups}
    for x in pre_macro:
        d = cs.get(x.symbol)
        if d is None:
            continue
        fam = SETUP_FAMILY.get(x.bias, x.bias)
        if id(x) in fin_ids:
            tag = f"✅ 추천 ({fam})"
        elif id(x) not in kept_ids:
            tag = f"거시 흐름과 반대라 제외 ({fam})"
        elif x.health == "paused":
            tag = f"자동 방어로 중지된 유형 ({fam})"
        else:
            tag = f"같은 방향 동시 추천 개수 제한으로 빠짐 ({fam})"
        d["final"] = (d.get("final", "") + " · " if d.get("final") else "") + tag
    paused_f = sorted({SETUP_FAMILY.get(x.bias, "") for x in paused} | {f for f, h in health.items() if h.get("state") == "paused"})
    stats["fingerprint"] = {
        "버전": APP_VERSION, "봉": TIMEFRAME,
        "기준 봉 마감(한국)": f"{utc_now().floor(TIMEFRAME) + pd.Timedelta(hours=9):%m-%d %H:%M}",
        "코인": f"{stats.get('universe', 0)}개 · {(stats.get('universe_diag') or {}).get('순위 기준', '-')}",
        "신호 유형": "·".join(f for f in ("돌파", "돌파 후 되돌림", "추세 눌림목", "빗각 지지", "아래꼬리 연속", "위치 다이버전스", "신고점 돌파", "추적선 전환")
                           if f in ENABLED_FAMILIES),
        "거시 필터": ("켬" if MACRO_FILTER else "끔") + (f"({macro_info['blocked_side']} 제외 중)" if macro_info.get("blocked_side") else ""),
        "자동 방어": ("켬" if AUTO_DEFENSE else "끔") + (f"(중지: {'·'.join(paused_f)})" if paused_f else ""),
        "손실 한도": breaker or "정상", "동시 추천 최대": risk_cfg.max_concurrent_setups,
    }
    try:  # 새 추천 추적 (자동 중지된 신호도 계속 추적해야 성과 회복을 알 수 있음)
        track_new_setups(setups + paused, tracks)
        _save_tracks(tracks)
    except Exception as e:
        print(f"[warn] 실전 추적 저장 실패: {e}")
    direction: Dict = {}
    try:  # v29.9 기간별 거시 방향 (표시용 — 추천 규칙에는 안 씀)
        bd_ = drop_unclosed(fetch_ohlcv("bitget", f"BTC/{QUOTE}", "1d", 300), "1d")
        direction = macro_direction(inputs["btc_df"], bd_, LAST_LOADED, macro_now)
    except Exception as e:
        print(f"[warn] 거시 방향 계산 실패: {e}")
    event_drift = None
    try:  # v32.3 FOMC 선반영 참고 (표시용 — 추천 규칙·리스크에는 안 씀)
        event_drift = fomc_drift_info(inputs["btc_df"], LAST_LOADED)
    except Exception as e:
        print(f"[warn] FOMC 참고 계산 실패: {e}")
    return {"regime": regime, "breaker": breaker, "setups": setups, "all_setups": all_setups, "health": health,
            "direction": direction, "event_drift": event_drift, "market_chop": chop_info,
            "trial": {fam: ("승격" if promoted.get(fam) else "시험 운용") for fam in TRIAL_FAMILIES},
            "new_families": {**{fam: ("정상 위험" if promoted.get(fam) else "위험 절반") for fam in NEW_FAMILY_RISK},
                             **{fam: f"위험 {v:g}배 고정" for fam, v in FIXED_FAMILY_RISK.items()}},
            "macro_filter": macro_info,
            "risk_cfg": risk_cfg, "asof": pd.Timestamp.now(), "asof_utc": utc_now(),
            "timeframe": TIMEFRAME, "filter_stats": stats}


# --------------------------------------------------------------------------
# 5.1 실전 로직 기반 WFO — build_setup()을 과거 데이터에 그대로 재사용
# --------------------------------------------------------------------------

def fetch_extended_ohlcv(exchange_id: str, symbol: str, timeframe: Optional[str] = None,
                          total_bars: int = 3000) -> pd.DataFrame:
    """긴 과거 기간을 나눠 받아 수집 (완성봉만). WFO는 데이터가 많을수록 신뢰도가 올라갑니다."""
    if ccxt is None:
        raise RuntimeError("ccxt가 설치되어 있지 않습니다.")
    timeframe = timeframe or TIMEFRAME
    ex = _get_ex(exchange_id)
    # 거래소마다 한 번에 주는 개수가 100~1000개라, 가장 적게 주는 경우(100개)도 최신까지 닿도록 호출 상한을 잡음
    rows = _fetch_ohlcv_paged(ex, _mkt_symbol(ex, symbol), timeframe, total_bars,
                              max_calls=min(400, total_bars // 100 + 20))
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    df = drop_unclosed(df, timeframe)
    return df.tail(total_bars).reset_index(drop=True)


def htf_trend_at(htf_df: pd.DataFrame, current_ts, htf_timeframe: Optional[str] = None) -> RegimeType:
    """백테스트용: 현재 봉(current_ts에 시작해 한 봉 뒤 마감)이 끝난 시점까지 '이미 마감된'
    상위 봉들만으로 상위 추세 판단 (미래 참조 방지). 4시간봉 모드면 일봉, 1시간봉 모드면 4시간봉."""
    htf_tf = pd.Timedelta(htf_timeframe or HTF_TIMEFRAME)
    cutoff = pd.Timestamp(current_ts) + pd.Timedelta(TIMEFRAME)
    closed = htf_df[htf_df["ts"] + htf_tf <= cutoff]
    if len(closed) < 60:
        return "sideways"
    return classify_price_trend(closed)


def _htf_bars_needed(total_bars: int) -> int:
    ratio = pd.Timedelta(HTF_TIMEFRAME) / pd.Timedelta(TIMEFRAME)
    return int(total_bars / ratio) + 100


def compute_trade_R(direction: str, entry: float, sl: float, exit_price: float,
                     holding_bars: int, fee_pct: float = 0.05, slippage_pct: float = 0.03,
                     funding_pct_per_8h: float = 0.01, bars_per_8h: Optional[float] = None,
                     entry_cost_pct: Optional[float] = None, exit_cost_pct: Optional[float] = None) -> float:
    """손익을 'R 배수'(최초 리스크 대비 몇 배)로 환산. 계좌 크기와 무관하게 전략 자체의
    품질을 비교할 수 있어서 WFO/기대값 계산에 표준적으로 쓰입니다. 수수료·슬리피지·펀딩비를
    전부 비용으로 차감한 '순(net) R'입니다."""
    bars_per_8h = bars_per_8h or BARS_PER_8H
    risk_per_unit = abs(entry - sl)
    if risk_per_unit <= 0:
        return 0.0
    raw = (exit_price - entry) if direction == "long" else (entry - exit_price)
    if entry_cost_pct is not None and exit_cost_pct is not None:     # 진입·청산 방식별 실제 비용
        cost_price = entry * (entry_cost_pct + exit_cost_pct) / 100
    else:                                                            # (예전 방식) 왕복 수수료+슬리피지
        cost_price = entry * 2 * (fee_pct + slippage_pct) / 100
    funding_price = entry * (funding_pct_per_8h / 100) * (holding_bars / bars_per_8h)
    net = raw - cost_price - funding_price
    return net / risk_per_unit


SETUP_FAMILY = {"long": "추세", "short": "추세", "wait_breakout_long": "돌파", "wait_breakout_short": "돌파",
                "range_fade_long": "박스 역매매", "range_fade_short": "박스 역매매",
                "donchian_long": "신고점 돌파", "donchian_short": "신고점 돌파",
                "st_long": "추적선 전환", "st_short": "추적선 전환",
                "retest_long": "돌파 후 되돌림", "retest_short": "돌파 후 되돌림",
                "div_long": "위치 다이버전스", "div_short": "위치 다이버전스",
                "pb_long": "추세 눌림목", "pb_short": "추세 눌림목", "diag_long": "빗각 지지",
                "wick_long": "아래꼬리 연속"}


def _htf_lookup(df: pd.DataFrame, htf_df: Optional[pd.DataFrame]):
    """봉 i가 끝난 시점까지 '마감된' 상위 봉 기준 추세를 돌려주는 함수 (미래 참조 방지)."""
    if htf_df is None or len(htf_df) < 60:
        return lambda i: "sideways"
    htf_df = htf_df.reset_index(drop=True)
    reg = trend_series(htf_df)
    close_t = (htf_df["ts"] + pd.Timedelta(HTF_TIMEFRAME)).to_numpy()
    ts = df["ts"].to_numpy()
    tf = pd.Timedelta(TIMEFRAME)

    def f(i: int) -> RegimeType:
        k = int(np.searchsorted(close_t, ts[i] + tf, side="right")) - 1
        return reg[k] if k >= 59 else "sideways"
    return f


def generate_signals(df: pd.DataFrame, btc_df: pd.DataFrame, htf_df: Optional[pd.DataFrame],
                     warmup: int = 250, alt_df: Optional[pd.DataFrame] = None,
                     families: Optional[set] = None) -> Dict[int, Dict]:
    """과거 모든 봉에서 라이브와 같은 규칙으로 신호를 계산 (그 봉까지의 데이터만 사용).
    - 국면: 코인 자신의 추세 (전체 구간 한 번 계산, 라이브와 99.9% 일치)
    - 상위추세: 그 시점까지 '마감된' 상위 봉 기준
    - 라이브와 같은 필터: 상위추세 역행 제외, 손익비 1.5(보조 근거 있으면 1.3) 미만 제외
    - families={"돌파"}면 횡보 국면 봉만 계산(돌파 신호는 횡보에서만 나옴 → 속도 향상)
    - 과거 기록이 없는 필터(스프레드·펀딩비·거래대금)는 적용하지 못함"""
    df = df.reset_index(drop=True)
    btc_df = btc_df.reset_index(drop=True)
    alt_df = alt_df.reset_index(drop=True) if alt_df is not None else None
    reg = trend_series(df)
    htf_at = _htf_lookup(df, htf_df)
    only_sideways = families is not None and families <= {"돌파", "박스 역매매"}
    window = max(260, max(RS_WINDOWS) + 20)
    signals: Dict[int, Dict] = {}
    for i in range(max(warmup, 60), len(df)):
        if only_sideways and reg[i] != "sideways":
            continue
        htf_i = htf_at(i)
        lo = max(0, i + 1 - window)
        x = build_setup("bt", "backtest", df.iloc[lo:i + 1], btc_df.iloc[lo:i + 1], reg[i], htf_i,
                        alt_df=alt_df.iloc[lo:i + 1] if alt_df is not None else None)
        if x is None:
            continue
        if families is not None and SETUP_FAMILY.get(x.bias) not in families:
            continue
        if (x.bias == "long" and htf_i == "downtrend") or (x.bias == "short" and htf_i == "uptrend"):
            continue
        if x.rr_ratio < (1.3 if (x.poc_confluence or x.sweep_confluence) else 1.5):
            continue
        sig_ = {"direction": "long" if x.bias in LONG_BIASES else "short", "bias": x.bias,
                "entry": x.entry_price, "sl": x.sl, "tp1": x.tp1, "atr": x.atr,
                "is_chase": x.is_chase, "rr": x.rr_ratio,
                "atr_pct": x.atr / x.entry_price * 100 if x.entry_price else None,
                "tags": list(x.tags), "rs": x.rs, "rs_alt": x.rs_alt}
        if x.bias.startswith("wait_breakout") and x.level is not None and x.box_h is not None:
            # v27: 봉 마감 직후 시장가 진입(종가) 기준 손절·목표, 손익비 상한은 예전 정의로 판정
            c_i, d_ = float(df["close"].iloc[i]), sig_["direction"]
            old_sl, old_tp = (x.level - x.atr, x.level + x.box_h) if d_ == "long" else (x.level + x.atr, x.level - x.box_h)
            sig_["rr_cap"] = abs(old_tp - c_i) / abs(c_i - old_sl) if abs(c_i - old_sl) > 0 else 99.0
            sig_["sl"], sig_["tp1"] = v27_levels("돌파", d_, c_i, x.level, x.box_h, x.atr)
        if x.exit_frac is not None:
            sig_.update(frac=x.exit_frac, trail=x.exit_trail, hold=x.max_hold)
        signals[i] = sig_
    return signals


def v27_family_signals(df: pd.DataFrame, fam: str, reg: Optional[np.ndarray] = None) -> Dict[int, Dict]:
    """과거 전체 봉의 새 매매법 신호를 simulate_exits 형식으로 (신호 봉 종가에 시장가 진입)."""
    raw = (retest_signals(df, reg) if fam == "돌파 후 되돌림" else
           pullback_signals(df) if fam == "추세 눌림목" else divergence_signals(df))
    closes = df["close"].to_numpy(float)
    xr = EXIT_RULES[fam]
    out: Dict[int, Dict] = {}
    for i, s in raw.items():
        sl, tp1 = v27_levels(fam, s["direction"], float(closes[i]), s["ref"], s.get("box_h"), s["atr"])
        bias = {"돌파 후 되돌림": "retest_", "추세 눌림목": "pb_"}.get(fam, "div_") + s["direction"]
        out[i] = {"direction": s["direction"], "bias": bias, "entry": float(closes[i]), "sl": sl, "tp1": tp1,
                  "atr": s["atr"], "is_chase": False, "market": True, "rr": None,
                  "atr_pct": s["atr"] / closes[i] * 100 if closes[i] else None, "tags": [],
                  "frac": xr["frac"], "trail": xr["trail"], "hold": xr["hold"]}
    return out


def generate_donchian_signals(df: pd.DataFrame, btc_df: pd.DataFrame, htf_df: Optional[pd.DataFrame],
                              alt_df: Optional[pd.DataFrame] = None, warmup: int = 250) -> Dict[int, Dict]:
    """과거 신고점 돌파 신호 (라이브 build_donchian_setup과 같은 규칙, 신호 봉 종가에 시장가 진입).
    변형 비교를 위해 신호마다 구조 기반 손절·목표(sl_s/tp1_s), 거래량 조건, 일봉 필터 결과를 함께 담음."""
    df = df.reset_index(drop=True)
    n = DONCHIAN_N.get(TIMEFRAME, 120)
    hi_prev = df["high"].shift(1).rolling(n).max().to_numpy()
    lo_prev = df["low"].shift(1).rolling(n).min().to_numpy()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - df["close"].shift()).abs(),
                    (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
    atr_s = tr.rolling(14).mean().to_numpy()
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    htf_at = _htf_lookup(df, htf_df)
    flags = _daily_flags(htf_df) if HTF_TIMEFRAME == "1d" else None
    window = max(260, max(RS_WINDOWS) + 20)
    signals: Dict[int, Dict] = {}
    for i in range(max(warmup, n + 20), len(df)):
        a = atr_s[i]
        if not a or np.isnan(a) or np.isnan(hi_prev[i]):
            continue
        if c[i] > hi_prev[i]:
            direction, bias = "long", "donchian_long"
        elif c[i] < lo_prev[i]:
            direction, bias = "short", "donchian_short"
        else:
            continue
        sl, tp1, _ = _donchian_levels(h, l, c, a, i, n, direction, "atr")
        st_ = _donchian_levels(h, l, c, a, i, n, direction, "structure")
        fl = _flags_at(flags, df["ts"].iloc[i])
        lo = max(0, i + 1 - window)
        w = df.iloc[lo:i + 1]
        rs = relative_strength_vs_btc(w, btc_df.iloc[lo:i + 1])
        rs_alt = relative_strength_vs_btc(w, alt_df.iloc[lo:i + 1]) if alt_df is not None else None
        signals[i] = {"direction": direction, "bias": bias, "entry": float(c[i]), "sl": float(sl),
                      "tp1": float(tp1), "atr": float(a), "is_chase": False, "market": True, "rr": 1.5,
                      "atr_pct": a / c[i] * 100, "tags": breakout_tags(w, direction, htf_at(i), rs_alt, rs),
                      "rs": rs, "rs_alt": rs_alt,
                      "sl_s": float(st_[0]) if st_ else None, "tp1_s": float(st_[1]) if st_ else None,
                      "vol_ok": _breakout_volume_ok(df, i, direction),
                      "weekly_ok": _donchian_filter_ok("weekly", direction, False, fl),
                      "tsm_ok": _donchian_filter_ok("tsm", direction, False, fl)}
    return signals


def generate_supertrend_signals(df: pd.DataFrame, btc_df: pd.DataFrame, htf_df: Optional[pd.DataFrame],
                                alt_df: Optional[pd.DataFrame] = None, warmup: int = 250) -> Dict[int, Dict]:
    """과거 ATR 추적선 전환 신호 (라이브 build_supertrend_setup과 같은 규칙, 전환 봉 종가에 시장가 진입)."""
    df = df.reset_index(drop=True)
    line, d = supertrend(df)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - df["close"].shift()).abs(),
                    (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
    atr_s = tr.rolling(14).mean().to_numpy()
    c = df["close"].to_numpy()
    htf_at = _htf_lookup(df, htf_df)
    window = max(260, max(RS_WINDOWS) + 20)
    out: Dict[int, Dict] = {}
    for i in range(max(warmup, 60), len(df)):
        if d[i] == d[i - 1]:
            continue
        a = atr_s[i]
        if not a or np.isnan(a):
            continue
        direction = "long" if d[i] == 1 else "short"
        sl, tp1, _ = _st_levels(float(c[i]), float(line[i]), a, direction)
        lo = max(0, i + 1 - window)
        w = df.iloc[lo:i + 1]
        rs = relative_strength_vs_btc(w, btc_df.iloc[lo:i + 1])
        rs_alt = relative_strength_vs_btc(w, alt_df.iloc[lo:i + 1]) if alt_df is not None else None
        out[i] = {"direction": direction, "bias": "st_long" if direction == "long" else "st_short",
                  "entry": float(c[i]), "sl": float(sl), "tp1": float(tp1), "atr": float(a), "is_chase": False,
                  "market": True, "rr": 2.0, "atr_pct": a / c[i] * 100,
                  "tags": breakout_tags(w, direction, htf_at(i), rs_alt, rs), "rs": rs, "rs_alt": rs_alt}
    return out


def donchian_variant_signals(dsig: Dict[int, Dict], variant: str) -> Dict[int, Dict]:
    """기본 신고점 돌파 신호에서 변형별 신호를 만듦 (구조 기반 손절·목표 또는 필터)."""
    if variant == "atr":
        return dsig
    if variant == "structure":
        return {i: {**x, "sl": x["sl_s"], "tp1": x["tp1_s"]} for i, x in dsig.items() if x.get("sl_s") is not None}
    key = {"weekly": "weekly_ok", "volume": "vol_ok", "tsm": "tsm_ok"}[variant]
    return {i: x for i, x in dsig.items() if x.get(key)}


def simulate_exits(df: pd.DataFrame, signals: Dict[int, Dict], exit_mode: str = "partial_trail",
                   pending_expiry_bars: int = 15, max_hold_bars: int = 60,
                   entry_mode: str = "retest", slip_pct: Optional[float] = None,
                   open_state: Optional[Dict] = None) -> List[Dict]:
    """신호를 봉 단위로 재생하며 체결·청산을 시뮬레이션. 거래별 상세(순 R, 비용 전 R, 표시 등)를 반환.
    - entry_mode="retest": 추격 신호는 지정가 대기 → 이후 봉에서 닿아야 체결(체결 전 손절선 넘으면 취소)
    - entry_mode="immediate": 추격 신호도 신호 봉 종가에 바로 시장가 진입
    - 신호에 market=True가 있으면(신고점 돌파) 항상 신호 봉 종가에 시장가 진입
    - 비용: 지정가 체결·목표1 익절 = 메이커 수수료 / 시장가 진입·손절·추적손절·기간만료 = 테이커 + 슬리피지
    - exit_mode="partial_trail": 목표1 절반 익절 → 남은 절반 본전 손절 → 최고/최저가에서 TRAIL_ATR×ATR 되돌리면 정리
    - exit_mode="fixed": 목표1에서 전량 청산 (비교용)
    - 같은 봉에서 손절·목표 동시 도달 시 손절 처리(보수적), 추적 손절선은 직전 봉까지 기준(미래 참조 방지)
    - 한 번에 하나의 포지션만"""
    slip = DEFAULT_SLIPPAGE_PCT if slip_pct is None else slip_pct
    maker, taker = MAKER_FEE_PCT, TAKER_FEE_PCT + slip
    lows, highs, closes = df["low"].to_numpy(), df["high"].to_numpy(), df["close"].to_numpy()
    trades: List[Dict] = []
    pending: Optional[Dict] = None
    t: Optional[Dict] = None

    def _R(exit_price: float, hold: int, weight: float, kind: str) -> float:
        exit_cost = maker if kind == "tp" else taker
        return weight * compute_trade_R(t["direction"], t["entry"], t["sl"], exit_price, hold,
                                        entry_cost_pct=t["entry_cost"], exit_cost_pct=exit_cost)

    def _G(exit_price: float, weight: float) -> float:  # 비용(수수료·슬리피지·펀딩) 빼기 전
        return weight * compute_trade_R(t["direction"], t["entry"], t["sl"], exit_price, 0,
                                        fee_pct=0.0, slippage_pct=0.0, funding_pct_per_8h=0.0)

    def _done(total_r: float, i: int, reason: str, gross: float) -> None:
        trades.append({"R": float(total_r), "gross_R": float(gross), "atr_pct": t.get("atr_pct"),
                       "entry_idx": t["entry_idx"], "exit_idx": i, "direction": t["direction"],
                       "bias": t["bias"], "reason": reason, "tags": list(t.get("tags", [])),
                       "rs": t.get("rs"), "rs_alt": t.get("rs_alt"), "entry_type": t["entry_type"]})

    def _open(sig: Dict, i: int, price: float, entry_type: str) -> Optional[Dict]:
        long_side = sig["direction"] == "long"
        if (long_side and (price <= sig["sl"] or price >= sig["tp1"])) or \
           (not long_side and (price >= sig["sl"] or price <= sig["tp1"])):
            return None  # 진입 시점에 이미 손절선 너머이거나 목표 도달 → 진입 안 함
        return {**sig, "entry": float(price), "entry_idx": i, "stage": 0, "realized": 0.0, "realized_g": 0.0,
                "entry_type": entry_type, "entry_cost": maker if entry_type == "지정가" else taker}

    for i in range(len(df)):
        if t:
            long_side = t["direction"] == "long"
            hold = i - t["entry_idx"]
            # v27: 신호마다 청산 규칙(목표1 익절 비율·추적 폭·최대 보유)을 가질 수 있음. 없으면 예전 규칙
            frac_ = t.get("frac"); frac_ = PARTIAL_TP_FRACTION if frac_ is None else frac_
            trail_ = t.get("trail") or TRAIL_ATR
            hmax_ = t.get("hold") or max_hold_bars
            if t["stage"] == 0:
                hit_sl = lows[i] <= t["sl"] if long_side else highs[i] >= t["sl"]
                hit_tp = highs[i] >= t["tp1"] if long_side else lows[i] <= t["tp1"]
                if hit_sl:
                    _done(_R(t["sl"], hold, 1.0, "stop"), i, "손절", _G(t["sl"], 1.0)); t = None
                elif hit_tp and (exit_mode == "fixed" or frac_ >= 1.0):
                    _done(_R(t["tp1"], hold, 1.0, "tp"), i, "목표1", _G(t["tp1"], 1.0)); t = None
                elif hit_tp:
                    t["realized"] = _R(t["tp1"], hold, frac_, "tp") if frac_ > 0 else 0.0
                    t["realized_g"] = _G(t["tp1"], frac_) if frac_ > 0 else 0.0
                    t["stage"], t["stop"] = 1, t["entry"]
                    t["best"] = highs[i] if long_side else lows[i]
                elif hold >= hmax_:
                    _done(_R(closes[i], hold, 1.0, "time"), i, "기간만료", _G(closes[i], 1.0)); t = None
            else:
                rest = 1.0 - frac_
                if long_side:
                    t["stop"] = max(t["stop"], t["best"] - trail_ * t["atr"])
                    if lows[i] <= t["stop"]:
                        _done(t["realized"] + _R(t["stop"], hold, rest, "stop"), i, "추적손절",
                              t["realized_g"] + _G(t["stop"], rest)); t = None
                    else:
                        t["best"] = max(t["best"], highs[i])
                else:
                    t["stop"] = min(t["stop"], t["best"] + trail_ * t["atr"])
                    if highs[i] >= t["stop"]:
                        _done(t["realized"] + _R(t["stop"], hold, rest, "stop"), i, "추적손절",
                              t["realized_g"] + _G(t["stop"], rest)); t = None
                    else:
                        t["best"] = min(t["best"], lows[i])
                if t and hold >= hmax_:
                    _done(t["realized"] + _R(closes[i], hold, rest, "time"), i, "기간만료",
                          t["realized_g"] + _G(closes[i], rest)); t = None
            continue
        if pending:
            long_side = pending["direction"] == "long"
            filled = lows[i] <= pending["entry"] if long_side else highs[i] >= pending["entry"]
            invalid = closes[i] < pending["sl"] if long_side else closes[i] > pending["sl"]
            if filled:
                t = {**pending, "entry_idx": i, "stage": 0, "realized": 0.0, "realized_g": 0.0,
                     "entry_type": "지정가", "entry_cost": maker}
                pending = None
                # 체결된 봉 안에서 손절선까지 밀렸으면 손절 처리 (보수적 가정 — 예전엔 다음 봉부터 검사해 낙관적이었음)
                if (lows[i] <= t["sl"]) if long_side else (highs[i] >= t["sl"]):
                    _done(_R(t["sl"], 0, 1.0, "stop"), i, "체결봉 손절", _G(t["sl"], 1.0)); t = None
            elif invalid or i >= pending["expiry_idx"]:
                pending = None
            continue
        sig = signals.get(i)
        if not sig:
            continue
        if sig.get("market") or entry_mode == "immediate":
            t = _open(sig, i, closes[i], "시장가")
        elif sig["is_chase"]:
            pending = {**sig, "expiry_idx": i + pending_expiry_bars}
        else:
            t = _open(sig, i, sig["entry"], "지정가")
    if open_state is not None and t:   # v29.6: 아직 보유 중인 포지션의 지금 상태(보유 중 관리 카드용)
        open_state.update({k: t.get(k) for k in ("entry", "entry_idx", "stage", "stop", "best", "sl", "tp1", "atr",
                                                 "direction", "trail", "hold")})
        open_state["last_idx"] = len(df) - 1
    return trades


def simulate_strategy_history(df: pd.DataFrame, btc_df: pd.DataFrame, daily_df: pd.DataFrame,
                               pending_expiry_bars: int = 15, max_hold_bars: int = 60,
                               min_lookback: int = 80, exit_mode: str = "partial_trail") -> List[float]:
    """(기존 호출 방식 유지) 신호 계산 → 체결·청산 시뮬레이션 → 거래별 순 R 목록."""
    sig = generate_signals(df, btc_df, daily_df, warmup=max(min_lookback, 220))
    return [x["R"] for x in simulate_exits(df.reset_index(drop=True), sig, exit_mode,
                                           pending_expiry_bars, max_hold_bars)]


def summarize_trades(trades: List[Dict]) -> Dict:
    """거래 목록 요약: 승률, 평균 R, 손익비(PF), 최대 연속 손실, 최대 낙폭(R), 전반·후반 일관성."""
    if not trades:
        return {"n": 0}
    tr = sorted(trades, key=lambda x: x.get("exit_ts", x["exit_idx"]))
    R_ = np.array([x["R"] for x in tr])
    wins, losses = R_[R_ > 0], R_[R_ <= 0]
    streak = best_streak = 0
    for r in R_:
        streak = streak + 1 if r <= 0 else 0
        best_streak = max(best_streak, streak)
    cum = np.cumsum(R_)
    max_dd = float(np.max(np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:] - cum)) if len(cum) else 0.0
    half = len(R_) // 2
    G_ = np.array([x.get("gross_R", np.nan) for x in tr], dtype=float)
    return {"avg_gross_R": float(np.nanmean(G_)) if np.isfinite(G_).any() else None,
            "n": int(len(R_)), "win_rate": float(len(wins) / len(R_)), "avg_R": float(R_.mean()),
            "total_R": float(R_.sum()),
            "pf": float(wins.sum() / abs(losses.sum())) if losses.sum() != 0 else float("inf"),
            "avg_win": float(wins.mean()) if len(wins) else 0.0,
            "avg_loss": float(losses.mean()) if len(losses) else 0.0,
            "best": float(R_.max()), "max_consec_loss": int(best_streak), "max_dd_R": max_dd,
            "first_half_avg": float(R_[:half].mean()) if half else None,
            "second_half_avg": float(R_[half:].mean()) if len(R_) - half else None}


TIER_BOUNDS = [("거래량 1~30위", 0, 30), ("거래량 31~60위", 30, 60), ("거래량 61~100위", 60, 100)]


EXIT_MODE_LABEL = {"partial_trail": "분할익절+추적손절", "fixed": "목표1 전량"}


def _vol_edges(trades: List[Dict]):
    v = np.array([x["atr_pct"] for x in trades if x.get("atr_pct") is not None], dtype=float)
    return (float(np.quantile(v, 1 / 3)), float(np.quantile(v, 2 / 3))) if len(v) >= 6 else None


def _group_key(x: Dict, by: str, edges=None) -> str:
    if by == "direction":
        return "롱" if x["direction"] == "long" else "숏"
    if by == "family":
        return SETUP_FAMILY.get(x["bias"], x["bias"])
    if by == "tier":
        return x.get("tier", "구간 정보 없음")
    if by == "vol":
        v = x.get("atr_pct")
        if v is None or edges is None:
            return "정보 없음"
        q1, q2 = edges
        return f"낮음(~{q1:.1f}%)" if v <= q1 else (f"중간({q1:.1f}~{q2:.1f}%)" if v <= q2 else f"높음({q2:.1f}%~)")
    return x.get("symbol", "?")


def backtest_group_table(trades: List[Dict], by: str) -> pd.DataFrame:
    """by: direction(롱/숏) · family(추세/돌파/박스 역매매) · tier(거래량 구간) · vol(진입 시점 변동성) · symbol."""
    edges = _vol_edges(trades) if by == "vol" else None
    groups: Dict[str, List[Dict]] = {}
    for x in trades:
        groups.setdefault(_group_key(x, by, edges), []).append(x)
    rows = []
    for g, v in groups.items():
        sm = summarize_trades(v)
        rows.append({"구분": g, "거래 수": sm["n"], "승률": f"{sm['win_rate']:.0%}", "평균 R": round(sm["avg_R"], 2),
                     "비용 전 R": round(sm["avg_gross_R"], 2) if sm.get("avg_gross_R") is not None else None,
                     "합계 R": round(sm["total_R"], 1)})
    out = pd.DataFrame(rows)
    if not len(out):
        return out
    if by in ("tier", "vol"):  # 순서가 의미 있는 구분은 자연 순서로
        order = [t[0] for t in TIER_BOUNDS] if by == "tier" else ["낮음", "중간", "높음"]
        out["_o"] = out["구분"].map(lambda g: next((i for i, o in enumerate(order) if g.startswith(o)), 99))
        return out.sort_values("_o").drop(columns="_o").reset_index(drop=True)
    return out.sort_values("합계 R", ascending=False).reset_index(drop=True)


def suggest_scan_count(trades: List[Dict], families: Optional[set] = None, min_n: int = 20) -> str:
    """거래량 구간별 결과로 '어디까지 스캔해도 되는지' 제안 (켜둔 신호 유형 기준).
    구간마다 거래가 min_n건 이상이고 평균이 플러스여야 통과. 표본 부족과 마이너스를 구분해서 알려줌."""
    tr = [x for x in trades if families is None or SETUP_FAMILY.get(x["bias"]) in families]
    ok_upto, detail, stop_reason = 0, [], ""
    for label, _, b_ in TIER_BOUNDS:
        sm = summarize_trades([x for x in tr if x.get("tier") == label])
        if sm["n"] == 0:
            detail.append(f"{label} 거래 없음")
            stop_reason = f"{label}에서 거래가 없어 판단할 수 없어요"
            break
        detail.append(f"{label} {sm['n']}건 {sm['avg_R']:+.2f}R")
        if sm["n"] < min_n:
            stop_reason = f"{label} 거래가 {sm['n']}건뿐이라 판단하기엔 표본이 부족해요(검증 코인 수·기간을 늘려보세요)"
            break
        if sm["avg_R"] <= 0:
            stop_reason = f"{label}에서 마이너스라 이 구간부터는 스캔하지 않는 게 좋아요"
            break
        ok_upto = b_
    fam = "·".join(sorted(families)) if families else "전체"
    head = f"🔎 [{fam}] " + " / ".join(detail) + " → "
    if ok_upto == 0:
        return head + stop_reason + ". 지금은 스캔 수를 늘릴 근거가 없어요."
    tail = f" ({stop_reason})" if stop_reason else ""
    return head + f"스캔 코인 수는 {ok_upto}개까지가 검증된 범위예요{tail}."


# ==========================================================================
# 추세 포트폴리오 (시계열 모멘텀, 롱만, 매일 조정)
# v28: 시장 관문(BTC 100일선 + 대상 코인 과반이 20일선 위) · 조정 문턱 50% · BTC 포함
#   일봉 2024.04~2026.10, 수수료·슬리피지·펀딩 포함: 연 +15%·최대 낙폭 −47% → 연 +26%·−25%
#   정직한 추정(앞 기간으로 고르고 다음 기간 적용): 수익은 비슷(연 +8~13%), 낙폭이 −45% → −21~25%로 줄어드는 게 확실한 효과
# ==========================================================================
TSM_ENABLED = False              # 연구실 통과 후 '이 설정 적용'으로 켜짐 (설정에서 직접 켤 수도 있음)
TSM_EMA = 50                     # 코인 보유 조건 ①: 일봉 종가 > 50일 지수이동평균
TSM_LOOKBACK = 30                # 코인 보유 조건 ②: 30일 수익률 > 0
TSM_VOL_WIN = 30                 # 변동성 계산 기간(일)
TSM_TARGET_DAILY_VOL = 0.005     # 코인 하나가 하루에 포트폴리오를 약 0.5% 움직이도록 비중 결정(변동성 반비례)
TSM_MAX_WEIGHT = 0.30            # 코인 하나 최대 비중(포트폴리오 배분의 30%)
TSM_MAX_GROSS = 1.0              # 비중 합계 상한 기본 100%. 설정에서 150%(적극) 선택 가능
TSM_GROSS_CHOICES = (1.0, 1.5)   # 150%: 연 +40%·낙폭 −35% (100%: +26%·−25%) — 같은 기간 검증
TSM_REBAL_BAND = 0.50            # v28: 25% → 50%. 비중 변화가 지금 비중의 50% 미만이면 조정 안 함(주문·수수료 조금 감소)
                                 # 단, 그 때문에 비중 합계가 상한을 넘는 날은 전체를 목표 비중으로 다시 맞춤
TSM_FUNDING_DAILY = 0.0003       # 롱 펀딩비 근사(8시간 0.01% × 3 = 하루 0.03%)
TSM_MIN_BARS = 60                # 일봉 60개 미만(상장 60일 미만) 코인은 보유·관문 계산에서 제외
TSM_GATE = True                  # v28 시장 관문: 아래 ①② 모두 맞는 날만 보유, 하나라도 깨지면 전부 정리
TSM_GATE_BTC_SMA = 100           # ① BTC 일봉 종가 > 100일 단순평균
TSM_GATE_BREADTH_EMA = 20        # ② 대상 코인 중 일봉 종가가 20일 지수평균 위인 비율이
TSM_GATE_BREADTH_MIN = 0.5       #    50% 이상
TSM_INCLUDE_BTC = True           # v28: BTC도 보유 대상(관문 비율 계산에도 포함)
TSM_EXEC_DEADLINE_H = 12         # 일봉 마감(한국 09시) 뒤 12시간(21시) 안에 조정. 하루 늦게 하면 효과 대부분 사라짐(검증)
TSM_SLIP_PCT = 0.03              # 메이저 시장가 슬리피지(%)
PORT_LOG_FILE = "portfolio_log.json"   # 날마다 앱이 낸 목표 비중 기록 → '앱 지시대로 했다면' 실전 추적


def tsm_universe() -> List[str]:
    """포트폴리오 대상: 메이저 + (v28) BTC"""
    bases = sorted(MAJORS) + (["BTC"] if TSM_INCLUDE_BTC else [])
    return [f"{b}/{QUOTE}" for b in bases]


def _tsm_valid(close: pd.DataFrame) -> pd.DataFrame:
    return close.notna() & (close.notna().cumsum() >= TSM_MIN_BARS)


def tsm_gate(close: pd.DataFrame, btc: Optional[pd.Series] = None) -> pd.DataFrame:
    """날짜별 시장 관문. close: 날짜×코인 일봉 종가(BTC 포함 권장), btc: 따로 줄 때 BTC 종가.
    반환 열: btc, btc_sma, btc_ok, n_up, n, breadth, breadth_ok, gate"""
    btc_sym = f"BTC/{QUOTE}"
    if btc is None:
        if btc_sym not in close.columns:
            raise ValueError("시장 관문 계산에 BTC 일봉이 필요해요")
        btc = close[btc_sym]
    btc = btc.reindex(close.index)
    sma = btc.rolling(TSM_GATE_BTC_SMA, min_periods=TSM_GATE_BTC_SMA).mean()
    valid = _tsm_valid(close)
    ema = close.ewm(span=TSM_GATE_BREADTH_EMA, adjust=False, min_periods=TSM_GATE_BREADTH_EMA).mean()
    up = (close > ema) & valid
    n, n_up = valid.sum(axis=1), up.sum(axis=1)
    breadth = (n_up / n.where(n > 0)).fillna(0.0)
    btc_ok = (btc > sma).fillna(False).astype(bool)
    breadth_ok = breadth >= TSM_GATE_BREADTH_MIN
    return pd.DataFrame({"btc": btc, "btc_sma": sma, "btc_ok": btc_ok, "n_up": n_up, "n": n, "breadth": breadth,
                         "breadth_ok": breadth_ok, "gate": btc_ok & breadth_ok})


def tsm_coin_on(close: pd.DataFrame) -> pd.DataFrame:
    """코인별 보유 조건(관문과 별개): 종가 > 50일선 그리고 30일 수익률 > 0"""
    ema = close.ewm(span=TSM_EMA, adjust=False, min_periods=TSM_EMA).mean()
    return (close > ema) & (close / close.shift(TSM_LOOKBACK) - 1 > 0) & _tsm_valid(close)


def tsm_weights(close: pd.DataFrame, gate: Optional[pd.Series] = None, max_gross: Optional[float] = None,
                band: Optional[float] = None) -> pd.DataFrame:
    """close: 날짜×코인 일봉 종가 → 날짜별 목표 비중(포트폴리오 배분 대비). t일 종가로 정한 비중을 t+1일에 보유.
    gate: 날짜별 True/False. False인 날은 전부 0(정리). None이면 관문 없이(v27 이전 방식)."""
    G = TSM_MAX_GROSS if max_gross is None else float(max_gross)
    band = TSM_REBAL_BAND if band is None else float(band)
    on = tsm_coin_on(close)
    if gate is not None:
        g = gate.reindex(close.index).fillna(False).astype(bool)
        on = on.mul(g.astype(int), axis=0).astype(bool)
    vol = close.pct_change(fill_method=None).rolling(TSM_VOL_WIN, min_periods=20).std()
    raw = (TSM_TARGET_DAILY_VOL / vol).where(on, 0.0).fillna(0.0).clip(upper=TSM_MAX_WEIGHT)
    gross = raw.sum(axis=1)
    raw = raw.mul((G / gross).where(gross > G, 1.0), axis=0)
    tgt = raw.to_numpy()
    out = np.zeros_like(tgt)
    for t in range(len(tgt)):
        prev = out[t - 1] if t else np.zeros(tgt.shape[1])
        change = (tgt[t] == 0) | (prev == 0) | (np.abs(tgt[t] - prev) > band * np.maximum(prev, 1e-12))
        out[t] = np.where(change, tgt[t], prev)
        if out[t].sum() > G + 1e-9:                    # 문턱 때문에 합계가 상한을 넘으면 그날은 전체를 목표 비중으로
            out[t] = tgt[t]
    return pd.DataFrame(out, index=close.index, columns=close.columns)


def tsm_backtest(close: pd.DataFrame, slip_pct: Optional[float] = None, gate=None, max_gross: Optional[float] = None,
                 band: Optional[float] = None) -> Dict:
    """일별 순수익(수수료·슬리피지·롱 펀딩비 차감) 시계열과 비중.
    gate: None → TSM_GATE 설정대로(켜져 있으면 close로 관문 계산, BTC 열 필요) · False → 관문 없이 · Series → 그대로 사용"""
    if gate is None:
        gate = TSM_GATE
    g = tsm_gate(close)["gate"] if gate is True else (gate if isinstance(gate, pd.Series) else None)
    w = tsm_weights(close, g, max_gross, band)
    rets = close.pct_change(fill_method=None).fillna(0.0)
    held = w.shift(1).fillna(0.0)
    gross_ret = (held * rets).sum(axis=1)
    turnover = (w - held).abs().sum(axis=1)
    cost = turnover * (TAKER_FEE_PCT + (DEFAULT_SLIPPAGE_PCT if slip_pct is None else slip_pct)) / 100
    funding = held.sum(axis=1) * TSM_FUNDING_DAILY
    return {"daily": gross_ret - cost - funding, "gross": gross_ret, "weights": w, "gate": g}


def perf_stats(daily: pd.Series) -> Dict:
    """연수익률(복리), 연변동성, 샤프, 최대 낙폭, 누적 수익, 플러스 달 비율."""
    d = daily.dropna()
    if len(d) < 20:
        return {"n_days": len(d)}
    eq = (1 + d).cumprod()
    years = len(d) / 365
    monthly = (1 + d).groupby(d.index.to_period("M")).prod() - 1 if isinstance(d.index, pd.DatetimeIndex) else pd.Series(dtype=float)
    sd = d.std()
    return {"n_days": len(d), "total": float(eq.iloc[-1] - 1),
            "cagr": float(eq.iloc[-1] ** (1 / years) - 1) if eq.iloc[-1] > 0 else -1.0,
            "vol": float(sd * np.sqrt(365)), "sharpe": float(d.mean() / sd * np.sqrt(365)) if sd > 0 else 0.0,
            "max_dd": float((eq / eq.cummax() - 1).min()),
            "pos_months": float((monthly > 0).mean()) if len(monthly) else None}


def _block_boot_p(x, block: int = 10, n_boot: int = 2000, seed: int = 11) -> float:
    """일별 수익처럼 앞뒤가 이어진 데이터용 블록 부트스트랩: '평균 ≤ 0'일 확률."""
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n < block * 3:
        return 1.0
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(n / block))
    starts = rng.integers(0, n - block + 1, (n_boot, nb))
    idx = (starts[:, :, None] + np.arange(block)).reshape(n_boot, -1)[:, :n]
    return float((x[idx].mean(axis=1) <= 0).mean())


def tsm_gate_text(g: Dict) -> str:
    """관문 상태 한 줄 설명"""
    b = (f"BTC {g['btc']:,.0f} {'>' if g['btc_ok'] else '≤'} 100일선 {g['btc_sma']:,.0f} ({g['btc_gap']:+.1%})"
         if g.get("btc_sma") else "BTC 100일선 계산 불가(일봉 부족)")
    w = f"20일선 위 {g['n_up']}/{g['n']}개 ({g['breadth']:.0%}, 기준 {TSM_GATE_BREADTH_MIN:.0%} = {g['need_up']}개)"
    return f"{'✅' if g['btc_ok'] else '❌'} {b} · {'✅' if g['breadth_ok'] else '❌'} {w}"


def bitget_min_notional(perp_symbol: str, price: float) -> Optional[float]:
    """Bitget 선물 최소 주문 금액(USDT) 근사: max(최소 주문 금액, 최소 수량 × 계약 크기 × 가격). 모르면 None."""
    try:
        m = (_get_ex("bitget").markets or {}).get(perp_symbol)
        if not m:
            return None
        lim = m.get("limits") or {}
        amt = ((lim.get("amount") or {}).get("min") or 0.0) * float(m.get("contractSize") or 1.0) * float(price)
        cost = (lim.get("cost") or {}).get("min") or 0.0
        v = max(float(amt), float(cost))
        return v if v > 0 else None
    except Exception:
        return None


def build_daily_portfolio(top_n: int = 0, balance: float = 1000.0, max_gross: Optional[float] = None) -> Dict:
    """오늘(마지막 완성 일봉 기준)의 추세 포트폴리오: 시장 관문 상태와 코인별 신규 진입·유지·비중 조정·정리 목록.
    balance = 포트폴리오에 배분한 금액. top_n은 호환용."""
    perps = bitget_perp_symbols() if BITGET_ONLY else set()
    want = tsm_universe()
    btc_sym = f"BTC/{QUOTE}"
    if btc_sym not in want:
        want = want + [btc_sym]                        # BTC를 보유하지 않아도 관문 계산엔 필요
    closes, uni = {}, {}
    for sym in want:
        p, m = perp_match(sym, perps) if perps else (f"{sym}:{QUOTE}", 1)
        if perps and p is None and sym != btc_sym:
            continue
        for ex_id in EXCHANGES:
            try:
                d = drop_unclosed(fetch_ohlcv(ex_id, sym, "1d", 200), "1d")
            except Exception:
                d = None
            # 일봉 경계가 UTC 00시(한국 09시)인 데이터만 사용 — 거래소가 다른 시간대 일봉을 주면 다음 거래소로
            if d is not None and len(d) >= TSM_MIN_BARS and bool((pd.to_datetime(d["ts"]).dt.hour == 0).all()):
                closes[sym] = d.set_index("ts")["close"]
                uni[sym] = {"src": ex_id, "perp": p, "mult": m}
                break
    if btc_sym not in closes:
        raise RuntimeError("BTC 일봉을 받지 못해 시장 관문을 계산할 수 없어요")
    allc = pd.DataFrame(closes).sort_index()
    hold_syms = [s for s in allc.columns if s in tsm_universe()]
    close = allc[hold_syms]
    gdf = tsm_gate(close if btc_sym in close.columns else allc[hold_syms + [btc_sym]], allc[btc_sym])
    G = TSM_MAX_GROSS if max_gross is None else float(max_gross)
    w = tsm_weights(close, gdf["gate"] if TSM_GATE else None, G) * (TRIAL_RISK_MULT if TSM_TRIAL else 1.0)
    today, prev = w.iloc[-1], w.iloc[-2]
    coin_on = tsm_coin_on(close).iloc[-1]
    gl = gdf.iloc[-1]
    gate_on = bool(gl["gate"]) if TSM_GATE else True
    gser = gdf["gate"].astype(bool)
    streak = int((gser.iloc[::-1] == gser.iloc[-1]).cumprod().sum())
    gate = {"enabled": TSM_GATE, "on": gate_on, "changed": bool(gser.iloc[-1] != gser.iloc[-2]), "days": streak,
            "btc": float(gl["btc"]), "btc_sma": float(gl["btc_sma"]) if pd.notna(gl["btc_sma"]) else None,
            "btc_gap": float(gl["btc"] / gl["btc_sma"] - 1) if pd.notna(gl["btc_sma"]) else None,
            "btc_ok": bool(gl["btc_ok"]), "n_up": int(gl["n_up"]), "n": int(gl["n"]), "breadth": float(gl["breadth"]),
            "breadth_ok": bool(gl["breadth_ok"]), "need_up": int(np.ceil(TSM_GATE_BREADTH_MIN * int(gl["n"]))),
            "on_ratio_90d": float(gser.iloc[-90:].mean())}
    ema = close.ewm(span=TSM_EMA, adjust=False).mean().iloc[-1]
    ret30 = (close.iloc[-1] / close.shift(TSM_LOOKBACK).iloc[-1] - 1)
    rows = []
    for sym in close.columns:
        t_, p_ = float(today[sym]), float(prev[sym])
        if t_ == 0 and p_ == 0:
            continue
        status = "신규 진입" if p_ == 0 else ("정리" if t_ == 0 else ("비중 조정" if abs(t_ - p_) > 1e-12 else "유지"))
        info = uni.get(sym, {})
        reason = ""
        if status == "정리":
            if TSM_GATE and not gate_on:
                reason = "시장 관문 꺼짐(" + ("BTC 100일선 아래" if not gate["btc_ok"] else "") + \
                         (" · " if not gate["btc_ok"] and not gate["breadth_ok"] else "") + \
                         (f"20일선 위 코인 {gate['n_up']}/{gate['n']}개" if not gate["breadth_ok"] else "") + ")"
            elif close[sym].iloc[-1] <= ema[sym]:
                reason = "종가가 50일선 아래로 마감"
            else:
                reason = f"30일 수익률 마이너스({ret30[sym]:+.1%})"
        px = float(close[sym].iloc[-1])
        rows.append({"symbol": sym, "status": status, "weight": t_, "prev_weight": p_, "reason": reason,
                     "ret30": float(ret30[sym]) if pd.notna(ret30[sym]) else None,
                     "notional": balance * t_, "prev_notional": balance * p_, "close": px, "exit_line": float(ema[sym]),
                     "qty": balance * t_ / px if px > 0 else 0.0, "delta_qty": balance * (t_ - p_) / px if px > 0 else 0.0,
                     "min_notional": bitget_min_notional(info.get("perp", ""), px * int(info.get("mult", 1) or 1))
                                     if (BITGET_ONLY and info.get("perp")) else None,
                     "perp_symbol": info.get("perp", ""), "perp_mult": int(info.get("mult", 1))})
    order = {"신규 진입": 0, "비중 조정": 1, "정리": 2, "유지": 3}
    rows.sort(key=lambda r: (order[r["status"]], -r["weight"]))
    # 관문이 꺼져 있을 때: 관문만 켜지면 들어갈 코인(코인별 추세 조건은 이미 충족)
    waiting = [s for s in close.columns if bool(coin_on.get(s, False)) and float(today[s]) == 0] if not gate_on else []
    date = close.index[-1]
    return {"date": date, "rows": rows, "gross": float(today.sum()), "n_hold": int((today > 0).sum()),
            "coins": len(close.columns), "made_at": utc_now(), "trial": TSM_TRIAL, "gate": gate, "gross_cap": G,
            "waiting": waiting, "deadline": pd.Timestamp(date) + pd.Timedelta(days=1, hours=TSM_EXEC_DEADLINE_H),
            "weights": {s: float(today[s]) for s in close.columns},
            "closes": {s: float(close[s].iloc[-1]) for s in close.columns if pd.notna(close[s].iloc[-1])}}


# ---------- 포트폴리오 실전 추적: 앱이 낸 목표 비중을 날짜별로 기록 → '앱 지시대로 했다면' 성과
def load_portfolio_log() -> List[Dict]:
    try:
        with open(PORT_LOG_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return []
    out = [e for e in raw if isinstance(e, dict) and e.get("date") and isinstance(e.get("w"), dict)
           and isinstance(e.get("c"), dict)] if isinstance(raw, list) else []
    return sorted(out, key=lambda e: e["date"])


def _save_portfolio_log(log: List[Dict]) -> None:
    """v30.2: 실전 추적과 같이 파일에 있는 기록과 합쳐서 저장(동시에 저장될 때 날짜 기록이 사라지지 않게)."""
    try:
        with _FILE_LOCK:
            mine = json.dumps(log, ensure_ascii=False, default=str)
            cur = open(PORT_LOG_FILE, encoding="utf-8").read() if os.path.exists(PORT_LOG_FILE) else ""
            merged = json.loads(_merge_port_log(mine, cur) if cur.strip() else mine)
            with open(PORT_LOG_FILE, "w", encoding="utf-8") as f:
                json.dump(merged[-1000:], f, ensure_ascii=False, default=str)
    except Exception as e:
        print(f"[warn] 포트폴리오 기록 저장 실패: {e}")


def _merge_port_log(local_txt: str, remote_txt: str) -> str:
    """두 곳의 포트폴리오 기록 합치기: 날짜별로 먼저 만든 기록(사용자가 처음 본 지시)을 남김"""
    def _load(t):
        try:
            v = json.loads(t) if t and t.strip() else []
            return v if isinstance(v, list) else []
        except Exception:
            return []
    merged: Dict[str, Dict] = {}
    for e in _load(remote_txt) + _load(local_txt):
        if not isinstance(e, dict) or not e.get("date"):
            continue
        cur = merged.get(e["date"])
        if cur is None or str(e.get("made_at", "")) < str(cur.get("made_at", "")):
            merged[e["date"]] = e
    return json.dumps([merged[k] for k in sorted(merged)], ensure_ascii=False)


def record_portfolio(pf: Dict) -> bool:
    """오늘 목표 비중을 기록(날짜당 1번, 처음 계산한 것). 새로 기록했으면 True."""
    if not pf:
        return False
    log = load_portfolio_log()
    d = str(pd.Timestamp(pf["date"]).date())
    if any(e["date"] == d for e in log):
        return False
    log.append({"date": d, "gate": bool((pf.get("gate") or {}).get("on", True)),
                "w": {s: round(w, 5) for s, w in pf["weights"].items() if w > 0},
                "c": pf["closes"], "cap": pf.get("gross_cap", TSM_MAX_GROSS), "trial": bool(pf.get("trial")),
                "made_at": str(pf.get("made_at", utc_now())), "v": APP_VERSION})
    _save_portfolio_log(sorted(log, key=lambda e: e["date"]))
    return True


def portfolio_tracking(log: Optional[List[Dict]] = None, slip_pct: float = TSM_SLIP_PCT) -> Optional[Dict]:
    """기록된 목표 비중대로 했다면의 성과(수수료·슬리피지·롱 펀딩 차감, 포트폴리오 배분 대비).
    기록이 빠진 날(앱을 안 연 날)은 그 사이 비중을 그대로 들고 있었다고 봄."""
    log = load_portfolio_log() if log is None else log
    if len(log) < 2:
        return None
    eq, prev_w, n_reb, pts, rets = 1.0, {}, 0, [(pd.Timestamp(log[0]["date"]), 1.0)], []
    for a, b in zip(log[:-1], log[1:]):
        w = a["w"]
        turn = sum(abs(w.get(s, 0.0) - prev_w.get(s, 0.0)) for s in set(w) | set(prev_w))
        days = max((pd.Timestamp(b["date"]) - pd.Timestamp(a["date"])).days, 1)
        r = sum(x * (b["c"][s] / a["c"][s] - 1) for s, x in w.items() if a["c"].get(s) and b["c"].get(s))
        step = r - turn * (TAKER_FEE_PCT + slip_pct) / 100 - sum(w.values()) * TSM_FUNDING_DAILY * days
        eq *= 1 + step
        rets.append(step)
        n_reb += turn > 1e-9
        pts.append((pd.Timestamp(b["date"]), eq))
        prev_w = w
    curve = pd.Series([v for _, v in pts], index=[t for t, _ in pts])
    days_total = (curve.index[-1] - curve.index[0]).days
    return {"curve": curve, "total": float(eq - 1), "max_dd": float((curve / curve.cummax() - 1).min()),
            "days": days_total, "records": len(log), "missing": max(days_total + 1 - len(log), 0),
            "rebalances": int(n_reb), "gate_days": int(sum(1 for e in log if e.get("gate"))),
            "start": log[0]["date"], "end": log[-1]["date"]}


# ==========================================================================
# v32.4: 장기 후보(그림자 기록) — '월봉 수렴 + 긴 바닥' 코인 명단을 매달 기록하고 이후 성과를 추적
#  표시·기록만 함. 추천 규칙·위험·순위에는 전혀 쓰지 않음.
#  2026-10-22 검증: 이 명단 코인에서 나온 앱 4시간봉 신호는 거래당 R이 나머지의 약 2배(사이클 B, 3개 자료 모두)였지만
#  '매달 같은 수의 코인을 무작위로 골라도' 그만큼 나오는 경우가 5~18%라 우연을 배제하지 못함 → 실전 기록으로 확인.
# ==========================================================================
LT_LOG_FILE = "longterm_log.json"
LT_Q = 1 / 3                  # 월봉 수렴 하위 1/3 + 최고점 이후 주 수 상위 1/3 (스캔 코인 안에서 순위)
LT_HIST_START = "2020-12-18"  # 연구 자료와 같은 시작일 — 최고점을 이 날 이후(그리고 Bitget 선물 상장 이후)에서 찾음
LT_RECENT_DAYS = 430          # 매달 새로 받는 일봉 수 (월봉 12개 + 150일선 + 여유)
LT_CACHE_LAG_DAYS = 400       # 최고점 기록은 400일 전까지만 확정 저장 → 다음 달엔 최근 430일만 받으면 됨
LT_MA_EXIT = 150              # 참고 계산: 전날 일봉 종가가 150일 평균 위일 때만 들고 있었다면(아래면 쉬고, 다시 위면 다시 보유)
LT_KEEP_MONTHS = 120
LT_JUDGE_MONTHS = 12          # 판정(미리 정함): 명단 12개 이상 쌓이면 ① 명단 코인 신호 R > 나머지(명단 쪽 30건 이상)
LT_JUDGE_MIN_SIG = 30         #   ② 명단 평균(보유)이 전체 평균보다 높은 달이 12달 중 7달 이상 → 둘 다면 '쓸만한 후보'
LT_JUDGE_WIN = 7
# 연구 자료(Bitget 선물 일봉 2020-12-18 ~ 2025-08-31)의 코인별 최고가·날짜(현물 단위) — 처음 실행할 때 5년치를 다시 받지 않게
LT_SEED_UPTO = "2025-08-31"
LT_ATH_SEED = {
    "AAVE": (666.233, "2021-05-18"), "ADA": (3.10485, "2021-09-02"), "ALGO": (0.988, "2022-03-31"),
    "APT": (20.318, "2023-01-26"), "ARB": (2.4057, "2024-01-12"), "ATOM": (44.74, "2021-09-20"),
    "AVAX": (146.792, "2021-11-21"), "BAT": (0.3839, "2024-03-13"), "BCH": (1639.26, "2021-05-12"),
    "BNB": (901.23, "2025-08-22"), "BTC": (124539, "2025-08-14"), "CAKE": (4.8346, "2024-03-29"),
    "CFX": (0.55098, "2024-03-24"), "CKB": (0.03795, "2024-04-10"), "CRV": (6.8103, "2022-01-04"),
    "DOGE": (0.485, "2024-12-08"), "DOT": (55.114, "2021-11-04"), "DYM": (8.022, "2024-03-12"),
    "EIGEN": (5.6614, "2024-12-17"), "ERA": (1.9472, "2025-07-17"), "ETH": (4956.31, "2025-08-24"),
    "ETHFI": (8.692, "2024-03-27"), "FET": (3.4855, "2024-03-28"), "FIL": (237.775, "2021-04-01"),
    "HBAR": (0.40172, "2025-01-17"), "ICP": (150.03, "2021-05-26"), "INJ": (53.192, "2024-03-14"),
    "IO": (6.528, "2024-06-12"), "JST": (0.0427, "2025-07-29"), "JTO": (5.335, "2024-04-03"),
    "JUP": (1.8451, "2024-04-01"), "KAIA": (0.4231, "2024-12-02"), "LDO": (4.0408, "2024-01-11"),
    "LINK": (52.974, "2021-05-10"), "LTC": (412.98, "2021-05-10"), "LUMIA": (2.593, "2024-12-04"),
    "MAGIC": (2.3292, "2023-02-16"), "MANTA": (4.089, "2024-03-12"), "MINA": (1.7249, "2024-03-12"),
    "MUBARAK": (0.1623, "2025-03-27"), "NEAR": (20.619, "2022-01-14"), "NIL": (0.8768, "2025-03-24"),
    "OGN": (0.2821, "2024-03-12"), "ONDO": (2.1463, "2024-12-16"), "OP": (4.8675, "2024-03-06"),
    "PAXG": (3525.14, "2025-04-22"), "PENGU": (0.04702, "2025-01-06"), "PEPE": (2.83936e-05, "2024-12-09"),
    "PLUME": (0.11864, "2025-08-18"), "POL": (0.7685, "2024-12-03"), "PUMP": (0.005314, "2025-07-18"),
    "PYTH": (1.1475, "2024-03-16"), "QNT": (171.94, "2024-12-07"), "RAY": (8.6909, "2025-01-23"),
    "RENDER": (11.869, "2024-12-05"), "RVN": (0.06114, "2024-03-31"), "S": (0.9909, "2025-02-21"),
    "SAND": (6.9595, "2021-12-26"), "SEI": (1.14592, "2024-03-16"), "SOL": (295.642, "2025-01-19"),
    "STRK": (2.6761, "2024-03-13"), "SUI": (5.3705, "2025-01-06"), "TAO": (745, "2024-12-06"),
    "TIA": (21.1859, "2024-02-10"), "TRUMP": (90.16, "2025-01-19"), "TRX": (0.45067, "2024-12-04"),
    "UNI": (45.019, "2021-05-03"), "VIRTUAL": (2.5893, "2025-05-27"), "W": (1.275, "2024-04-04"),
    "WLD": (11.97, "2024-03-10"), "XAUT": (3530.4, "2025-04-22"), "XLM": (0.79975, "2021-05-16"),
    "XRP": (3.6655, "2025-07-18"), "ZK": (0.2727, "2024-12-07"), "ZRO": (7.57, "2024-12-06")}


def _lt_wk(s: pd.Series) -> pd.Series:
    """주봉 이름 = 그 주의 일요일 날짜 (pandas W-SUN과 같음, 월요일 00시 UTC에 새 주 시작)"""
    n = pd.to_datetime(s).dt.normalize()
    return n + pd.to_timedelta(6 - n.dt.weekday, unit="D")


def longterm_features(daily: Dict[str, pd.DataFrame], t, ath: Optional[Dict] = None) -> pd.DataFrame:
    """월 시작 t 기준(t 전에 끝난 봉만): F1 = 최근 3개월 폭 ÷ 최근 12개월 폭(월봉) — 작을수록 '수렴',
    F4 = 최고점 이후 지난 주 수(일요일 마감 주봉) — 클수록 '긴 바닥'. 연구 계산(mw2)과 같음.
    스캔 코인 안에서 백분위를 매겨 F1 하위 1/3 + F4 상위 1/3이 명단. ath = {심볼: (최고가, 날짜)}: 일봉보다 오래된 구간의 최고점.
    표시용으로 지금 기준 고점 대비·150일선 위/아래·90일 수익률도 함께 계산."""
    t = pd.Timestamp(t).normalize()
    last_wk = t - pd.Timedelta(days=1)
    last_wk = last_wk - pd.Timedelta(days=(last_wk.weekday() + 1) % 7)       # t 전의 마지막 일요일
    months = pd.date_range(end=t - pd.offsets.MonthBegin(1), periods=12, freq="MS")
    rows = []
    for s, d0 in daily.items():
        if d0 is None or not len(d0):
            continue
        d0 = d0.sort_values("ts")
        d = d0[d0["ts"] < t]
        if not len(d):
            continue
        x = d.set_index("ts")
        mh = x["high"].resample("MS").max().reindex(months)
        ml = x["low"].resample("MS").min().reindex(months)
        den = mh.max() - ml.min()
        f1 = float((mh.iloc[-3:].max() - ml.iloc[-3:].min()) / den) if pd.notna(den) and den > 0 else np.nan
        dw = d[_lt_wk(d["ts"]) <= last_wk]
        hh, hd = np.nan, None
        if len(dw):
            i = int(np.argmax(dw["high"].to_numpy()))
            hh, hd = float(dw["high"].iloc[i]), pd.Timestamp(dw["ts"].iloc[i])
        a = (ath or {}).get(s)
        if a and pd.Timestamp(a[1]) <= last_wk and (hd is None or not hh > float(a[0])):
            hh, hd = float(a[0]), pd.Timestamp(a[1])                           # 같으면 더 오래된 쪽(연구와 같음)
        f4 = float((last_wk - _lt_wk(pd.Series([hd])).iloc[0]).days // 7) if hd is not None else np.nan
        c = d0["close"].astype(float)
        top = max(float(d0["high"].max()), float(a[0]) if a else 0.0)
        rows.append({"sym": s, "F1": f1, "F4": f4, "close": float(c.iloc[-1]),
                     "dd": float(c.iloc[-1] / top - 1) if top > 0 else np.nan,
                     "a150": (bool(c.iloc[-1] > c.iloc[-LT_MA_EXIT:].mean()) if len(c) >= LT_MA_EXIT else None),
                     "r90": float(c.iloc[-1] / c.iloc[-91] - 1) if len(c) > 90 else np.nan})
    F = pd.DataFrame(rows)
    if not len(F):
        return F
    F["F1_p"] = F["F1"].rank(pct=True)
    F["F4_p"] = F["F4"].rank(pct=True)
    F["sel"] = (F["F1_p"] < LT_Q) & (F["F4_p"] > 1 - LT_Q)
    return F


def _lt_parse(txt: str) -> Dict:
    try:
        raw = json.loads(txt) if txt and txt.strip() else {}
    except Exception:
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    months = [m for m in raw.get("months") or [] if isinstance(m, dict) and m.get("month")
              and isinstance(m.get("sel"), list) and isinstance(m.get("p0"), dict)]
    ath = {k: v for k, v in (raw.get("ath") or {}).items() if isinstance(v, list) and len(v) == 3}
    return {"months": months, "ath": ath}


def load_longterm_log() -> Dict:
    try:
        with open(LT_LOG_FILE, encoding="utf-8") as f:
            v = _lt_parse(f.read())
    except Exception:
        v = {"months": [], "ath": {}}
    v["months"] = sorted(v["months"], key=lambda m: m["month"])
    return v


def _merge_lt_log(local_txt: str, remote_txt: str) -> str:
    """두 곳의 장기 후보 기록 합치기: 달마다 먼저 만든 명단(처음 본 명단·가격)을 남기고, 최고점 기록은 더 최근까지 확정된 쪽."""
    a, b = _lt_parse(remote_txt), _lt_parse(local_txt)
    months: Dict[str, Dict] = {}
    for m in a["months"] + b["months"]:
        cur = months.get(m["month"])
        if cur is None or str(m.get("made_at", "")) < str(cur.get("made_at", "")):
            months[m["month"]] = m
    ath = dict(a["ath"])
    for k, v in b["ath"].items():
        if k not in ath or str(v[2]) > str(ath[k][2]):
            ath[k] = v
    return json.dumps({"months": [months[k] for k in sorted(months)][-LT_KEEP_MONTHS:], "ath": ath}, ensure_ascii=False)


def _save_longterm_log(obj: Dict) -> None:
    try:
        with _FILE_LOCK:
            mine = json.dumps(obj, ensure_ascii=False, default=str)
            cur = open(LT_LOG_FILE, encoding="utf-8").read() if os.path.exists(LT_LOG_FILE) else ""
            text = _merge_lt_log(mine, cur) if cur.strip() else _merge_lt_log(mine, "")
            with open(LT_LOG_FILE, "w", encoding="utf-8") as f:
                f.write(text)
    except Exception as e:
        print(f"[warn] 장기 후보 기록 저장 실패: {e}")


def longterm_month(now: Optional[pd.Timestamp] = None) -> str:
    """명단의 달 = 월봉 기준(UTC). 한국 시간으로는 매달 1일 09시에 다음 달로 넘어감."""
    return (now or utc_now()).strftime("%Y-%m")


def longterm_due(now: Optional[pd.Timestamp] = None) -> Optional[str]:
    """이번 달 명단이 아직 없으면 그 달('2026-10'), 있으면 None."""
    m = longterm_month(now)
    return None if any(x["month"] == m for x in load_longterm_log()["months"]) else m


def bitget_perp_last_prices() -> Dict[str, float]:
    """Bitget USDT-M 선물 현재가 {선물 심볼: 가격(선물 표기)}. 실패하면 빈 dict."""
    try:
        ex = _get_ex("bitget")
        ex.load_markets()
    except Exception as e:
        print(f"[warn] Bitget 시장 목록 조회 실패: {e}")
        return {}
    tk: Dict = {}
    for params in ({"type": "swap", "productType": "USDT-FUTURES"}, {"productType": "USDT-FUTURES"}):
        try:
            tk = ex.fetch_tickers(None, params) or {}
            if tk:
                break
        except Exception as e:
            print(f"[warn] Bitget 선물 시세 조회 실패: {e}")
    out: Dict[str, float] = {}
    for s, v in tk.items():
        p = s if ":" in s else f"{s}:{QUOTE}"
        try:
            px = float((v or {}).get("last") or (v or {}).get("close") or 0)
        except Exception:
            px = 0.0
        if px > 0 and p.endswith(f"/{QUOTE}:{QUOTE}"):
            out[p] = px
    return out


def longterm_scan(top_n: Optional[int] = None, progress_cb=None, now: Optional[pd.Timestamp] = None) -> Dict:
    """이번 달 장기 후보 명단 만들기 (스캔 코인 전체 일봉 → 월봉·주봉). 기록은 record_longterm()이 함.
    코인당 최근 430일 일봉 1번(처음 보는 코인만 2020-12부터 전체) — 100개 코인이면 몇 분 걸릴 수 있어 앱에서는 뒤에서 돌림."""
    now = now or utc_now()
    t = now.normalize().replace(day=1)
    saved = (dict(UNIVERSE_DIAG), dict(_VOL_DIAG))      # 뒤에서 돌 때 화면의 '코인 목록 진단'을 덮어쓰지 않게
    try:
        perps = bitget_perp_symbols()
        uni = build_universe(top_n or TOP_N_BY_VOLUME, perps or None)
    finally:
        UNIVERSE_DIAG.clear()
        UNIVERSE_DIAG.update(saved[0])
        _VOL_DIAG.clear()
        _VOL_DIAG.update(saved[1])
    if not uni:
        raise RuntimeError("스캔할 코인 목록을 받지 못했어요.")
    cache = dict(load_longterm_log()["ath"])
    new_upto = (now - pd.Timedelta(days=LT_CACHE_LAG_DAYS)).normalize()
    daily, ath_in, errs = {}, {}, []
    items = list(uni.items())
    for k, (sym, info) in enumerate(items):
        if progress_cb:
            progress_cb(k, len(items), sym.split("/")[0])
        base = sym.split("/")[0]
        c = cache.get(base)
        if c:
            a, upto = (float(c[0]), str(c[1])), pd.Timestamp(c[2])
        elif base in LT_ATH_SEED:
            a, upto = LT_ATH_SEED[base], pd.Timestamp(LT_SEED_UPTO)
        else:
            a, upto = None, None
        days = (max(LT_RECENT_DAYS, (now - upto).days + 10) if upto is not None
                else (now - pd.Timestamp(LT_HIST_START)).days + 2)
        try:
            d = drop_unclosed(fetch_ohlcv(info.get("src") or "bitget", sym, "1d", days), "1d")
        except Exception:
            d = None
        if d is None or len(d) < 2:
            errs.append(base)
            continue
        d = d[["ts", "high", "low", "close"]].copy()
        d[["high", "low", "close"]] = d[["high", "low", "close"]].astype(float)
        daily[sym] = d
        if a:
            ath_in[sym] = a
        old = d[d["ts"] <= new_upto]                    # 다음 달부터 다시 안 받을 구간의 최고점 확정
        if upto is not None:
            old = old[old["ts"] > upto]
        h, hd = (float(a[0]), str(a[1])) if a else (None, None)
        if len(old):
            i = int(np.argmax(old["high"].to_numpy()))
            if h is None or float(old["high"].iloc[i]) > h:
                h, hd = float(old["high"].iloc[i]), str(pd.Timestamp(old["ts"].iloc[i]).date())
        if h is not None and (upto is None or new_upto > upto):
            cache[base] = [float(f"{h:.6g}"), hd, str(new_upto.date())]
    if progress_cb:
        progress_cb(len(items), len(items), "계산")
    F = longterm_features(daily, t, ath_in)
    if not len(F):
        raise RuntimeError("일봉을 받은 코인이 없어요.")
    px = bitget_perp_last_prices()
    p0: Dict[str, float] = {}
    sel = []
    for r_ in F.sort_values("F1").to_dict("records"):
        info = uni.get(r_["sym"]) or {}
        perp, mult = info.get("perp") or r_["sym"], int(info.get("mult") or 1)
        p = px.get(perp) or r_["close"] * mult              # 시세 조회 실패 → 어제 일봉 종가(선물 표기)
        p0[perp] = float(f"{p:.8g}")
        if r_["sel"]:
            sel.append({"s": r_["sym"], "perp": perp, "m": mult, "f1": round(r_["F1"], 4), "wk": int(r_["F4"]),
                        "dd": round(r_["dd"], 4) if pd.notna(r_["dd"]) else None, "a150": r_["a150"],
                        "r90": round(r_["r90"], 4) if pd.notna(r_["r90"]) else None})
    rec = {"month": t.strftime("%Y-%m"), "t": str(t.date()), "made_at": str(now), "v": APP_VERSION, "q": round(LT_Q, 4),
           "n": int(len(F)), "sel": sel, "p0": p0, "price_src": "선물 현재가" if px else "어제 일봉 종가", "err": errs[:30]}
    return {"rec": rec, "ath": cache}


def record_longterm(res: Dict) -> bool:
    """명단 기록(달마다 1번, 처음 만든 것) + 최고점 기록 저장. 새로 기록했으면 True."""
    if not res or not res.get("rec"):
        return False
    log = load_longterm_log()
    new = not any(m["month"] == res["rec"]["month"] for m in log["months"])
    _save_longterm_log({"months": log["months"] + ([res["rec"]] if new else []), "ath": res.get("ath") or log["ath"]})
    return new


def _lt_ma_hold(d: pd.DataFrame, made: pd.Timestamp) -> Optional[tuple]:
    """기록한 날부터 '전날 종가가 150일 평균 위인 날만 보유'했다면의 수익(일봉 종가 기준)과 지금 보유 상태.
    (명단 코인은 바닥권이라 기록 때 150일선 아래인 경우가 많아 '아래로 내려가면 정리' 한 번으로는 의미가 없음)"""
    c = d.drop_duplicates("ts").set_index("ts")["close"].astype(float).sort_index()
    if len(c) < 2:
        return None
    ma = c.rolling(LT_MA_EXIT, min_periods=LT_MA_EXIT).mean()
    on = (c > ma).shift(1, fill_value=False).astype(bool)
    m = c.index >= made.normalize()
    r = c.pct_change().fillna(0.0)
    ret = float(np.prod(1 + r[m].to_numpy() * on[m].to_numpy()) - 1)
    return ret, (bool(c.iloc[-1] > ma.iloc[-1]) if pd.notna(ma.iloc[-1]) else None)


def longterm_tracking(log: Optional[Dict] = None, tracks: Optional[List[Dict]] = None,
                      prices: Optional[Dict[str, float]] = None, daily: Optional[Dict[str, pd.DataFrame]] = None,
                      max_months: int = 12) -> Optional[Dict]:
    """기록된 명단의 이후 성과: ① 명단 코인을 기록 시점 가격에 사서 들고 있었다면(같은 비중) vs 스캔 코인 전체 평균
    ② 150일선 위에서만 들고 있었다면 ③ 기록 뒤 그 달에 앱이 낸 신호(실전 추적, 끝난 것)의 거래당 R — 명단 코인 vs 나머지.
    수수료·펀딩은 빼지 않은 단순 가격 수익(비교용)."""
    log = load_longterm_log() if log is None else log
    months = log["months"][-max_months:]
    if not months:
        return None
    px = bitget_perp_last_prices() if prices is None else prices
    tracks = load_tracks() if tracks is None else tracks
    if daily is None:                                   # 150일선 계산용 일봉 (명단 코인만)
        need: Dict[str, int] = {}
        for m in months:
            for x in m["sel"]:
                need[x["s"]] = max(need.get(x["s"], 0), (utc_now() - pd.Timestamp(m["made_at"])).days + LT_MA_EXIT + 15)
        daily = {}
        for s, days in need.items():
            try:
                d = drop_unclosed(fetch_ohlcv("bitget", s, "1d", days), "1d")
                if d is not None and len(d):
                    daily[s] = d[["ts", "close"]].copy()
            except Exception:
                pass
    done = [t for t in tracks if t.get("status") == "종료" and t.get("R") is not None]
    rows, detail, all_in, all_out = [], [], [], []
    for k, m in enumerate(months):
        made = pd.Timestamp(m["made_at"])
        end = pd.Timestamp(m["t"]) + pd.offsets.MonthBegin(1)
        names = {x["s"] for x in m["sel"]}
        ret = lambda perp: (px[perp] / m["p0"][perp] - 1) if px.get(perp) and m["p0"].get(perp) else None
        uni_r = [v for v in (ret(p) for p in m["p0"]) if v is not None]
        hold, mae = [], []
        for x in m["sel"]:
            r_ = ret(x["perp"])
            if r_ is None:
                continue
            hold.append(r_)
            mh_ = _lt_ma_hold(daily[x["s"]], made) if x["s"] in daily else None
            if mh_:
                mae.append(mh_[0])
            if k == len(months) - 1:
                detail.append({"코인": x["s"].split("/")[0], "기록 가격": m["p0"].get(x["perp"]), "지금": px.get(x["perp"]),
                               "그냥 보유(%)": round(100 * r_, 1),
                               "150일선 위에서만(%)": round(100 * mh_[0], 1) if mh_ else None,
                               "지금 150일선": ("위" if mh_[1] else "아래") if mh_ and mh_[1] is not None else "-"})
        sig = [t for t in done if made <= pd.Timestamp(t["signal_ts"]) < end]
        s_in = [float(t["R"]) for t in sig if t["symbol"] in names]
        s_out = [float(t["R"]) for t in sig if t["symbol"] not in names]
        all_in += s_in
        all_out += s_out
        avg = lambda v: float(np.mean(v)) if v else None
        rows.append({"month": m["month"], "made_at": m["made_at"], "n_sel": len(m["sel"]), "n_uni": m.get("n", len(m["p0"])),
                     "hold": avg(hold), "ma": avg(mae), "uni": avg(uni_r),
                     "diff": (avg(hold) - avg(uni_r)) if hold and uni_r else None,
                     "sig_in": (len(s_in), avg(s_in)), "sig_out": (len(s_out), avg(s_out))})
    full = [r_ for r_ in rows if r_["diff"] is not None]
    judge = {"months": len(log["months"]), "need": LT_JUDGE_MONTHS, "win": sum(1 for r_ in full if r_["diff"] > 1e-9),
             "sig_in": (len(all_in), float(np.mean(all_in)) if all_in else None),
             "sig_out": (len(all_out), float(np.mean(all_out)) if all_out else None)}
    judge["ready"] = judge["months"] >= LT_JUDGE_MONTHS
    judge["pass"] = bool(judge["ready"] and judge["win"] >= LT_JUDGE_WIN and len(all_in) >= LT_JUDGE_MIN_SIG
                         and all_out and np.mean(all_in) > np.mean(all_out))
    return {"rows": rows, "detail": detail, "judge": judge, "at": utc_now(), "prices_ok": bool(px)}


def longterm_judge_text(j: Dict) -> str:
    si, so = j["sig_in"], j["sig_out"]
    sig = (f"명단 코인 신호 {si[0]}건 {si[1]:+.2f}R vs 나머지 {so[0]}건 {so[1]:+.2f}R" if si[1] is not None and so[1] is not None
           else f"명단 코인 신호 {si[0]}건 · 나머지 {so[0]}건 (끝난 거래가 아직 적음)")
    head = (f"명단 {j['months']}개 쌓임 → 판정: " + ("✅ 쓸만한 후보 (적용 여부는 그때 따로 결정)" if j["pass"] else "❌ 아직 근거 부족")
            if j["ready"] else f"명단 {j['months']}/{j['need']}개 — 판정 전")
    return f"{head} · 전체 평균보다 오른 달 {j['win']}개 · {sig}"


# ---------- 시장 국면(5년 검증 분류): 표시용 — 국면별로 설정을 바꾸는 건 5년 교차 검증에서 효과가 없어 하지 않음
PHASE_NAMES = ["대상승", "상승·횡보", "하락·횡보", "대하락"]
PHASE_RULE = "BTC 일봉 종가 vs 200일 평균 + 60일 수익률(±20%)"
# 5년(2021.10~2026.10) 검증: v29 세 매매법(수동 시각 09·13·17·21시) 거래당 R, v28 포트폴리오의 그 국면 연환산 수익
PHASE_STATS = {
    "대상승": {"long": (237, 0.19), "short": (66, 2.11), "port": "+310%",
              "note": "포트폴리오가 5년 수익 대부분을 낸 국면이에요. 추천 신호는 같은 설정 그대로"},
    "상승·횡보": {"long": (199, 0.61), "short": (309, 0.43), "port": "−15%",
               "note": "추천 신호 성과가 가장 고르게 좋았던 국면이에요. 포트폴리오는 대부분 쉬어요"},
    "하락·횡보": {"long": (162, 0.41), "short": (338, 0.01), "port": "−8%",
               "note": "숏 신호가 본전 수준이었던 국면이에요(끄면 두 사이클 결과가 엇갈려 끄지는 않음)"},
    "대하락": {"long": (25, 0.12), "short": (140, 0.20), "port": "0%(쉼)",
             "note": "2022년엔 플러스, 2026년엔 마이너스로 사이클마다 달랐어요. 포트폴리오는 관문이 꺼져 쉬어요"},
}


def phase_from_close(btc_close: pd.Series) -> Optional[Dict]:
    """BTC 일봉 종가(마감된 봉만) → 오늘의 국면"""
    b = btc_close.dropna()
    if len(b) < 200:
        return None
    sma = b.rolling(200, min_periods=200).mean(); r60 = b / b.shift(60) - 1
    lab = pd.Series(np.where(b > sma, np.where(r60 >= 0.20, 0, 1), np.where(r60 <= -0.20, 3, 2)), index=b.index)[sma.notna()]
    k = int(lab.iloc[-1]); days = int((lab.iloc[::-1] == k).cumprod().sum())
    name = PHASE_NAMES[k]
    return {"idx": k, "name": name, "close": float(b.iloc[-1]), "sma200": float(sma.iloc[-1]),
            "gap": float(b.iloc[-1] / sma.iloc[-1] - 1), "r60": float(r60.iloc[-1]), "days": days,
            "date": b.index[-1], "stats": PHASE_STATS[name]}


# ==========================================================================
# v29.9 거시 방향 (기간별) — 5년 검증으로 고른 것만 '판단', 나머지는 사실·참고로만 표시
#  · 앞으로 2주: BTC 2주 방향 모델(근거 9개 로지스틱 회귀). 사이클 교차 시험(A로 학습→B, B로 학습→A)에서
#    적중 56%·58%로 단일 지표(30일 수익률 ±5%: 53%·54%)보다 나아 채택. 화면 확률은 '학습에 안 쓴 구간'의 실제 결과.
#  · 앞으로 1~3일: 후보 4개+다수결 모두 두 사이클 적중 50% 이하 → 판단하지 않고 최근 3일 변화만 표시
#  · 2달 이상: 후보 모두 2024년 이후 적중 50% 미만 → 200일선 위치만 참고로 표시
#  · 이 모델로 추천 신호를 거르는 규칙은 미리 정한 기준 미달(2021~23 연수익 감소)이라 추천에는 쓰지 않음
# ==========================================================================
MID_MODEL = {"features": ["r7", "r30", "r90", "d_e50", "d_s200", "breadth", "macro", "alt_r30", "altbtc_r30"], "mu": [0.00389, 0.019767, 0.060728, 0.005551, 0.04148, 0.394575, -0.07744, 0.002752, -0.022188], "sd": [0.070751, 0.157028, 0.292612, 0.097733, 0.246853, 0.336633, 0.43512, 0.237498, 0.131868], "coef": [0.153709, 0.441884, 0.258385, -0.412581, -0.405876, -0.165119, 0.198707, 0.164745, -0.324279], "intercept": 0.088111, "bins": [{"lo": 0, "hi": 0.4, "n": 219, "p_up": 0.388, "mean": -0.0221, "alt_p_up": 0.288, "alt_mean": -0.0676}, {"lo": 0.4, "hi": 0.45, "n": 201, "p_up": 0.498, "mean": -0.0027, "alt_p_up": 0.408, "alt_mean": -0.0267}, {"lo": 0.45, "hi": 0.55, "n": 616, "p_up": 0.511, "mean": 0.0057, "alt_p_up": 0.477, "alt_mean": -0.016}, {"lo": 0.55, "hi": 0.6, "n": 329, "p_up": 0.556, "mean": 0.0111, "alt_p_up": 0.483, "alt_mean": 0.004}, {"lo": 0.6, "hi": 1.0, "n": 440, "p_up": 0.586, "mean": 0.006, "alt_p_up": 0.568, "alt_mean": 0.0263}], "trained": "2021-10-19~2026-09-27", "n": 1805}
BTC_HOLD_MA = 150   # v30: 5년 검증(고정 교차) 통과 — 100·150·200일 모두 비슷, 150일은 두 기간 중 낮은 샤프가 가장 큰 대표값
BTC_HOLD_STATS = {"A": {"cagr": 0.24, "mdd": -0.37, "bh_cagr": -0.14, "bh_mdd": -0.79},
                  "B": {"cagr": 0.18, "mdd": -0.31, "bh_cagr": 0.13, "bh_mdd": -0.57}}
MID_FEAT_KR = {"r7": "BTC 최근 7일", "r30": "BTC 30일", "r90": "BTC 90일", "d_e50": "BTC가 50일 평균보다",
               "d_s200": "BTC가 200일선보다", "breadth": "20일선 위 알트 비율", "macro": "일봉 흐름 점수",
               "alt_r30": "알트 지수 30일", "altbtc_r30": "알트/BTC 30일"}


def mid_state(p: float) -> tuple:
    """2주 방향 확률 → (상태 키, 표시 이름)"""
    if p <= 0.40:
        return "down", "🔻 하락 우위"
    if p <= 0.45:
        return "down_w", "↘ 약한 하락 우위"
    if p < 0.55:
        return "flat", "→ 방향 없음"
    if p < 0.60:
        return "up_w", "↗ 약한 상승 우위"
    return "up", "🔺 상승 우위"


def macro_direction(btc_4h: pd.DataFrame, btc_daily: Optional[pd.DataFrame], loaded: Dict[str, tuple],
                    macro_score: Optional[float]) -> Dict:
    """기간별 거시 방향. btc_4h·btc_daily는 완성봉, loaded는 스캔에서 받은 코인 캔들 {심볼: (거래소, df)}."""
    out: Dict = {"short": {}, "mid": {}, "long": {}}
    try:
        c4 = btc_4h["close"].astype(float)
        if len(c4) > 18:
            out["short"] = {"r3d": float(c4.iloc[-1] / c4.iloc[-19] - 1)}
    except Exception:
        pass
    if btc_daily is None or len(btc_daily) < 201:
        out["mid"]["error"] = "BTC 일봉 부족"
        return out
    cd = btc_daily.set_index("ts")["close"].astype(float)
    sma200 = cd.rolling(200).mean()
    out["long"] = {"d_s200": float(cd.iloc[-1] / sma200.iloc[-1] - 1), "close": float(cd.iloc[-1]), "sma200": float(sma200.iloc[-1])}
    # v30: BTC 보유 판단 — 일봉 종가가 150일선 위면 보유, 아래로 마감하면 청산 후 대기 (숏은 검증 탈락)
    s150 = cd.rolling(BTC_HOLD_MA).mean()
    ab = (cd > s150)[s150.notna()]
    if len(ab):
        run = int(ab.groupby((ab != ab.shift()).cumsum()).cumcount().iloc[-1] + 1)
        out["hold"] = {"above": bool(ab.iloc[-1]), "ma": float(s150.iloc[-1]), "close": float(cd.iloc[-1]),
                       "gap": float(cd.iloc[-1] / s150.iloc[-1] - 1), "days": run, "asof": str(cd.index[-1].date())}
    closes = {}
    for sym, (_, d) in (loaded or {}).items():
        b = sym.split("/")[0]
        if b in ("BTC", "ETH", "XAUT", "PAXG") or d is None or len(d) < 60:
            continue
        v = _daily_closes(d)
        if len(v) >= 40:
            closes[sym] = v
    if len(closes) < 10:
        out["mid"]["error"] = "알트 자료 부족"
        return out
    CD = pd.DataFrame(closes).sort_index()
    alt = (1 + CD.pct_change().clip(-0.5, 0.5).mean(axis=1, skipna=True).fillna(0)).cumprod()
    last_day = min(cd.index[-1], CD.index[-1])
    above = pd.Series({k: v.loc[:last_day].iloc[-1] > v.loc[:last_day].ewm(span=20, adjust=False).mean().iloc[-1]
                       for k, v in closes.items() if len(v.loc[:last_day]) >= 20})
    cdl = cd.loc[:last_day]
    altl = alt.loc[:last_day]
    e50 = cdl.ewm(span=50, adjust=False).mean()
    ab = (altl / cdl.reindex(altl.index)).dropna()
    feat = {"r7": cdl.iloc[-1] / cdl.iloc[-8] - 1, "r30": cdl.iloc[-1] / cdl.iloc[-31] - 1, "r90": cdl.iloc[-1] / cdl.iloc[-91] - 1,
            "d_e50": cdl.iloc[-1] / e50.iloc[-1] - 1, "d_s200": cdl.iloc[-1] / sma200.loc[:last_day].iloc[-1] - 1,
            "breadth": float(above.mean()), "macro": macro_score,
            "alt_r30": altl.iloc[-1] / altl.iloc[-31] - 1 if len(altl) > 31 else None,
            "altbtc_r30": ab.iloc[-1] / ab.iloc[-31] - 1 if len(ab) > 31 else None}
    if any(feat[k] is None or not np.isfinite(feat[k]) for k in MID_MODEL["features"]):
        out["mid"]["error"] = "근거 일부 계산 불가(" + ", ".join(k for k in MID_MODEL["features"]
                                                       if feat[k] is None or not np.isfinite(feat[k])) + ")"
        return out
    z = {k: (float(feat[k]) - mu) / sd for k, mu, sd in zip(MID_MODEL["features"], MID_MODEL["mu"], MID_MODEL["sd"])}
    contrib = {k: c * z[k] for k, c in zip(MID_MODEL["features"], MID_MODEL["coef"])}
    p = float(1 / (1 + np.exp(-(MID_MODEL["intercept"] + sum(contrib.values())))))
    key, label = mid_state(p)
    b = next(x for x in MID_MODEL["bins"] if x["lo"] < p <= x["hi"] or (p <= 0 and x["lo"] == 0))
    out["mid"] = {"p": p, "state": key, "label": label, "bin": b, "asof": str(last_day.date()), "n_alts": len(closes),
                  "feat": {k: float(v) for k, v in feat.items()},
                  "reasons": sorted(({"k": k, "c": float(v), "v": float(feat[k])} for k, v in contrib.items()),
                                    key=lambda r: -abs(r["c"]))[:3]}
    return out


def market_phase() -> Optional[Dict]:
    """오늘(마지막 마감 일봉)의 시장 국면. 받지 못하면 None."""
    for ex_id in EXCHANGES:
        try:
            d = drop_unclosed(fetch_ohlcv(ex_id, f"BTC/{QUOTE}", "1d", 300), "1d")
        except Exception:
            continue
        if d is not None and len(d) >= 200:
            return phase_from_close(d.set_index("ts")["close"])
    return None


# ==========================================================================
# 성장 시뮬레이터: 실제 거래 결과 분포를 다시 뽑아(부트스트랩) 앞으로의 계좌를 여러 번 시뮬레이션
# ==========================================================================
def simulation_source(tracks: List[Dict], ref: Optional[Dict]) -> tuple:
    """성장 시뮬레이터에 쓸 거래 결과: 실전 추적 30건 이상 → 과거 검증 → 가정치 순.
    반환 (R 목록, 출처 설명)."""
    live = [t["R"] for t in tracks if t.get("status") == "종료" and t.get("R") is not None]
    if len(live) >= 30:
        return live, f"실전 추적 {len(live)}건"
    if ref:
        key = "immediate" if BREAKOUT_ENTRY == "immediate" and ref.get("immediate") else "base"
        v = ref.get(key) or {}
        if len(v.get("R", [])) >= 20:
            return v["R"], f"과거 검증({'돌파 즉시 진입' if key == 'immediate' else '박스 돌파'}) {len(v['R'])}건"
    rng = np.random.default_rng(3)  # 가정치: 승률 30%, 거래당 평균 약 +0.3R의 돌파형 분포
    wins = rng.lognormal(np.log(3.33) - 0.405, 0.9, 300)
    synth = np.where(rng.random(1000) < 0.3, rng.choice(wins, 1000), -rng.uniform(0.8, 1.1, 1000))
    return list(synth), "가정치(승률 30%·거래당 약 +0.3R) — 연구실 실행 또는 실전 추적 30건 후 실제 값으로 바뀜"


GOAL_USD = 500e8 / 1380          # 500억 원 ≈ 3,620만 달러 (환율 1,380원 가정)


def growth_projection(R: List[float], trades_per_year: float, risk_pct: float, years: float,
                      start: float, monthly_deposit: float = 0.0, n_sims: int = 2000,
                      cap_start: float = 50_000, decay_per_double: float = 0.1, seed: int = 0) -> Dict:
    """- 거래 결과: R 목록에서 무작위로 다시 뽑음(실제 분포의 두꺼운 꼬리 유지)
    - 체결 한계: 계좌가 cap_start를 넘으면 두 배가 될 때마다 거래당 R이 decay_per_double씩 줄어듦
    - 매달 monthly_deposit 추가 입금
    반환: 연도별 하위10%/중앙값/상위10%, 목표(500억) 도달 확률, 고점 대비 90% 이상 손실 확률"""
    R = np.asarray([r for r in R if r is not None], dtype=float)
    if len(R) < 10:
        raise ValueError("거래 결과가 10건 미만이라 시뮬레이션할 수 없어요")
    rng = np.random.default_rng(seed)
    k = max(1, int(trades_per_year * years))
    per_month = max(trades_per_year / 12, 1e-9)
    eq = np.full(n_sims, float(start))
    peak, ruin, hit = eq.copy(), np.zeros(n_sims, bool), np.zeros(n_sims, bool)
    f = risk_pct / 100
    marks = {int(round(trades_per_year * y)): y for y in range(1, int(np.ceil(years)) + 1)}
    marks[k] = years
    out = {"year": [], "p10": [], "p50": [], "p90": []}
    dep_every = per_month  # 거래 몇 건마다 한 달이 지나는지 (= 한 달 거래 수)
    next_dep = dep_every
    for i in range(1, k + 1):
        adj = decay_per_double * np.maximum(0.0, np.log2(np.maximum(eq, 1e-9) / cap_start))
        r = rng.choice(R, n_sims) - adj
        eq = np.maximum(eq * (1 + f * r), 1e-6)
        while monthly_deposit and i >= next_dep - 1e-6:
            eq += monthly_deposit
            next_dep += dep_every
        peak = np.maximum(peak, eq)
        ruin |= eq / peak <= 0.1
        hit |= eq >= GOAL_USD
        if i in marks:
            out["year"].append(marks[i])
            for q, key in ((10, "p10"), (50, "p50"), (90, "p90")):
                out[key].append(float(np.percentile(eq, q)))
    months = years * 12
    return {**out, "hit": float(hit.mean()), "ruin": float(ruin.mean()),
            "deposited": float(start + monthly_deposit * months), "n_trades": k, "avg_R": float(R.mean())}


# ==========================================================================
# 전략 연구실: 미리 정한 소수의 후보만, 개발 구간에서 비교하고 확인 구간에서 검증
# ==========================================================================
LAB_TAGS: List[str] = []   # 표시 후보는 모두 실제 데이터에서 탈락(변동성 수축·일봉 방향·알트/BTC 대비·강한 마감·OBV 매집)
LAB_VARIANTS = {"donchian": "신고점 돌파(메이저·약 20일)",
                "don_structure": "신고점 돌파 · 구조 기반 손절·목표", "don_weekly": "신고점 돌파 · 주봉 추세 일치",
                "don_volume": "신고점 돌파 · 거래량 동반", "don_tsm": "신고점 돌파 · 포트폴리오 보유 코인"}
# (ATR 추적선 전환은 2년 정밀 검증에서 최근 8개월 −0.11R로 탈락 → 후보에서 제외)
DON_KEYS = {"donchian": "atr", "don_structure": "structure", "don_weekly": "weekly", "don_volume": "volume",
            "don_tsm": "tsm"}
V27_LAB = {"retest": "돌파 후 되돌림", "pb": "추세 눌림목", "div": "위치 다이버전스"}   # 새 매매법 (연구실에서 성과 확인·자동 방어 기준선)
LAB_ALPHA = 0.05  # 시험 후보 수로 나눠서 사용 (여러 개를 시험하면 우연히 좋아 보이는 게 나오기 때문)


def _load_lab_data(n_coins: int, days: int, progress_cb=None) -> Dict:
    """연구실용 과거 데이터: 과거를 가장 길게 주는 거래소 선택 → 거래량 구간별 균등 표본 → 코인·상위봉 데이터."""
    bar_hours = pd.Timedelta(TIMEFRAME) / pd.Timedelta("1h")
    warmup = 250 if TIMEFRAME == "4h" else 800
    total_bars = int(days * 24 / bar_hours) + warmup
    diag: Dict = {"요청 봉 수": total_bars}
    ex_id, btc = None, None
    for cand in EXCHANGES:
        try:
            b_ = fetch_extended_ohlcv(cand, f"BTC/{QUOTE}", TIMEFRAME, total_bars)
        except Exception as e:
            diag[f"{cand} BTC"] = f"실패({str(e)[:60]})"
            continue
        diag[f"{cand} BTC"] = f"{len(b_)}봉"
        if len(b_) >= warmup + 100 and (btc is None or len(b_) > len(btc)):
            ex_id, btc = cand, b_
        if btc is not None and len(btc) >= 0.9 * total_bars:
            break
    if ex_id is None:
        raise RuntimeError(f"과거 데이터를 받을 수 있는 거래소가 없어요 — 진단: {diag}")
    perps = bitget_perp_symbols() if BITGET_ONLY else set()
    universe = build_universe(TIER_BOUNDS[-1][2], perps or None)
    diag.update(UNIVERSE_DIAG)
    ranked = [x for x in universe if x != f"BTC/{QUOTE}"]
    per = [n_coins // 3 + (1 if i < n_coins % 3 else 0) for i in range(3)]
    cands: List[tuple] = []
    for (label, a_, b_), k_ in zip(TIER_BOUNDS, per):
        seg = ranked[a_:b_]
        if seg and k_ > 0:
            idx = np.linspace(0, len(seg) - 1, num=min(k_, len(seg))).round().astype(int)
            cands += [(seg[j], label) for j in dict.fromkeys(idx.tolist())]
    diag["표본"] = ", ".join(f"{lb} {sum(1 for _, t_ in cands if t_ == lb)}개" for lb, _, _ in TIER_BOUNDS)
    have = {c_ for c_, _ in cands}
    extra = []
    for k_, sym_ in enumerate(ranked):   # 메이저는 신고점 돌파·추세 포트폴리오 검증을 위해 항상 포함
        if coin_group(sym_) == "메이저" and sym_ not in have:
            lb_ = next((lb for lb, a_, b_ in TIER_BOUNDS if a_ <= k_ < b_), TIER_BOUNDS[-1][0])
            extra.append((sym_, lb_))
    cands += extra
    diag["표본"] += f", 메이저 추가 {len(extra)}개"
    if not cands:
        raise RuntimeError(f"검증할 코인 후보가 없어요 — 진단: {diag}")
    coins, errors = [], []
    for k, (sym, tier) in enumerate(cands):
        if progress_cb:
            progress_cb(k, len(cands) * 2, f"데이터 받는 중 {sym}")
        try:
            d, htf = None, None
            for src in dict.fromkeys([ex_id, universe.get(sym, {}).get("src", ex_id)]):
                try:
                    d = fetch_extended_ohlcv(src, sym, TIMEFRAME, total_bars)
                except Exception:
                    d = None
                if d is not None and len(d) >= warmup + 100:
                    htf = fetch_extended_ohlcv(src, sym, HTF_TIMEFRAME, _htf_bars_needed(total_bars) + 250)
                    break
            if d is None or htf is None:
                errors.append(f"{sym}: 과거 데이터 부족")
                continue
            m = d.merge(btc[["ts", "close"]].rename(columns={"close": "btc_close"}), on="ts", how="inner")
            if len(m) < warmup + 100:
                errors.append(f"{sym}: BTC와 겹치는 기간 부족({len(m)}봉)")
                continue
            coins.append({"sym": sym, "tier": tier, "htf": htf,
                          "df": m[["ts", "open", "high", "low", "close", "volume"]].reset_index(drop=True),
                          "btc": pd.DataFrame({"ts": m["ts"], "close": m["btc_close"]}).reset_index(drop=True)})
        except Exception as e:
            errors.append(f"{sym}: {str(e)[:80]}")
    start, end = btc["ts"].iloc[warmup], btc["ts"].iloc[-1]
    if utc_now() - end > pd.Timedelta(TIMEFRAME) * 3:
        diag["⚠️ 최근 데이터 누락"] = f"마지막 봉 {end:%Y-%m-%d %H:%M}"
    btc_htf = None                     # 추세 포트폴리오: BTC 일봉(시장 관문 계산·BTC 보유)
    if HTF_TIMEFRAME == "1d":
        try:
            btc_htf = fetch_extended_ohlcv(ex_id, f"BTC/{QUOTE}", "1d", _htf_bars_needed(total_bars) + 250)
        except Exception as e:
            errors.append(f"BTC 일봉: {str(e)[:80]}")
    return {"exchange": ex_id, "coins": coins, "errors": errors, "diag": diag, "warmup": warmup,
            "start": start, "end": end, "days": days, "timeframe": TIMEFRAME, "btc_htf": btc_htf}


def _boot_p(a, b=None, n_boot: int = 3000, seed: int = 7) -> float:
    """부트스트랩: '평균(a) − 평균(b) ≤ 0'일 확률 (b가 없으면 '평균(a) ≤ 0'일 확률). 작을수록 우연이 아님.
    크립토 수익은 소수의 큰 거래에 쏠려 있어서 정규분포를 가정하는 t검정보다 이 방식이 안전함."""
    a = np.sort(np.asarray(a, dtype=float))   # 거래 순서와 무관하게: 같은 거래면 항상 같은 결과
    if len(a) < 5:
        return 1.0
    rng = np.random.default_rng(seed)
    ma = rng.choice(a, (n_boot, len(a))).mean(axis=1)
    if b is None:
        return float((ma <= 0).mean())
    b = np.sort(np.asarray(b, dtype=float))
    if len(b) < 5:
        return 1.0
    mb = rng.choice(b, (n_boot, len(b))).mean(axis=1)
    return float((ma - mb <= 0).mean())


def _rs(tr: List[Dict]) -> List[float]:
    return [x["R"] for x in tr]


def _avg(tr: List[Dict]) -> Optional[float]:
    return float(np.mean(_rs(tr))) if tr else None


def _tot(tr: List[Dict]) -> float:
    return float(np.sum(_rs(tr))) if tr else 0.0


def _ex_top5(tr: List[Dict]) -> Optional[float]:
    r = sorted(_rs(tr), reverse=True)
    return float(np.mean(r[5:])) if len(r) > 5 else None


def _tiers_ok(tr: List[Dict], min_n: int = 10):
    """표본이 min_n건 이상인 거래량 구간이 모두 플러스인지 (구간이 하나도 없으면 판단 불가 → False)."""
    parts, ok_any, ok_all = [], False, True
    for label, _, _ in TIER_BOUNDS:
        v = [x for x in tr if x.get("tier") == label]
        if len(v) >= min_n:
            ok_any = True
            ok_all &= _avg(v) > 0
            parts.append(f"{label.replace('거래량 ', '')} {_avg(v):+.2f}")
    return ok_any and ok_all, " / ".join(parts) or "표본 부족"


def _split(tr: List[Dict], split_ts) -> tuple:
    return [x for x in tr if x["entry_ts"] < split_ts], [x for x in tr if x["entry_ts"] >= split_ts]


def _better(t: List[Dict], u: List[Dict], min_n: int = 5) -> bool:
    """두 묶음 모두 min_n건 이상일 때만 '더 좋음'을 판단 (한쪽이 비면 비교 불가 → False)."""
    return len(t) >= min_n and len(u) >= min_n and _avg(t) > _avg(u)


def _verdict(checks: Dict[str, bool], trial: bool = False) -> str:
    """✅ 통과: 모든 기준 충족 / 🧪 시험 운용(trial=True인 후보): 개발·확인 구간 기준은 넘었지만 표본·우연 확률 등
    나머지 일부 미달 → 리스크 ¼로 실전 투입해 기록으로 확인 / 🟡 보류(표시): 방향만 맞음 / ❌ 탈락"""
    if all(checks.values()):
        return "✅ 통과"
    core = [k for k in checks if k.startswith(("개발", "확인"))]
    if all(checks[k] for k in core):
        return "🧪 시험 운용" if trial else "🟡 보류"
    return "❌ 탈락"


def run_strategy_lab(n_coins: int = 30, days: int = 730, progress_cb=None) -> Dict:
    """전략 연구실 실행.
    - 기준: 박스 돌파(리테스트 지정가 대기), 분할익절+추적손절
    - 진입 방식 후보: 돌파 즉시 진입, 신고점 돌파(약 20일)
    - 표시 후보: 변동성 수축, 일봉 방향 일치, 알트 지수 대비 강함, BTC 대비 강함, 강한 마감
    - 기간의 앞 2/3 = 개발 구간(비교·선택), 뒤 1/3 = 확인 구간(선택 뒤 검증)
    - 비용: 지정가 메이커 / 시장가·손절 테이커 + 거래량 구간별 슬리피지"""
    data = _load_lab_data(n_coins, days, progress_cb)
    coins = data["coins"]
    alt = build_alt_index([c["df"] for c in coins if c["sym"].split("/")[0] not in ("BTC", "ETH")])
    trades: Dict[str, List[Dict]] = {"base": [], **{k: [] for k in DON_KEYS}, **{k: [] for k in V27_LAB}}
    for k, c in enumerate(coins):
        if progress_cb:
            progress_cb(len(coins) + k, len(coins) * 2, f"검증 중 {c['sym']}")
        df, slip = c["df"], SLIPPAGE_BY_TIER.get(c["tier"], DEFAULT_SLIPPAGE_PCT)
        alt_al = align_to(df, alt)
        sig = generate_signals(df, c["btc"], c["htf"], warmup=data["warmup"], alt_df=alt_al, families={"돌파"})
        dsig = generate_donchian_signals(df, c["btc"], c["htf"], alt_al, warmup=data["warmup"]) \
            if coin_group(c["sym"]) == "메이저" else {}   # 신고점 돌파는 메이저 전용
        closes_ = df["close"].to_numpy()

        def _rrc(i, x):   # 손익비 상한: v27 신호는 예전 정의로 미리 계산한 값(rr_cap) 사용
            if x.get("rr_cap") is not None:
                return x["rr_cap"]
            return abs(x["tp1"] - closes_[i]) / abs(closes_[i] - x["sl"]) if abs(closes_[i] - x["sl"]) > 0 else 99.0
        sig_b = {i: x for i, x in sig.items() if _rrc(i, x) < BOX_RR_MAX}
        reg_c = trend_series(df)
        runs = {"base": simulate_exits(df, sig_b, "partial_trail", entry_mode="immediate", slip_pct=slip),
                **{key: simulate_exits(df, donchian_variant_signals(dsig, var), "partial_trail", slip_pct=slip)
                   for key, var in DON_KEYS.items()},
                **{key: simulate_exits(df, {i: x for i, x in v27_family_signals(df, fam, reg_c).items() if i >= data["warmup"]},
                                       "partial_trail", entry_mode="immediate", slip_pct=slip)
                   for key, fam in V27_LAB.items()}}
        for key, tr in runs.items():
            for x in tr:
                x.update(symbol=c["sym"], tier=c["tier"], entry_ts=df["ts"].iloc[x["entry_idx"]],
                         exit_ts=df["ts"].iloc[x["exit_idx"]])
            trades[key] += tr
    if progress_cb:
        progress_cb(1, 1, "")
    split_ts = data["start"] + (data["end"] - data["start"]) * 2 / 3
    k_tests = len(LAB_TAGS) + len(LAB_VARIANTS) + 1 + len(V27_LAB)  # +1: 추세 포트폴리오
    alpha = LAB_ALPHA / k_tests
    base = trades["base"]
    bd, bh = _split(base, split_ts)
    base_tier_ok, base_tier_txt = _tiers_ok(base)
    baseline = {"dev_n": len(bd), "dev_avg": _avg(bd), "dev_tot": _tot(bd), "hold_n": len(bh),
                "hold_avg": _avg(bh), "hold_tot": _tot(bh), "tiers": base_tier_txt, "ex_top5": _ex_top5(base),
                "p": _boot_p(_rs(base)), "summary": summarize_trades(base)}
    rows: List[Dict] = []
    for key, name in LAB_VARIANTS.items():
        v = trades[key]
        vd, vh = _split(v, split_ts)
        tier_ok, tier_txt = _tiers_ok(v)
        ex5 = _ex_top5(v)
        if key == "immediate":   # 기존 박스 돌파의 진입 방식을 바꾸는 후보 → 기준과 비교
            checks = {"개발: 기준보다 합계 R 큼": _tot(vd) > _tot(bd),
                      "확인: 기준보다 합계 R 큼": _tot(vh) > _tot(bh),
                      f"확인: 평균 +{HOLD_MIN_R:.1f}R 이상": (_avg(vh) or 0) >= HOLD_MIN_R}
            apply = "박스 돌파 진입 방식 교체"
        else:                    # 새로 추가하는 신호 → 그 신호 자체의 거래당 성과로 평가
            checks = {f"개발: 평균 +{HOLD_MIN_R:.1f}R 이상": (_avg(vd) or 0) >= HOLD_MIN_R,
                      f"확인: 평균 +{HOLD_MIN_R:.1f}R 이상": (_avg(vh) or 0) >= HOLD_MIN_R}
            apply = "추적선 전환 신호 추가" if key == "supertrend" else "신고점 돌파 신호 추가"
        checks.update({"거래량 구간 모두 플러스": tier_ok,
                       "상위 5건 빼도 플러스": ex5 is not None and ex5 > 0,
                       "표본 30건 이상(개발)": len(vd) >= 30,
                       f"우연 확률 {alpha:.3f} 미만": _boot_p(_rs(v)) < alpha})
        rows.append({"kind": "진입 방식", "key": key, "name": name, "dev": f"{len(vd)}건 {(_avg(vd) or 0):+.2f}R (합계 {_tot(vd):+.0f})",
                     "hold": f"{len(vh)}건 {(_avg(vh) or 0):+.2f}R (합계 {_tot(vh):+.0f})", "tiers": tier_txt,
                     "checks": checks, "verdict": _verdict(checks, trial=key != "immediate"), "apply": apply,
                     "hold_avg": _avg(vh) or 0.0})
    for key, name in V27_LAB.items():   # v27 새 매매법: 그 신호 자체의 거래당 성과로 확인 (켜고 끄는 건 설정에서)
        v = trades[key]
        vd, vh = _split(v, split_ts)
        tier_ok, tier_txt = _tiers_ok(v)
        ex5 = _ex_top5(v)
        checks = {f"개발: 평균 +{HOLD_MIN_R:.1f}R 이상": (_avg(vd) or 0) >= HOLD_MIN_R,
                  f"확인: 평균 +{HOLD_MIN_R:.1f}R 이상": (_avg(vh) or 0) >= HOLD_MIN_R,
                  "거래량 구간 모두 플러스": tier_ok, "상위 5건 빼도 플러스": ex5 is not None and ex5 > 0,
                  "표본 30건 이상(개발)": len(vd) >= 30, f"우연 확률 {alpha:.3f} 미만": _boot_p(_rs(v)) < alpha}
        rows.append({"kind": "v27 매매법", "key": key, "name": name,
                     "dev": f"{len(vd)}건 {(_avg(vd) or 0):+.2f}R (합계 {_tot(vd):+.0f})",
                     "hold": f"{len(vh)}건 {(_avg(vh) or 0):+.2f}R (합계 {_tot(vh):+.0f})", "tiers": tier_txt,
                     "checks": checks, "verdict": _verdict(checks, trial=True),
                     "apply": "설정 → 추천할 신호 유형에서 켜고 끔", "hold_avg": _avg(vh) or 0.0})
    for tag in LAB_TAGS:
        tg = [x for x in base if tag in x.get("tags", [])]
        un = [x for x in base if tag not in x.get("tags", [])]
        td, th = _split(tg, split_ts)
        ud, uh = _split(un, split_ts)
        tier_ok, tier_txt = _tiers_ok(tg)
        ex5 = _ex_top5(tg)
        checks = {"개발: 표시 있음이 더 좋음": _better(td, ud),
                  "확인: 표시 있음이 더 좋음": _better(th, uh),
                  "확인: 평균 플러스": (_avg(th) or 0) > 0,
                  "거래량 구간 모두 플러스": tier_ok,
                  "상위 5건 빼도 플러스": ex5 is not None and ex5 > 0,
                  "표본 충분(개발 30/15건)": len(td) >= 30 and len(ud) >= 15,
                  f"우연 확률 {alpha:.3f} 미만": _boot_p(_rs(tg), _rs(un)) < alpha}
        as_filter = (_avg(ud) or 0) <= 0 and (_avg(uh) or 0) <= 0
        rows.append({"kind": "표시", "key": tag, "name": TAG_LABELS[tag],
                     "dev": f"있음 {len(td)}건 {(_avg(td) or 0):+.2f}R / 없음 {len(ud)}건 {(_avg(ud) or 0):+.2f}R",
                     "hold": f"있음 {len(th)}건 {(_avg(th) or 0):+.2f}R / 없음 {len(uh)}건 {(_avg(uh) or 0):+.2f}R",
                     "tiers": tier_txt, "checks": checks, "verdict": _verdict(checks),
                     "apply": "필터(표시 없으면 추천 안 함)" if as_filter else "우선순위(먼저 보여줌)"})
    # ---- 추세 포트폴리오 (일봉, 메이저+BTC, 시장 관문·매일 조정·롱)
    tsm = None
    if HTF_TIMEFRAME == "1d":
        uni_ = set(tsm_universe())
        closes = {c["sym"]: c["htf"].set_index("ts")["close"] for c in coins if c["sym"] in uni_}
        bh_ = data.get("btc_htf")
        btc_c = bh_.set_index("ts")["close"] if bh_ is not None and len(bh_) else None
        if btc_c is not None and f"BTC/{QUOTE}" in uni_:
            closes[f"BTC/{QUOTE}"] = btc_c
        if len(closes) >= 5:
            close = pd.DataFrame(closes).sort_index()
            g_ = tsm_gate(close, btc_c)["gate"] if (TSM_GATE and btc_c is not None) else False
            bt_ = tsm_backtest(close, gate=g_)
            bt0 = tsm_backtest(close, gate=False, band=0.25)                    # v27까지 방식(관문 없음·문턱 25%)
            win = (bt_["daily"].index >= data["start"]) & (bt_["daily"].index <= data["end"])
            daily = bt_["daily"][win]
            daily0 = bt0["daily"][win]
            ew = close.pct_change().mean(axis=1)[win].fillna(0.0)   # 같은 코인을 같은 비중으로 그냥 들고 있었다면
            s_all, s_ew = perf_stats(daily), perf_stats(ew)
            s_dev, s_hold = perf_stats(daily[daily.index < split_ts]), perf_stats(daily[daily.index >= split_ts])
            fmt = lambda st_: (f"연 {st_['cagr']:+.0%} · 낙폭 {st_['max_dd']:.0%} · 샤프 {st_['sharpe']:.2f}"
                               if st_.get("cagr") is not None else "표본 부족")
            checks = {"개발: 수익 플러스": s_dev.get("total", -1) > 0,
                      "확인: 수익 플러스": s_hold.get("total", -1) > 0,
                      "확인: 샤프 0.5 이상": s_hold.get("sharpe", 0) >= 0.5,
                      "최대 낙폭이 단순 보유보다 작음": s_all.get("max_dd", -1) > s_ew.get("max_dd", -1),
                      f"우연 확률 {alpha:.3f} 미만": _block_boot_p(daily.to_numpy()) < alpha}
            s_old = perf_stats(daily0)
            rows.append({"kind": "포트폴리오", "key": "tsm",
                         "name": "추세 포트폴리오(메이저+BTC·시장 관문·매일 조정·롱)" if g_ is not False
                                 else "추세 포트폴리오(메이저·관문 없음 — BTC 일봉 없음)",
                         "dev": fmt(s_dev), "hold": fmt(s_hold),
                         "tiers": f"단순 보유: {fmt(s_ew)} · 관문 없던 방식: {fmt(s_old)}", "checks": checks,
                         "verdict": _verdict(checks, trial=True), "apply": "오늘의 투자에 포트폴리오 표시"})
            tsm = {"all": s_all, "dev": s_dev, "hold": s_hold, "ew": s_ew, "old": s_old, "daily": daily, "ew_daily": ew,
                   "old_daily": daily0, "coins": list(closes), "gated": g_ is not False}
    months = max((data["end"] - data["start"]) / pd.Timedelta("30D"), 1e-9)
    per_coin_month = {k: len(v) / max(len(coins), 1) / months for k, v in trades.items()}
    passed = [r for r in rows if r["verdict"].startswith("✅")]
    trial = [r for r in rows if r["verdict"].startswith("🧪")]
    # 신고점 돌파: 통과한 변형 중 확인 구간 성과가 가장 좋은 것, 없으면 시험 운용 변형 중 가장 좋은 것 1개만
    don_pass = sorted([r for r in passed if r["key"] in DON_KEYS], key=lambda r: -r.get("hold_avg", 0))
    don_trial = sorted([r for r in trial if r["key"] in DON_KEYS], key=lambda r: -r.get("hold_avg", 0))
    don_pick = (don_pass or don_trial or [None])[0]
    tsm_row = next((r for r in rows if r["key"] == "tsm"), None)
    st_row = next((r for r in rows if r["key"] == "supertrend"), None)
    cfg = {"breakout_entry": "immediate",   # 정밀 검증(1시간봉)에서 리테스트 대기는 불리 → 항상 즉시 진입
           "donchian": don_pick is not None,
           "donchian_variant": DON_KEYS[don_pick["key"]] if don_pick else "atr",
           "trial_families": (["신고점 돌파"] if (don_pick is not None and not don_pass) else [])
                             + (["추적선 전환"] if (st_row and st_row["verdict"].startswith("🧪")) else []),
           "tsm_trial": bool(tsm_row and tsm_row["verdict"].startswith("🧪")),
           "priority_tags": [r["key"] for r in passed if r["kind"] == "표시" and r["apply"].startswith("우선")],
           "filter_tags": [r["key"] for r in passed if r["kind"] == "표시" and r["apply"].startswith("필터")],
           "tsm": bool(tsm_row and tsm_row["verdict"].startswith(("✅", "🧪"))),
           "supertrend": bool(st_row and st_row["verdict"].startswith(("✅", "🧪"))),
           "baseline_ok": (baseline["dev_avg"] or 0) > 0 and (baseline["hold_avg"] or 0) > 0,
           "made_at": str(utc_now()), "version": APP_VERSION, "timeframe": TIMEFRAME}
    return {**{k: data[k] for k in ("exchange", "errors", "diag", "start", "end", "days", "timeframe")},
            "coins": [c["sym"] for c in coins], "split_ts": split_ts, "k_tests": k_tests, "alpha": alpha,
            "baseline": baseline, "rows": rows, "config": cfg, "trades": trades, "ran_at": utc_now(),
            "per_coin_month": per_coin_month, "tsm": tsm}


def run_strategy_lab_and_save(n_coins: int = 30, days: int = 730, progress_cb=None) -> Dict:
    """연구실 실행 + 자동 방어 기준선 저장 (앱은 이 함수를 씀)."""
    lab = run_strategy_lab(n_coins, days, progress_cb)
    save_lab_reference(lab_reference_from(lab))
    return lab


def lab_frequency_text(lab: Dict, scan_n: Optional[int] = None) -> str:
    """'스캔 N개 기준 한 달에 몇 건' 예상 (과거 거래 수 기준, 실제는 시장 상황에 따라 크게 달라짐)."""
    n = scan_n or TOP_N_BY_VOLUME
    pcm = lab.get("per_coin_month", {})
    parts = [f"{name} 약 {pcm.get(key, 0) * n:.0f}건"
             for key, name in (("base", "박스 돌파"), ("retest", "돌파 후 되돌림"), ("pb", "추세 눌림목"), ("div", "위치 다이버전스"),
                               ("donchian", "신고점 돌파(기본)"), ("don_structure", "구조 기반"), ("don_volume", "거래량 동반"))]
    return f"📅 스캔 {n}개 기준 한 달 예상 거래 수: " + " · ".join(parts)


HOUR_GROUPS = {  # 1시간봉 '시작 시각'(한국시간) 기준
    "4시간봉 마감 직전 1시간": [0, 4, 8, 12, 16, 20],
    "4시간봉 마감 직후 1시간": [1, 5, 9, 13, 17, 21],
    "마감 1시간 뒤 (02·06·10·14·18·22시~)": [2, 6, 10, 14, 18, 22],
    "그 외 시간": [3, 7, 11, 15, 19, 23],
}
FUNDING_HOURS_KST = [1, 9, 17]  # Bitget 8시간 펀딩 정산 (코인마다 다를 수 있음)


def hourly_volatility_profile(n_coins: int = 10, days: int = 60, progress_cb=None) -> Dict:
    """한국시간 시간대별 평균 변동폭 (1시간봉 고가−저가).
    코인마다, 시기마다 변동성 크기가 달라서 각 봉의 변동폭을 그 코인의 '직전 1주 평균'으로 나눠 비교(평균 = 1.0)."""
    total = days * 24
    perps = bitget_perp_symbols() if BITGET_ONLY else set()
    uni = build_universe(30, perps or None)
    syms = [f"BTC/{QUOTE}"] + [x for x in uni if x != f"BTC/{QUOTE}"][:max(n_coins - 1, 0)]
    frames, used, errors = [], [], []
    for k, sym in enumerate(syms):
        if progress_cb:
            progress_cb(k, len(syms), sym)
        d = None
        for ex_id in dict.fromkeys([x for x in [uni.get(sym, {}).get("src"), *EXCHANGES] if x]):
            try:
                d = fetch_extended_ohlcv(ex_id, sym, "1h", total)
                if len(d) >= 24 * 14:
                    break
            except Exception:
                d = None
        if d is None or len(d) < 24 * 14:
            errors.append(sym)
            continue
        tr = (d["high"] - d["low"]) / d["open"] * 100
        rel = tr / tr.rolling(24 * 7, min_periods=24).mean().shift(1)
        frames.append(pd.DataFrame({"hour": (d["ts"] + pd.Timedelta(hours=9)).dt.hour, "rel": rel}))
        used.append(sym)
    if progress_cb:
        progress_cb(1, 1, "")
    if not frames:
        raise RuntimeError("1시간봉 데이터를 받지 못했어요 (네트워크·거래소 확인)")
    allf = pd.concat(frames).replace([np.inf, -np.inf], np.nan).dropna()
    allf = allf[allf["rel"] < 10]  # 극단적 이상치 제외
    prof = allf.groupby("hour")["rel"].mean().reindex(range(24))
    groups = {name: float(prof.loc[hs].mean()) for name, hs in HOUR_GROUPS.items()}
    return {"profile": {int(h): float(v) for h, v in prof.items()}, "groups": groups,
            "funding": float(prof.loc[FUNDING_HOURS_KST].mean()), "coins": used, "errors": errors,
            "days": days, "bars": int(len(allf)), "ran_at": utc_now()}


def hourly_vol_lines(hv: Dict) -> List[str]:
    g = hv["groups"]
    after1 = g["4시간봉 마감 직후 1시간"]
    after2 = g["마감 1시간 뒤 (02·06·10·14·18·22시~)"]
    lines = [f"{name}: 평균의 {v:.2f}배" for name, v in g.items()]
    lines.append(f"펀딩 정산 직후(01·09·17시~): 평균의 {hv['funding']:.2f}배")
    if after1 > after2 * 1.10:
        lines.append(f"✅ 관찰이 맞아요: 마감 직후 1시간이 그 다음 1시간보다 약 {(after1 / after2 - 1) * 100:.0f}% 더 출렁여요. "
                     f"마감 1시간 뒤에 확인·진입하는 게 덜 흔들린 가격을 보는 방법이에요.")
    elif after1 > after2:
        lines.append(f"🟡 마감 직후가 조금 더 출렁이지만 차이가 약 {(after1 / after2 - 1) * 100:.0f}%로 크지 않아요.")
    else:
        lines.append("ℹ️ 이 기간 데이터로는 마감 직후가 더 출렁인다는 차이가 보이지 않아요.")
    prof = pd.Series(hv["profile"]).dropna()
    calm = ", ".join(f"{h:02d}시" for h in prof.nsmallest(3).index)
    wild = ", ".join(f"{h:02d}시" for h in prof.nlargest(3).index)
    lines.append(f"가장 잠잠한 시간대: {calm} · 가장 출렁이는 시간대: {wild} (1시간봉 시작 시각, 한국시간)")
    return lines


EXPORT_CACHE_HOURS = 12  # 내보내기 도중 끊겨도 다시 누르면 이미 받은 코인은 이 시간 안에선 재사용


def _export_cached_ohlcv(src: str, sym: str, tf: str, total: int) -> pd.DataFrame:
    """연구용 내보내기 전용: 받은 캔들을 서버 임시 폴더에 저장해 두고, 중간에 끊겨 다시 누르면 이어서 받기.
    (5년치는 20~40분 걸려 폰 화면이 꺼지면 끊길 수 있음)"""
    import tempfile
    folder = os.path.join(tempfile.gettempdir(), "cmr_export_cache")
    path = os.path.join(folder, re.sub(r"[^A-Za-z0-9]+", "_", f"{src}_{sym}_{tf}_{total}") + ".pkl")
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < EXPORT_CACHE_HOURS * 3600:
            return pd.read_pickle(path)
    except Exception:
        pass
    df = fetch_extended_ohlcv(src, sym, tf, total)
    try:
        os.makedirs(folder, exist_ok=True)
        df.to_pickle(path)
    except Exception:
        pass
    return df


def export_research_data(n_coins: int = 100, days: int = 730, include_1h: bool = False, progress_cb=None,
                         only_1h: bool = False) -> bytes:
    """연구용 데이터 묶음(zip): 거래량 상위 n_coins개(+BTC)의 4시간봉·일봉(+선택: 상위 30개 1시간봉 1년),
    코인 메타정보(합산 거래량 순위, 데이터 거래소, Bitget 선물 심볼·가격 배수). CSV라 어디서나 읽을 수 있음.
    v26.1: 최대 5년(1825일). 상장 기간이 요청보다 짧은 코인도 상장일부터 받고, 두 거래소 중 더 긴 기록을 고름.
    1시간봉은 1년, 코인베이스 프리미엄 1시간봉은 최대 2년으로 제한(파일 크기·시간).
    v27.1: only_1h=True면 1시간봉 연구 전용 묶음 — 거래량 상위 n_coins개의 1시간봉(days일, 최대 2년)과 일봉(거시 흐름 계산용)만."""
    import io as _io
    import zipfile
    if only_1h:
        days = min(days, 730)
    total4h = int(days * 6) + 300
    main_tf, total_main = ("1h", days * 24 + 300) if only_1h else ("4h", total4h)
    if only_1h:
        include_1h = False
    ex_id, btc = None, None
    for cand in EXCHANGES:  # 과거 데이터를 가장 길게 주는 거래소
        try:
            b_ = _export_cached_ohlcv(cand, f"BTC/{QUOTE}", "4h", total4h)
        except Exception:
            continue
        if len(b_) >= 500 and (btc is None or len(b_) > len(btc)):
            ex_id, btc = cand, b_
        if btc is not None and len(btc) >= 0.9 * total4h:
            break
    if ex_id is None:
        raise RuntimeError("과거 데이터를 받을 수 있는 거래소가 없어요")
    perps = bitget_perp_symbols() if BITGET_ONLY else set()
    uni = build_universe(n_coins + 1, perps or None, rank_by="spot")
    syms = [f"BTC/{QUOTE}"] + [x for x in uni if x != f"BTC/{QUOTE}"][:n_coins]
    parts = {"4h": [], "1d": [], "1h": []}
    meta = {"exchange": ex_id, "made_at": str(utc_now()), "version": APP_VERSION, "days": days,
            "universe_diag": dict(UNIVERSE_DIAG), "coins": [], "mode": "1h_only" if only_1h else "standard"}
    jobs = len(syms) + (30 if include_1h else 0)
    for k, sym in enumerate(syms):
        if progress_cb:
            progress_cb(k, jobs, sym)
        info = uni.get(sym, {})
        got = None
        for src in dict.fromkeys([ex_id, info.get("src", ex_id)]):
            try:
                d4 = _export_cached_ohlcv(src, sym, main_tf, total_main)
            except Exception:
                d4 = None
            if d4 is not None and len(d4) >= 300 and (got is None or len(d4) > len(got[1])):
                got = (src, d4)
            if got is not None and len(got[1]) >= 0.9 * total_main:
                break  # 요청 기간을 거의 다 채웠으면 다른 거래소는 안 봄
        if got is None:
            continue
        src, d4 = got
        try:
            dd = _export_cached_ohlcv(src, sym, "1d", days + 300)
        except Exception:
            dd = None
        parts[main_tf].append(d4.assign(symbol=sym))
        if dd is not None and len(dd):
            parts["1d"].append(dd.assign(symbol=sym))
        meta["coins"].append({"symbol": sym, "rank": k, "src": src, "vol24h": info.get("vol"),
                              "perp": info.get("perp", ""), "mult": int(info.get("mult", 1)), f"bars{main_tf}": len(d4)})
    if include_1h:
        for k, c in enumerate(meta["coins"][:30]):
            if progress_cb:
                progress_cb(len(syms) + k, jobs, f"1시간봉 {c['symbol']}")
            try:
                d1 = _export_cached_ohlcv(c["src"], c["symbol"], "1h", 365 * 24)
                parts["1h"].append(d1.assign(symbol=c["symbol"]))
            except Exception:
                pass
    # 펀딩비·미결제약정(레버리지 쏠림) 기록: 거래소가 주는 만큼만 (상위 30개, 실패해도 다른 데이터는 그대로)
    deriv = {"funding": [], "oi": []}
    meta["deriv_errors"] = []
    since_ms = int((utc_now() - pd.Timedelta(days=min(days, 730))).timestamp() * 1000)
    for k, c in enumerate([] if only_1h else meta["coins"][:30]):
        if progress_cb:
            progress_cb(len(syms) + (30 if include_1h else 0) + k, jobs + 30, f"펀딩비·미결제약정 {c['symbol']}")
        base = c["symbol"].split("/")[0]
        perp_b = c["perp"] or f"{base}/{QUOTE}:{QUOTE}"
        perp_o = f"{base}/{QUOTE}:{QUOTE}"
        for ex_name, perp in (("okx", perp_o), ("bitget", perp_b), ("binance", perp_o)):
            try:
                ex = _get_ex(ex_name)
                ex.load_markets()
                if perp not in ex.markets:
                    continue
                rows, since, calls = [], since_ms, 0
                while calls < 60:
                    batch = ex.fetch_funding_rate_history(perp, since=since, limit=100)
                    calls += 1
                    if not batch:
                        break
                    rows += [(b["timestamp"], b.get("fundingRate")) for b in batch]
                    nxt = batch[-1]["timestamp"] + 1
                    if nxt <= since or len(batch) < 2:
                        break
                    since = nxt
                if rows:
                    deriv["funding"] += [(c["symbol"], ex_name, t, f) for t, f in rows]
                # 미결제약정: 거래소마다 과거 제공 범위가 짧아(OKX는 기간 지정 시 'Illegal time range') 최근 데이터만 요청
                oi, last_err = [], None
                for tf_, kw in (("1d", {"limit": 100}), ("4h", {"limit": 100}), ("1d", {})):
                    try:
                        oi = ex.fetch_open_interest_history(perp, tf_, **kw)
                        if oi:
                            break
                    except Exception as e:
                        last_err = e
                if oi:
                    deriv["oi"] += [(c["symbol"], ex_name, o["timestamp"], o.get("openInterestValue"),
                                     o.get("openInterestAmount")) for o in oi]
                elif last_err is not None:
                    meta["deriv_errors"].append(f"{c['symbol']} {ex_name} 미결제약정: {str(last_err)[:60]}")
                if rows:
                    break
            except Exception as e:
                meta["deriv_errors"].append(f"{c['symbol']} {ex_name} 펀딩비: {str(e)[:60]}")
    meta["deriv_errors"] = meta["deriv_errors"][:40]
    # 코인베이스 프리미엄 검증용: 미국 기관 자금이 드나드는 코인베이스(달러)와 해외 거래소(USDT)의 1시간봉
    prem, meta["premium_errors"] = [], []
    for ex_name, sym in (() if only_1h else (("coinbase", "BTC/USD"), ("coinbase", "ETH/USD"), ("okx", f"BTC/{QUOTE}"), ("okx", f"ETH/{QUOTE}"))):
        if progress_cb:
            progress_cb(jobs + 29, jobs + 30, f"코인베이스 프리미엄 {ex_name} {sym}")
        try:
            d = _export_cached_ohlcv(ex_name, sym, "1h", min(days, 730) * 24)
            prem.append(d.assign(exchange=ex_name, symbol=sym))
        except Exception as e:
            meta["premium_errors"].append(f"{ex_name} {sym}: {str(e)[:80]}")
    if progress_cb:
        progress_cb(1, 1, "압축 중")
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for tf, frames in parts.items():
            if frames:
                df = pd.concat(frames, ignore_index=True)
                df["ts"] = (pd.to_datetime(df["ts"]) - pd.Timestamp("1970-01-01")) // pd.Timedelta(milliseconds=1)  # 밀리초
                z.writestr(f"ohlcv_{tf}.csv", df[["symbol", "ts", "open", "high", "low", "close", "volume"]]
                           .to_csv(index=False, float_format="%.10g"))
        if deriv["funding"]:
            z.writestr("funding.csv", pd.DataFrame(deriv["funding"], columns=["symbol", "exchange", "ts", "funding_rate"])
                       .to_csv(index=False, float_format="%.8g"))
        if deriv["oi"]:
            z.writestr("open_interest.csv", pd.DataFrame(deriv["oi"], columns=["symbol", "exchange", "ts", "oi_value",
                                                                             "oi_amount"]).to_csv(index=False, float_format="%.10g"))
        if prem:
            pf_ = pd.concat(prem, ignore_index=True)
            pf_["ts"] = (pd.to_datetime(pf_["ts"]) - pd.Timestamp("1970-01-01")) // pd.Timedelta(milliseconds=1)
            z.writestr("premium_1h.csv", pf_[["exchange", "symbol", "ts", "open", "high", "low", "close", "volume"]]
                       .to_csv(index=False, float_format="%.10g"))
        meta["econ_events_kst"] = ECON_EVENTS
        z.writestr("meta.json", json.dumps(meta, ensure_ascii=False, default=str))
    return buf.getvalue()


_PERP_GRAN = {"1h": "1H", "4h": "4H", "1d": "1Dutc"}   # Bitget 선물 봉 단위(일봉은 UTC 00시 기준)


def _bitget_perp_history(perp: str, tf: str, days: int, mark: bool = False) -> pd.DataFrame:
    """v29.3: Bitget 선물 과거 캔들을 빈틈없이 받기 (현물 대비 검증용).
    ccxt 통합 함수는 오래된 구간에서 요청 개수와 거래소 한도(200개)가 어긋나 구간을 건너뛰는 경우가 있어,
    거래소 원본 API(history-candles)를 '창 하나 = 190봉, 한 봉 겹치기'로 직접 이어 받음. mark=True면 마크 가격 캔들."""
    ex = _get_ex("bitget")
    ex.load_markets()
    m = ex.market(perp)
    dur = ex.parse_timeframe(tf) * 1000
    span = (85 if tf == "1d" else 190) * dur                  # 거래소 제한: 한 번에 200개·90일 이내
    now = ex.milliseconds()
    t = now - days * 86_400_000
    t -= t % dur
    if m.get("created"):
        t = max(t, int(m["created"]) - (int(m["created"]) % dur))   # 상장 전 구간은 건너뜀
    name = "HistoryMarkCandles" if mark else "HistoryCandles"
    fn = getattr(ex, f"publicMixGetV2MixMarket{name}", None) or \
        getattr(ex, f"public_mix_get_v2_mix_market_{'history_mark_candles' if mark else 'history_candles'}")
    rows: List = []
    empty_run, fails = 0, 0
    while t < now:
        e = min(t + span, now)
        try:
            r = fn({"symbol": m["id"], "productType": "USDT-FUTURES", "granularity": _PERP_GRAN[tf],
                    "startTime": str(t), "endTime": str(e), "limit": "200"})
            data = (r or {}).get("data") or []
            fails = 0
        except Exception as ex_:
            fails += 1
            if fails >= 3:
                raise RuntimeError(f"{perp} {tf} 조회 실패: {str(ex_)[:80]}")
            time.sleep(1.0)
            continue
        if data:
            need = 4 if mark else 6                              # 시가·고가·저가·종가 (+ 거래량·거래대금)
            for x in data:
                vals = [float(v) for v in list(x[1:1 + need]) if v not in (None, "")]
                rows.append([int(x[0])] + vals + [np.nan] * (need - len(vals)))
            empty_run = 0
        elif rows:
            empty_run += 1
            if empty_run > 20:      # 받는 도중 오래 비면(상장 폐지 후 등) 멈춤
                break
        t = e - dur                 # 한 봉 겹쳐서 다음 창(경계 봉 누락 방지, 중복은 아래에서 제거)
        if e >= now:
            break
    cols = ["ts", "open", "high", "low", "close"] + ([] if mark else ["volume", "quote_volume"])
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame([r_[:len(cols)] for r_ in rows], columns=cols)
    df = df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    return drop_unclosed(df, tf)


def _export_cached_perp(perp: str, tf: str, days: int, mark: bool = False) -> pd.DataFrame:
    """선물 캔들도 내보내기 캐시(12시간) 사용 — 도중에 끊겨도 다시 누르면 이어서."""
    import tempfile
    folder = os.path.join(tempfile.gettempdir(), "cmr_export_cache")
    path = os.path.join(folder, re.sub(r"[^A-Za-z0-9]+", "_", f"bgperp_{'mark_' if mark else ''}{perp}_{tf}_{days}") + ".pkl")
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < EXPORT_CACHE_HOURS * 3600:
            return pd.read_pickle(path)
    except Exception:
        pass
    df = _bitget_perp_history(perp, tf, days, mark)
    try:
        os.makedirs(folder, exist_ok=True)
        df.to_pickle(path)
    except Exception:
        pass
    return df


def export_perp_data(n_coins: int = 100, days: int = 1825, include_mark: bool = True, progress_cb=None) -> bytes:
    """v29.3: Bitget USDT-M 선물 캔들 묶음(zip) — 현물로 검증한 매매법을 실제 거래 상품(선물) 차트로 다시 검증하기 위한 자료.
    거래량 상위 n_coins개(+BTC)의 선물 4시간봉·일봉(+선택: 마크 가격 4시간봉 — 손절 발동 기준 비교용).
    가격은 선물 표기 그대로(예: 1000PEPE) — meta.json의 mult로 나누면 현물 단위."""
    import io as _io
    import zipfile
    perps = bitget_perp_symbols()
    if not perps:
        raise RuntimeError("Bitget 선물 목록을 받지 못했어요. 잠시 후 다시 시도해 주세요.")
    uni = build_universe(n_coins + 1, perps, rank_by="spot")   # 현물 연구 자료와 같은 코인 목록
    syms = [f"BTC/{QUOTE}"] + [x for x in uni if x != f"BTC/{QUOTE}"][:n_coins]
    meta = {"kind": "bitget_perp", "made_at": str(utc_now()), "version": APP_VERSION, "days": days,
            "universe_diag": dict(UNIVERSE_DIAG), "coins": [], "errors": [],
            "note": "가격·거래량은 Bitget 선물 표기 그대로. 현물 단위 = 가격/mult, 거래량×mult. volume=코인 수량, quote_volume=USDT"}
    parts: Dict[str, List[pd.DataFrame]] = {"4h": [], "1d": [], "mark_4h": []}
    jobs = len(syms) * (3 if include_mark else 2)
    k = 0
    for sym in syms:
        info = uni.get(sym) or {}
        perp, mult = info.get("perp"), int(info.get("mult", 1))
        if not perp:
            perp, mult = perp_match(sym, perps)
        if not perp:
            meta["errors"].append(f"{sym}: Bitget 선물 없음")
            k += 3 if include_mark else 2
            continue
        bars = {}
        for tf, mark in (("4h", False), ("1d", False)) + ((("4h", True),) if include_mark else ()):
            if progress_cb:
                progress_cb(k, jobs, f"{'마크 ' if mark else ''}{tf} {perp.split(':')[0]}")
            k += 1
            try:
                d = _export_cached_perp(perp, tf, days + (300 if tf == "1d" else 50), mark)
            except Exception as e:
                meta["errors"].append(f"{sym} {'mark ' if mark else ''}{tf}: {str(e)[:80]}")
                continue
            if d is not None and len(d):
                parts["mark_4h" if mark else tf].append(d.assign(symbol=sym))
                bars[("mark_" if mark else "") + tf] = len(d)
        meta["coins"].append({"symbol": sym, "perp": perp, "mult": mult, "rank": len(meta["coins"]),
                              "vol24h": info.get("vol"), **{f"bars_{a}": b for a, b in bars.items()}})
    meta["errors"] = meta["errors"][:60]
    if progress_cb:
        progress_cb(1, 1, "압축 중")
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for key, frames in parts.items():
            if not frames:
                continue
            df = pd.concat(frames, ignore_index=True)
            df["ts"] = (pd.to_datetime(df["ts"]) - pd.Timestamp("1970-01-01")) // pd.Timedelta(milliseconds=1)
            cols = ["symbol", "ts", "high", "low", "close"] if key == "mark_4h" else \
                ["symbol", "ts", "open", "high", "low", "close", "volume", "quote_volume"]
            z.writestr(f"perp_{key}.csv" if key != "mark_4h" else "perp_mark_4h.csv",
                       df[cols].to_csv(index=False, float_format="%.10g"))
        z.writestr("meta.json", json.dumps(meta, ensure_ascii=False, default=str))
    return buf.getvalue()


# ==========================================================================
# v30.1: 시장가 매수·매도 거래량(Binance) 내보내기 — '매집' 검증용
#  캔들 거래량만으로는 누가 샀는지 알 수 없어서(2026-10-13 매집 검증 9개 신호 모두 탈락),
#  시장가로 산 거래량(taker buy)이 들어 있는 Binance 캔들을 받아 CVD(시장가 매수 − 매도 누적)로 다시 검증.
#  Binance는 미국 서버(폰용 Streamlit Cloud)에서 막혀 있어(HTTP 451) PC에서만 받을 수 있음.
# ==========================================================================
_BN_API = {"spot": ("https://api.binance.com", "/api/v3/klines", "/api/v3/exchangeInfo"),
           "perp": ("https://fapi.binance.com", "/fapi/v1/klines", "/fapi/v1/exchangeInfo")}
_BN_PREFIX = re.compile(r"^(1000000|100000|10000|1000|1M)(?=[A-Z])")


class BinanceBlocked(RuntimeError):
    pass


def _bn_get(market: str, path: str, params: Optional[dict] = None, tries: int = 4):
    """Binance 공개 API GET. 451/403(지역 차단)은 바로 BinanceBlocked, 429/418(과부하)은 기다렸다 재시도."""
    host = _BN_API[market][0]
    last = ""
    for _ in range(tries):
        try:
            r = requests.get(host + path, params=params or {}, timeout=20)
        except requests.exceptions.RequestException as e:
            last = str(e)[:80]
            time.sleep(2.0)
            continue
        if r.status_code in (451, 403):
            raise BinanceBlocked(f"Binance 접속이 막혀 있어요(HTTP {r.status_code}). 이 네트워크·서버에서는 받을 수 없어요 — PC에서 실행해 주세요.")
        if r.status_code in (418, 429):
            time.sleep(min(int(r.headers.get("Retry-After", "30") or 30), 120))
            continue
        if r.status_code != 200:
            last = f"HTTP {r.status_code} {r.text[:60]}"
            time.sleep(2.0)
            continue
        used = r.headers.get("X-MBX-USED-WEIGHT-1M") or r.headers.get("x-mbx-used-weight-1m")
        if used and int(used) > (1800 if market == "perp" else 4800):   # 분당 한도의 75% 넘으면 쉬어 감
            time.sleep(20)
        return r.json()
    raise RuntimeError(f"Binance {market} {path} 조회 실패: {last}")


def binance_symbol_map(market: str) -> Dict[str, tuple]:
    """{기초 코인: (Binance 심볼, 배수)} — 선물은 1000PEPE처럼 배수 표기를 기초 코인으로 되돌림(같은 이름이 있으면 그것 우선)."""
    info = _bn_get(market, _BN_API[market][2])
    out: Dict[str, tuple] = {}
    for s in info.get("symbols", []):
        if s.get("quoteAsset") != QUOTE or s.get("status") != "TRADING":
            continue
        if market == "perp" and s.get("contractType") != "PERPETUAL":
            continue
        if market == "spot" and not s.get("isSpotTradingAllowed", True):
            continue
        b = s.get("baseAsset", "")
        mm = _BN_PREFIX.match(b)
        if mm:
            pre = mm.group(1)
            mult = 1_000_000 if pre == "1M" else int(pre)
            out.setdefault(b[len(pre):], (s["symbol"], mult))
        out[b] = (s["symbol"], 1)                       # 이름 그대로인 심볼이 있으면 그걸 씀
    return out


def _binance_klines(market: str, bsym: str, tf: str, days: int) -> pd.DataFrame:
    """Binance 캔들(시장가 매수 거래량 포함)을 처음부터 끝까지 이어 받기. 상장 전 시작 시각을 주면 상장일부터 돌려줌."""
    dur = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}[tf]
    now = int(time.time() * 1000)
    t = now - days * 86_400_000
    t -= t % dur
    rows: List = []
    for _ in range(5000):
        data = _bn_get(market, _BN_API[market][1], {"symbol": bsym, "interval": tf, "startTime": t, "limit": 1000})
        if not data:
            break
        for x in data:
            if int(x[6]) >= now:                         # 아직 안 끝난 봉 제외
                continue
            rows.append([int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]),
                         int(x[8]), float(x[9])])
        t = int(data[-1][0]) + dur
        if len(data) < 1000 or t >= now:
            break
        time.sleep(0.15 if market == "perp" else 0.06)
    cols = ["ts", "open", "high", "low", "close", "volume", "trades", "taker_buy_volume"]
    df = pd.DataFrame(rows, columns=cols)
    return df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)


def _export_cached_bn(market: str, bsym: str, tf: str, days: int) -> pd.DataFrame:
    import tempfile
    folder = os.path.join(tempfile.gettempdir(), "cmr_export_cache")
    path = os.path.join(folder, re.sub(r"[^A-Za-z0-9]+", "_", f"bn_{market}_{bsym}_{tf}_{days}") + ".pkl")
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < EXPORT_CACHE_HOURS * 3600:
            return pd.read_pickle(path)
    except Exception:
        pass
    df = _binance_klines(market, bsym, tf, days)
    try:
        os.makedirs(folder, exist_ok=True)
        df.to_pickle(path)
    except Exception:
        pass
    return df


def export_taker_data(n_coins: int = 100, days: int = 1825, markets=("spot", "perp"), progress_cb=None) -> Dict[str, bytes]:
    """v30.1: Binance 현물·선물 4시간봉 + 시장가 매수 거래량 → 시장별 zip {market: bytes}.
    코인 목록은 지금까지의 연구 자료와 같음(Bitget 선물 있는 코인, 현물 거래량 순위). Binance에 없는 코인은 빠짐.
    단위는 현물 기준으로 맞춤(선물 1000PEPE 등은 가격÷배수, 거래량×배수). 일봉은 4시간봉 6개를 합쳐 만들면 됨(UTC 00시 기준)."""
    import io as _io
    import zipfile
    markets = [m for m in markets if m in _BN_API]
    if not markets:
        raise RuntimeError("시장을 하나 이상 고르세요.")
    maps = {m: binance_symbol_map(m) for m in markets}          # 여기서 막히면 BinanceBlocked로 바로 끝남
    perps = bitget_perp_symbols()
    if not perps:
        raise RuntimeError("Bitget 선물 목록을 받지 못했어요. 잠시 후 다시 시도해 주세요.")
    uni = build_universe(n_coins + 1, perps, rank_by="spot")
    syms = [f"BTC/{QUOTE}"] + [x for x in uni if x != f"BTC/{QUOTE}"][:n_coins]
    jobs = len(syms) * len(markets)
    k = 0
    out: Dict[str, bytes] = {}
    for market in markets:
        meta = {"kind": f"binance_taker_{market}", "made_at": str(utc_now()), "version": APP_VERSION, "days": days,
                "timeframe": "4h", "coins": [], "errors": [],
                "note": ("Binance " + ("현물" if market == "spot" else "USDT-M 선물") + " 4시간봉. taker_buy_volume = 시장가로 산 수량, "
                         "시장가 매도 수량 = volume − taker_buy_volume. trades = 체결 건수. 단위는 현물 기준(가격÷mult, 수량×mult).")}
        frames: List[pd.DataFrame] = []
        for sym in syms:
            base = sym.split("/")[0]
            if progress_cb:
                progress_cb(k, jobs, f"{'현물' if market == 'spot' else '선물'} {base}")
            k += 1
            hit = maps[market].get(base)
            if not hit:
                meta["errors"].append(f"{sym}: Binance {'현물' if market == 'spot' else '선물'} 없음")
                continue
            bsym, mult = hit
            try:
                d = _export_cached_bn(market, bsym, "4h", days + 100)
            except BinanceBlocked:
                raise
            except Exception as e:
                meta["errors"].append(f"{sym}: {str(e)[:80]}")
                continue
            if d is None or not len(d):
                meta["errors"].append(f"{sym}: 캔들 없음")
                continue
            d = d.copy()
            if mult != 1:
                for c in ("open", "high", "low", "close"):
                    d[c] = d[c] / mult
                for c in ("volume", "taker_buy_volume"):
                    d[c] = d[c] * mult
            frames.append(d.assign(symbol=sym))
            meta["coins"].append({"symbol": sym, "binance": bsym, "mult": mult, "rank": len(meta["coins"]), "bars_4h": len(d),
                                  "first": str(pd.to_datetime(d["ts"].iloc[0], unit="ms"))})
        meta["errors"] = meta["errors"][:80]
        if progress_cb:
            progress_cb(k, jobs, "압축 중")
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            if frames:
                df = pd.concat(frames, ignore_index=True)
                cols = ["symbol", "ts", "open", "high", "low", "close", "volume", "trades", "taker_buy_volume"]
                z.writestr(f"taker_{market}_4h.csv", df[cols].to_csv(index=False, float_format="%.8g"))
            z.writestr("meta.json", json.dumps(meta, ensure_ascii=False, default=str))
        out[market] = buf.getvalue()
    return out


# ==========================================================================
# 경제 일정 (한국시간). 출처: 연준 FOMC 일정(금리 결정 미 동부 14:00), 미 노동통계국 CPI 일정(08:30).
# 서머타임 반영해 변환. 2027년 FOMC는 연준의 잠정 일정이고, 2027년 CPI는 노동통계국 발표 후 추가해야 함.
# ==========================================================================
ECON_EVENTS = [
    ("2026-01-13 22:30", "CPI"), ("2026-01-29 04:00", "FOMC"), ("2026-02-13 22:30", "CPI"),
    ("2026-03-11 21:30", "CPI"), ("2026-03-19 03:00", "FOMC"), ("2026-04-10 21:30", "CPI"),
    ("2026-04-30 03:00", "FOMC"), ("2026-05-12 21:30", "CPI"), ("2026-06-10 21:30", "CPI"),
    ("2026-06-18 03:00", "FOMC"), ("2026-07-14 21:30", "CPI"), ("2026-07-30 03:00", "FOMC"),
    ("2026-08-12 21:30", "CPI"), ("2026-09-11 21:30", "CPI"), ("2026-09-17 03:00", "FOMC"),
    ("2026-10-14 21:30", "CPI"), ("2026-10-29 03:00", "FOMC"), ("2026-11-10 22:30", "CPI"),
    ("2026-12-10 04:00", "FOMC"), ("2026-12-10 22:30", "CPI"),
    ("2027-01-28 04:00", "FOMC(잠정)"), ("2027-03-18 03:00", "FOMC(잠정)"), ("2027-04-29 03:00", "FOMC(잠정)"),
    ("2027-06-10 03:00", "FOMC(잠정)"), ("2027-07-29 03:00", "FOMC(잠정)"), ("2027-09-16 03:00", "FOMC(잠정)"),
    ("2027-10-28 03:00", "FOMC(잠정)"), ("2027-12-09 04:00", "FOMC(잠정)"),
]
EVENT_PRE_H = 12       # 발표 몇 시간 전부터 위험 시간대
EVENT_POST_H = 4       # 발표 몇 시간 후까지 위험 시간대
EVENT_RISK_MULT = 0.5  # 위험 시간대 신규 진입 권장 리스크 배수
EVENT_LABEL = {"CPI": "미국 CPI(물가)", "FOMC": "FOMC 금리 결정", "FOMC(잠정)": "FOMC 금리 결정(잠정 일정)"}


def kst_now() -> pd.Timestamp:
    return utc_now() + pd.Timedelta(hours=9)


def next_events(now: Optional[pd.Timestamp] = None, n: int = 3) -> List[Dict]:
    """앞으로 다가올 경제 이벤트 (한국시간, 남은 시간 포함)."""
    now = now or kst_now()
    out = []
    for t, kind in ECON_EVENTS:
        ts = pd.Timestamp(t)
        if ts + pd.Timedelta(hours=EVENT_POST_H) >= now:
            out.append({"time": ts, "kind": kind, "label": EVENT_LABEL.get(kind, kind), "left": ts - now})
    return out[:n]


def active_event(now: Optional[pd.Timestamp] = None) -> Optional[Dict]:
    """지금이 이벤트 위험 시간대(발표 EVENT_PRE_H시간 전 ~ EVENT_POST_H시간 후)인지."""
    now = now or kst_now()
    for t, kind in ECON_EVENTS:
        ts = pd.Timestamp(t)
        if ts - pd.Timedelta(hours=EVENT_PRE_H) <= now <= ts + pd.Timedelta(hours=EVENT_POST_H):
            return {"time": ts, "kind": kind, "label": EVENT_LABEL.get(kind, kind), "left": ts - now,
                    "phase": "발표 전" if now < ts else "발표 후"}
    return None


def event_text(e: Dict) -> str:
    left = e["left"]
    if left.total_seconds() >= 0:
        h = int(left.total_seconds() // 3600)
        rest = f"{h // 24}일 {h % 24}시간 남음" if h >= 24 else f"{h}시간 {int(left.total_seconds() % 3600 // 60)}분 남음"
    else:
        rest = f"발표 {int(-left.total_seconds() // 60)}분 지남"
    return f"{e['label']} {e['time']:%m-%d %H:%M} ({rest})"


# --------------------------------------------------------------------------
# v32.3 FOMC 선반영 참고 문구 (사용자 결정 2026-10-18) — 표시만, 추천·필터·리스크 배수는 바꾸지 않음.
# 근거: README 'FOMC·CPI·빅테크 실적' 검증. FOMC 40회(2021-11~2026-09), 진입 = 발표 전 마지막 09·13·17·21시,
# 3일 수익률(그 시각 마감 4시간봉 기준)의 부호와 발표 뒤 24시간 반응. 사전 기준(탐색 전체)은 통과 못 했고
# 미리 정한 1차 가설(선반영 → 반전)만 확인된 것이라 '참고'로만 보여 줌. CPI·실적엔 이 경향이 없었음.
# --------------------------------------------------------------------------
FOMC_DRIFT_PRE_H = 72     # 발표 몇 시간 전부터 화면 상단 참고 문구를 보여 줄지
FOMC_DRIFT_POST_H = 24    # 발표 몇 시간 뒤까지
FOMC_DRIFT_REF = {        # 3일 수익률 부호별 발표 뒤 24시간 반응(Binance 선물 4시간봉, 알트 = BTC·ETH 뺀 동일 가중 지수)
    "n": 40,
    "alt_up": (11, 14), "alt_dn": (20, 26),      # 오른 채 → 24시간 하락 11/14 · 내린 채 → 24시간 상승 20/26
    "alt_cyc": "21~23년 16/18 · 24~26년 15/22",
    "btc_hit": 55, "eth_hit": 60,                # BTC·ETH는 같은 기준 적중률이 낮음(%)
}


def fomc_entry_kst(t0: pd.Timestamp) -> pd.Timestamp:
    """발표 15분 전까지 마지막 한국 09·13·17·21시(직접 매매 시각) — 검증의 진입 M."""
    c = (pd.Timestamp(t0) - pd.Timedelta(minutes=15) - pd.Timedelta(hours=9)).floor("4h")   # UTC 마감 시각
    while c.hour not in (0, 4, 8, 12):
        c -= pd.Timedelta(hours=4)
    return c + pd.Timedelta(hours=9)


def _ret3_at(df: Optional[pd.DataFrame], close_utc: pd.Timestamp) -> Optional[float]:
    """close_utc에 마감한 4시간봉 종가 ÷ 72시간 전 종가 − 1."""
    if df is None or not len(df):
        return None
    s = df.set_index("ts")["close"].astype(float)
    s = s[~s.index.duplicated(keep="last")]
    a, b = close_utc - pd.Timedelta(hours=4), close_utc - pd.Timedelta(hours=76)
    if a not in s.index or b not in s.index or not (s[b] > 0):
        return None
    return float(s[a] / s[b] - 1)


def fomc_drift_info(btc_df: Optional[pd.DataFrame], loaded: Optional[Dict] = None,
                    now: Optional[pd.Timestamp] = None) -> Optional[Dict]:
    """다가오는(또는 막 끝난) FOMC의 발표 전 3일 BTC·알트 수익률. 진입 시각(M) 전이면 최근 마감 봉 기준 미리 보기."""
    now = now or kst_now()
    ev = None
    for t, kind in ECON_EVENTS:
        ts = pd.Timestamp(t)
        if kind.startswith("FOMC") and ts - pd.Timedelta(hours=FOMC_DRIFT_PRE_H + 4) <= now <= ts + pd.Timedelta(hours=FOMC_DRIFT_POST_H):
            ev = (ts, kind)
            break
    if ev is None or btc_df is None or not len(btc_df):
        return None
    m_kst = fomc_entry_kst(ev[0])
    last_close = pd.Timestamp(btc_df["ts"].iloc[-1]) + pd.Timedelta(hours=4)         # 최근 마감 봉의 마감 시각(UTC)
    close_utc = min(m_kst - pd.Timedelta(hours=9), last_close)
    frames = [d for sym, (_, d) in (loaded or {}).items() if sym.split("/")[0] not in ("BTC", "ETH")]
    alt = build_alt_index(frames) if len(frames) >= 5 else None
    return {"time": ev[0], "kind": ev[1], "label": EVENT_LABEL.get(ev[1], ev[1]), "m_kst": m_kst,
            "basis_kst": close_utc + pd.Timedelta(hours=9), "fixed": close_utc >= m_kst - pd.Timedelta(hours=9),
            "btc": _ret3_at(btc_df, close_utc), "alt": _ret3_at(alt, close_utc), "n_alt": len(frames)}


def _fomc_drift_live(d: Optional[Dict], now: Optional[pd.Timestamp] = None, pre_h: float = FOMC_DRIFT_PRE_H) -> bool:
    if not d or d.get("alt") is None:
        return False
    now = now or kst_now()
    return d["time"] - pd.Timedelta(hours=pre_h) <= now <= d["time"] + pd.Timedelta(hours=FOMC_DRIFT_POST_H)


def fomc_drift_text(d: Optional[Dict], now: Optional[pd.Timestamp] = None) -> str:
    """화면 상단 일정 안내용 참고 문구(발표 3일 전 ~ 발표 뒤 24시간). 표시할 게 없으면 빈 문자열."""
    if not _fomc_drift_live(d, now):
        return ""
    R = FOMC_DRIFT_REF
    btc = f" · BTC {d['btc'] * 100:+.1f}%" if d.get("btc") is not None else ""
    basis = (f"{d['m_kst']:%m-%d %H:%M} 마감 봉 기준 확정" if d["fixed"] else
             f"{d['basis_kst']:%m-%d %H:%M} 마감 봉까지 — 판단 기준은 발표 전 마지막 매매 시각 {d['m_kst']:%m-%d %H:%M}")
    now = now or kst_now()
    verb = "맞은" if now >= d["time"] else "맞는"
    end_ = d["time"] + pd.Timedelta(hours=FOMC_DRIFT_POST_H)
    side = (f"알트가 {'오른' if d['alt'] > 0 else '내린'} 채 발표를 {verb} 쪽 → 과거엔 발표 뒤 24시간"
            f"({end_:%m-%d %H:%M}까지) {'되돌림(하락)' if d['alt'] > 0 else '반등'}이 잦았어요")
    return (f"📐 FOMC 선반영 참고 — {d['label']} {d['time']:%m-%d %H:%M}: 발표 전 3일 알트 {d['alt'] * 100:+.1f}%{btc} "
            f"({basis}). 지금은 {side}. 5년 FOMC {R['n']}회에서 알트가 3일 오른 채 맞은 {R['alt_up'][1]}회 중 "
            f"{R['alt_up'][0]}회는 발표 뒤 24시간 내렸고, 내린 채 맞은 {R['alt_dn'][1]}회 중 {R['alt_dn'][0]}회는 올랐어요"
            f"({R['alt_cyc']}). BTC·ETH는 이 경향이 약하고(같은 기준 {R['btc_hit']}~{R['eth_hit']}%), CPI·빅테크 실적엔 없었어요. "
            "사전 기준을 다 통과한 규칙이 아니라서 추천·리스크에는 반영하지 않은 참고 표시예요.")


def fomc_card_note(d: Optional[Dict], setup, now: Optional[pd.Timestamp] = None) -> str:
    """추천 카드용 한 줄(발표 EVENT_PRE_H시간 전 ~ 발표 뒤 24시간): 이 신호 방향이 되돌림 경향과 같은지·반대인지."""
    if setup is None or not _fomc_drift_live(d, now, pre_h=EVENT_PRE_H):
        return ""
    base = str(getattr(setup, "symbol", "")).split("/")[0]
    if base in ("BTC", "ETH"):
        return (f"FOMC 참고: BTC·ETH는 발표 전 3일 흐름의 되돌림 경향이 약했어요"
                f"({FOMC_DRIFT_REF['btc_hit']}~{FOMC_DRIFT_REF['eth_hit']}%) — 추천·리스크는 그대로")
    up = d["alt"] > 0
    k, n = FOMC_DRIFT_REF["alt_up"] if up else FOMC_DRIFT_REF["alt_dn"]
    is_long = getattr(setup, "bias", "") in LONG_BIASES
    same = (is_long and not up) or (not is_long and up)
    return (f"FOMC 참고: 발표 전 3일 알트 {d['alt'] * 100:+.1f}% → 과거 이런 때 발표 뒤 24시간 알트 "
            f"{'하락' if up else '상승'} {n}회 중 {k}회. 이 {'롱' if is_long else '숏'}은 그 경향과 "
            f"{'같은 방향' if same else '반대 방향'}이에요 — 추천·리스크는 그대로(참고만)")


def lab_config_text(cfg: Dict) -> str:
    parts = ["박스 돌파: 봉 마감 직후 시장가"]
    if cfg.get("donchian"):
        trial_ = "신고점 돌파" in cfg.get("trial_families", [])
        parts.append(f"신고점 돌파 통과({DONCHIAN_VARIANT_LABEL.get(cfg.get('donchian_variant', 'atr'), '')}"
                     + (", 🧪 시험 운용·리스크 ¼" if trial_ else "") + ") — 1시간 안 진입 가능할 때만 설정에서 직접 켜기")
    if cfg.get("tsm"):
        parts.append("추세 포트폴리오 켜짐" + (" 🧪 시험 운용·비중 ¼" if cfg.get("tsm_trial") else ""))
    if cfg.get("supertrend"):
        parts.append("ATR 추적선 전환 추가" + (" 🧪 시험 운용·리스크 ¼" if "추적선 전환" in cfg.get("trial_families", []) else ""))
    if cfg.get("priority_tags"):
        parts.append("우선 표시: " + ", ".join(TAG_LABELS[t] for t in cfg["priority_tags"]))
    if cfg.get("filter_tags"):
        parts.append("필수 표시: " + ", ".join(TAG_LABELS[t] for t in cfg["filter_tags"]))
    return " · ".join(parts)


def apply_lab_config(cfg: Dict, save: bool = True) -> None:
    """연구실에서 통과한 항목을 실전 추천 설정에 반영 (앱의 '이 설정 적용' 버튼). 파일로도 저장."""
    global BREAKOUT_ENTRY, PRIORITY_TAGS, FILTER_TAGS, TSM_ENABLED, DONCHIAN_VARIANT, TRIAL_FAMILIES, TSM_TRIAL
    BREAKOUT_ENTRY = "immediate"   # 예전 설정 파일에 'retest'가 남아 있어도 무시 (검증에서 불리했던 방식)
    TSM_ENABLED = bool(cfg.get("tsm"))
    DONCHIAN_VARIANT = cfg.get("donchian_variant", "atr")
    TRIAL_FAMILIES = set(cfg.get("trial_families", [])) | FIXED_TRIAL_FAMILIES
    TSM_TRIAL = bool(cfg.get("tsm_trial"))
    PRIORITY_TAGS = set(cfg.get("priority_tags", []))
    FILTER_TAGS = set(cfg.get("filter_tags", []))
    # 신고점 돌파는 연구실을 통과해도 자동으로 켜지 않음: 봉 마감 후 1시간 안에 진입해야 하고, 수동 운용에서
    # 낮 신호만 더하면 오히려 계좌 성과가 나빠졌음(동시 보유 한도를 차지) → 설정에서 직접 켜도록
    if cfg.get("supertrend"):
        ENABLED_FAMILIES.add("추적선 전환")
    if save:
        try:
            with open(LAB_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=1)
        except Exception as e:
            print(f"[warn] 연구실 설정 저장 실패: {e}")


def load_lab_config() -> Optional[Dict]:
    """저장된 연구실 설정을 읽어 적용 (없으면 None). Streamlit Cloud는 앱이 재시작되면 파일이 지워질 수 있음."""
    try:
        with open(LAB_CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        apply_lab_config(cfg, save=False)
        return cfg
    except Exception:
        return None


def reset_lab_config() -> None:
    apply_lab_config({"breakout_entry": "retest", "donchian": False, "priority_tags": [], "filter_tags": [], "tsm": False,
                      "donchian_variant": "atr", "trial_families": [], "tsm_trial": False}, save=False)
    ENABLED_FAMILIES.discard("신고점 돌파")
    ENABLED_FAMILIES.discard("추적선 전환")
    try:
        os.remove(LAB_CONFIG_FILE)
    except Exception:
        pass


def lab_report_text(lab: Dict) -> str:
    b = lab["baseline"]
    lines = [f"[전략 연구실 {APP_VERSION} · 기준=박스 돌파 즉시 진입] {lab['exchange']} · {tf_label(lab['timeframe'])} · 코인 {len(lab['coins'])}개 · "
             f"{pd.Timestamp(lab['start']):%Y-%m-%d} ~ {pd.Timestamp(lab['end']):%Y-%m-%d} "
             f"(확인 구간 {pd.Timestamp(lab['split_ts']):%Y-%m-%d}~)",
             "- 진단: " + ", ".join(f"{k} {v}" for k, v in lab["diag"].items()),
             f"- 기준(박스 돌파·즉시 진입): 개발 {b['dev_n']}건 {(b['dev_avg'] or 0):+.2f}R / 확인 {b['hold_n']}건 "
             f"{(b['hold_avg'] or 0):+.2f}R / 구간 {b['tiers']} / 상위5 제외 "
             f"{(b['ex_top5'] if b['ex_top5'] is not None else float('nan')):+.2f}R / 우연 확률 {b['p']:.3f}"]
    for r in lab["rows"]:
        failed = [k for k, v in r["checks"].items() if not v]
        lines.append(f"- {r['verdict']} {r['name']}: 개발 {r['dev']} | 확인 {r['hold']} | 구간 {r['tiers']}"
                     + (f" | 미충족: {', '.join(failed)}" if failed else ""))
    lines.append("- 추천 설정: " + lab_config_text(lab["config"]))
    if lab.get("per_coin_month"):
        lines.append("- " + lab_frequency_text(lab))
    if lab["errors"]:
        lines.append(f"- 제외된 코인 {len(lab['errors'])}개: " + " / ".join(lab["errors"][:6]))
    return "\n".join(lines)



def backtest_wfo_real(exchange_id: str, symbol: str, timeframe: Optional[str] = None,
                       total_bars: int = 2000, train_bars: int = 1000,
                       test_bars: int = 200) -> pd.DataFrame:
    """실제 build_setup() 로직으로 구간별 워크포워드 검증.
    ⚠️ 지금 build_setup()에는 그리드서치할 자유 파라미터가 없으므로(임계값이 코드에 고정),
    이건 엄밀히는 '최적화 후 검증'이 아니라 '고정 규칙의 롤링 아웃오브샘플 검증'입니다.
    (오히려 파라미터를 데이터에 맞출 기회가 없다는 점에서 과최적화 위험은 더 낮습니다.)

    train_avg_R/test_avg_R가 구간마다 꾸준히 비슷한 부호·크기로 나오면 신뢰할 만한 신호,
    구간마다 들쭉날쭉하거나 test 구간에서 계속 마이너스면 이 전략은 재검토가 필요합니다."""
    df = fetch_extended_ohlcv(exchange_id, symbol, timeframe, total_bars)
    btc_symbol = f"BTC/{QUOTE}"
    btc_df = df.copy() if symbol == btc_symbol else fetch_extended_ohlcv(exchange_id, btc_symbol, timeframe, total_bars)
    daily_df = fetch_extended_ohlcv(exchange_id, symbol, HTF_TIMEFRAME, _htf_bars_needed(total_bars))

    rows = []
    start = 0
    while start + train_bars + test_bars <= len(df):
        train_df = df.iloc[start:start + train_bars].reset_index(drop=True)
        train_btc = btc_df.iloc[start:start + train_bars].reset_index(drop=True)
        test_df = df.iloc[start + train_bars:start + train_bars + test_bars].reset_index(drop=True)
        test_btc = btc_df.iloc[start + train_bars:start + train_bars + test_bars].reset_index(drop=True)

        train_R = simulate_strategy_history(train_df, train_btc, daily_df)
        test_R = simulate_strategy_history(test_df, test_btc, daily_df)

        rows.append({
            "train_start": train_df["ts"].iloc[0], "train_end": train_df["ts"].iloc[-1],
            "test_start": test_df["ts"].iloc[0], "test_end": test_df["ts"].iloc[-1],
            "train_trades": len(train_R), "train_avg_R": float(np.mean(train_R)) if train_R else 0.0,
            "train_total_R": float(sum(train_R)),
            "test_trades": len(test_R), "test_avg_R": float(np.mean(test_R)) if test_R else 0.0,
            "test_total_R": float(sum(test_R)),
        })
        start += test_bars

    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# 5.15 횡보 '기울기(lean)'의 예측력 검증 — 점수가 실제로 방향을 맞추는가?
# --------------------------------------------------------------------------

def evaluate_lean_predictions(df: pd.DataFrame, btc_df: pd.DataFrame, daily_df: pd.DataFrame,
                               weights: tuple = (0.5, 0.3, 0.2), forward_bars: int = 12,
                               min_lookback: int = 80, threshold: float = 0.3,
                               cost_pct: float = 0.16,
                               ctx: Optional[Dict] = None) -> List[Dict]:
    """이 코인 자신의 국면이 '횡보'인 시점마다 lean 점수를 계산하고, 강한 기울기(|score|>threshold)가
    나온 경우 이후 forward_bars봉 뒤 실제 수익률과 비교합니다.
    - 겹치는 표본으로 적중률이 부풀려지지 않도록 forward_bars 간격으로만 샘플링
    - hit = 방향 적중(부호), net_ret_pct = 왕복 비용(cost_pct, 수수료+슬리피지 근사) 차감 후 수익률
    - 미래 데이터는 예측 시점 이후 forward_bars 결과 확인에만 사용"""
    records: List[Dict] = []
    ctx = ctx if ctx is not None else {}   # 봉별 (국면, HTF) 캐시 — 가중치를 바꿔 반복 평가할 때 재사용
    i = min_lookback
    while i + forward_bars < len(df):
        key = df["ts"].iloc[i]
        if key not in ctx:
            # 라이브(screen_market)·백테스트와 동일하게: 이 코인 자신의 국면 기준으로 판단
            # (BTC 국면으로 판단하면 실제 신호 생성 로직과 다른 걸 검증하게 됨)
            window_df = df.iloc[max(0, i + 1 - OHLCV_LIMIT):i + 1]
            regime_i = classify_price_trend(window_df)
            htf_i = htf_trend_at(daily_df, key) if regime_i == "sideways" else None
            ctx[key] = (regime_i, htf_i)
        regime_i, htf_i = ctx[key]
        if regime_i == "sideways":
            lean = compute_sideways_lean(df.iloc[max(0, i + 1 - OHLCV_LIMIT):i + 1], htf_i, weights=weights)
            if abs(lean["score"]) > threshold:
                direction = 1 if lean["score"] > 0 else -1
                fwd_ret_pct = (df["close"].iloc[i + forward_bars] / df["close"].iloc[i] - 1) * 100
                records.append({
                    "ts": df["ts"].iloc[i], "score": lean["score"], "direction": direction,
                    "fwd_ret_pct": fwd_ret_pct,
                    # 방향 적중은 부호만으로 판정(무엇도 못 맞히면 50% → z검정 기준이 유효).
                    # 비용은 net_ret_pct(평균 순수익률)에서 따로 반영합니다.
                    "hit": direction * fwd_ret_pct > 0,
                    "net_ret_pct": direction * fwd_ret_pct - cost_pct,
                })
                i += forward_bars   # 표본 간 겹침 방지
                continue
        i += 1
    return records


def summarize_lean(records: List[Dict]) -> Dict:
    """적중률과 '우연 대비 유의성'(z-score, 50% 기준)을 요약. |z|<2면 우연과 구분 어렵다는 뜻."""
    n = len(records)
    if n == 0:
        return {"n": 0, "hit_rate": None, "z": None, "avg_net_ret_pct": None}
    hits = sum(1 for r in records if r["hit"])
    hit_rate = hits / n
    z = (hit_rate - 0.5) / math.sqrt(0.25 / n)
    return {"n": n, "hit_rate": hit_rate, "z": z,
            "avg_net_ret_pct": float(np.mean([r["net_ret_pct"] for r in records]))}


def pooled_lean_evaluation(exchange_id: str, symbols: List[str], timeframe: Optional[str] = None,
                            total_bars: int = 3000, weight_grid: Optional[List[tuple]] = None,
                            split_ratio: float = 0.6, forward_bars: int = 12) -> pd.DataFrame:
    """여러 코인의 기울기 신호를 합쳐서 평가 (코인 1개는 표본이 너무 적어 통계적으로 무의미).
    - 각 코인의 앞쪽 split_ratio 구간 = 학습(가중치 선택), 뒤쪽 = 검증(OOS)
    - 가중치는 '학습 풀 전체'에서 적중률 최고인 조합 1개를 고르고, 검증 풀에서 그대로 평가
    - 결과의 n과 z를 반드시 확인: n이 100 미만이거나 |z|<2면 '엣지 있다'고 결론내리지 마세요."""
    grid = weight_grid or [(0.5, 0.3, 0.2), (0.7, 0.2, 0.1), (0.3, 0.4, 0.3), (0.34, 0.33, 0.33)]
    btc_symbol = f"BTC/{QUOTE}"
    btc_full = fetch_extended_ohlcv(exchange_id, btc_symbol, timeframe, total_bars)

    datasets = []
    for sym in symbols:
        try:
            d = fetch_extended_ohlcv(exchange_id, sym, timeframe, total_bars)
            daily = fetch_extended_ohlcv(exchange_id, sym, HTF_TIMEFRAME, _htf_bars_needed(total_bars))
            m = min(len(d), len(btc_full))
            datasets.append((sym, d.tail(m).reset_index(drop=True),
                             btc_full.tail(m).reset_index(drop=True), daily, {}))
        except Exception as e:
            print(f"[warn] {sym} 수집 실패: {e}")

    def _pool(part: str, w: tuple) -> List[Dict]:
        pool: List[Dict] = []
        for sym, d, b, daily, ctx in datasets:
            cut = int(len(d) * split_ratio)
            if part == "train":
                dd, bb = d.iloc[:cut].reset_index(drop=True), b.iloc[:cut].reset_index(drop=True)
                recs = evaluate_lean_predictions(dd, bb, daily, weights=w, forward_bars=forward_bars, ctx=ctx)
            else:
                # 검증 구간: 앞선 이력은 지표 계산에만 쓰고, 기록은 cut 이후 시점만 채택
                recs = evaluate_lean_predictions(d, b, daily, weights=w, forward_bars=forward_bars,
                                                 min_lookback=max(80, cut), ctx=ctx)
            pool += recs
        return pool

    rows = []
    for w in grid:
        tr, te = summarize_lean(_pool("train", w)), summarize_lean(_pool("test", w))
        rows.append({"weights": w, "train_n": tr["n"], "train_hit": tr["hit_rate"],
                     "test_n": te["n"], "test_hit": te["hit_rate"], "test_z": te["z"],
                     "test_avg_net_ret_pct": te["avg_net_ret_pct"]})
    out = pd.DataFrame(rows)
    # 학습 적중률 기준 선택(검증 성과로 고르면 안 됨 — 검증 데이터 오염)
    if out["train_hit"].notna().any():
        out["selected_on_train"] = out["train_hit"] == out["train_hit"].max()
    return out


# --------------------------------------------------------------------------
# 5.2 (참고용) 단순 예시 전략 WFO — 실제 추천 로직과 무관한 EMA크로스 뼈대.
#     새로 만든 backtest_wfo_real()을 실전 검증에 사용하세요. 이건 WFO 매커니즘
#     자체를 이해하기 위한 최소 예시로만 남겨둡니다.
# --------------------------------------------------------------------------

def backtest_wfo(df: pd.DataFrame, train_window: int = 500, test_window: int = 100,
                  param_grid: Optional[List[Dict]] = None) -> pd.DataFrame:
    """워크포워드 최적화 뼈대.
    - train_window 구간에서 파라미터(예: EMA fast/slow, ADX threshold)를 그리드서치로 최적화
    - 바로 다음 test_window 구간에서 '한 번도 본 적 없는 데이터'로 검증
    - 이 과정을 데이터 끝까지 롤링 반복 → 구간별 성과를 모아야 '진짜 엣지'인지 판단 가능

    ⚠️ 이 함수는 뼈대만 제공합니다. 실제 전략 로직(진입/청산 규칙)을 채워 넣고,
    거래비용·슬리피지·펀딩비를 반드시 반영해야 현실적인 결과가 나옵니다.
    과최적화 방지를 위해 파라미터 그리드는 최소한으로 유지하세요.
    """
    if param_grid is None:
        param_grid = [
            {"ema_fast": 20, "ema_slow": 50, "adx_th": 15},
            {"ema_fast": 50, "ema_slow": 200, "adx_th": 20},
        ]

    results = []
    start = 0
    while start + train_window + test_window <= len(df):
        train = df.iloc[start:start + train_window]
        test = df.iloc[start + train_window:start + train_window + test_window]

        best_param, best_score = None, -math.inf
        for params in param_grid:
            score = _evaluate_strategy(train, params)
            if score > best_score:
                best_score, best_param = score, params

        oos_score = _evaluate_strategy(test, best_param)
        results.append({
            "train_start": train["ts"].iloc[0], "train_end": train["ts"].iloc[-1],
            "test_start": test["ts"].iloc[0], "test_end": test["ts"].iloc[-1],
            "best_param": best_param, "in_sample_score": best_score, "out_of_sample_score": oos_score,
        })
        start += test_window  # 롤링

    return pd.DataFrame(results)


def _evaluate_strategy(df: pd.DataFrame, params: Dict,
                        fee_pct: float = 0.05, slippage_pct: float = 0.03,
                        funding_pct_per_8h: float = 0.01) -> float:
    """예시 전략 평가 함수: EMA 골든/데드크로스 + ADX 필터.
    ⚠️ 실전에서는 여기를 본인의 실제 전략 로직으로 교체하세요.

    비용을 반드시 반영합니다 (기본값은 대략적인 예시이며 실제 거래소 수수료로 교체하세요):
    - fee_pct: 편도 거래 수수료 (%) — 왕복이면 2번 발생
    - slippage_pct: 체결 슬리피지 (%) — 시장가 진입/청산 시 발생
    - funding_pct_per_8h: 무기한 선물 보유 시 8시간마다 발생하는 펀딩비 (%)
      → 포지션을 오래 들고 있을수록 비용이 누적되므로, 봉 하나(TIMEFRAME)당
        경과 시간에 비례해 비용을 차감합니다."""
    fast = ema(df["close"], params["ema_fast"])
    slow = ema(df["close"], params["ema_slow"])
    strength = adx(df) if len(df) > params["ema_slow"] else 0

    bars_per_8h = BARS_PER_8H  # 8시간 동안의 봉 개수 (4시간봉 2개, 1시간봉 8개)
    per_bar_funding_cost = funding_pct_per_8h / bars_per_8h / 100

    position = 0
    pnl = 0.0
    for i in range(1, len(df)):
        if strength < params["adx_th"]:
            continue
        prev_position = position
        if fast.iloc[i] > slow.iloc[i] and position <= 0:
            position = 1
        elif fast.iloc[i] < slow.iloc[i] and position >= 0:
            position = -1

        # 포지션이 바뀌는 시점(=진입/청산 발생)에는 수수료+슬리피지를 왕복 비용으로 차감
        if position != prev_position:
            pnl -= (fee_pct + slippage_pct) / 100

        ret = (df["close"].iloc[i] / df["close"].iloc[i - 1] - 1) * position
        pnl += ret

        # 포지션을 들고 있는 매 봉마다 펀딩비 차감 (방향 무관하게 비용으로만 근사 반영;
        # 실제로는 펀딩비 부호가 시장 상황에 따라 바뀌므로 이건 보수적 근사치입니다)
        if position != 0:
            pnl -= per_bar_funding_cost

    return pnl


# --------------------------------------------------------------------------
# 6. 메인 파이프라인
# --------------------------------------------------------------------------

def main(risk_cfg: Optional[RiskConfig] = None):
    if risk_cfg is None:
        # 기본값: 계좌 예시 1,000 USDT, 트레이드당 1% 리스크, 동일방향 최대 3개
        # 실전에서는 반드시 본인 실제 잔고로 바꿔서 호출하세요: main(RiskConfig(account_balance=..., ...))
        risk_cfg = RiskConfig(account_balance=1000, risk_per_trade_pct=1.0, max_concurrent_setups=10)

    # 서킷브레이커: 오늘/이번 주 실현 손실이 한도를 넘었으면 신규 신호 자체를 생성하지 않음
    breaker_msg = circuit_breaker_triggered(risk_cfg)
    if breaker_msg:
        print("=" * 60)
        print(f"🛑 서킷브레이커 작동: {breaker_msg}")
        print("   손실을 만회하려는 추가 진입이 계좌를 가장 크게 망가뜨립니다.")
        print("   내일(또는 다음 주) 한도가 초기화될 때까지 신규 진입을 쉬세요.")
        print("=" * 60)
        return

    print("=" * 60)
    print("1) 거시 국면 판단 중...")
    regime = determine_overall_regime()
    print(f"  BTC 가격추세      : {regime.btc_trend}")
    print(f"  BTC.D 추세        : {regime.btc_d_trend}")
    print(f"  USDT.D 추세       : {regime.usdt_d_trend}")
    print(f"  TOTAL2 추세       : {regime.total2_trend}")
    print(f"  TOTAL3 추세       : {regime.total3_trend}")
    print(f"  ▶ 종합 국면       : {regime.overall.upper()}")
    print("=" * 60)

    print("2) 코인 스크리닝 중 (Binance/OKX/Bitget, 거래량 상위)...")
    setups = screen_market(regime.overall)

    if not setups:
        print("  조건을 만족하는 셋업이 없습니다. (거래량 급증 + RR 1.5 이상 기준)")
        return

    # 상관관계(BTC 동조화) 노출 제한 — 같은 방향 신호가 아무리 많아도 RR 상위 N개만 실전 후보로 남김
    setups = cap_correlated_exposure(setups, risk_cfg)

    print(f"  총 {len(setups)}개 후보 발견 (상관관계 제한 적용 후)\n")

    ready = [s for s in setups if not s.is_chase]
    waiting = [s for s in setups if s.is_chase]

    def _print_setup(s: CoinSetup):
        sizing = calculate_position_size(s.entry_price, s.sl, risk_cfg)
        poc_tag = " 🎯POC컨플루언스" if s.poc_confluence else ""
        print(f"[{s.exchange}] {s.symbol} | {s.bias}{poc_tag}")
        print(f"   근거     : {s.entry_note}")
        asym_str = f"{s.asymmetry:+.2f}" if s.asymmetry is not None else "표본부족"
        print(f"   상대강도 : {s.rs:+.2f}% (지수 대비)   비대칭점수: {asym_str} (상승↑/하락↓ 비대칭)")
        print(f"   현재가   : {s.current_price:.6f}   지정가 진입가: {s.entry_price:.6f}")
        print(f"   TP1/TP2  : {s.tp1:.6f} / {s.tp2:.6f}   SL: {s.sl:.6f}   RR: {s.rr_ratio:.2f}")
        print(f"   포지션   : 수량 {sizing['size']:.4f} (명목가치 {sizing['notional']:.2f} USDT, "
              f"리스크 {sizing['risk_amount']:.2f} USDT = 계좌의 {risk_cfg.risk_per_trade_pct}%)")
        print("-" * 50)

    def _rs_sort_key(s: CoinSetup):
        asym = s.asymmetry if s.asymmetry is not None else 0.0
        if s.bias in LONG_BIASES:
            return (s.rs, asym)
        return (-s.rs, -asym)

    print("=" * 60)
    print(f"✅ 되돌림 진입가 도달 — 지금 진입 검토 가능 ({len(ready)}개, 상대강도 순)")
    print("=" * 60)
    if not ready:
        print("  (현재 없음 — 전부 추격 구간이거나 신호 자체가 없음)")
    for s in sorted(ready, key=_rs_sort_key, reverse=True):
        _print_setup(s)

    print()
    print("=" * 60)
    print(f"⏳ 추격 구간 — 아직 진입 대기, 지정가만 걸어두고 관망 ({len(waiting)}개, 상대강도 순)")
    print("=" * 60)
    if not waiting:
        print("  (현재 없음)")
    for s in sorted(waiting, key=_rs_sort_key, reverse=True):
        _print_setup(s)


def validate_lean_auto(exchange_id: str = "binance", top_n: int = 25) -> None:
    """코인 목록을 직접 넣을 필요 없이, 거래량 상위 top_n개를 자동으로 골라
    횡보 기울기 점수의 예측력을 검증하고 결과를 해석까지 붙여 출력합니다.
    (추천용이 아니라 '이 기울기 점수를 믿어도 되는지' 점검용 — 한 번만 돌려보면 됩니다)"""
    symbols = [s for s in get_top_volume_symbols(exchange_id, top_n + 5)
               if s != f"BTC/{QUOTE}"][:top_n]
    print(f"검증 대상 {len(symbols)}개 코인 데이터 수집·분석 중... (수 분 걸릴 수 있음)")
    res = pooled_lean_evaluation(exchange_id, symbols)
    print(res.to_string())

    chosen = res[res.get("selected_on_train", False) == True]  # noqa: E712
    row = chosen.iloc[0] if len(chosen) else res.iloc[0]
    n, hit, z, net = row["test_n"], row["test_hit"], row["test_z"], row["test_avg_net_ret_pct"]
    print("\n[해석]")
    if not n or n < 100 or hit is None or z is None:
        print(f"- 검증 신호 {n}건으로 표본이 부족해 결론을 낼 수 없습니다. 기울기 점수를 방향 선택 근거로 쓰지 마세요.")
    elif hit >= 0.55 and z >= 2 and net > 0:
        print(f"- 적중률 {hit:.1%}, z={z:.1f}, 비용 후 평균 {net:+.2f}% → 쓸 만한 엣지가 있어 보입니다(과거 기준, 미래 보장 아님).")
    else:
        print(f"- 적중률 {hit:.1%}, z={z:.1f}, 비용 후 평균 {net:+.2f}% → 우연과 구분되는 엣지가 확인되지 않았습니다.")
        print("  횡보에서는 방향을 고르지 말고 양쪽 신호를 다 열어두는 쪽이 안전합니다.")


if __name__ == "__main__":
    import sys
    if "--validate" in sys.argv:
        validate_lean_auto()
    else:
        main()
