
import io
import json
import os
import re
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
import xml.etree.ElementTree as ET

import feedparser
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="GP 투자 모니터", page_icon="📈", layout="wide", initial_sidebar_state="collapsed")

DART_BASE = "https://opendart.fss.or.kr/api"
DART_VIEW = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

CACHE_DIR = Path(".cache")
CACHE_DIR.mkdir(exist_ok=True)
CORP_CACHE = CACHE_DIR / "corp_codes.json"

CRYPTO = {
    "BTC": {"id": "bitcoin", "name": "비트코인"},
    "SOL": {"id": "solana", "name": "솔라나"},
    "NEAR": {"id": "near", "name": "니어프로토콜"},
    "UNI": {"id": "uniswap", "name": "유니스왑"},
}

KEYWORDS = {
    "내부자·지분": [
        "임원", "주요주주", "특정증권등소유상황보고서", "대량보유상황보고서",
        "최대주주", "주식등의대량보유상황"
    ],
    "자사주": [
        "자기주식취득", "자기주식처분", "자기주식소각",
        "자기주식취득신탁계약", "자기주식처분결과", "자기주식취득결과"
    ],
    "자금조달": [
        "유상증자", "전환사채", "교환사채", "신주인수권부사채",
        "CB", "EB", "BW", "증권신고서"
    ],
    "주식수·지배구조": [
        "감자", "주식분할", "주식병합", "무상증자", "합병", "분할", "최대주주변경"
    ],
    "사업·수주": [
        "단일판매", "공급계약", "투자판단관련주요경영사항", "시설투자", "타법인주식"
    ],
}

