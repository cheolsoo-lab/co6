"""
app.py — 코인 추천 웹 화면 (핸드폰 우선)
실행:  streamlit run app.py
배포:  README.md 참고 (Streamlit Community Cloud 또는 본인 PC/서버)

동작 방식
- 전체 스캔(무거움)은 신호 봉이 마감될 때만 자동 실행 (4시간봉 기준 한국시간 01·05·09·13·17·21시 직후)
- 그 사이에는 추천 코인의 현재가만 몇 초 만에 받아서 상태(진입가 근처/추격/무효/놓침)를 자동 갱신
⚠️ 참고용 화면입니다. 자동 주문 기능은 없습니다.
"""
import contextlib
import dataclasses
import io
import threading
import traceback

import pandas as pd
import streamlit as st

import app_logic as L
import crypto_market_regime as cmr

# 앱을 켜둔 채 엔진 파일만 바뀌면 예전 엔진이 메모리에 남음 → 디스크 버전이 다르면 자동으로 다시 불러옴
_reloaded = False
try:
    import importlib
    import re as _re0
    _m0 = _re0.search(r'APP_VERSION = "([^"]+)"', open(cmr.__file__, encoding="utf-8").read())
    if _m0 and getattr(cmr, "APP_VERSION", None) != _m0.group(1):
        cmr = importlib.reload(cmr)
        L = importlib.reload(L)
        _reloaded = True
except Exception:
    pass

st.set_page_config(page_title="▲△▲", page_icon="▲△▲", layout="centered",
                   initial_sidebar_state="collapsed")
st.markdown(f"<style>{L.CSS}</style>", unsafe_allow_html=True)


@st.cache_resource
def get_store() -> dict:
    """모든 접속(폰/PC)이 분석 결과를 공유 → 접속할 때마다 새로 스캔하지 않음."""
    return {"lock": threading.Lock(), "result": None, "log": "", "error": None, "scan_cfg": None,
            "live_at": None, "live_log": "", "bt": None, "bt_error": None}


store = get_store()
if _reloaded:  # 예전 엔진으로 만든 결과는 버리고 새 엔진으로 다시 분석
    for _k in ("result", "port", "port_key", "mval", "hvol", "export", "sim"):   # 연구실 결과는 데이터라 유지
        store[_k] = None
    store["lab_cfg_loaded"], store["force_rescan"] = False, True
for _k, _v in {"lab": None, "lab_error": None, "lab_cfg": None, "lab_cfg_loaded": False,
               "force_rescan": False, "hvol": None, "hvol_error": None, "port": None, "port_key": None,
               "port_error": None, "sim": None, "mval": None, "mval_error": None,
               "export": None, "export_name": None, "export_error": None}.items():
    store.setdefault(_k, _v)  # 앱이 켜진 채 코드만 바뀐 경우에도 새 항목이 생기도록


def kst(ts) -> str:
    return (pd.Timestamp(ts) + pd.Timedelta(hours=9)).strftime("%H:%M")


APP_VERSION = "2026-10-12 v29.1"
st.title("▲△▲ ▲△▲")
_engine_ver = getattr(cmr, "APP_VERSION", None)
st.caption(f"Bitget 선물용 · 스윙 신호 · 참고용(자동 주문 아님) · 버전 {APP_VERSION}")
if _engine_ver != APP_VERSION:
    import os
    import re as _re
    _path = getattr(cmr, "__file__", "?")
    try:
        _disk = _re.search(r'APP_VERSION = "([^"]+)"', open(_path, encoding="utf-8").read())
        _disk_ver = _disk.group(1) if _disk else "버전 표시 없음"
    except Exception:
        _disk_ver = "읽기 실패"
    if _disk_ver == APP_VERSION:
        st.error(f"⚠️ 엔진 파일은 새 버전({_disk_ver})으로 바뀌었는데, 실행 중인 앱이 예전 엔진({_engine_ver or '이전 버전'})을 "
                 "메모리에 그대로 들고 있어요. 앱을 완전히 종료한 뒤 다시 실행해 주세요.")
        st.markdown("- 내 PC에서 실행 중이면: 실행 창(검은 창)에서 **Ctrl + C**로 끄고 `streamlit run app.py`로 다시 실행\n"
                    "- Streamlit Cloud면: 오른쪽 아래 **Manage app → Reboot app**")
    elif _disk_ver > APP_VERSION:
        st.error(f"⚠️ 엔진 파일({_disk_ver})이 화면 파일 app.py({APP_VERSION})보다 새 버전이에요. "
                 "app.py와 app_logic.py도 같은 버전으로 교체해 주세요.")
    else:
        st.error(f"⚠️ 화면 파일 app.py({APP_VERSION})가 엔진 파일({_disk_ver})보다 새 버전이에요. "
                 "crypto_market_regime.py를 새 파일로 교체해 주세요.")
    st.code(f"화면(app.py) 버전: {APP_VERSION}\n엔진 파일(디스크) 버전: {_disk_ver}\n"
            f"실행 중인 엔진(메모리) 버전: {_engine_ver or '이전 버전'}\n엔진 파일 위치: {_path}\n"
            f"엔진 파일 크기: {os.path.getsize(_path) if os.path.exists(_path) else 0:,} 바이트", language=None)
    st.stop()

# 클라우드 영구 저장(GitHub Gist): Secrets에 GITHUB_TOKEN·GIST_ID가 있으면 시작할 때 불러오고, 바뀌면 저장
try:
    _gh_tok, _gh_gist = st.secrets.get("GITHUB_TOKEN", ""), st.secrets.get("GIST_ID", "")
except Exception:
    _gh_tok, _gh_gist = "", ""
cmr.configure_cloud_store(_gh_tok, _gh_gist)
if cmr.CLOUD["enabled"] and cmr.CLOUD["restored"] is None:
    with store["lock"]:
        store["cloud_msg"] = cmr.cloud_restore()
        store["lab_cfg_loaded"] = False   # 내려받은 연구실 설정을 다시 읽도록
else:
    cmr.cloud_sync()

# 전략 연구실에서 적용한 설정 불러오기 (저장 파일 → 한 번만), 매 실행마다 엔진에 반영
if not store["lab_cfg_loaded"]:
    store["lab_cfg"] = cmr.load_lab_config()
    store["lab_cfg_loaded"] = True
if store["lab_cfg"]:
    cmr.apply_lab_config(store["lab_cfg"], save=False)
if "pending_fams" in st.session_state:  # 연구실 설정 적용 직후 신호 유형 선택을 맞춤
    st.session_state["fams"] = st.session_state.pop("pending_fams")
if "pending_tsm" in st.session_state:
    st.session_state["tsm_on"] = st.session_state.pop("pending_tsm")