def safe_get(url, params=None, timeout=20):
    headers = {"User-Agent": "Mozilla/5.0 GP-Invest-Monitor/1.0"}
    r = requests.get(url, params=params, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r

def classify_disclosure(title: str):
    title_compact = re.sub(r"\s+", "", title or "")
    hits = []
    for category, kws in KEYWORDS.items():
        if any(re.sub(r"\s+", "", k).lower() in title_compact.lower() for k in kws):
            hits.append(category)
    return ", ".join(hits) if hits else "기타"

@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def load_corp_codes(api_key: str):
    """OpenDART 고유번호 ZIP을 내려받아 회사명/종목코드 매핑을 만든다."""
    # 로컬 캐시는 키와 무관한 공개 기업코드 데이터이므로 재사용
    if CORP_CACHE.exists():
        try:
            cached = json.loads(CORP_CACHE.read_text(encoding="utf-8"))
            if cached:
                return cached
        except Exception:
            pass

    r = safe_get(f"{DART_BASE}/corpCode.xml", params={"crtfc_key": api_key}, timeout=40)
    content = r.content

    # 정상 응답은 ZIP. 오류면 XML/텍스트일 수 있음.
    if content[:2] != b"PK":
        raise RuntimeError("DART 고유번호 다운로드 실패: API 키 또는 호출 제한을 확인하세요.")

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        xml_name = zf.namelist()[0]
        xml_bytes = zf.read(xml_name)

    root = ET.fromstring(xml_bytes)
    companies = []
    for item in root.findall("list"):
        corp_name = (item.findtext("corp_name") or "").strip()
        corp_code = (item.findtext("corp_code") or "").strip()
        stock_code = (item.findtext("stock_code") or "").strip()
        if corp_name and corp_code:
            companies.append({
                "corp_name": corp_name,
                "corp_code": corp_code,
                "stock_code": stock_code,
            })

    CORP_CACHE.write_text(json.dumps(companies, ensure_ascii=False), encoding="utf-8")
    return companies

def resolve_company(name: str, companies):
    n = name.strip().replace(" ", "")
    exact = [c for c in companies if c["corp_name"].replace(" ", "") == n]
    if exact:
        # 상장사 우선
        exact.sort(key=lambda x: bool(x.get("stock_code")), reverse=True)
        return exact[0]
    partial = [c for c in companies if n in c["corp_name"].replace(" ", "")]
    partial.sort(key=lambda x: (bool(x.get("stock_code")), -len(x["corp_name"])), reverse=True)
    return partial[0] if partial else None

def fetch_disclosures(api_key, corp_code, start_dt, end_dt, page_count=100):
    params = {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bgn_de": start_dt.strftime("%Y%m%d"),
        "end_de": end_dt.strftime("%Y%m%d"),
        "page_count": min(int(page_count), 100),
        "sort": "date",
        "sort_mth": "desc",
    }
    r = safe_get(f"{DART_BASE}/list.json", params=params)
    data = r.json()

    status = data.get("status")
    if status == "013":  # 조회된 데이터 없음
        return []
    if status != "000":
        raise RuntimeError(f"DART 오류 {status}: {data.get('message', '알 수 없는 오류')}")

    return data.get("list", [])

def disclosure_score(title, category):
    # 투자자가 놓치기 쉬운 핵심 공시를 상단에 올리기 위한 단순 규칙
    score = 0
    t = title or ""
    weights = [
        (["유상증자", "전환사채", "교환사채", "신주인수권부사채", "감자"], 5),
        (["자기주식취득", "자기주식소각", "자기주식처분"], 5),
        (["주요주주", "대량보유", "임원"], 4),
        (["단일판매", "공급계약", "시설투자", "투자판단"], 3),
        (["최대주주변경", "합병", "분할"], 4),
    ]
    for kws, w in weights:
        if any(k in t for k in kws):
            score += w
    if category == "기타":
        score -= 1
    return score

def dart_tab():
    st.header("① 한국 주식 DART 공시 자동 정리")
    st.caption("회사명을 입력하면 최근 공시를 가져와 내부자·자사주·자금조달·수주 등으로 자동 분류합니다.")

    with st.expander("처음 1번만 설정", expanded=True):
        api_key = st.text_input(
            "OpenDART API 인증키",
            value=(
                st.secrets.get("DART_API_KEY", "")
                if hasattr(st, "secrets")
                else os.getenv("DART_API_KEY", "")
            ) or os.getenv("DART_API_KEY", ""),
            type="password",
            help="OpenDART에서 무료 발급받은 40자리 인증키를 입력하세요. 앱이 키를 저장하지는 않습니다."
        )

    default_names = "태성, 주성엔지니어링, 제이앤티씨, LS머트리얼즈"
    names_text = st.text_area(
        "분석할 종목명 (쉼표 또는 줄바꿈)",
        value=default_names,
        height=90,
    )

    c1, c2, c3 = st.columns(3)
    with c1:
        start_dt = st.date_input("시작일", value=date.today() - timedelta(days=365))
    with c2:
        end_dt = st.date_input("종료일", value=date.today())
    with c3:
        important_only = st.checkbox("중요 공시만 보기", value=True)

    if st.button("DART 공시 불러오기", type="primary", use_container_width=True):
        if not api_key:
            st.error("OpenDART API 인증키를 먼저 입력하세요.")
            return

        names = [x.strip() for x in re.split(r"[,\n]+", names_text) if x.strip()]
        if not names:
            st.error("종목명을 입력하세요.")
            return

        try:
            with st.spinner("DART 기업코드와 공시를 불러오는 중..."):
                companies = load_corp_codes(api_key)
                all_rows = []
                unresolved = []

                for name in names:
                    comp = resolve_company(name, companies)
                    if not comp:
                        unresolved.append(name)
                        continue

                    filings = fetch_disclosures(
                        api_key, comp["corp_code"], start_dt, end_dt, page_count=100
                    )

                    for f in filings:
                        title = f.get("report_nm", "")
                        category = classify_disclosure(title)
                        score = disclosure_score(title, category)
                        all_rows.append({
                            "중요도": "★★★★★" if score >= 5 else "★★★★" if score >= 4 else "★★★" if score >= 3 else "★★" if score >= 1 else "★",
                            "점수": score,
                            "회사명": f.get("corp_name", comp["corp_name"]),
                            "종목코드": f.get("stock_code") or comp.get("stock_code", ""),
                            "접수일": f.get("rcept_dt", ""),
                            "분류": category,
                            "공시명": title,
                            "제출인": f.get("flr_nm", ""),
                            "정정": "정정" if f.get("rm", "") else "",
                            "DART 링크": DART_VIEW + f.get("rcept_no", ""),
                        })

            if unresolved:
                st.warning("회사명을 찾지 못함: " + ", ".join(unresolved))

            if not all_rows:
                st.info("해당 기간에 조회된 공시가 없습니다.")
                return

            df = pd.DataFrame(all_rows)
            if important_only:
                df = df[df["분류"] != "기타"].copy()

            df = df.sort_values(["점수", "접수일"], ascending=[False, False])
            st.success(f"{len(df):,}건 표시")

            st.dataframe(
                df.drop(columns=["점수"]),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "DART 링크": st.column_config.LinkColumn("DART 원문"),
                },
            )

            csv = df.drop(columns=["점수"]).to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "CSV로 저장",
                data=csv,
                file_name=f"dart_{date.today().isoformat()}.csv",
                mime="text/csv",
                use_container_width=True,
            )

            st.subheader("핵심 공시 요약")
            top = df.head(15)
            for _, row in top.iterrows():
                st.markdown(
                    f"**{row['회사명']} · {row['접수일']} · {row['분류']}**  \n"
                    f"{row['공시명']}  \n"
                    f"[DART 원문]({row['DART 링크']})"
                )

        except Exception as e:
            st.error(str(e))

@st.cache_data(ttl=60, show_spinner=False)
def fetch_crypto_prices(symbols):
    ids = ",".join(CRYPTO[s]["id"] for s in symbols)
    params = {
        "ids": ids,
        "vs_currencies": "krw,usd",
        "include_24hr_change": "true",
        "include_24hr_vol": "true",
        "include_market_cap": "true",
        "include_last_updated_at": "true",
    }
    return safe_get(f"{COINGECKO_BASE}/simple/price", params=params).json()

@st.cache_data(ttl=300, show_spinner=False)
def fetch_market_chart(coin_id, days):
    r = safe_get(
        f"{COINGECKO_BASE}/coins/{coin_id}/market_chart",
        params={"vs_currency": "usd", "days": days, "interval": "daily"},
    )
    data = r.json()
    prices = data.get("prices", [])
    if not prices:
        return pd.DataFrame(columns=["date", "price"])
    df = pd.DataFrame(prices, columns=["ts", "price"])
    df["date"] = pd.to_datetime(df["ts"], unit="ms")
    return df[["date", "price"]].set_index("date")

@st.cache_data(ttl=600, show_spinner=False)
def fetch_google_news(query, limit=10):
    # API키 없이 Google News RSS 사용
    from urllib.parse import quote
    url = (
        "https://news.google.com/rss/search?q="
        + quote(query)
        + "&hl=ko&gl=KR&ceid=KR:ko"
    )
    feed = feedparser.parse(url)
    rows = []
    for e in feed.entries[:limit]:
        rows.append({
            "제목": e.get("title", ""),
            "발행": e.get("published", ""),
            "링크": e.get("link", ""),
        })
    return rows

def crypto_tab():
    st.header("③ 암호화폐 투자 대시보드")
    st.caption("BTC·SOL·NEAR·UNI 가격, 24시간 변동, 거래량, 차트와 관련 뉴스를 한 화면에서 봅니다.")

    selected = st.multiselect(
        "코인 선택",
        options=list(CRYPTO.keys()),
        default=list(CRYPTO.keys()),
        format_func=lambda s: f"{s} · {CRYPTO[s]['name']}",
    )
    if not selected:
        st.info("코인을 하나 이상 선택하세요.")
        return

    try:
        prices = fetch_crypto_prices(selected)
    except Exception as e:
        st.error(f"가격 조회 실패: {e}")
        return

    cols = st.columns(len(selected))
    for i, sym in enumerate(selected):
        info = CRYPTO[sym]
        d = prices.get(info["id"], {})
        price_krw = d.get("krw")
        chg = d.get("krw_24h_change")
        with cols[i]:
            st.metric(
                f"{sym} · {info['name']}",
                f"₩{price_krw:,.0f}" if isinstance(price_krw, (int, float)) else "-",
                f"{chg:+.2f}%" if isinstance(chg, (int, float)) else None,
            )

    st.divider()
    c1, c2 = st.columns([1, 3])
    with c1:
        chart_symbol = st.selectbox("차트 코인", selected)
        chart_days = st.selectbox("기간", [7, 30, 90, 180, 365], index=1)
    with c2:
        try:
            chart = fetch_market_chart(CRYPTO[chart_symbol]["id"], chart_days)
            if not chart.empty:
                st.line_chart(chart, y="price", use_container_width=True)
            else:
                st.info("차트 데이터가 없습니다.")
        except Exception as e:
            st.warning(f"차트 조회 실패: {e}")

    st.subheader("시장 데이터")
    rows = []
    for sym in selected:
        info = CRYPTO[sym]
        d = prices.get(info["id"], {})
        rows.append({
            "코인": f"{sym} · {info['name']}",
            "원화": d.get("krw"),
            "달러": d.get("usd"),
            "24시간 변동률(%)": d.get("krw_24h_change"),
            "24시간 거래량(USD)": d.get("usd_24h_vol"),
            "시가총액(USD)": d.get("usd_market_cap"),
        })
    market_df = pd.DataFrame(rows)
    st.dataframe(
        market_df.style.format({
            "원화": "{:,.0f}",
            "달러": "{:,.4f}",
            "24시간 변동률(%)": "{:+.2f}",
            "24시간 거래량(USD)": "{:,.0f}",
            "시가총액(USD)": "{:,.0f}",
        }, na_rep="-"),
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("관련 최신 뉴스")
    for sym in selected:
        with st.expander(f"{sym} · {CRYPTO[sym]['name']} 뉴스", expanded=(sym == selected[0])):
            query = f'{CRYPTO[sym]["name"]} OR {sym} 암호화폐'
            try:
                news = fetch_google_news(query, limit=8)
                if news:
                    for item in news:
                        st.markdown(f"- [{item['제목']}]({item['링크']})")
                else:
                    st.info("뉴스가 없습니다.")
            except Exception as e:
                st.warning(f"뉴스 조회 실패: {e}")

    st.caption(
        "가격 데이터는 외부 공개 API, 뉴스는 공개 RSS를 사용합니다. "
        "API 지연·호출 제한이 있을 수 있으며 투자판단 전 원문 확인이 필요합니다."
    )

def main():
    st.title("📈 GP 투자 모니터")
    st.write("형이 요청한 **1번 DART 공시 자동 정리 + 3번 암호화폐 대시보드**를 한 앱에 합쳤습니다.")

    tab1, tab2 = st.tabs(["① DART 공시", "③ 암호화폐"])
    with tab1:
        dart_tab()
    with tab2:
        crypto_tab()

    st.divider()
    st.caption("투자 참고용 도구입니다. 공시·가격·뉴스 원문을 최종 확인하세요.")

if __name__ == "__main__":
    main()