# ---------------------------------------------------------------- 설정
with st.expander("⚙️ 설정"):
    c1, c2 = st.columns(2)
    balance = c1.number_input("계좌 잔고 (USDT)", min_value=10.0, value=300.0, step=50.0, key="balance")
    risk_pct = c2.number_input("트레이드당 리스크 (%)", min_value=0.1, max_value=3.0, value=1.0, step=0.1,
                               key="risk_pct", help="손절가에 닿았을 때 잃는 금액이 계좌의 몇 %인지")
    c3, c4 = st.columns(2)
    max_total = c3.number_input("총 리스크 상한 (%)", min_value=1.0, max_value=20.0, value=12.0, step=0.5,
                                key="max_total",
                                help="동시에 들고 있는 모든 포지션이 한꺼번에 손절될 때 잃는 합계 상한")
    open_pos = c4.number_input("지금 보유 중인 포지션 수", min_value=0, max_value=30, value=0, step=1,
                               key="open_pos", help="프로그램은 거래소 계좌를 볼 수 없어서 직접 입력해야 정확해요")
    max_n = st.slider("같은 방향 동시 추천 최대 개수", 1, 15, 12, key="max_n",
                      help="알트코인은 BTC와 같이 움직여서, 같은 방향을 많이 잡아도 분산이 잘 안 됩니다")
    c5, c6 = st.columns(2)
    top_n = c5.slider("스캔 코인 수 (3개 거래소 합산 거래량 순위)", 10, 150, 100, key="top_n",
                      help="과거 검증의 '거래량 구간별' 결과에서 플러스가 확인된 구간까지만 늘리세요. "
                           "바꾼 뒤 '새로 분석'을 눌러야 반영")
    live_sec = c6.selectbox("가격 자동 갱신(초)", [15, 30, 60], index=1, key="live_sec")
    tf_choice = st.radio("신호 봉", ["4시간봉 (기본)", "1시간봉 (비교용)"], horizontal=True, key="tf_choice")
    bitget_only = st.checkbox("Bitget 선물 거래 가능한 코인만", value=True, key="bitget_only")
    _tsm_default = {} if "tsm_on" in st.session_state else {"value": bool((store["lab_cfg"] or {}).get("tsm"))}
    c7, c8 = st.columns(2)
    tsm_on = c7.checkbox("📈 추세 포트폴리오 표시", key="tsm_on",
                         help="매일 일봉 마감 후 추세가 살아있는 코인을 변동성 비중으로 보유(롱). 연구실에서 통과하면 자동으로 켜져요",
                         **_tsm_default)
    c8.caption("포트폴리오 대상: **메이저 + BTC** · v28 시장 관문: BTC가 100일선 위 **그리고** 대상 코인 절반 이상이 "
               "20일선 위일 때만 보유 (2.5년 검증: 최대 낙폭 −47% → −25%)")
    port_n = len(cmr.tsm_universe())
    port_mode = st.radio("포트폴리오 자금", ["신호 매매에 더해서 (권장 · 신호가 메인)", "계좌를 나눠서"], horizontal=True,
                         key="port_mode",
                         help="더해서: 신호 매매 수량은 계좌 전체 기준 그대로, 포트폴리오는 계좌의 아래 비율만큼 추가로 보유. "
                              "나눠서: 계좌를 둘로 나눠 신호 매매 수량이 줄어요. 신호와 합친 검증(2024.10~, 포트폴리오 20%): "
                              "더해서 → 신호 단독보다 연 수익↑·최대 낙폭 거의 같음(−41%→−42%) / 나눠서 → 수익↓·낙폭↓(−35%)") \
        if tsm_on else "신호 매매에 더해서"
    port_overlay = not str(port_mode).startswith("계좌")
    port_alloc = st.slider("추세 포트폴리오 금액 (계좌의 %)", 10, 100, 20, step=5, key="port_alloc",
                           help="포트폴리오에 쓰는 금액. 관문이 켜지면 이 금액의 최대 100%(또는 150%)만큼 코인을 보유해요. "
                                "신호 매매가 메인이면 20~30%를 권해요") if tsm_on else 0
    port_gross_lbl = st.radio("포트폴리오 비중 합계 상한", ["100% (기본)", "150% (적극)"], horizontal=True, key="port_gross",
                              help="관문이 켜졌을 때 코인들의 비중 합계 상한. 2.5년 검증: 100% → 연 +26%·낙폭 −25%, "
                                   "150% → 연 +40%·낙폭 −35%. 실전 기록 90일 전에는 100%를 권해요") if tsm_on else "100%"
    port_gross = 1.5 if str(port_gross_lbl).startswith("150") else 1.0
    _cl = cmr.CLOUD
    if _cl.get("config_error"):
        st.warning(f"☁️ 기록 저장 설정 확인 필요: {_cl['config_error']} (Streamlit Cloud → 앱 Settings → Secrets)")
    elif _cl["enabled"]:
        _when = f" · 마지막 저장 {kst(_cl['last_sync'])}" if _cl.get("last_sync") is not None else ""
        if _cl.get("last_error"):
            st.warning(f"☁️ 기록 저장(GitHub Gist) 오류: {_cl['last_error']}")
        else:
            st.caption(f"☁️ 기록 저장: GitHub Gist 연결됨{_when} — 재시작해도 실전 추적·연구실 설정이 유지돼요")
    else:
        st.caption("⚠️ 기록 저장 안 됨: Streamlit Cloud가 재시작되면 실전 추적·연구실 설정이 초기화돼요. "
                   "README의 'GitHub Gist 연결' 순서대로 설정하세요.")
    macro_f = st.checkbox("🧭 거시 흐름과 반대 방향 신호 제외 (2년 검증: 역방향 진입 평균 −0.43R)", value=True, key="macro_f")
    auto_def = st.checkbox("🛡 자동 방어 (실전 성과가 나빠진 신호 유형은 리스크 절반 → 계속 나쁘면 자동 중지)",
                           value=True, key="auto_def")
    _fam_default = {} if "fams" in st.session_state else {
        "default": ["돌파", "돌파 후 되돌림", "추세 눌림목"]
    }
    fams = st.multiselect("추천할 신호 유형", ["돌파", "돌파 후 되돌림", "추세 눌림목", "위치 다이버전스", "신고점 돌파", "추세", "박스 역매매"],
                          key="fams",
                          help="v29(5년·2021.10~2026.10, 2021-23 ↔ 2024-26 두 사이클 교차 검증)에서 살아남은 3가지가 기본이에요. "
                               "박스 돌파: 15일(90봉) 박스·거래량 2배. 돌파 후 되돌림: 품질 박스 돌파 뒤 경계 재확인. "
                               "추세 눌림목: 상승 추세 속 눌림 롱·하락 추세 속 반등 숏(새로 추가, 실전 30건 전까지 위험 절반). "
                               "위치 다이버전스는 2021-23 사이클에서 마이너스라 기본에서 뺐어요. "
                               "신고점 돌파·추세·박스 역매매는 근거가 약해요.", **_fam_default)
    if store["lab_cfg"]:
        st.caption("🧪 연구실 적용 설정: " + cmr.lab_config_text(store["lab_cfg"]))
        if st.button("연구실 설정 초기화"):
            cmr.reset_lab_config()
            store["lab_cfg"], store["force_rescan"] = None, True
            st.rerun()

tf = "1h" if tf_choice.startswith("1") else "4h"
# 신호 매매가 메인: '더해서'면 신호 수량은 계좌 전체 기준 그대로, '나눠서'면 포트폴리오 금액을 뺀 나머지 기준
bal_sig = balance if port_overlay else balance * (1 - port_alloc / 100)
bal_port = balance * port_alloc / 100        # 추세 포트폴리오에 쓰는 금액
risk_cfg = cmr.RiskConfig(account_balance=bal_sig, risk_per_trade_pct=risk_pct, max_concurrent_setups=max_n,
                          max_total_risk_pct=max_total, open_positions=int(open_pos))
scan_cfg = (top_n, bitget_only, tf, tuple(sorted(fams)), cmr.lab_config_text(store["lab_cfg"] or {}), auto_def, macro_f)


# ---------------------------------------------------------------- 전략 연구실 화면
def render_lab() -> None:
    st.caption("근거 있는 소수의 후보만 미리 정해두고, 과거의 앞 2/3(개발 구간)에서 비교한 뒤 뒤 1/3(확인 구간)으로 "
               "한 번 더 확인해요. 모든 기준을 통과한 후보만 실전 추천에 적용할 수 있어요.")
    b1, b2 = st.columns(2)
    n_coins = b1.slider("검증할 코인 수", 9, 30, 30, step=3, key="lab_n",
                        help="거래량 1~30위·31~60위·61~100위에서 3분의 1씩 고르게 뽑아요")
    period = b2.selectbox("기간", ["1년", "2년"], index=1, key="lab_period")
    days = {"1년": 365, "2년": 730}[period]
    est = max(1, round(n_coins * (6 if tf == "4h" else 20) * days / 365 / 60))
    st.caption(f"{cmr.tf_label(tf)} 기준 · 예상 소요 약 {est}분 이상(데이터 받는 시간 포함) · 화면을 켜둔 채 기다려주세요")
    if st.button("▶ 연구실 실행", type="primary", use_container_width=True):
        prog = st.progress(0.0, text="과거 데이터 받는 중...")

        def cb(i: int, n: int, sym: str) -> None:
            prog.progress(min(i / max(n, 1), 1.0), text=f"{sym}")

        with store["lock"]:
            cmr.BITGET_ONLY = bitget_only
            cmr.set_timeframe(tf)
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    store["lab"] = cmr.run_strategy_lab_and_save(n_coins, days, cb)
                store["lab_error"] = None
            except Exception:
                store["lab_error"] = traceback.format_exc() + "\n" + buf.getvalue()
            finally:  # 추천 화면이 쓰는 봉 모드로 되돌림
                r0 = store["result"]
                cmr.set_timeframe(r0.get("timeframe", "4h") if r0 else "4h")
        prog.empty()

    if store.get("lab_error"):
        st.error("연구실 실행 중 오류가 발생했어요. 아래 내용을 그대로 복사해서 알려주세요.")
        st.code(store["lab_error"], language=None)
    lab = store.get("lab")
    if not lab:
        st.info("아직 실행한 결과가 없어요. 코인 수와 기간을 고르고 '▶ 연구실 실행'을 눌러주세요.")
        return

    st.caption(f"{lab['exchange']} · {cmr.tf_label(lab['timeframe'])} · "
               f"{pd.Timestamp(lab['start']):%Y-%m-%d} ~ {pd.Timestamp(lab['end']):%Y-%m-%d} · "
               f"확인 구간 {pd.Timestamp(lab['split_ts']):%Y-%m-%d}~ · 코인 {len(lab['coins'])}개 · "
               f"시험 후보 {lab['k_tests']}개(우연 확률 기준 {lab['alpha']:.3f} 미만)")
    b = lab["baseline"]
    st.markdown(f"**기준 전략 (박스 돌파 · 봉 마감 직후 시장가)** — 개발 {b['dev_n']}건 {(b['dev_avg'] or 0):+.2f}R · "
                f"확인 {b['hold_n']}건 {(b['hold_avg'] or 0):+.2f}R · 거래량 구간 {b['tiers']}")
    if not lab["config"]["baseline_ok"]:
        st.warning("기준 전략이 개발·확인 구간 중 한쪽에서 마이너스예요. 실전 투입은 보류하고 모의로 지켜보는 걸 권해요.")
    rows = [{"판정": r["verdict"], "후보": r["name"], "종류": r["kind"], "개발 구간": r["dev"], "확인 구간": r["hold"],
             "거래량 구간": r["tiers"], "통과 시 적용": r["apply"],
             "미충족 기준": ", ".join(k for k, v in r["checks"].items() if not v) or "-"} for r in lab["rows"]]
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    st.caption("✅ 통과: 모든 기준 충족 → 정상 리스크 · 🧪 시험 운용: 개발·확인 구간 기준은 넘었지만 표본·우연 확률 등 "
               "일부 미달 → 리스크 ¼로 실전 투입, 실전 30건이 기준선 안이면 자동 승격 · 🟡 보류(표시): 참고만 · ❌ 탈락")

    if lab.get("per_coin_month"):
        st.caption(cmr.lab_frequency_text(lab, top_n) + " (과거 기준 예상치, 시장 상황에 따라 크게 달라져요)")
    st.caption("🛡 이 결과의 신호 유형별 성과가 자동 방어 기준선으로 저장됐어요. 실전 추적 성과가 이 기준 아래로 "
               "떨어지면 추천이 자동으로 감축·중지돼요.")
    cfg = lab["config"]
    if any(r["verdict"].startswith(("✅", "🧪")) for r in lab["rows"] if r.get("kind") != "v27 매매법"):
        st.markdown("**추천 설정:** " + cmr.lab_config_text(cfg))
        if st.button("✔ 이 설정 적용", use_container_width=True):
            cmr.apply_lab_config(cfg)
            store["lab_cfg"], store["force_rescan"] = cfg, True
            add_ = set()   # 신고점 돌파는 자동으로 켜지 않음 (1시간 안 진입이 필요해 수동 운용에 부적합)
            if add_:
                st.session_state["pending_fams"] = sorted(set(st.session_state.get("fams", ["돌파"])) | add_)
            if cfg.get("tsm"):
                st.session_state["pending_tsm"] = True
            st.rerun()
    else:
        st.info("통과하거나 시험 운용할 후보가 없어요. 지금 설정(박스 돌파 · 봉 마감 직후 시장가)을 그대로 쓰는 게 근거에 맞아요.")

    t1, t3, t2 = st.tabs(["기준 전략 상세", "추세 포트폴리오", "📋 복사용"])
    with t3:
        tsm_ = lab.get("tsm")
        if tsm_:
            st.caption(f"대상 {len(tsm_['coins'])}개(메이저+BTC)를 일봉으로 매일 점검 · 수수료·슬리피지·롱 펀딩비 차감 후 · "
                       + ("v28 시장 관문 적용" if tsm_.get("gated") else "BTC 일봉이 없어 관문 없이 계산"))
            cv = {"추세 포트폴리오(v28)": (1 + tsm_["daily"]).cumprod(), "같은 코인 단순 보유": (1 + tsm_["ew_daily"]).cumprod()}
            if tsm_.get("old_daily") is not None:
                cv["관문 없던 방식(v27)"] = (1 + tsm_["old_daily"]).cumprod()
            st.line_chart(pd.DataFrame(cv))
            a_, e_ = tsm_["all"], tsm_["ew"]
            o_ = tsm_.get("old") or {}
            st.markdown(f"- 추세 포트폴리오(v28): 연 {a_['cagr']:+.0%} · 최대 낙폭 {a_['max_dd']:.0%} · 샤프 {a_['sharpe']:.2f} · "
                        f"플러스 달 {(a_.get('pos_months') or 0):.0%}\n"
                        + (f"- 관문 없던 방식(v27): 연 {o_['cagr']:+.0%} · 최대 낙폭 {o_['max_dd']:.0%} · 샤프 {o_['sharpe']:.2f}\n"
                           if o_.get("cagr") is not None else "")
                        + f"- 단순 보유: 연 {e_['cagr']:+.0%} · 최대 낙폭 {e_['max_dd']:.0%} · 샤프 {e_['sharpe']:.2f}")
        else:
            st.write("추세 포트폴리오는 4시간봉 모드에서 일봉 데이터로 검증해요.")
    base_tr = lab["trades"]["base"]
    with t1:
        st.caption("기준 전략(박스 돌파) 거래를 거래량 구간·방향·진입 시점 변동성으로 나눠 본 결과")
        for by in ("tier", "direction", "vol"):
            st.dataframe(cmr.backtest_group_table(base_tr, by), hide_index=True, use_container_width=True)
        st.markdown(cmr.suggest_scan_count(base_tr, {"돌파"}))
    with t2:
        st.caption("이 내용을 복사해서 보내주시면 결과를 해석해 드릴게요.")
        st.code(cmr.lab_report_text(lab), language=None)
    st.caption("ⓘ 스프레드·펀딩비 필터는 과거 기록이 없어 반영되지 않았어요. 후보를 결과에 맞춰 계속 바꾸면 과거에만 맞는 "
               "전략이 되기 쉬우니, 적용한 뒤에는 최소 4주 소액·모의로 실제 결과를 확인하세요.")


def render_hourly_vol() -> None:
    st.markdown("#### 🕐 시간대별 변동성")
    st.caption("1시간봉으로 한국시간 시간대별 평균 변동폭을 재요. 코인·시기마다 변동성 크기가 달라서, 각 봉을 그 코인의 "
               "직전 1주 평균 변동폭으로 나눈 값(평균 = 1.0)으로 비교해요. 확인·진입하기 좋은 시간을 고르는 참고용이에요.")
    h1, h2 = st.columns(2)
    hv_n = h1.slider("분석 코인 수", 5, 20, 10, key="hv_n", help="BTC + 거래량 상위 코인")
    hv_days = h2.selectbox("기간", [30, 60, 90], index=1, key="hv_days", format_func=lambda d: f"최근 {d}일")
    if st.button("▶ 시간대 분석", use_container_width=True):
        prog = st.progress(0.0, text="1시간봉 받는 중...")

        def cb(i: int, n: int, sym: str) -> None:
            prog.progress(min(i / max(n, 1), 1.0), text=f"{sym}")

        with store["lock"]:
            cmr.BITGET_ONLY = bitget_only
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    store["hvol"] = cmr.hourly_volatility_profile(hv_n, hv_days, cb)
                store["hvol_error"] = None
            except Exception:
                store["hvol_error"] = traceback.format_exc() + "\n" + buf.getvalue()
        prog.empty()
    if store.get("hvol_error"):
        st.error("시간대 분석 중 오류가 발생했어요. 아래 내용을 그대로 복사해서 알려주세요.")
        st.code(store["hvol_error"], language=None)
    hv = store.get("hvol")
    if not hv:
        return
    st.caption(f"코인 {len(hv['coins'])}개 · 최근 {hv['days']}일 · 1시간봉 {hv['bars']:,}개 · 분석 {kst(hv['ran_at'])}")
    chart = pd.DataFrame({"변동폭 (평균=1)": [hv["profile"][h] for h in range(24)]},
                         index=[f"{h:02d}시" for h in range(24)])
    st.bar_chart(chart)
    st.markdown("\n".join(f"- {x}" for x in cmr.hourly_vol_lines(hv)))
    st.caption("ⓘ 막대의 시각은 1시간봉이 '시작하는' 한국시간이에요. 예) 01시 막대 = 01:00~02:00. "
               "4시간봉은 01·05·09·13·17·21시에 마감돼요.")


def render_macro_validation() -> None:
    st.markdown("#### 🧭 거시 방향 검증")
    st.caption("2년치 일봉으로 매일의 거시 방향 판정을 다시 계산하고, 그 뒤 7일 동안 알트 지수가 실제로 어떻게 "
               "움직였는지 판정별로 집계해요. 결과는 메인 화면 시장 카드에 '과거 검증' 줄로 표시돼요.")
    m1, m2 = st.columns(2)
    mv_n = m1.slider("검증 코인 수", 10, 30, 20, key="mv_n", help="알트 지수·50일선 참여도 계산용")
    mv_h = m2.selectbox("판정 뒤 관찰 기간", [3, 7, 14], index=1, key="mv_h", format_func=lambda d: f"{d}일")
    if st.button("▶ 거시 방향 검증", use_container_width=True):
        prog = st.progress(0.0, text="일봉 받는 중...")

        def cb(i: int, n: int, sym: str) -> None:
            prog.progress(min(i / max(n, 1), 1.0), text=f"{sym}")

        with store["lock"]:
            cmr.BITGET_ONLY = bitget_only
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    store["mval"] = cmr.validate_macro(mv_n, 730, mv_h, cb)
                store["mval_error"], store["force_rescan"] = None, True  # 메인 카드 문구 갱신
            except Exception:
                store["mval_error"] = traceback.format_exc() + "\n" + buf.getvalue()
        prog.empty()
    if store.get("mval_error"):
        st.error("거시 방향 검증 중 오류가 발생했어요. 아래 내용을 그대로 복사해서 알려주세요.")
        st.code(store["mval_error"], language=None)
    v = store.get("mval") or cmr.load_macro_validation()
    if not v:
        return
    st.caption(f"{v['exchange']} · {v['start']} ~ {v['end']} · 코인 {len(v['coins'])}개 · 판정 뒤 {v['horizon']}일")
    kr = {"uptrend": "상승 판정", "sideways": "횡보 판정", "downtrend": "하락 판정", "all": "전체 날짜"}
    st.dataframe(pd.DataFrame([{"판정": kr[r["state"]], "일수": r["days"], "알트 지수 평균": f"{r['alt_mean']:+.1%}",
                                "알트 오른 비율": f"{r['alt_up']:.0%}", "BTC 평균": f"{r['btc_mean']:+.1%}",
                                "BTC 오른 비율": f"{r['btc_up']:.0%}"} for r in v["all"]]),
                 hide_index=True, use_container_width=True)
    st.markdown("\n".join(f"- {x}" for x in cmr.macro_validation_lines(v)))
    st.caption("ⓘ 과거로 재현할 수 있는 일봉 근거(BTC 일봉·알트 지수·알트/BTC·ETH/BTC·50일선 참여도, 실제 판정의 약 75%)만 "
               "검증해요. 4시간봉 근거와 도미넌스 기록은 과거 데이터가 없어 제외돼요.")


def render_export() -> None:
    st.markdown("#### 📦 연구용 데이터 내보내기")
    st.caption("거래소 API로 과거 데이터를 받아 파일 하나(zip)로 저장해요. 이 파일을 Claude와의 대화에 올리면 "
               "Claude가 직접 수십 가지 변형을 돌려 정밀 검증할 수 있어요.")
    ex_mode = st.radio("내보낼 데이터", ["4시간봉·일봉 (기본, 최대 5년)", "1시간봉 전용 (최대 2년)"], horizontal=True, key="ex_mode",
                       help="1시간봉 전용: 1시간봉 매매법을 찾는 연구용. 1시간봉과 일봉(거시 흐름 계산용)만 담아요")
    only_1h = ex_mode.startswith("1시간봉")
    x1, x2 = st.columns(2)
    ex_n = x1.slider("코인 수 (거래량 순위)", 30, 100, 60 if only_1h else 100, step=10, key="ex_n1" if only_1h else "ex_n",
                     help="61~100위까지 넣어야 스캔 범위 판단이 가능해요")
    if only_1h:
        ex_days = x2.selectbox("기간", [365, 730], index=1, key="ex_days1", format_func=lambda d: f"{d // 365}년")
        ex_1h = False
    else:
        ex_days = x2.selectbox("기간", [365, 730, 1095, 1825], index=1, key="ex_days",
                               format_func=lambda d: f"{d // 365}년" + (" (2022 하락장 포함)" if d >= 1825 else ""))
        ex_1h = st.checkbox("1시간봉 포함 (상위 30개·1년, 파일이 커지고 시간이 더 걸려요)", value=False, key="ex_1h")
    if only_1h:
        st.caption(f"1시간봉 {ex_n}개 코인 · {ex_days // 365}년 + 일봉 · 예상 소요 {max(5, round(ex_n * 0.35 * ex_days / 365))}분 이상 · "
                   f"예상 크기 약 {max(2, round(ex_n * 0.17 * ex_days / 365 + 1))}MB (30MB가 넘으면 코인 수를 줄여 주세요)")
    else:
        st.caption("함께 담기는 것: 경제 일정, 상위 30개 코인의 펀딩비·미결제약정 기록, 코인베이스 프리미엄 계산용 BTC·ETH 1시간봉(최대 2년)")
        _yrs_full, _yrs_more = min(ex_days, 730) / 730, max(0, ex_days - 730) / 730
        st.caption(f"예상 소요 {max(5, round(ex_n * (0.12 * _yrs_full + 0.08 * _yrs_more) + 5 + (6 if ex_1h else 0)))}분 이상 · "
                   f"예상 크기 약 {max(2, round(ex_n * (0.11 * _yrs_full + 0.06 * _yrs_more) + 2 + (5 if ex_1h else 0)))}MB")
    if ex_days > 730:
        st.caption("⏳ 3·5년은 오래 걸려요. 도중에 화면이 꺼져 끊겨도 같은 설정으로 다시 누르면, 12시간 안에 받아 둔 코인은 "
                   "건너뛰고 이어서 받아요. 오래된 코인만 5년치가 있고, 최근 상장 코인은 상장일부터 들어가요. "
                   "1시간봉은 이미 받은 2년 파일에 있으니 빼는 게 좋아요(업로드 크기).")
    if st.button("▶ 데이터 만들기", use_container_width=True):
        prog = st.progress(0.0, text="데이터 받는 중...")

        def cb(i: int, n: int, sym: str) -> None:
            prog.progress(min(i / max(n, 1), 1.0), text=f"{sym} ({i}/{n})")

        with store["lock"]:
            cmr.BITGET_ONLY = bitget_only
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    store["export"] = cmr.export_research_data(ex_n, ex_days, ex_1h, cb, only_1h=only_1h)
                now_ = pd.Timestamp.now(tz="UTC").tz_localize(None) + pd.Timedelta(hours=9)
                store["export_name"] = (f"research_data_{'1h_' if only_1h else ''}{ex_n}coins_{ex_days}d_{now_:%Y%m%d_%H%M}.zip")
                store["export_error"] = None
            except Exception:
                store["export_error"] = traceback.format_exc() + "\n" + buf.getvalue()
        prog.empty()
    if store.get("export_error"):
        st.error("데이터 만들기 중 오류가 발생했어요. 아래 내용을 그대로 복사해서 알려주세요.")
        st.code(store["export_error"], language=None)
    if store.get("export"):
        st.download_button(f"⬇ 다운로드 ({len(store['export']) / 1e6:.1f}MB)", data=store["export"],
                           file_name=store["export_name"], mime="application/zip", use_container_width=True)
        st.caption("받은 zip 파일을 그대로 Claude 대화창에 올려주세요. 압축을 풀 필요 없어요.")


page = st.radio("화면", ["📅 오늘의 투자", "📋 추천", "🧪 전략 연구실"], horizontal=True, key="page",
                label_visibility="collapsed")
if page.endswith("전략 연구실"):
    render_lab()
    st.divider()
    render_export()
    st.divider()
    render_macro_validation()
    st.divider()
    render_hourly_vol()
    st.stop()


def _cloud_after_scan() -> None:
    try:
        cmr.cloud_sync(force=True)
    except Exception:
        pass


def run_scan(force: bool) -> None:
    prog = st.progress(0.0, text="다른 분석이 진행 중이면 잠시 기다려요...")

    def cb(i: int, n: int, sym: str) -> None:
        prog.progress(min(i / max(n, 1), 1.0), text=f"분석 중 {i}/{n}  {sym}")

    with store["lock"]:
        r = store["result"]
        # 기다리는 동안 다른 접속이 이미 이번 봉 기준으로 스캔했다면 그 결과를 재사용
        if not force and r is not None and not cmr.needs_full_rescan(r.get("asof_utc"), r.get("timeframe")):
            prog.empty()
            return
        cmr.TOP_N_BY_VOLUME = top_n
        cmr.BITGET_ONLY = bitget_only
        cmr.ENABLED_FAMILIES = set(fams)
        cmr.AUTO_DEFENSE = auto_def
        cmr.MACRO_FILTER = macro_f
        cmr.set_timeframe(tf)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                res_ = cmr.run_analysis(risk_cfg, cb)
            store.update(result=res_, log=buf.getvalue(), error=None, scan_cfg=scan_cfg, live_at=cmr.utc_now())
        except Exception:
            store["error"] = traceback.format_exc()
            store["log"] = buf.getvalue()
        _cloud_after_scan()   # 실전 추적이 갱신됐으니 바로 클라우드에 저장
    prog.empty()


top_l, top_r = st.columns([3, 2])
refresh = top_r.button("🔄 새로 분석", type="primary", use_container_width=True)
cur = store["result"]
forced = refresh or store.get("force_rescan", False)
if forced or cur is None or cmr.needs_full_rescan(cur.get("asof_utc"), cur.get("timeframe")):
    store["force_rescan"] = False
    run_scan(force=forced)

res = store["result"]
if store["error"]:
    st.error("분석 중 오류가 발생했어요. 아래 내용을 그대로 복사해서 알려주세요.")
    st.code(store["error"], language=None)
if res is None:
    st.stop()
res_tf = res.get("timeframe", "4h")
top_l.caption(f"전체 분석 {kst(res['asof_utc'])} · 다음 신호 갱신 {kst(cmr.next_candle_close(res_tf))} "
              f"({cmr.tf_label(res_tf)} 마감)")
if store["scan_cfg"] != scan_cfg:
    st.info("설정(스캔 코인 수·봉 모드 등)이 바뀌었어요. '🔄 새로 분석'을 누르면 반영됩니다.")
if res_tf == "1h":
    st.caption("⚠️ 1시간봉은 비교용이에요. 신호가 잦은 대신 가짜 신호와 수수료 비중이 커집니다.")

# ---------------------------------------------------------------- 시장 국면
regime = res["regime"]
macro_hours = regime.snapshot.get("macro_span_hours", 0.0)
st.markdown(L.regime_html(regime, macro_hours), unsafe_allow_html=True)
if "btc_d" not in regime.snapshot:
    st.caption("ⓘ 이번엔 도미넌스 데이터(CoinGecko·CoinPaprika) 응답을 못 받았어요. "
               "나머지 근거로 정상 판단했고, 다음 갱신 때 다시 시도합니다.")
_flow_html = L.flow_summary_html(regime)
if _flow_html:
    f_ = regime.flow
    _idx = pd.to_datetime(f_["date"])
    st.markdown(_flow_html, unsafe_allow_html=True)
    st.line_chart(pd.DataFrame({"일봉 흐름 점수": f_["score"], "상승 기준 +0.25": 0.25, "하락 기준 −0.25": -0.25},
                               index=_idx), height=170)
    with st.expander("📈 BTC·알트 지수 흐름 · 50일선 위 코인 비율"):
        cols_ = {"BTC": f_["btc"]}
        if f_.get("alt"):
            cols_["알트 지수"] = f_["alt"]
        st.caption("기간 시작일 = 100으로 맞춘 흐름이에요. 알트 지수가 BTC보다 위로 벌어지면 알트 강세, 아래로 벌어지면 BTC 주도예요.")
        st.line_chart(pd.DataFrame(cols_, index=_idx), height=200)
        st.caption("50일선 위에 있는 코인 비율(%) — 50% 위면 시장 참여가 넓고, 아래면 좁아요.")
        st.bar_chart(pd.DataFrame({"50일선 위 코인 %": f_["b50pct"]}, index=_idx), height=150)
        st.caption("ⓘ 일봉 근거로 날마다 다시 계산한 점수예요(메인 판정 근거의 약 75%). 4시간봉 근거와 도미넌스 기록은 "
                   "일별로 재현할 수 없어서 빠지므로, 위 시장 카드의 판정과 조금 다를 수 있어요.")
with st.expander("📊 판단 근거 자세히"):
    st.markdown(L.regime_detail_html(regime), unsafe_allow_html=True)

# ---------------------------------------------------------------- 서킷브레이커
breaker = cmr.circuit_breaker_triggered(risk_cfg)
if breaker:
    st.error(f"🛑 {breaker}\n\n손실 만회 목적의 추가 진입이 계좌를 가장 크게 망가뜨립니다. 오늘은 쉬세요.")
    st.stop()


# ---------------------------------------------------------------- 추천 (가격만 자동 갱신)
def live_refresh_if_due() -> None:
    r = store["result"]
    if not r or not (r.get("all_setups") or (r.get("filter_stats") or {}).get("watchlist")):
        return
    last = store.get("live_at")
    if last is not None and (cmr.utc_now() - last).total_seconds() < live_sec:
        return
    if not store["lock"].acquire(blocking=False):  # 전체 스캔 중이면 이번 갱신은 건너뜀
        return
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmr.refresh_live_status(r["all_setups"])
            wl = (r.get("filter_stats") or {}).get("watchlist") or []
            if wl:
                cmr.refresh_watch_prices(wl[:30])
        store["live_at"], store["live_log"] = cmr.utc_now(), buf.getvalue()
    except Exception:
        store["live_log"] = traceback.format_exc()
    finally:
        store["lock"].release()


def render(items: list, slots: int = 10 ** 6) -> None:
    ev = cmr.active_event()
    for k, s in enumerate(items):
        mult = getattr(s, "risk_mult", 1.0) or 1.0  # 자동 방어 '주의'면 0.5
        s.event_note = cmr.event_text(ev) if ev else ""
        if ev:
            mult *= cmr.EVENT_RISK_MULT
        rc = dataclasses.replace(risk_cfg, risk_per_trade_pct=risk_pct * mult)
        sizing = cmr.apply_liquidity_cap(cmr.calculate_position_size(s.entry_price, s.sl, rc), getattr(s, "vol24h", None))
        st.markdown(L.card_html(s, sizing, risk_pct * mult, over_limit=k >= slots), unsafe_allow_html=True)
        with st.expander("📋 주문 메모 (복사)"):
            st.code(L.order_memo(s, sizing, L.leverage_guide(s.entry_price, s.sl)), language=None)


def recommendations() -> None:
    # 화면을 켜둔 채 봉이 마감되면(자동 갱신 중 감지) 전체 재분석을 위해 앱 전체를 다시 실행
    r0 = store["result"]
    if fragment is not None and r0 is not None and not store["error"] \
            and cmr.needs_full_rescan(r0.get("asof_utc"), r0.get("timeframe")):
        st.rerun()
    live_refresh_if_due()
    r = store["result"]
    fs_ = r.get("filter_stats") or {}
    if fs_.get("perp_source") == "none":
        st.warning("⚠️ Bitget 선물 목록을 받지 못해 선물 거래 가능 여부를 확인하지 못했어요. 이번 추천·돌파 임박 코인은 "
                   "Bitget에 있는지 직접 확인하세요. 다음 분석 때 자동으로 다시 시도해요.")
    elif fs_.get("perp_source") == "cache":
        st.caption("ⓘ Bitget 선물 목록 조회가 일시적으로 실패해 최근 저장본(7일 이내)으로 걸렀어요.")
    _ud = fs_.get("universe_diag") or {}
    if fs_.get("universe") and fs_["universe"] < 0.8 * top_n:
        st.warning(f"⚠️ 스캔한 코인이 설정({top_n}개)보다 적은 {fs_['universe']}개예요. 거래소 응답: {_ud.get('거래량 목록', '확인 불가')}. "
                   "실패한 거래소가 있으면 서버 위치 때문에 막힌 거예요(Streamlit Cloud는 해외 서버). 추천이 PC와 다를 수 있어요.")
    mf_ = r.get("macro_filter") or {}
    if mf_.get("blocked_side"):
        st.info(f"🧭 거시 필터: 일봉 흐름 점수 {mf_['score']:+.2f} → {mf_['blocked_side']} 신호 제외 중"
                + (f" (이번 스캔 {mf_['excluded']}개 제외)" if mf_.get("excluded") else ""))
    ev_ = cmr.active_event()
    if ev_:
        st.warning(f"📅 이벤트 위험 시간대: {cmr.event_text(ev_)}. 크게 흔들리는 구간이라 신규 진입 리스크를 절반으로 "
                   "계산했어요. 시장가 진입은 피하고, 보유 포지션의 손절 주문을 확인하세요.")
    paused = [s for s in r["all_setups"] if getattr(s, "health", "") == "paused"]
    active_all = [s for s in r["all_setups"] if getattr(s, "health", "") != "paused"]
    with contextlib.redirect_stdout(io.StringIO()):
        setups = cmr.cap_correlated_exposure(active_all, risk_cfg) if active_all else []
    for fam_, h_ in (r.get("health") or {}).items():
        if h_.get("state") == "paused":
            st.error(f"⏸ 자동 중지: '{fam_}' 신호 — {h_['reason']}. 성과가 회복되면 자동으로 다시 추천돼요.")
        elif h_.get("state") == "caution":
            st.warning(f"🛡 자동 방어: '{fam_}' 신호 — {h_['reason']}.")
    dead = [s for s in setups if s.live_status in ("invalid", "missed")]
    alive = [s for s in setups if s.live_status not in ("invalid", "missed")]
    ready = sorted([s for s in alive if s.live_status != "chase"], key=L.sort_key, reverse=True)
    waiting = sorted([s for s in alive if s.live_status == "chase"], key=L.sort_key, reverse=True)
    slots = cmr.available_slots(risk_cfg)

    if store.get("live_at") is not None:
        st.caption(f"💹 가격 갱신 {kst(store['live_at'])} · {live_sec}초마다 자동")
    if slots == 0:
        st.warning(f"총 리스크 상한 {max_total:g}%에 도달했어요 (보유 {int(open_pos)}개 × {risk_pct:g}%). "
                   "새 진입은 기존 포지션이 정리된 뒤에 검토하세요.")
    else:
        st.caption(f"🎯 새로 잡을 수 있는 포지션 {slots}개 (총 리스크 상한 {max_total:g}%, 보유 {int(open_pos)}개)")

    if not alive:
        rare = " 돌파 신호는 원래 드물어서 비어 있는 날이 많아요. 아래 '돌파 임박 관찰'에서 곧 신호가 날 수 있는 코인을 확인하세요." \
            if set(fams) == {"돌파"} else ""
        st.info("**지금은 조건에 맞는 코인이 없어요.**\n\n"
                "켜둔 신호 유형에서 손익비·상위추세·펀딩비·스프레드 조건을 모두 통과한 코인이 없다는 뜻입니다."
                f"{rare} 억지로 진입하지 않는 것도 전략이에요.")
    else:
        tab1, tab2 = st.tabs([f"✅ 진입 검토 ({len(ready)})", f"⏳ 대기 ({len(waiting)})"])
        with tab1:
            st.caption("현재가가 진입가 근처인 코인. 우선순위 순이고, 리스크 한도 안의 코인만 진입 대상이에요.")
            if ready:
                render(ready, slots)
            else:
                st.write("지금 진입가 근처인 코인은 없어요. '대기' 탭의 지정가를 확인하세요.")
        with tab2:
            st.caption("아직 되돌림 전이라 지금 들어가면 추격인 코인. 지정가만 걸어두고, "
                       "체결될 때 리스크 한도가 남아 있는지 확인하세요.")
            if waiting:
                render(waiting)
            else:
                st.write("대기 중인 코인이 없어요.")
    if dead:
        with st.expander(f"❌ 무효·놓침 ({len(dead)}) — 다음 봉 마감 때 목록에서 정리돼요"):
            for s in dead:
                st.markdown(L.dead_line(s), unsafe_allow_html=True)
    if paused:
        with st.expander(f"⏸ 자동 중지된 신호 ({len(paused)}) — 참고용, 진입 대상 아님"):
            st.caption("최근 실전 성과가 기준 아래로 떨어진 유형이에요. 결과는 계속 추적하고, 회복되면 자동으로 다시 추천돼요.")
            for s in paused:
                st.markdown(L.paused_line(s), unsafe_allow_html=True)
    watch = (r.get("filter_stats") or {}).get("watchlist") or []
    if watch:
        with st.expander(f"👀 돌파 임박 관찰 ({len(watch)}) — 아직 진입 신호 아님", expanded=not alive):
            st.caption("유효한 박스의 경계에 1 ATR 이내로 붙은 코인이에요. 봉이 거래량과 함께 경계 밖에서 마감하면 "
                       "위 추천 목록으로 올라와요. 미리 알림을 걸어두고 기다리세요.")
            for w in watch[:15]:
                st.markdown(L.watch_line(w), unsafe_allow_html=True)


def get_portfolio():
    """오늘의 추세 포트폴리오 (하루에 한 번 계산해서 저장). 꺼져 있으면 None."""
    if not tsm_on:
        return None
    day_key = (str(cmr.current_candle_start("1d")), port_n, round(bal_port, 2), cmr.TSM_TRIAL, port_gross)
    if store.get("port_key") != day_key:
        with st.spinner("일봉으로 포트폴리오 계산 중..."):
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    store["port"] = cmr.build_daily_portfolio(port_n, bal_port, port_gross)
                store["port_key"], store["port_error"] = day_key, None
                if cmr.record_portfolio(store["port"]):          # 날짜당 한 번: '앱 지시대로 했다면' 실전 추적용
                    cmr.cloud_sync()                                # (1분 간격 제한 — 못 올리면 다음 화면 갱신 때 올림)
            except Exception:
                store["port_error"] = traceback.format_exc() + "\n" + buf.getvalue()
    return None if store.get("port_error") else store.get("port")


def render_portfolio_section() -> None:
    st.markdown("#### ② 추세 포트폴리오 조정")
    if not tsm_on:
        st.info("추세 포트폴리오는 꺼져 있어요. 전략 연구실에서 통과하면 자동으로 켜지고, 설정에서 직접 켤 수도 있어요.")
        return
    if not (store["lab_cfg"] or {}).get("tsm"):
        st.caption("⚠️ 연구실 검증을 통과하지 않은 상태에서 켠 거예요. 참고용으로 보세요.")
    get_portfolio()
    if store.get("port_error"):
        st.error("포트폴리오 계산 중 오류가 발생했어요. 아래 내용을 그대로 복사해서 알려주세요.")
        st.code(store["port_error"], language=None)
        return
    pf = store.get("port")
    if not pf:
        return
    st.markdown(L.portfolio_gate_html(pf, cmr.tsm_gate_text(pf["gate"])), unsafe_allow_html=True)
    act = [r for r in pf["rows"] if r["status"] != "유지"]
    keep = [r for r in pf["rows"] if r["status"] == "유지"]
    dl = kst(pf["deadline"]) if pf.get("deadline") is not None else "21시"
    st.caption(f"기준 일봉 {pd.Timestamp(pf['date']):%m-%d} · 대상 {pf['coins']}개 중 보유 {pf['n_hold']}개 · "
               f"비중 합계 {pf['gross']:.0%} / 상한 {pf.get('gross_cap', 1.0):.0%} "
               f"(포트폴리오 금액 = 계좌의 {port_alloc}% = {bal_port:,.0f} USDT 기준) · ⏰ 조정 마감 {dl}")
    if pf.get("trial"):
        st.caption("🧪 시험 운용 중이라 비중을 ¼로 계산했어요. 다음 연구실에서 통과하면 정상 비중으로 바뀌어요.")
    if act:
        for r in act:
            st.markdown(L.portfolio_line(r), unsafe_allow_html=True)
    elif pf["n_hold"]:
        st.write("오늘은 조정할 포지션이 없어요. 보유 중인 코인을 그대로 두세요.")
    else:
        st.write("보유할 코인이 없어요. 포트폴리오 배분은 현금(USDT)으로 두세요.")
    if keep:
        with st.expander(f"⚪ 그대로 유지 ({len(keep)})"):
            for r in keep:
                st.markdown(L.portfolio_line(r), unsafe_allow_html=True)
    with st.expander("🧾 포트폴리오 주문 방법"):
        st.markdown(L.portfolio_exec_rules(pf.get("gross_cap", 1.0), dl))
    trk = cmr.portfolio_tracking()
    with st.expander("📒 포트폴리오 실전 추적 (앱 지시대로 했다면)", expanded=False):
        if not trk:
            st.write("기록이 이틀 이상 쌓이면 '매일 앱 목록대로 조정했다면'의 성과가 여기 나와요. 앱을 하루 한 번(09시 이후) 열면 자동 기록돼요.")
        else:
            st.line_chart(trk["curve"].rename("앱대로 했다면(시작=1)"))
            st.markdown(f"- {trk['start']} ~ {trk['end']} ({trk['days']}일, 기록 {trk['records']}일·빠진 날 {trk['missing']}일) · "
                        f"누적 **{trk['total']:+.1%}** · 최대 낙폭 {trk['max_dd']:.1%} · 관문 켜진 날 {trk['gate_days']}일 · 조정 {trk['rebalances']}회\n"
                        f"- 비교 기준(2.5년 검증): 보유한 날 약 31% · 플러스 달 29% · 쉬는 달 29% · 최악의 달 −8% — 수익이 몇 달에 몰려요")
        st.caption("ⓘ 규칙은 실전 기록 90일(관문 켜진 날 30일 이상)이 쌓이기 전에는 바꾸지 마세요. 짧은 기간 성과로 규칙을 바꾸면 "
                   "과거에만 맞는 규칙이 되기 쉬워요. 기록은 Gist에 함께 저장돼요.")
    st.caption("ⓘ 보유 조건: 시장 관문 켜짐 + 코인 종가가 50일선 위 + 30일 수익률 플러스. 하나라도 깨지면 다음 날 09시에 '정리'로 올라와요. "
               "포트폴리오 비중은 신호 매매의 리스크 한도와 별개라, 둘을 합친 전체 노출을 함께 확인하세요.")


def render_simulator() -> None:
    with st.expander("📈 성장 시뮬레이터 — 지금 페이스면 몇 년 뒤 얼마일까"):
        R_, src = cmr.simulation_source(cmr.load_tracks(), cmr.load_lab_reference())
        ref_ = cmr.load_lab_reference() or {}
        pcm = (ref_.get("per_coin_month") or {}).get("immediate" if cmr.BREAKOUT_ENTRY == "immediate" else "base")
        default_tpy = int(round(pcm * top_n * 12)) if pcm else 150
        g1, g2 = st.columns(2)
        dep = g1.number_input("월 추가 입금 (USDT)", min_value=0.0, value=0.0, step=50.0, key="sim_dep")
        until = g2.selectbox("기간", ["2030년 말", "2032년 말"], index=1, key="sim_until")
        g3, g4 = st.columns(2)
        tpy = g3.number_input("연간 거래 수", min_value=10, max_value=1000, value=max(10, default_tpy), step=10, key="sim_tpy",
                              help="기본값은 연구실 결과의 예상 빈도 × 스캔 코인 수. 1일 1거래면 365")
        rk = g4.number_input("거래당 리스크 (%)", min_value=0.1, max_value=5.0, value=float(risk_pct), step=0.1, key="sim_rk")
        st.caption(f"거래 결과 출처: {src}")
        if st.button("▶ 시뮬레이션", use_container_width=True):
            end = pd.Timestamp("2030-12-31" if until.startswith("2030") else "2032-12-31")
            years = max((end - pd.Timestamp.now()) / pd.Timedelta("365.25D"), 0.25)
            with st.spinner("계산 중..."):
                store["sim"] = {"out": cmr.growth_projection(R_, tpy, rk, years, balance, dep), "years": years,
                                "until": until, "tpy": tpy, "rk": rk, "dep": dep, "src": src}
        sim = store.get("sim")
        if not sim:
            return
        o = sim["out"]
        yrs = [f"{y:.2f}년 후" if y != int(y) else f"{int(y)}년 후" for y in o["year"]]
        st.dataframe(pd.DataFrame({"시점": yrs, "하위 10%": [f"${x:,.0f}" for x in o["p10"]],
                                   "중앙값": [f"${x:,.0f}" for x in o["p50"]], "상위 10%": [f"${x:,.0f}" for x in o["p90"]]}),
                     hide_index=True, use_container_width=True)
        st.markdown(f"- {sim['until']}까지 거래 {o['n_trades']:,}건 · 입금 총액 ${o['deposited']:,.0f}\n"
                    f"- 목표(500억 원 ≈ ${cmr.GOAL_USD / 1e6:.1f}M) 도달 확률 **{o['hit']:.1%}** · "
                    f"한때 고점 대비 90% 이상 잃을 확률 **{o['ruin']:.1%}**")
        st.caption("ⓘ 거래 결과를 실제 분포에서 다시 뽑아 2,000번 시뮬레이션해요. 계좌가 5만 달러를 넘으면 알트코인 체결 한계로 "
                   "거래당 성과가 조금씩 줄어드는 것까지 반영했어요. 과거·최근 성과가 앞으로도 이어진다는 가정이라 참고용이에요.")


def get_phase():
    """오늘의 시장 국면(하루 한 번). 실패하면 None — 화면에 표시만 하고 설정은 바꾸지 않음."""
    key = str(cmr.current_candle_start("1d"))
    if store.get("phase_key") != key:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                store["phase"] = cmr.market_phase()
        except Exception:
            store["phase"] = None
        store["phase_key"] = key
    return store.get("phase")


def phase_html(ph) -> str:
    s = ph["stats"]
    (nl, rl), (ns, rs) = s["long"], s["short"]
    return (f'<div class="allot">🧭 <b>시장 국면: {ph["name"]}</b> ({ph["days"]}일째) — BTC {ph["close"]:,.0f} · '
            f'200일 평균 대비 {ph["gap"]:+.1%} · 60일 수익률 {ph["r60"]:+.1%}<br>'
            f'<span class="k">5년 검증 이 국면 성과: 추천 신호 롱 {nl}건 {rl:+.2f}R{" (표본 적음)" if nl < 100 else ""} · '
            f'숏 {ns}건 {rs:+.2f}R{" (표본 적음)" if ns < 100 else ""} · '
            f'포트폴리오 연환산 {s["port"]} — {s["note"]}. 국면별로 설정을 바꾸는 건 5년 교차 검증에서 효과가 없어 '
            f'설정은 그대로예요.</span></div>')


def render_today() -> None:
    now_kst = pd.Timestamp.now(tz="UTC").tz_localize(None) + pd.Timedelta(hours=9)
    st.markdown(f"### 📅 {now_kst:%m월 %d일} 오늘의 투자")
    st.caption(f"⏱ 박스 돌파·돌파 후 되돌림은 봉 마감 직후(01·05·09·13·17·21시) 바로 시장가로 들어갈 때 가장 좋아요 — "
               f"2시간이 지나면 엣지가 줄고, 4시간이 넘으면 건너뛰세요. 📈 추세 눌림목은 8시간 안이면 늦게 들어가도 괜찮아요 · "
               f"다음 신호 갱신 {kst(cmr.next_candle_close(res_tf))}")
    live_refresh_if_due()
    r = store["result"]
    active_all = [s for s in r["all_setups"] if getattr(s, "health", "") != "paused"]
    with contextlib.redirect_stdout(io.StringIO()):
        setups = cmr.cap_correlated_exposure(active_all, risk_cfg) if active_all else []
    ready = sorted([s for s in setups if s.live_status == "ready"], key=L.sort_key, reverse=True)
    waiting = [s for s in setups if s.live_status == "chase"]
    slots = cmr.available_slots(risk_cfg)
    todo = ready[:slots] if slots > 0 else []
    pf = get_portfolio()
    watch = (r.get("filter_stats") or {}).get("watchlist") or []
    sig_notional = sig_risk = 0.0
    ev_now = cmr.active_event()
    for s in todo:
        mult = (getattr(s, "risk_mult", 1.0) or 1.0) * (cmr.EVENT_RISK_MULT if ev_now else 1.0)
        sz = cmr.apply_liquidity_cap(cmr.calculate_position_size(s.entry_price, s.sl, dataclasses.replace(
            risk_cfg, risk_per_trade_pct=risk_pct * mult)), getattr(s, "vol24h", None))
        sig_notional += sz["notional"]
        sig_risk += sz["risk_amount"]
    port_notional = (pf["gross"] * bal_port) if pf else 0.0
    n_adj = sum(1 for x in (pf or {}).get("rows", []) if x["status"] != "유지")
    gate_txt = ""
    if pf and (pf.get("gate") or {}).get("enabled"):
        gate_txt = (" (🟢 관문 켜짐" if pf["gate"]["on"] else " (🔴 관문 꺼짐") + \
                   (" · 오늘 바뀜)" if pf["gate"].get("changed") else ")")
    lev = (sig_notional + port_notional) / balance if balance else 0.0
    alloc_txt = ((f"신호 매매 계좌 전체 ({bal_sig:,.0f} USDT) 기준 + 추세 포트폴리오 계좌의 {port_alloc}% ({bal_port:,.0f} USDT) 추가"
                  if port_overlay else
                  f"신호 매매 {100 - port_alloc}% ({bal_sig:,.0f} USDT) · 추세 포트폴리오 {port_alloc}% ({bal_port:,.0f} USDT)")
                 if tsm_on else f"신호 매매 100% ({balance:,.0f} USDT) · 추세 포트폴리오 꺼짐")
    mf_ = r.get("macro_filter") or {}
    if mf_.get("blocked_side"):
        st.caption(f"🧭 거시 필터: 일봉 흐름 {mf_['score']:+.2f} → {mf_['blocked_side']} 신호 제외 중")
    n_maj = sum(1 for s in todo if getattr(s, "group", "") == "메이저")
    st.markdown(f'<div class="allot">📋 <b>오늘 할 일</b>: 신규 진입 {len(todo)}건(메이저 {n_maj} · 일반 알트 {len(todo) - n_maj}) · '
                f'지정가 대기 {len(waiting)}건 · '
                f'포트폴리오 조정 {n_adj}건{gate_txt} · 알림 {min(len(watch), 5)}건<br>'
                f'💼 <b>계좌 배분</b>: {alloc_txt}<br>'
                f'📊 <b>전체 노출</b>: 신규 진입 ${sig_notional:,.0f} + 포트폴리오 보유 ${port_notional:,.0f} '
                f'= 계좌의 <b>{lev:.1f}배</b> · 신규 진입이 모두 손절되면 −${sig_risk:,.0f} (계좌의 {sig_risk / balance * 100:.1f}%)'
                f'</div>', unsafe_allow_html=True)
    ph = get_phase()
    if ph:
        st.markdown(phase_html(ph), unsafe_allow_html=True)
    nxt = cmr.next_events(n=3)
    if ev_now:
        st.warning(f"📅 이벤트 위험 시간대: {cmr.event_text(ev_now)} — 발표 {cmr.EVENT_PRE_H}시간 전부터 {cmr.EVENT_POST_H}시간 "
                   "후까지는 크게 흔들려요. 신규 진입 리스크 절반·시장가 진입 자제·손절 주문 확인.")
    if nxt:
        st.caption("📅 다가오는 경제 일정: " + " · ".join(cmr.event_text(x) for x in nxt))
    if lev > 3:
        st.warning(f"전체 노출이 계좌의 {lev:.1f}배예요. 알트코인은 같이 움직여서 한꺼번에 손실이 날 수 있으니, "
                   "신규 진입 수를 줄이거나 포트폴리오 배분을 낮추세요.")
    st.caption("ⓘ 이미 보유 중인 신호 매매 포지션은 프로그램이 모르는 부분이라 노출 계산에 들어가지 않아요. "
               "설정의 '지금 보유 중인 포지션 수'로 리스크 한도에만 반영돼요.")
    st.markdown("#### ① 오늘 진입할 신호")
    if ready and slots > 0:
        st.caption(f"우선순위 순 · 리스크 한도 안에서 새로 잡을 수 있는 포지션 {slots}개")
        render(ready[:slots])
        if len(ready) > slots:
            st.caption(f"한도 밖 {len(ready) - slots}개는 '📋 추천' 화면에서 참고용으로 볼 수 있어요.")
    elif ready:
        st.warning("진입 신호가 있지만 총 리스크 한도에 도달했어요. 기존 포지션이 정리된 뒤에 검토하세요.")
    else:
        st.write("오늘은 새로 진입할 신호가 없어요." + (f" 지정가 대기 중인 신호 {len(waiting)}개는 '📋 추천' 화면에서 "
                                              "지정가를 걸어두세요." if waiting else ""))
    render_portfolio_section()
    st.markdown("#### ③ 알림 걸어둘 코인")
    watch = (r.get("filter_stats") or {}).get("watchlist") or []
    if watch:
        st.caption("거래소 앱에서 경계 가격에 가격 알림을 걸어두세요. 경계 밖에서 봉이 마감하면 신호로 올라와요.")
        for w in watch[:5]:
            st.markdown(L.watch_line(w), unsafe_allow_html=True)
    else:
        st.write("지금 박스 경계에 붙어 있는 코인이 없어요.")
    st.markdown("#### ④ 체크리스트")
    st.markdown("- 진입했다면 **손절 주문을 거래소에 바로 등록**했나요?\n"
                "- 보유 포지션 수를 설정의 '지금 보유 중인 포지션 수'에 반영했나요?\n"
                "- 어제 청산한 거래를 '📋 추천' 화면 아래 '매매 결과 기록'에 적었나요? (서킷브레이커용)\n"
                "- 자동 방어 경고(⚠️·⏸)가 있으면 그 유형은 비중을 줄이거나 쉬세요.")
    render_simulator()


fragment = getattr(st, "fragment", None)
if page.endswith("오늘의 투자"):
    render_today()
    st.stop()
if fragment is not None:
    recommendations = fragment(run_every=live_sec)(recommendations)
recommendations()

# ---------------------------------------------------------------- 실전 추천 자동 추적
_tracks = cmr.load_tracks()
if _tracks:
    with st.expander(f"📈 실전 추천 추적 ({len(_tracks)}건)"):
        st.caption("앱이 낸 추천을 '그대로 모두 진입했다면'으로 가정하고, 이후 실제 완성봉으로 체결·손절·익절 결과를 "
                   "자동 계산해요. 과거 검증이 아니라 지금 시장에서의 성과예요.")
        rows_, notes_ = cmr.tracking_summary(_tracks, cmr.load_lab_reference())
        if rows_:
            st.dataframe(pd.DataFrame(rows_), hide_index=True, use_container_width=True)
        for n_ in notes_:
            st.markdown(f"- {n_}")
        recent = [{"코인": t["symbol"], "유형": t["family"], "방향": "롱" if t["direction"] == "long" else "숏",
                   "신호(한국시간)": (pd.Timestamp(t["signal_ts"]) + pd.Timedelta(hours=9)).strftime("%m-%d %H:%M"),
                   "상태": t["status"], "R": t.get("R")} for t in reversed(_tracks[-12:])]
        st.dataframe(pd.DataFrame(recent), hide_index=True, use_container_width=True)
        st.caption("ⓘ 실제 체결 가격·시점과는 조금 다를 수 있어요. Streamlit Cloud는 앱이 재시작되면 기록이 지워질 수 있어요.")

# ---------------------------------------------------------------- 부가 기능
with st.expander("📖 용어 / 사용법"):
    st.markdown(
        "- **진입(지정가)**: 이 가격에 지정가 주문을 걸어요. 시장가로 쫓아 들어가지 않는 게 핵심입니다.\n"
        "- **손절**: 이 가격에 닿으면 무조건 정리. 수량은 '손절 시 손실이 계좌의 리스크 %'가 되도록 계산돼 있어요.\n"
        "- **청산 계획(v29)**: 목표1(추적 시작)에 닿아도 팔지 않고 손절을 진입가로 올림 → 이후 최고가(롱)·최저가(숏)에서 "
        "카드에 적힌 폭(박스 돌파 1 ATR, 되돌림·눌림목 0.75 ATR)만큼 되돌리면 전량 정리. 최대 보유 10일(60봉). "
        "진입 직후 손절 주문과 트레일링 주문(활성화 가격 = 목표1)을 함께 걸어두면 돼요.\n"
        "- **➕ 추가 진입(선택)**: 4시간봉 종가가 진입가 + 1R(손절폭만큼 유리)을 넘으면 계좌 0.5% 위험만큼 한 번 더. 추가분 손절 = 처음 진입가, "
        "정리는 본 포지션과 같이. 5년 검증 두 사이클 모두 수익↑·낙폭 +4%p.\n"
        "- **🔁 돌파 후 되돌림 / 📈 추세 눌림목**: 실전 추적 30건 평균이 플러스가 될 때까지 위험 절반.\n"
        "- **🧭 시장 국면**: BTC 일봉이 200일 평균 위·아래인지와 60일 수익률(±20%)로 나눈 4가지. 5년 검증에서 국면마다 설정을 "
        "바꾸는 건 효과가 없어 표시만 해요(국면별 과거 성과 참고용).\n"
        "- **손익비**: (목표1까지 거리) ÷ (손절까지 거리).\n"
        "- **상태**: ✅ 진입가 근처 / ⏳ 추격 구간(대기) / ❌ 무효(손절선을 먼저 넘음) / ⌛ 놓침(목표1에 먼저 닿음). "
        "가격만 자동 갱신하고, 신호 자체는 봉 마감 때만 새로 계산해요.\n"
        "- **총 리스크 상한**: 보유 포지션과 새 진입을 합쳐 한꺼번에 손절돼도 이 % 이상 잃지 않도록 진입 수를 제한해요.\n"
        "- **↔ 시장 역행**: 시장 전체 방향과 반대로 가는 코인. 개별적으로는 근거가 있지만 거시 흐름을 거스르니 비중을 작게.\n"
        "- **레버리지 상한 가이드**: 청산가가 손절가보다 훨씬 멀리 있도록 잡은 상한(최대 10배, 격리마진 기준).\n"
        "- **🎯 매물대 겹침 / 🩸 유동성 스윕**: 신뢰도를 높여주는 보조 근거예요."
    )

with st.expander("📝 매매 결과 기록 (서킷브레이커용)"):
    st.caption("하루 손실 5% / 주간 10%를 넘으면 신규 추천을 자동 중단합니다. "
               "※ Streamlit Cloud에서는 앱이 재시작되면 기록이 초기화될 수 있어요.")
    lc1, lc2 = st.columns(2)
    pnl = lc1.number_input("손익 (USDT, 손실은 음수)", value=0.0, step=1.0, key="pnl_in")
    sym_in = lc2.text_input("코인 (선택)", key="pnl_sym")
    if st.button("기록 저장"):
        cmr.log_trade_result(float(pnl), sym_in)
        st.success("저장했어요.")
        st.rerun()

fs = res.get("filter_stats") or {}
if fs.get("universe"):
    with st.expander(f"🧮 필터 통과 현황 ({fs.get('universe', 0)}개 → {fs.get('passed', 0)}개)"):
        st.caption("각 조건에서 몇 개가 걸러졌는지예요. 한 조건이 거의 다 걸러내거나 아무것도 못 거르면 기준 점검이 필요해요.")
        if fs.get("universe_diag"):
            st.caption("스캔 대상: " + ", ".join(f"{k} {v}" for k, v in fs["universe_diag"].items()))
        rows = sorted((fs.get("rejects") or {}).items(), key=lambda kv: -kv[1])
        if rows:
            st.table(pd.DataFrame([{"제외 사유": cmr.REJECT_LABELS.get(k, k), "코인 수": v} for k, v in rows]))

if store["log"].strip() or store.get("live_log", "").strip():
    with st.expander("🔍 분석 로그"):
        st.code((store["log"] + "\n" + store.get("live_log", "")).strip(), language=None)

st.caption("⚠️ 참고용 정보이며 수익을 보장하지 않습니다. 손절은 반드시 지키고, 감당 가능한 금액만 사용하세요.")
