"""
TRDB — Tungsten Recycling Daily Brief
BRDB(배터리 재활용) 파이프라인을 텅스텐 리사이클링용으로 이식한 버전.

BRDB와 달라진 핵심 3가지
  1) 선물 없음 : 텅스텐은 LME·GFEX 어디에도 상장돼 있지 않다.
     콘탱고/백워데이션 대신 가치사슬 스프레드 3종을 쓴다.
       ① 지불률   : 폐초경 스크랩 ÷ 신품 WC 분말
       ② 재생할인 : 재생 WC 분말 ÷ 신품 WC 분말
       ③ 중국-서방: CIF 로테르담 APT ÷ 중국 내수 APT
  2) 3일 윈도우 : 텅스텐은 기사가 적어 3일치를 본다. 대신 실행 간
     중복 제거(seen.json)가 필수다 — 없으면 같은 기사가 사흘 실린다.
  3) 단위 혼재 : USD/kg · USD/t · USD/mtu · USD/standard tonne 가 섞인다.
     고시가를 그대로 보여주고, W 금속 환산 열을 따로 병기한다.
"""

import os
import re
import json
import glob
import time
import html as html_lib
import asyncio
import smtplib
import hashlib
import requests
import urllib.parse
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone

from playwright.async_api import async_playwright
import google.generativeai as genai


# ============================================================
# 환경
# ============================================================
GEMINI_API_KEY = os.environ['GEMINI_API_KEY']
GMAIL_USER     = os.environ['GMAIL_USER']
GMAIL_APP_PASS = os.environ['GMAIL_APP_PASSWORD']
TO_EMAIL       = os.environ['TO_EMAIL']
BCC_EMAIL      = os.environ.get('BCC_EMAIL', '')

genai.configure(api_key=GEMINI_API_KEY)

# TODO: repo 만든 뒤 REPO_NAME 확정
GITHUB_USER = "benseo57-netizen"
REPO_NAME   = "trdb-proxy"
WEB_BASE    = f"https://{GITHUB_USER}.github.io/{REPO_NAME}"

PRICE_JSON = "docs/data/prices.json"
SEEN_JSON  = "docs/data/seen.json"
OPML_FILE  = "feeds.opml"

VAT_RATE   = 1.13          # 중국 증치세
MTU_PER_T  = 88.5          # 1 t APT = 88.5 mtu WO3 (APT 88.5% WO3 기준)
SEEN_KEEP_DAYS = 21        # 중복판정 보관기간

# 날씨 (Open-Meteo, 키 불필요) — BRDB와 동일
WEATHER_SPOTS = [
    {"name": "새만금", "lat": 35.9494, "lon": 126.6083},
    {"name": "전주시", "lat": 35.7983, "lon": 127.1150},
    {"name": "서울",   "lat": 37.5665, "lon": 126.9780},
]


def now_kst() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=9)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ============================================================
# 시세 대상
# ============================================================
# 헤드라인 8종 — 개별 상세 페이지에서 수집한다.
#   상세 페이지만 증치세 포함/제외를 둘 다 주기 때문에 목록 페이지로
#   대체할 수 없다. 목록 페이지는 아래 LIST_EXTRA_GROUPS 롱테일용.
#
#   w   : W 금속 환산계수 (순수 함량 기준, 회수율 미반영)
#         APT   0.885 x (183.84/231.84) = 0.7017
#         WC    183.84/195.85           = 0.9387
#         정광  0.65  x (183.84/231.84) = 0.5154
#         FeW   75~85% 중간값           = 0.80
#         스크랩 WC-Co (Co 6~10% 가정)   = 0.87   ← 사내 기준 있으면 교체
#   lo/hi: 파싱 오류 방지용 허용 범위
SPOT_TARGETS = [
    {"key": "apt_cn",     "name": "APT (중국 내수)",   "name_en": "Ammonium Paratungstate",
     "pid": "201308090018", "unit": "USD/t",     "w": 0.7017, "lo": 5_000, "hi": 500_000, "grp": "화합물"},
    {"key": "apt_rott",   "name": "APT (CIF 로테르담)", "name_en": "CIF Rotterdam 88.5% APT",
     "pid": "202511260001", "unit": "USD/mtu",   "w": 0.7017, "lo": 100,   "hi": 20_000,  "grp": "화합물",
     "novat": True},
    {"key": "wc_new",     "name": "WC 분말 (신품)",    "name_en": "Tungsten Carbide Powder",
     "pid": "201308090020", "unit": "USD/kg",    "w": 0.9387, "lo": 5,     "hi": 1_000,   "grp": "분말"},
    {"key": "wc_regen",   "name": "WC 분말 (재생)",    "name_en": "Regenerated Tungsten Carbide Powder",
     "pid": "202307200001", "unit": "USD/kg",    "w": 0.9387, "lo": 5,     "hi": 1_000,   "grp": "분말"},
    {"key": "scrap_cnc",  "name": "폐 CNC 인서트",     "name_en": "Waste CNC Blades",
     "pid": "202307200004", "unit": "USD/kg",    "w": 0.87,   "lo": 2,     "hi": 1_000,   "grp": "스크랩"},
    {"key": "scrap_ball", "name": "폐 볼치",           "name_en": "Waste Ball Teeth",
     "pid": "202511190007", "unit": "USD/kg",    "w": 0.87,   "lo": 2,     "hi": 1_000,   "grp": "스크랩"},
    {"key": "few",        "name": "페로텅스텐 75-85%", "name_en": "Ferrotungsten 75-85%",
     "pid": "201606010003", "unit": "USD/t",     "w": 0.80,   "lo": 5_000, "hi": 500_000, "grp": "화합물"},
    {"key": "conc65",     "name": "흑중석 정광 65%",   "name_en": "Wolframite Concentrate 65%",
     "pid": "201308090016", "unit": "USD/std t", "w": 0.5154, "lo": 2_000, "hi": 400_000, "grp": "원광"},
]

SPOT_URL = "https://www-old.metal.com/Tungsten/{pid}"

# 동반금속 — 초경 바인더 코발트 (BRDB 스크래퍼 그대로)
COBALT_TARGET = {
    "key": "co_sulph", "name": "황산코발트", "name_en": "Cobalt Sulphate",
    "url": "https://www-old.metal.com/Chemical-Compound/201102250381",
    "unit": "USD/t", "w": None, "lo": 500, "hi": 200_000, "grp": "동반금속",
    "metal_content": 0.205, "metal_label": "Co",
}

# 롱테일 — 목록 페이지 1회 로드로 긁어 웹 페이지 '펼쳐보기'에만 노출
LIST_URL = "https://www-old.metal.com/price/Minor-Metals/Tungsten"
LIST_EXTRAS = [
    ("스크랩", "Tungsten Drill Bit Scrap",                "폐 드릴비트"),
    ("스크랩", "Tungsten Rod Scrap",                      "폐 로드"),
    ("스크랩", "Tungsten Steel Scrap from Grinding Process", "연삭 슬러지"),
    ("스크랩", "Tungsten Alloy Blade Scrap (Domestic)",   "폐 합금 블레이드"),
    ("스크랩", "Waste Anvils",                            "폐 앤빌"),
    ("스크랩", "Waste Roller Rings",                      "폐 롤러링"),
    ("스크랩", "Waste Tungsten Chips, Wires",             "폐 칩·와이어"),
    ("스크랩", "Waste Tungsten Blocks, Sheets",           "폐 블록·시트"),
    ("분말",   "Tungsten Powder",                         "W 분말"),
    ("분말",   "Medium-Grain Tungsten Carbide Powder FOB", "WC 분말 FOB"),
    ("화합물", "Ammonium Metatungstate (AMT)",            "AMT"),
    ("화합물", "Ferrotungsten ≥70%",                      "페로텅스텐 70%+"),
    ("화합물", "Rotterdam 75% Ferro-tungsten",            "FeW 로테르담"),
    ("화합물", "Sodium Tungstate",                        "텅스텐산나트륨"),
    ("화합물", "Tungsten Oxide",                          "산화텅스텐"),
    ("원광",   "Scheelite Concentrate 65%",               "회중석 정광 65%"),
    ("원광",   "Wolframite Concentrate 55%",              "흑중석 정광 55%"),
    ("원광",   "Scheelite Concentrate (30%-40% WO₃)",     "회중석 정광 30-40%"),
    ("금속",   "1# Tungsten bar",                         "텅스텐 봉 1#"),
    ("금속",   "Tungsten Ingot FOB Price",                "텅스텐 잉곳 FOB"),
]


# ============================================================
# 피드
# ============================================================
# OPML 에 없는 것만 코드에 남긴다. SMM 은 <metal> 태그 필터가 필요해
# _fetch_smm_rss() 가 전담하므로 OPML 로더에서 제외한다.
SMM_RSS_URL = "https://rss.metal.com/news/the_latest.xml"


def load_opml(path: str = OPML_FILE) -> list:
    """Inoreader OPML → 피드 목록. 폴더명이 '_전문지'로 끝나면 화이트리스트 면제."""
    if not os.path.exists(path):
        print(f"  ! {path} 없음 — 피드 0개")
        return []

    out = []

    def walk(node, folder=None):
        for o in node.findall("outline"):
            url = (o.get("xmlUrl") or "").strip()
            if not url:                                   # 폴더
                walk(o, (o.get("title") or o.get("text") or "").strip())
                continue
            if "rss.metal.com" in url:                    # SMM 은 전담 함수가 처리
                continue
            out.append({
                "url":     url,
                "source":  (o.get("title") or o.get("text") or "").strip(),
                "folder":  folder,
                "google":  "news.google.com" in url,
                "special": bool(folder and folder.endswith("_전문지")),
            })

    try:
        body = ET.parse(path).getroot().find("body")
        if body is not None:
            walk(body)
    except ET.ParseError as e:
        print(f"  ! OPML 파싱 실패: {e}")
        return []

    print(f"  OPML: {len(out)}개 피드 "
          f"(구글 {sum(1 for f in out if f['google'])} / "
          f"전문지 {sum(1 for f in out if f['special'])})")
    return out


def normalize_google_feed(url: str, window_days: int = 4) -> str | None:
    """OPML 의 구글뉴스 URL 재작성.
       꼬리 AND "" 제거 · 언어별 로케일 교정 · when:Nd · num=50"""
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    except Exception:
        return None

    term = (q.get("q", [""])[0]).strip()
    # Inoreader 에서 넘어온 깨진 쿼리 정리.
    #   따옴표 개수가 홀수면 마지막 하나를 버린다
    #   ("tungsten" AND "Sumitomo" "Mitsubishi" AND "  ← 실제로 이렇게 온다)
    if term.count('"') % 2 == 1:
        term = term[:term.rfind('"')] + term[term.rfind('"') + 1:]
    term = re.sub(r'\s+(?:AND|OR)\s+""\s*$', "", term).strip()
    term = re.sub(r'\s+(?:AND|OR)\s*$', "", term).strip()
    term = re.sub(r'\s{2,}', " ", term).strip()
    if not term:
        return None

    if re.search(r"[\uac00-\ud7a3]", term):
        hl, gl, ceid, lang = "ko", "KR", "KR:ko", "ko"
    elif re.search(r"[\u4e00-\u9fff]", term):
        hl, gl, ceid, lang = "zh-TW", "TW", "TW:zh-Hant", "zh"
    else:
        hl, gl, ceid, lang = "en-US", "US", "US:en", "en"

    if "when:" not in term:
        term = f"{term} when:{window_days}d"

    built = (f"https://news.google.com/rss/search?q={urllib.parse.quote(term)}"
             f"&hl={hl}&gl={gl}&ceid={ceid}&num=50"
             f"&cb={int(time.time())}")
    return built, lang


# ============================================================
# 노이즈
# ============================================================
# 텅스텐 구글뉴스는 절반이 소비재다. 반지·다트·낚시추·밈큐브가 핵심 차단 대상.
NOISE_KEYWORDS = [
    # 소비재 — 텅스텐 최대 오탐원
    "tungsten ring", "tungsten rings", "wedding band", "wedding ring",
    "engagement ring", "jewelry", "jewellery", "텅스텐 반지", "반지 추천",
    "darts", "dart barrel", "fishing weight", "fishing sinker", "tungsten jig",
    "golf weight", "putter", "club fitting", "낚시추", "다트",
    "tungsten cube", "heaviest metal you can buy", "desk toy",
    "filament", "light bulb", "백열전구", "전구",
    "watch case", "tungsten watch",
    # 게임·밀덕
    "war thunder", "world of tanks", "wargaming", "armor penetration guide",
    "playstation", "nintendo", "xbox", "gaming",
    # 주식·IR (BRDB 사전 유지)
    "stock tip", "stocks:", "is it too late", "stock surges", "stock falls",
    "shares surge", "shares fall", "etf",
    "주가 상승", "주가 하락", "주가 급등", "주가 급락", "특징주",
    "목표가 상향", "목표가 하향", "목표 주가", "투자의견", "유증", "유상증자",
    "[ir]", "ir공시", "전환사채", "브랜드 평판",
    "dividend", "share price", "fundamentals",
    # 일반 소비 전자·완성차
    "smartphone", "iphone", "galaxy", "ipad", "foldable",
    "시승기", "신차 출시", "사전계약", "주행거리", "제로백",
    # 무관 산업
    "crypto", "bitcoin", "ethereum", "nft",
    "green hydrogen", "수소차", "태양광", "solar panel", "photovoltaic",
    "dow jones", "s&p 500", "nasdaq",
    "eurekaalert", "whale watching",
]

NOISE_SOURCES = [
    "openpr", "prnewswire", "businesswire", "globenewswire", "einpresswire",
    "accesswire", "prnews", "prlog", "marketwired", "newswire", "pr.com", "prweb",
    "discoveryalert", "bravenewcoin", "eurekaalert", "cryptoslate", "coindesk",
    "benzinga", "seekingalpha", "motleyfool", "investopedia", "indexbox",
    "msn", "msn.com", "aol.com", "simplywall.st", "futunn.com", "judal.co.kr",
    "investingnews.com", "thebull.com.au", "marketsmojo.com", "stocktitan",
    "switzer.com.au", "nai500.com", "kalkinemedia.com", "chartmill",
    "ad-hoc-news", "tradebrains.in", "marketindex.com.au",
    # 텅스텐 특유 — 반지·공구 쇼핑몰
    "etsy", "amazon.com", "ebay", "alibaba", "made-in-china.com",
]

NOISE_URL_PATHS = ["/stock/", "/en/stock/", "/stocks/", "/share-price/",
                   "/equity/", "/product/", "/shop/", "/collections/"]

NOISE_PAIRS = [
    ("plastic", "recycl"), ("alumin", "recycl"), ("paper", "recycl"),
    ("packaging", "recycl"), ("e-waste", "phone"), ("battery", "recycl"),
    ("ring", "carbide"), ("ring", "tungsten"),
]

_NOISE_RE_KO = re.compile(
    r'(목표\s*주가|목표가)\s*(상향|하향|제시|유지|\d+만원|\d+달러|\d+억)'
    r'|\d+만원\s*(목표|하향|상향)'
    r'|(kb|nh투자|ibk투자|하나|미래에셋|키움|신한투자|대신|삼성|한국투자)증권\s*(전망|상향|하향|제시|목표|리포트|보고서)'
    r'|주가\s*(상승|하락|급등|급락|전망|목표)'
    r'|52주\s*(신고가|신저가)'
    r'|(상한가|하한가|거래정지)'
    r'|(유증|유상증자|전환사채|CB\s*발행)'
    r'|(코스닥|코스피)\s*(거래량|상위|순위)'
    r'|(애널리스트|analyst).{0,15}(전망|목표|제시|rating|target)'
    r'|\[IR\]|\[ir\]|IR공시',
    re.IGNORECASE)

_NOISE_RE_EN = re.compile(
    r'(raises?|cuts?|lifts?|lowers?|maintains?|reiterates?)\s*(price\s*)?target'
    r'|price\s*target\s*(raised|cut|lifted|lowered|increased|decreased)'
    r'|(upgrades?|downgrades?)\s*(to\s*)?(buy|hold|sell|overweight|underweight|neutral)'
    r'|analyst\s*(rating|note|report|target)'
    r'|stock\s*(surges?|soars?|falls?|drops?|rises?|climbs?)\s*\d+\s*%'
    r'|shares\s*(up|down)\s*\d+\s*%'
    r'|earnings\s*call\s*transcript'
    r'|share\s*price\s*and\s*fundamentals'
    r'|\d+\.?\d*%\s*(return|gain|rise)\s'
    # 쇼핑성 기사
    r'|best\s+(tungsten|carbide)\s+\w+\s+(of|for)\s+\d{4}'
    r'|buying\s+guide|gift\s+guide|deals?\s+of\s+the\s+day',
    re.IGNORECASE)


def is_stock_noise(title: str) -> bool:
    return bool(_NOISE_RE_KO.search(title) or _NOISE_RE_EN.search(title))


WHITELIST = [
    # 소재
    "tungsten", "텅스텐", "钨", "タングステン",
    "carbide", "초경", "硬质合金", "cemented carbide", "hardmetal", "hard metal",
    "apt", "ammonium paratungstate", "파라텅스텐산암모늄", "仲钨酸铵",
    "ferrotungsten", "페로텅스텐", "wolfram", "scheelite", "wolframite",
    "회중석", "흑중석", "정광", "concentrate",
    "tungsten oxide", "산화텅스텐", "tungstate", "amt",
    "wc powder", "tungsten powder", "텅스텐 분말", "탄화텅스텐",
    # 공정
    "recycl", "재활용", "순환이용", "리사이클",
    "zinc reclaim", "아연 재생", "습식제련", "hydrometallurg", "hydromet",
    "건식제련", "pyrometallurg", "leaching", "침출", "소다용융",
    "산화환원", "oxidation", "reduction", "제련", "refining", "정제",
    "회수율", "recovery rate", "지불률", "payable",
    # 수요·전방
    "cutting tool", "절삭공구", "insert", "인서트", "drill bit", "드릴비트",
    "milling", "machining", "기계가공", "mining equipment", "굴착",
    "분말야금", "powder metallurgy", "방산", "defense", "penetrator",
    "semiconductor tungsten", "w plug", "sputtering target",
    # 정책·시장
    "critical mineral", "핵심광물", "export control", "수출통제", "수출 규제",
    "stockpile", "비축", "crma", "critical raw materials",
    "supply chain", "공급망", "basel", "바젤", "관세", "tariff",
    "scrap", "스크랩", "폐기물", "circular economy",
    # 가격·기관
    "fastmarkets", "asian metal", "argus", "smm", "shanghai metals",
    "itia", "usgs",
]

# ---- 경쟁사·거래처 (초안: 글로벌 포함. 불필요한 건 지우세요) ----
_SCORE_COMPANY = [
    # 글로벌 리사이클러·분말사
    ("global tungsten", 12), ("plansee", 11), ("gtp ", 8),
    ("h.c. starck", 12), ("hc starck", 12), ("starck tungsten", 12),
    ("kennametal", 11), ("ceratizit", 11), ("sandvik", 9), ("hyperion", 9),
    ("wolfram bergbau", 10), ("buffalo tungsten", 10), ("cronimet", 9),
    ("tungsten parts wyoming", 9), ("global advanced metals", 8),
    # 일본
    ("mitsubishi materials", 11), ("sumitomo electric hardmetal", 11),
    ("nippon tungsten", 11), ("jx advanced metals", 9), ("a.l.m.t", 9),
    ("fujilloy", 9), ("allied material", 8),
    # 중국
    ("xiamen tungsten", 12), ("厦门钨业", 12),
    ("chongyi zhangyuan", 11), ("章源钨业", 11),
    ("xiamen golden egret", 10), ("china molybdenum", 9), ("cmoc", 8),
    ("china minmetals", 8), ("ganzhou", 8),
    # 광산·원료
    ("almonty", 13), ("sangdong", 13), ("상동광산", 13), ("알몬티", 13),
    ("masan high-tech", 11), ("nui phao", 11),
    ("eq resources", 9), ("tungsten west", 9), ("wolf minerals", 8),
    # 국내
    ("대구텍", 11), ("taegutec", 11), ("한국야금", 11), ("korloy", 11),
    ("와이지원", 8), ("yg-1", 8), ("베스트알", 10),
]

_SCORE_CORE = [
    ("텅스텐 재활용", 13), ("tungsten recycl", 13),
    ("초경 재활용", 13), ("carbide recycl", 13), ("hard metal recycl", 12),
    ("폐초경", 12), ("초경 스크랩", 12), ("carbide scrap", 12),
    ("tungsten scrap", 12), ("스크랩", 6), ("scrap", 5),
    ("아연 재생", 12), ("zinc reclaim", 12),
    ("습식제련", 10), ("hydrometallurg", 10), ("leaching", 7), ("침출", 7),
    ("산화환원", 9), ("oxidation reduction", 9),
    ("재생 분말", 11), ("regenerated", 9), ("recycled tungsten", 12),
    ("회수율", 7), ("recovery rate", 7), ("지불률", 10), ("payable", 8),
    ("재활용", 6), ("recycl", 5), ("순환이용", 6), ("circular economy", 5),
]

_SCORE_METAL = [
    ("apt", 7), ("ammonium paratungstate", 10), ("파라텅스텐산암모늄", 10),
    ("仲钨酸铵", 10), ("tungsten carbide", 8), ("초경합금", 8), ("硬质合金", 8),
    ("ferrotungsten", 8), ("페로텅스텐", 8),
    ("tungsten oxide", 7), ("산화텅스텐", 7), ("tungstate", 6), ("amt", 5),
    ("tungsten powder", 8), ("텅스텐 분말", 8), ("wc 분말", 9),
    ("scheelite", 8), ("wolframite", 8), ("회중석", 8), ("흑중석", 8),
    ("정광", 6), ("concentrate", 4),
    ("텅스텐", 5), ("tungsten", 4), ("钨", 5),
    ("코발트", 4), ("cobalt", 4),   # 초경 바인더 — 동반금속
]

_SCORE_POLICY = [
    ("수출통제", 12), ("export control", 12), ("수출 규제", 10), ("export ban", 10),
    ("수출 허가", 9), ("export licen", 9), ("수출 쿼터", 9), ("export quota", 9),
    ("핵심광물", 9), ("critical mineral", 9), ("critical raw material", 9),
    ("crma", 9), ("전략비축", 11), ("stockpile", 10), ("비축", 8),
    ("section 232", 9), ("반덤핑", 8), ("anti-dumping", 8),
    ("관세", 6), ("tariff", 6), ("바젤", 9), ("basel convention", 9),
    ("폐기물관리법", 9), ("환경부", 5), ("산업부", 5), ("조달청", 7),
    ("공급망", 6), ("supply chain", 6), ("de-risking", 8), ("탈중국", 9),
]

_SCORE_MARKET = [
    ("증설", 7), ("착공", 6), ("신설", 6), ("공장 신설", 8),
    ("합작", 7), ("joint venture", 7), ("mou", 4),
    ("인수", 6), ("acquisition", 6), ("지분 인수", 7),
    ("offtake", 9), ("장기 공급", 8), ("long-term supply", 8), ("장기계약", 8),
    ("가동 중단", 9), ("가동중단", 9), ("감산", 8), ("증산", 6),
    ("생산 차질", 9), ("halted", 7), ("shutdown", 8), ("suspend", 6),
    ("가동률", 7), ("재고", 6), ("inventory", 5),
    ("원료 확보", 9), ("원재료 확보", 9), ("내재화", 8), ("수직계열화", 8),
    ("사상 최고", 8), ("record high", 8), ("급등", 5), ("가격 인상", 7),
]

_SCORE_PENALTY = [
    # 배터리 — 대량 유입되므로 감점 (단 코발트는 동반금속이라 제외)
    ("리튬", -10), ("lithium", -10), ("니켈", -8), ("nickel", -8),
    ("양극재", -10), ("cathode", -9), ("전구체", -9), ("precursor", -8),
    ("블랙매스", -12), ("black mass", -12), ("폐배터리", -10),
    ("이차전지", -9), ("전기차 배터리", -9), ("ess", -8),
    # 소비재
    ("반지", -15), ("ring", -10), ("jewel", -15), ("다트", -15), ("darts", -15),
    ("낚시", -15), ("fishing", -12), ("golf", -12), ("watch", -8),
    ("전구", -12), ("filament", -10),
    # 주식·완성차
    ("주가", -15), ("특징주", -15), ("목표주가", -15), ("etf", -12),
    ("신차", -10), ("시승", -12), ("판매량", -6),
    # 무관 산업
    ("수소", -10), ("hydrogen", -10), ("태양광", -10), ("solar", -9),
    ("희토류", -6), ("rare earth", -6),   # 정책 기사엔 같이 나와 약하게만
    ("발전소", -9), ("epc", -8), ("충전", -8),
]

_ALL_SCORES = (_SCORE_CORE + _SCORE_METAL + _SCORE_POLICY +
               _SCORE_MARKET + _SCORE_COMPANY + _SCORE_PENALTY)

MIN_SCORE    = 8      # 텅스텐은 기사가 적어 BRDB(10)보다 낮춤
TARGET_TOTAL = 60


def relevance_score(article: dict) -> int:
    text  = (article.get("title", "") + " " + article.get("snippet", "")).lower()
    score = sum(pt for kw, pt in _ALL_SCORES if kw in text)

    src = (article.get("source") or "").lower()
    if "smm" in src:
        score += 6
    elif article.get("priority"):
        score += 3

    title = article.get("title", "").lower()
    if any(kw in title for kw, _ in _SCORE_CORE[:10]):
        score += 5
    return score


_STOPWORDS = {
    "the", "a", "an", "to", "in", "of", "on", "for", "and", "or", "as", "at", "by",
    "is", "are", "be", "was", "were", "it", "its", "with", "from", "that", "this",
    "has", "have", "will", "not", "new", "up", "down", "over", "out", "after",
    "안", "및", "등", "것", "수", "위", "더", "이", "그", "저", "중", "해", "때", "약",
}


# ============================================================
# 유틸
# ============================================================
def decode_entities(t):
    return html_lib.unescape(t or "")


def esc(t):
    return (str(t or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _to_naive_utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=None)


def parse_date(s):
    if not s:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return _to_naive_utc(parsedate_to_datetime(s))
    except Exception:
        pass
    try:
        return _to_naive_utc(datetime.fromisoformat(s.replace('Z', '+00:00')))
    except Exception:
        return None


def extract_real_url(url):
    if "google.com/url" in url:
        m = re.search(r'[?&]url=([^&]+)', url)
        if m:
            return urllib.parse.unquote(m.group(1))
    return url


def _sig_words(title: str) -> set:
    ws = re.sub(r'[^\w\s]', ' ', title.lower()).split()
    return {w for w in ws if len(w) >= 2 and w not in _STOPWORDS}


WINDOW_DAYS = 3          # §11 선택: 3일 윈도우


def get_cutoff_utc() -> datetime:
    base_kst = (now_kst().replace(hour=0, minute=0, second=0, microsecond=0)
                - timedelta(days=WINDOW_DAYS))
    return base_kst - timedelta(hours=9)


def _fmt(val, prefix="", nd=0) -> str:
    if val is None:
        return "—"
    return f"{prefix}{val:,.{nd}f}"


def _pct_color(pct: str) -> str:
    if not pct or pct == "N/A":
        return "#888888"
    return "#c0392b" if "+" in pct else "#2471a3"


_MON3 = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
         "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def parse_quote_date(s: str):
    if not s:
        return None
    m = re.match(r"([A-Za-z]{3,})\s+(\d{1,2}),\s*(\d{4})", s.strip())
    if not m:
        return None
    mi = _MON3.get(m.group(1)[:3].lower())
    if not mi:
        return None
    try:
        return datetime(int(m.group(3)), mi, int(m.group(2)))
    except ValueError:
        return None


def make_basis(date_str: str = None, suffix: str = "고시") -> tuple:
    d = parse_quote_date(date_str)
    if d is None:
        return ("기준일 불명", True)
    lag = (now_kst().date() - d.date()).days
    return (f"{d.month:02d}.{d.day:02d} {suffix}", lag >= 4)


def _basis_badge(text: str, stale: bool = False) -> str:
    if not text:
        return ""
    bg, fg = ("#fef3c7", "#92400e") if stale else ("#e0f2fe", "#075985")
    return (f'<span style="display:inline-block;background:{bg};color:{fg};'
            f'font-size:9px;font-weight:700;padding:1px 5px;border-radius:3px;'
            f'margin-left:4px;vertical-align:middle;white-space:nowrap;">'
            f'{esc(text)}</span>')


def get_usd_cny_rate() -> float:
    try:
        r = requests.get("https://api.frankfurter.app/latest?from=USD&to=CNY", timeout=10)
        rate = r.json()["rates"]["CNY"]
        print(f"  환율: 1 USD = {rate:.4f} CNY")
        return rate
    except Exception as e:
        print(f"  환율 조회 실패({e}) — 기본값 7.15")
        return 7.15


# ============================================================
# 시세 — 상세 페이지
# ============================================================
_MON_RE = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"

# 값 + 단위 쌍. 텅스텐은 USD/kg · USD/tonne · USD/mtu · USD/standard tonne
# · USD/tonne-degree · USD/kg tungsten 이 섞여 나온다.
_UNIT_ALT = (r"kg\s*tungsten|standard\s*tonne|tonne-degree|tonne|mtu|kg|km|t\b")
_USD_PAIR_RE = re.compile(
    rf"([\d]{{1,3}}(?:,\d{{3}})*(?:\.\d+)?)\s*\n\s*USD\s*/\s*(?:{_UNIT_ALT})",
    re.IGNORECASE)
_CNY_PAIR_RE = re.compile(
    rf"([\d]{{1,3}}(?:,\d{{3}})*(?:\.\d+)?)\s*\n\s*yuan\s*/\s*(?:{_UNIT_ALT})",
    re.IGNORECASE)


async def _scrape_detail(page, t: dict) -> dict:
    """SMM 상세 페이지 1건. 증치세 포함/제외를 둘 다 확보한다.

    포함/제외 표기 순서가 품목마다 다를 수 있어 라벨에 의존하지 않고
    두 값 중 작은 쪽을 제외가로 본다 (증치세 포함 > 제외는 항상 성립)."""
    base = {"key": t["key"], "name": t["name"], "name_en": t["name_en"],
            "unit": t["unit"], "grp": t["grp"], "w": t.get("w")}
    url = t.get("url") or SPOT_URL.format(pid=t["pid"])
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        text = await page.inner_text("body")

        usd_vals = [float(x.replace(",", "")) for x in _USD_PAIR_RE.findall(text)]
        cny_vals = [float(x.replace(",", "")) for x in _CNY_PAIR_RE.findall(text)]
        if not usd_vals:
            return {**base, "status": "ERROR: USD 파싱 실패"}

        if t.get("novat") or len(usd_vals) < 2:
            usd_excl = usd_vals[0]
        else:
            usd_excl = min(usd_vals[0], usd_vals[1])

        cny_incl = cny_vals[0] if cny_vals else None
        cny_excl = round(cny_incl / VAT_RATE) if cny_incl else None
        if t.get("novat"):
            cny_excl = cny_incl

        if not (t["lo"] < usd_excl < t["hi"]):
            return {**base, "status": f"ERROR: 범위 이탈 ({usd_excl})"}

        pcts = re.findall(r"[+\-][\d,]+\.?\d*\s*\(([+\-]?\d+\.?\d*%)\)", text)
        change = next((p for p in pcts if p != "0%"), ("0.00%" if pcts else "N/A"))

        today  = now_kst().date()
        cands  = [d for d in (parse_quote_date(s)
                              for s in re.findall(rf"({_MON_RE}\s+\d{{1,2}},\s+\d{{4}})", text))
                  if d and d.date() <= today]
        picked = max(cands) if cands else None
        date_txt = picked.strftime("%b %d, %Y") if picked else "N/A"
        basis, stale = make_basis(date_txt)

        # W 금속 환산 (USD/kg-W). 회수율 미반영 순수 함량 기준.
        usd_kg_w = None
        w = t.get("w")
        if w:
            u = t["unit"]
            if u == "USD/kg":
                usd_kg_w = usd_excl / w
            elif u in ("USD/t", "USD/std t"):
                usd_kg_w = usd_excl / 1000.0 / w
            elif u == "USD/mtu":
                usd_kg_w = (usd_excl * MTU_PER_T) / 1000.0 / w

        out = {
            **base, "url": url,
            "date": date_txt, "usd_excl": usd_excl,
            "cny_incl": cny_incl, "cny_excl": cny_excl,
            "usd_kg_w": usd_kg_w, "change_pct": change,
            "basis": basis, "delayed": stale, "status": "OK",
        }
        # 코발트는 금속 환산 라벨이 별도
        if t.get("metal_content"):
            out["usd_metal"]     = round(usd_excl / t["metal_content"])
            out["metal_label"]   = t.get("metal_label")
            out["metal_content"] = t["metal_content"]
        return out

    except Exception as e:
        return {**base, "status": f"ERROR: {e}"}


# ============================================================
# 시세 — 목록 페이지(롱테일)
# ============================================================
_RANGE_RE = re.compile(r"([\d]{1,3}(?:,\d{3})*(?:\.\d+)?)\s*[-–~]\s*"
                       r"([\d]{1,3}(?:,\d{3})*(?:\.\d+)?)")
_LIST_UNIT_RE = re.compile(rf"USD\s*/\s*({_UNIT_ALT})", re.IGNORECASE)


def _norm_line(s: str) -> str:
    """품목명 비교용 정규화. 아래첨자·부등호·공백 표기 흔들림을 흡수한다."""
    s = (s.replace("₃", "3").replace("₂", "2").replace("≥", ">=")
          .replace("–", "-").replace("—", "-").replace(" ", " "))
    return re.sub(r"\s+", " ", s).strip().lower()


def _parse_list_page(text: str) -> dict:
    """목록 페이지 inner_text → {영문품목명: {low, high, unit, date}}

    주의 — 같은 문자열이 섹션 헤더와 품목명으로 두 번 나온다.
    실제 페이지에 'Tungsten Powder' 섹션 헤더와 'Tungsten Powder' 품목이
    모두 있어서, 첫 줄만 잡으면 바로 아래 'Tungsten Carbide Powder' 의
    가격을 가져가 버린다. 그래서 같은 이름의 모든 줄을 후보로 두고,
    '가격범위가 바로 뒤(3줄 이내)에 오는' 줄만 진짜 데이터 행으로 본다.
    """
    lines = [l.strip() for l in text.split("\n")]
    norm  = [_norm_line(l) for l in lines]

    cand = {}
    for i, nl in enumerate(norm):
        if nl:
            cand.setdefault(nl, []).append(i)

    out = {}
    for _, en, _ in LIST_EXTRAS:
        key = _norm_line(en)
        best = None
        for i in cand.get(key, []):
            # 바로 뒤의 비어있지 않은 줄들만 본다
            nxt = [(j, lines[j]) for j in range(i + 1, min(i + 9, len(lines)))
                   if lines[j]]
            if not nxt:
                continue
            dist = None
            mr = None
            for rank, (j, ln) in enumerate(nxt[:3]):      # 범위는 3줄 이내
                m = _RANGE_RE.fullmatch(ln) or _RANGE_RE.match(ln)
                if m:
                    mr, dist = m, rank
                    break
            if mr is None:
                continue                                   # 섹션 헤더 → 탈락
            win = "\n".join(ln for _, ln in nxt[:6])
            mu  = _LIST_UNIT_RE.search(win)
            if mu is None:
                continue                                   # 단위 없으면 데이터 행 아님
            md = re.search(rf"({_MON_RE}\s+\d{{1,2}},\s*\d{{4}})", win)
            try:
                low  = float(mr.group(1).replace(",", ""))
                high = float(mr.group(2).replace(",", ""))
            except ValueError:
                continue
            row = {"low": low, "high": high,
                   "unit": f"USD/{mu.group(1)}", "date": md.group(1) if md else ""}
            if best is None or dist < best[0]:
                best = (dist, row)
        if best:
            out[en] = best[1]
    return out


async def _scrape_list(page) -> dict:
    try:
        await page.goto(LIST_URL, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(5000)
        text = await page.inner_text("body")
        got = _parse_list_page(text)
        print(f"  목록페이지: 롱테일 {len(got)}/{len(LIST_EXTRAS)}건")
        return got
    except Exception as e:
        print(f"  목록페이지 오류: {e}")
        return {}


CHROMIUM_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
BROWSER_UA    = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


async def scrape_prices() -> dict:
    print("\n[SMM 텅스텐 시세 수집]")
    spot, extras = [], {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        ctx     = await browser.new_context(user_agent=BROWSER_UA, locale="en-US")
        page    = await ctx.new_page()

        for t in SPOT_TARGETS + [COBALT_TARGET]:
            print(f"  {t['name']} ...", end=" ", flush=True)
            r = await _scrape_detail(page, t)
            spot.append(r)
            print("OK" if r["status"] == "OK" else f"! {r['status']}")
            await asyncio.sleep(2)

        extras = await _scrape_list(page)
        await browser.close()

    ok = sum(1 for r in spot if r["status"] == "OK")
    print(f"  헤드라인 {ok}/{len(spot)}건 수집")
    return {"spot": spot, "extras": extras}


# ============================================================
# 스프레드 — 선물이 없으니 가치사슬 스프레드로 대체
# ============================================================
def compute_spreads(price_data: dict) -> dict:
    m = {r["key"]: r for r in price_data.get("spot", []) if r.get("status") == "OK"}
    out = {}

    def v(k):
        return (m.get(k) or {}).get("usd_excl")

    # ① 지불률 — 폐 CNC 인서트 매입가 ÷ 신품 WC 분말가 (둘 다 USD/kg)
    scrap, wc = v("scrap_cnc"), v("wc_new")
    if scrap and wc:
        out["지불률"] = {
            "label": "지불률", "pct": scrap / wc * 100,
            "num": scrap, "den": wc, "unit": "USD/kg",
            "desc": "폐 CNC 인서트 ÷ 신품 WC 분말",
            "good_high": False,   # 매입 측에선 낮을수록 유리
        }

    # ② 재생 디스카운트 — 재생 WC 분말 ÷ 신품 WC 분말
    regen = v("wc_regen")
    if regen and wc:
        out["재생가치"] = {
            "label": "재생가치", "pct": regen / wc * 100,
            "num": regen, "den": wc, "unit": "USD/kg",
            "desc": "재생 WC 분말 ÷ 신품 WC 분말",
            "good_high": True,    # 판매 측에선 높을수록 유리
        }

    # ③ 중국-서방 괴리 — CIF 로테르담 APT ÷ 중국 내수 APT
    #    로테르담은 USD/mtu 이므로 x88.5 로 USD/t APT 로 맞춘다.
    rott, cn = v("apt_rott"), v("apt_cn")
    if rott and cn:
        rott_t = rott * MTU_PER_T
        out["중서괴리"] = {
            "label": "중국-서방", "pct": rott_t / cn * 100,
            "num": rott_t, "den": cn, "unit": "USD/t",
            "desc": "CIF 로테르담 APT ÷ 중국 내수 APT",
            "good_high": True,
            "note": f"로테르담 ${rott:,.0f}/mtu × {MTU_PER_T} = ${rott_t:,.0f}/t",
        }
    return out


def format_price_for_prompt(price_data: dict, usd_cny: float, spreads: dict) -> str:
    L = [f"수집 {now_kst():%H:%M} KST | USD/CNY {usd_cny:.2f} | SMM 증치세 제외 기준"]

    L.append("\n[헤드라인 시세]")
    for r in price_data.get("spot", []):
        if r.get("status") != "OK":
            L.append(f"  {r['name']}: 수집실패")
            continue
        s = f"  {r['name']}: {r['usd_excl']:,.2f} {r['unit']} | {r['change_pct']}"
        if r.get("usd_kg_w"):
            s += f" | W환산 ${r['usd_kg_w']:,.2f}/kg-W"
        if r.get("usd_metal"):
            s += f" | {r['metal_label']}환산 ${r['usd_metal']:,.0f}/t"
        s += f" | 기준 {r.get('basis', 'N/A')}"
        L.append(s)

    L.append("\n[가치사슬 스프레드]")
    for k, s in spreads.items():
        line = f"  {s['label']}: {s['pct']:.1f}%  ({s['desc']})"
        if s.get("note"):
            line += f"  ※{s['note']}"
        L.append(line)

    ex = price_data.get("extras") or {}
    if ex:
        L.append("\n[참고 — 롱테일]")
        for _, en, ko in LIST_EXTRAS:
            d = ex.get(en)
            if d:
                L.append(f"  {ko}: {d['low']:,.2f}~{d['high']:,.2f} {d['unit']}")
    return "\n".join(L)


# ============================================================
# 시세 이력
# ============================================================
def append_price_history(price_data: dict, usd_cny: float, spreads: dict) -> list:
    os.makedirs(os.path.dirname(PRICE_JSON), exist_ok=True)
    hist = []
    if os.path.exists(PRICE_JSON):
        try:
            with open(PRICE_JSON, encoding="utf-8") as f:
                hist = json.load(f)
        except Exception as e:
            print(f"  시세 이력 읽기 실패({e}) — 새로 시작")

    m = {r["key"]: r for r in price_data.get("spot", []) if r.get("status") == "OK"}

    def g(k, field="usd_excl"):
        return (m.get(k) or {}).get(field)

    row = {
        "date":       now_kst().strftime("%Y-%m-%d"),
        "usd_cny":    round(usd_cny, 4),
        "apt_cn":     g("apt_cn"),
        "apt_rott":   g("apt_rott"),
        "wc_new":     g("wc_new"),
        "wc_regen":   g("wc_regen"),
        "scrap_cnc":  g("scrap_cnc"),
        "scrap_ball": g("scrap_ball"),
        "few":        g("few"),
        "conc65":     g("conc65"),
        "co_sulph":   g("co_sulph"),
        "payable":    round(spreads["지불률"]["pct"], 2)   if "지불률"   in spreads else None,
        "regen":      round(spreads["재생가치"]["pct"], 2) if "재생가치" in spreads else None,
        "cn_west":    round(spreads["중서괴리"]["pct"], 1) if "중서괴리" in spreads else None,
    }
    hist = [h for h in hist if h.get("date") != row["date"]]
    hist.append(row)
    hist.sort(key=lambda x: x["date"])

    with open(PRICE_JSON, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=1)
    print(f"  시세 이력 저장: {len(hist)}일치")
    return hist


# ============================================================
# 실행 간 중복 제거 — 3일 윈도우라 필수
# ============================================================
def _link_key(link: str, title: str) -> str:
    base = re.sub(r"[?#].*$", "", (link or "").strip().lower())
    if not base:
        base = (title or "").strip().lower()
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def load_seen() -> dict:
    if not os.path.exists(SEEN_JSON):
        return {}
    try:
        with open(SEEN_JSON, encoding="utf-8") as f:
            seen = json.load(f)
    except Exception:
        return {}
    cut = (now_kst() - timedelta(days=SEEN_KEEP_DAYS)).strftime("%Y-%m-%d")
    return {k: v for k, v in seen.items() if v >= cut}


def save_seen(seen: dict, articles: list):
    today = now_kst().strftime("%Y-%m-%d")
    for a in articles:
        seen[_link_key(a.get("link", ""), a.get("title", ""))] = today
    os.makedirs(os.path.dirname(SEEN_JSON), exist_ok=True)
    with open(SEEN_JSON, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=0, sort_keys=True)
    print(f"  중복사전 저장: {len(seen)}건 (보관 {SEEN_KEEP_DAYS}일)")


# ============================================================
# 기사 수집
# ============================================================
_CROSS_LANG_COMPANIES = [
    "almonty", "sangdong", "kennametal", "ceratizit", "sandvik", "plansee",
    "starck", "xiamen tungsten", "masan", "nui phao", "mitsubishi",
    "sumitomo", "대구텍", "taegutec", "korloy",
]


def _pre_cluster_articles(articles: list) -> list:
    kept = []

    def nums(t):
        return set(re.findall(r'\d+\.?\d*%|\$[\d,]+[BMK]?|\d{2,}', t))

    for a in articles:
        wa, na = _sig_words(a["title"]), nums(a["title"])
        la = a["title"].lower()
        len_a = len(a.get("body", "") or a.get("snippet", ""))
        merged = False
        for i, b in enumerate(kept):
            lb = b["title"].lower()
            len_b = len(b.get("body", "") or b.get("snippet", ""))
            if b.get("lang") == a.get("lang") and len(wa & _sig_words(b["title"])) >= 5:
                if len_a > len_b:
                    kept[i] = a
                merged = True
                break
            nb = nums(b["title"])
            if na and nb and len(na & nb) >= 2:
                if any(c in la and c in lb for c in _CROSS_LANG_COMPANIES):
                    if len_a > len_b:
                        kept[i] = a
                    merged = True
                    break
        if not merged:
            kept.append(a)

    if len(articles) - len(kept) > 0:
        print(f"  pre-clustering: {len(articles) - len(kept)}건 통합 → {len(kept)}건")
    return kept


def _parse_feed(url, source_fixed=None, lang="en", is_special=False,
                cutoff=None, seen_links=None, max_items=50) -> list:
    out = []
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": BROWSER_UA})
        if resp.status_code != 200:
            print(f"  [{source_fixed or url[:40]}] HTTP {resp.status_code}")
            return out

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as pe:
            raw   = resp.content.decode("utf-8", errors="ignore")
            items = re.findall(r"<item[\s>].*?</item>", raw, re.DOTALL)
            if not items:
                print(f"  [{source_fixed or url[:40]}] XML 파싱 실패: {pe}")
                return out
            try:
                root = ET.fromstring(("<rss><channel>" + "".join(items)
                                      + "</channel></rss>").encode("utf-8"))
                print(f"  [{source_fixed or url[:40]}] XML 복구 ({len(items)}개)")
            except ET.ParseError:
                return out

        for entry in root.findall(".//item")[:max_items]:
            title = decode_entities((entry.findtext("title") or "").strip())
            link  = extract_real_url((entry.findtext("link") or "").strip())
            if not title or not link or (seen_links is not None and link in seen_links):
                continue

            pub = parse_date((entry.findtext("pubDate") or "").strip())
            if not pub or (cutoff and pub < cutoff):
                continue

            snippet = decode_entities(
                re.sub(r'<[^>]+>', '', entry.findtext("description") or ""))[:400]

            if source_fixed:
                source = source_fixed
            else:
                el = entry.find("source")
                source = el.text.strip() if el is not None and el.text else ""

            lt, ls, ll = title.lower(), source.lower(), link.lower()
            combined = (title + " " + snippet).lower()

            if any(k in lt for k in NOISE_KEYWORDS):             continue
            if any(x in lt and y in lt for x, y in NOISE_PAIRS): continue
            if is_stock_noise(title):                            continue
            if not is_special:
                if any(s in ls or s in ll for s in NOISE_SOURCES): continue
                if any(p in ll for p in NOISE_URL_PATHS):          continue
            if not any(w in combined for w in WHITELIST):          continue

            if seen_links is not None:
                seen_links.add(link)

            out.append({
                "title": title, "link": link, "source": source,
                "pub": (entry.findtext("pubDate") or "").strip(),
                "pub_date": pub, "lang": lang, "snippet": snippet,
                "priority": is_special,
            })
    except Exception as e:
        print(f"  [{source_fixed or url[:40]}] 오류: {e}")
    return out


def _fetch_smm_rss(cutoff=None, existing_titles=None, max_fetch=8) -> list:
    """SMM RSS. <metal> 태그로 텅스텐 관련만 고른다."""
    RELEVANT = {"tungsten", "minor metals", "others"}
    KW = {"tungsten", "apt", "paratungstate", "carbide", "ferrotungsten",
          "scheelite", "wolframite", "tungstate", "hard metal", "cemented",
          "cobalt", "wolfram"}
    existing_titles = existing_titles or []

    def pd(s):
        try:
            return datetime.strptime(s.strip(), "%H:%M:%S %b %d, %Y")
        except Exception:
            return None

    def dup(t):
        wn = _sig_words(t)
        return any(len(wn & _sig_words(x)) >= 4 for x in existing_titles)

    try:
        r = requests.get(SMM_RSS_URL, timeout=15, headers={"User-Agent": BROWSER_UA})
        if r.status_code != 200:
            print(f"  SMM RSS: HTTP {r.status_code}")
            return []
        root = ET.fromstring(r.content)
        arts, sk_m, sk_d, sk_u = [], 0, 0, 0

        for item in root.findall(".//item"):
            el = item.find("metal")
            metals = {m.strip().lower()
                      for m in ((el.text or "") if el is not None else "").split(",")}
            if not (metals & RELEVANT):
                sk_m += 1
                continue

            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link") or "").strip()
            if not title or not link:
                continue

            pub = pd(item.findtext("pubDate") or "")
            if pub is None:
                sk_d += 1
                continue
            if cutoff and pub < (cutoff - timedelta(hours=8)):
                sk_d += 1
                continue

            snippet = (item.findtext("description") or "").strip()[:400]
            if not any(k in (title + " " + snippet).lower() for k in KW):
                sk_m += 1
                continue
            if dup(title):
                sk_u += 1
                continue

            arts.append({"title": title, "link": link, "snippet": snippet,
                         "source": "SMM Metal", "priority": True,
                         "pub_date": pub, "pub": pub.strftime("%Y-%m-%d"),
                         "lang": "en"})
            existing_titles.append(title)
            if len(arts) >= max_fetch:
                break

        print(f"  SMM RSS: {len(arts)}건 (제외 태그{sk_m}/날짜{sk_d}/중복{sk_u})")
        return arts
    except Exception as e:
        print(f"  SMM RSS 오류: {e}")
        return []


def collect_rss(seen: dict) -> list:
    cutoff = get_cutoff_utc()
    print(f"  [날짜 필터] {(cutoff + timedelta(hours=9)):%Y-%m-%d %H:%M} KST 이후 "
          f"({WINDOW_DAYS}일 윈도우)")

    feeds = load_opml()
    raw, seen_links = [], set()

    pubs = [f for f in feeds if not f["google"]]
    goos = [f for f in feeds if f["google"]]

    print("\n[매체 피드]")
    for f in pubs:
        got = _parse_feed(f["url"], f["source"], "ko" if re.search(r"[\uac00-\ud7a3]", f["source"]) else "en",
                          is_special=f["special"], cutoff=cutoff, seen_links=seen_links)
        print(f"  {f['source']}: {len(got)}건")
        raw += got
        time.sleep(0.2)

    print("\n[구글 뉴스]")
    total = 0
    for f in goos:
        norm = normalize_google_feed(f["url"], window_days=WINDOW_DAYS + 1)
        if not norm:
            print(f"  ! 스킵(쿼리 비어있음): {f['source']}")
            continue
        url, lang = norm
        got = _parse_feed(url, None, lang, is_special=False,
                          cutoff=cutoff, seen_links=seen_links, max_items=50)
        total += len(got)
        raw += got
        time.sleep(0.15)
    print(f"  구글 합계: {total}건")

    raw.sort(key=lambda x: x.get("pub_date") or datetime.min, reverse=True)

    # 제목 유사 중복
    deduped = []
    for a in raw:
        w = _sig_words(a["title"])
        if any(len(w & _sig_words(b["title"])) >= 5 for b in deduped):
            continue
        deduped.append(a)

    deduped += [a for a in _fetch_smm_rss(cutoff, [x["title"] for x in deduped])
                if not any(a["link"] == x.get("link") for x in deduped)]
    deduped = _pre_cluster_articles(deduped)

    # 지난 실행에서 이미 내보낸 기사 제거 (3일 윈도우 필수)
    before = len(deduped)
    deduped = [a for a in deduped
               if _link_key(a.get("link", ""), a.get("title", "")) not in seen]
    print(f"\n  기발송 제외: {before - len(deduped)}건 → 신규 {len(deduped)}건")

    dts = [a["pub_date"] for a in deduped if a.get("pub_date")]
    rng = (f"{(min(dts) + timedelta(hours=9)):%m-%d %H:%M} ~ "
           f"{(max(dts) + timedelta(hours=9)):%m-%d %H:%M} KST") if dts else "날짜 없음"
    print(f"  수집 {len(raw)}건 → 최종 {len(deduped)}건 | 범위 {rng}")
    return deduped


# ============================================================
# 본문 추출
# ============================================================
def fetch_body(url):
    try:
        r = requests.get(f"https://r.jina.ai/{url}", timeout=(10, 45),
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            return r.text[:3000]
    except Exception as e:
        print(f"    Jina 오류: {e}")
    return ""


async def get_real_url(page, cbm_url):
    try:
        await page.goto(cbm_url, wait_until="commit", timeout=15000)
    except Exception:
        pass
    try:
        await page.wait_for_url(lambda u: "news.google.com" not in u, timeout=10000)
    except Exception:
        pass
    return page.url if "news.google.com" not in page.url else None


async def enrich_articles(articles):
    for a in articles:
        a["score"] = relevance_score(a)

    smm = sorted([a for a in articles
                  if "SMM" in (a.get("source") or "") and a["score"] >= MIN_SCORE + 4],
                 key=lambda x: -x["score"])[:4]
    smm_ids = {id(x) for x in smm}

    pool = [a for a in articles if id(a) not in smm_ids and a["score"] >= MIN_SCORE]
    pool.sort(key=lambda x: (-x["score"],
                             -(x.get("pub_date") or datetime.min).timestamp()))
    general = pool[:max(0, TARGET_TOTAL - len(smm))]
    targets = smm + general

    print(f"\n본문추출: SMM{len(smm)} + 선별{len(general)} = {len(targets)}건 "
          f"({MIN_SCORE}점 이상 {len(pool)}건)")
    if general:
        print(f"  점수 범위: {general[0]['score']} ~ {general[-1]['score']}")

    browser = None
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
            page = await browser.new_page()
            await page.set_extra_http_headers({"User-Agent": BROWSER_UA})

            ok = sn = 0
            for i, a in enumerate(targets):
                print(f"[{i+1}/{len(targets)}] ({a.get('score', 0):>3}점) {a['title'][:52]}")
                link = a["link"]
                if "news.google.com" not in link:
                    body = fetch_body(link)
                    if body:
                        a["body"] = body; ok += 1; print(f"    OK ({len(body)}자)")
                    else:
                        sn += 1; print("    ~ 스니펫")
                    continue
                real = await get_real_url(page, link)
                if real:
                    a["real_url"] = real
                    a["body"] = fetch_body(real)
                    if a["body"]:
                        ok += 1; print(f"    OK {real[:55]}")
                    else:
                        sn += 1; print("    ~ 본문없음")
                else:
                    sn += 1; print("    ~ 리다이렉트 실패")
            print(f"\n본문결과: 성공{ok} / 스니펫{sn}")
        finally:
            if browser:
                await browser.close()
    return targets


# ============================================================
# Gemini
# ============================================================
TAG_ORDER = ["원재료 및 시황", "공급망 및 파트너십", "투자 및 M&A",
             "정책 및 규제", "기술 및 공정"]


def analyze(articles, price_data=None, usd_cny=7.15):
    today = now_kst().strftime("%Y년 %m월 %d일")

    smm_a = [a for a in articles if "SMM" in (a.get("source") or "")][:3]
    gen_a = [a for a in articles if "SMM" not in (a.get("source") or "")]

    def fmt(i, a):
        du = a.get("real_url") or a["link"]
        s = (f"{i+1}. [{a['lang'].upper()}] {a['title']}\n"
             f"   출처:{a.get('source', '불명')} | 날짜:{a.get('pub', '')} | {du}")
        b = a.get("body") or a.get("snippet") or ""
        if b:
            s += f"\n   [본문]: {b[:1500]}"
        return s

    smm_sec = "\n\n".join(fmt(i, a) for i, a in enumerate(smm_a))
    gen_sec = "\n\n".join(fmt(i, a) for i, a in enumerate(gen_a))

    if price_data:
        spreads = compute_spreads(price_data)
        price_sec = ("\n[당일 시세]\n"
                     + format_price_for_prompt(price_data, usd_cny, spreads) + "\n")
        price_guide = """
[시사점 5~7개 — 경영진이 읽는다는 전제로 작성]
① 시세 종합 (1개, 필수): 지불률·재생가치·중국-서방 괴리 3종 스프레드를
   묶어 원료 매입 / 제품 판매 전략 시사점. 반드시 구체적 수치 인용.
② 기사 기반 (3~4개, 필수): 오늘 뉴스에서 직접 도출. 정책·규제 /
   경쟁사 / 원료 확보 / 공급망 중 실제 움직임이 있었던 것만.
③ 신규 진입 관점 (1개, 가능할 때): 텅스텐 리사이클링 신규 진입자
   입장에서 오늘 뉴스가 진입 타이밍·설비 투자·원료 확보에 갖는 의미.
   억지로 쓰지 말 것.
④ 단기 모멘텀 (1개, 필수, 마지막): 향후 1~2주 전망 한 문장.

[톤] 경영진 수신. 각 시사점은 '사실 → 그래서 우리에게 무엇'의 두 부분.
업계 약어는 처음 나올 때만 한 번 풀어쓸 것(APT, WC 등).
[금지] 시세 시사점 2개 이상. 근거 없는 추측. 매일 반복되는 일반론.
"""
    else:
        price_sec = price_guide = ""

    prompt = f"""당신은 텅스텐·초경 리사이클링 산업 전문 시니어 애널리스트입니다. JSON만 출력하세요.

오늘: {today}
{price_sec}
[SMM 기사 최우선]
{smm_sec if smm_sec else "없음"}

[일반 뉴스]
{gen_sec}

[사업 맥락 — 선별 기준]
텅스텐 리사이클링 신규 사업을 준비 중인 국내 사업자 관점이다.
- 공정: 아연 재생법 · 소다용융/알칼리 침출 후 습식 APT · 산화-환원 세 가지를
  모두 검토 중. 따라서 어느 공법이든 기술·설비 뉴스는 중요하다.
- 투입물: 폐 CNC 인서트·블레이드, 광산/굴착용 드릴비트·볼치,
  연삭 슬러지·분진, 롤러링·앤빌·금형 등 대형 초경재 전부.
- 산출물: 재생 WC(탄화텅스텐) 분말, APT, 산화텅스텐·W 금속분말,
  페로텅스텐 전부.
- 매입처: 공구 재연마 업체, 기계가공·제조사 자가발생분,
  광산·건설 굴착 업체, 스크랩 딜러·트레이더.
- 판매처: 초경공구 제조사, 분말야금·소재 업체, 상사·트레이더,
  그리고 중국·일본·유럽 수출. → 각국 수출입 규제가 직접 영향.
- 동반 금속: 초경 바인더인 코발트를 공동 회수한다. Co 시황도 관련 있음.
- 거점: 신규 사업이라 아직 미확정. 따라서 진입장벽, 설비 투자 규모,
  인허가, 원료 확보 경쟁에 관한 뉴스를 특히 무겁게 볼 것.

★ '재활용'이라는 단어가 없어도 아래는 핵심 기사로 취급할 것:
  · 중국의 텅스텐 수출통제·허가제 운용 변화 (지금 이 시장 최대 변수)
  · EU CRMA·미국 방산 비축 등 서방의 공급망 정책
  · 절삭공구사·기계가공 업계의 가동률·증설·감산 (스크랩 발생량 직결)
  · 광산 신규 생산 (알몬티 상동광산 등) — 1차 원료가 재생 원료와 경쟁
  · 경쟁 리사이클러의 증설·M&A·신공법

[필수 선별 규칙]
- 오늘 실제로 중요한 기사만 선택. 5건이면 5건, 30건이면 30건. (최대 30건)
  분야별 균등 배분 강제 금지. 건수를 채우려 관련도 낮은 기사를 넣지 말 것.
- 텅스텐·초경 밸류체인과 무관하면 제외.
- ★ 텅스텐 반지·다트·낚시추·밈 상품·백열전구 기사는 절대 넣지 말 것.
- ★ 배터리(리튬·니켈·양극재·폐배터리) 기사는 넣지 말 것.
  단 코발트가 초경 바인더 맥락으로 나오면 허용.
- 태그 판별 (반드시 하나만):
  · 원재료 및 시황 — 가격·수급·재고·생산량 변동이 기사의 핵심일 때
  · 정책 및 규제 — 법령·수출입 통제·비축·인허가가 핵심
  · 공급망 및 파트너십 — 계약·MOU·JV·공급 개시·수주·offtake
  · 투자 및 M&A — 지분 인수·자금 조달·증설 투자 결정
  · 기술 및 공정 — 신공법·수율·설비·소재 개발
  ※ 수주·계약은 M&A가 아니라 공급망. 자금 조달은 정책이 아니라 투자.
  ※ 어느 태그에도 명확히 속하지 않으면 그 기사는 선택하지 말 것.
     '정책 및 규제'를 애매한 기사의 기본값으로 쓰지 말 것.
- 금지: 증권리포트/주가/IR공시/ETF/신차리뷰/PR배포/스마트폰/쇼핑 가이드
- 금지: 본문에 언급된 발표일이 14일 이상 지난 기사

★ [유사 기사 통합]
- 동일 이슈가 2건 이상이면 가장 정보가 풍부한 1건만.
  summary에 "○○·△△ 등 복수 보도"로 통합 언급.
- 한국어판과 영어판은 관점이 다르면 각 1건 허용.

[요약기준] 3문장 이내. 기관명·기업명·금액·수치·날짜 필수. 추상적 요약 금지.
계획≠실행, MOU≠계약, 검토≠확정.

[트렌드 2~3개] 오늘 기사에서 실제로 흐름이 보인 것만. 억지로 채우지 말 것.
기사 수치·정책명 직접 인용.
{price_guide}
JSON:
{{"articles":[{{"title":"","source":"","date":"","link":"","summary":"3문장이내 수치포함","tag":"원재료 및 시황|투자 및 M&A|정책 및 규제|공급망 및 파트너십|기술 및 공정","region":"한국|중국|미국|EU|일본|베트남|글로벌"}}],"trends":[{{"title":"","body":"2~3문장"}}],"insights":[""]}}

articles 최대 30건. trends 2~3개. insights 5~7개. 모든 텍스트 한국어."""

    model = genai.GenerativeModel("gemini-2.5-flash")
    cfg   = genai.GenerationConfig(response_mime_type="application/json", temperature=0.2)

    for attempt in range(3):
        try:
            if attempt:
                time.sleep(6)
            resp = model.generate_content(prompt, generation_config=cfg)
            try:
                return json.loads(resp.text)
            except json.JSONDecodeError:
                return json.loads(re.sub(r"^```json\s*|\s*```$", "", resp.text.strip()))
        except Exception as ex:
            print(f"Gemini 오류({attempt+1}/3): {ex}")
            if attempt < 2:
                w = 30 * (attempt + 1)
                print(f"{w}초 대기...")
                time.sleep(w)
    raise Exception("Gemini 3회 실패")


# ============================================================
# 시세 표 (웹·메일 공용 조각)
# ============================================================
def _spread_badges(spreads: dict) -> str:
    out = ""
    for k in ("지불률", "재생가치", "중서괴리"):
        s = spreads.get(k)
        if not s:
            continue
        if k == "중서괴리":
            txt, clr = f"중국-서방 {s['pct']/100:.1f}배", "#b91c1c"
        else:
            good = (s["pct"] >= 80) if s["good_high"] else (s["pct"] <= 80)
            clr  = "#2563eb" if good else "#c0392b"
            txt  = f"{s['label']} {s['pct']:.1f}%"
        out += (f'<span style="display:inline-block;background:{clr};color:#fff;'
                f'font-size:11px;font-weight:700;padding:4px 10px;'
                f'border-radius:4px;margin:0 6px 4px 0;">{txt}</span>')
    return out


FAIL_TAG = ('<span style="display:inline-block;background:#fee2e2;color:#b91c1c;'
            'font-size:9px;font-weight:700;padding:1px 5px;border-radius:3px;'
            'margin-left:3px;vertical-align:middle;">수집실패</span>')


def _price_rows(price_data: dict) -> str:
    rows, cur = "", None
    for r in price_data.get("spot", []):
        if r.get("grp") != cur:
            cur = r.get("grp")
            rows += (f'<tr><td colspan="4" style="padding:7px 14px;background:#f1f5f9;'
                     f'font-size:11px;font-weight:700;color:#64748b;">{esc(cur)}</td></tr>')

        ok  = r.get("status") == "OK"
        tag = _basis_badge(r.get("basis"), r.get("delayed")) if ok else FAIL_TAG
        pct = r.get("change_pct", "N/A") if ok else "N/A"

        if ok:
            nd    = 2 if r["unit"] in ("USD/kg", "USD/mtu") else 0
            price = (f'<b style="font-size:13px;">{_fmt(r["usd_excl"], "$", nd)}</b>'
                     f'<br><span style="color:#94a3b8;font-size:10px;">{esc(r["unit"])}</span>')
            if r.get("usd_kg_w"):
                conv = (f'<b style="font-size:12px;">${r["usd_kg_w"]:,.2f}</b>'
                        f'<br><span style="color:#94a3b8;font-size:10px;">/kg-W</span>')
            elif r.get("usd_metal"):
                conv = (f'<b style="font-size:12px;">{_fmt(r["usd_metal"], "$")}</b>'
                        f'<br><span style="color:#94a3b8;font-size:10px;">'
                        f'{esc(r.get("metal_label"))} 환산</span>')
            else:
                conv = '<span style="color:#d1d5db;">—</span>'
        else:
            price = '<span style="color:#aaaaaa;">—</span>'
            conv  = '<span style="color:#d1d5db;">—</span>'

        rows += f"""
        <tr>
          <td style="padding:11px 14px;border-bottom:1px solid #e8edf2;">
            <b style="font-size:13px;color:#0f2744;">{esc(r.get('name'))}</b>{tag}<br>
            <span style="font-size:11px;color:#8f9ba8;">{esc(r.get('name_en', ''))}</span></td>
          <td style="padding:11px 14px;border-bottom:1px solid #e8edf2;text-align:right;">{price}</td>
          <td style="padding:11px 14px;border-bottom:1px solid #e8edf2;text-align:right;">{conv}</td>
          <td style="padding:11px 14px;border-bottom:1px solid #e8edf2;text-align:center;
                     font-weight:700;font-size:13px;color:{_pct_color(pct)};">{pct or '—'}</td>
        </tr>"""
    return rows


def _spread_table(spreads: dict) -> str:
    if not spreads:
        return ""
    rows = ""
    for k in ("지불률", "재생가치", "중서괴리"):
        s = spreads.get(k)
        if not s:
            continue
        val = f"{s['pct']/100:.2f}배" if k == "중서괴리" else f"{s['pct']:.1f}%"
        note = f"<br><span style='color:#94a3b8;font-size:10px;'>{esc(s['note'])}</span>" \
               if s.get("note") else ""
        rows += f"""
        <tr>
          <td style="padding:9px 14px;border-bottom:1px solid #e8edf2;font-size:12px;">
            <b>{esc(s['label'])}</b><br>
            <span style="color:#8f9ba8;font-size:11px;">{esc(s['desc'])}</span>{note}</td>
          <td style="padding:9px 14px;border-bottom:1px solid #e8edf2;text-align:right;
                     font-size:15px;font-weight:700;color:#0f2744;">{val}</td>
        </tr>"""
    return f"""
    <table width="100%" cellpadding="0" cellspacing="0" border="0"
           style="background:#fff;border:1px solid #e2e8f0;border-collapse:collapse;margin-top:14px;">
      <thead><tr style="background:#f8fafc;">
        <td colspan="2" style="padding:9px 14px;font-size:11px;font-weight:700;color:#475569;
            border-bottom:2px solid #e2e8f0;">가치사슬 스프레드 · 텅스텐은 선물이 없어 이걸로 대체</td>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


# ============================================================
# 웹 페이지
# ============================================================
_WEB_CSS = """
  body { margin:0;background:#f8fafc;font-family:'Apple SD Gothic Neo',
         'Malgun Gothic',Arial,sans-serif;color:#0f2744;-webkit-text-size-adjust:100%; }
  .wrap { max-width:760px;margin:0 auto;padding:0 16px 60px; }
  details summary::-webkit-details-marker { display:none; }
  details summary { list-style:none; }
  a:hover { text-decoration:underline !important; }
  h2.sec { font-size:16px;margin:34px 0 10px;font-weight:700; }
  table.px { width:100%;border-collapse:collapse;background:#fff;border:1px solid #e2e8f0; }
  @media (max-width:600px) { .wrap { padding:0 12px 40px; } }
"""

_WEATHER_JS = """
const spots = __SPOTS__;
const CODE = {0:"맑음",1:"대체로 맑음",2:"구름 조금",3:"흐림",
  45:"안개",48:"안개",51:"이슬비",53:"이슬비",55:"이슬비",
  56:"어는 이슬비",57:"어는 이슬비",61:"약한 비",63:"비",65:"강한 비",
  66:"어는 비",67:"어는 비",71:"약한 눈",73:"눈",75:"강한 눈",77:"싸락눈",
  80:"소나기",81:"소나기",82:"강한 소나기",85:"눈소나기",86:"눈소나기",
  95:"뇌우",96:"뇌우",99:"뇌우"};
Promise.all(spots.map(s =>
  fetch("https://api.open-meteo.com/v1/forecast?latitude=" + s.lat
      + "&longitude=" + s.lon
      + "&current=temperature_2m,weather_code"
      + "&daily=temperature_2m_max,temperature_2m_min"
      + "&timezone=Asia%2FSeoul&forecast_days=1")
    .then(r => r.json())
    .then(d => {
      const c = d.current, dy = d.daily;
      return '<span><b style="color:#fff;">' + s.name + '</b> '
           + (CODE[c.weather_code] || "") + " "
           + Math.round(c.temperature_2m) + "\\u00B0 "
           + '<span style="color:#94a3b8;">'
           + Math.round(dy.temperature_2m_min[0]) + "/"
           + Math.round(dy.temperature_2m_max[0]) + "\\u00B0</span></span>";
    })
    .catch(() => '<span>' + s.name + ' —</span>')
)).then(h => { document.getElementById('wx').innerHTML = h.join(''); });
"""

_CHART_JS = """
(function() {
  const D = __CD__;
  const solid = D.labels.length > 20 ? 0 : 3;
  const ds = (label, data, color, dash) => ({
    label, data, borderColor: color, backgroundColor: color,
    borderDash: dash ? [5, 4] : [], tension: .25,
    borderWidth: 2, pointRadius: solid, spanGaps: false
  });
  const opts = (fmt) => ({
    responsive: true, maintainAspectRatio: false,
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { labels: { boxWidth: 12, font: { size: 11 } } },
      tooltip: { callbacks: { label: c => c.dataset.label + ': '
        + (c.parsed.y == null ? '—' : fmt(c.parsed.y)) } }
    },
    scales: {
      y: { ticks: { font: { size: 10 }, callback: v => fmt(v) } },
      x: { ticks: { maxRotation: 0, autoSkipPadding: 20, font: { size: 10 } } }
    }
  });
  const money = v => '$' + (v >= 1000 ? (v/1000).toFixed(0) + 'k' : v.toFixed(0));
  const pct   = v => v.toFixed(0) + '%';
  const draw = (id, sets, fmt) => {
    const el = document.getElementById(id);
    if (!el) return;
    new Chart(el, { type:'line', data:{ labels: D.labels, datasets: sets },
                    options: opts(fmt) });
  };
  draw('ch_sp', [
    ds('지불률 (스크랩÷신품)',   D.payable, '#b45309', false),
    ds('재생가치 (재생÷신품)',   D.regen,   '#059669', false)
  ], pct);
  draw('ch_wc', [
    ds('신품 WC 분말', D.wc_new,    '#1d4ed8', false),
    ds('재생 WC 분말', D.wc_regen,  '#93c5fd', true),
    ds('폐 CNC 인서트', D.scrap_cnc, '#f59e0b', false)
  ], v => '$' + v.toFixed(0));
  draw('ch_apt', [
    ds('중국 내수 APT',  D.apt_cn, '#7c3aed', false),
    ds('페로텅스텐',     D.few,    '#c4b5fd', true),
    ds('정광 65%',       D.conc65, '#a78bfa', false)
  ], money);
})();
"""

_CSV_JS = """
function dlCsv() {
  const H = ['날짜','중국APT_USDt','로테르담APT_USDmtu','신품WC_USDkg','재생WC_USDkg',
             '폐CNC_USDkg','폐볼치_USDkg','FeW_USDt','정광65_USDt','황산코발트_USDt',
             '지불률_%','재생가치_%','중서괴리_%','USD/CNY'];
  const K = ['date','apt_cn','apt_rott','wc_new','wc_regen','scrap_cnc','scrap_ball',
             'few','conc65','co_sulph','payable','regen','cn_west','usd_cny'];
  fetch('__DATA__').then(r => r.json()).then(rows => {
    const csv = '\\uFEFF' + [H.join(',')]
      .concat(rows.map(r => K.map(k => (r[k] == null ? '' : r[k])).join(','))).join('\\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([csv], {type:'text/csv;charset=utf-8'}));
    a.download = 'trdb_prices.csv';
    a.click();
  }).catch(() => alert('시세 데이터를 불러오지 못했습니다.'));
}
"""


def build_price_web(price_data, usd_cny, hist, data_path="data/prices.json") -> str:
    if not price_data:
        return ""
    today   = now_kst().strftime("%Y.%m.%d")
    spreads = compute_spreads(price_data)

    extras = price_data.get("extras") or {}
    ex_rows = ""
    cur = None
    for grp, en, ko in LIST_EXTRAS:
        d = extras.get(en)
        if not d:
            continue
        if grp != cur:
            cur = grp
            ex_rows += (f'<tr><td colspan="3" style="padding:6px 14px;background:#f1f5f9;'
                        f'font-size:11px;font-weight:700;color:#64748b;">{esc(grp)}</td></tr>')
        ex_rows += f"""
        <tr>
          <td style="padding:8px 14px;border-bottom:1px solid #eef2f6;font-size:12px;">
            {esc(ko)}<br><span style="color:#94a3b8;font-size:10px;">{esc(en)}</span></td>
          <td style="padding:8px 14px;border-bottom:1px solid #eef2f6;text-align:right;
                     font-size:12px;">{d['low']:,.2f} ~ {d['high']:,.2f}</td>
          <td style="padding:8px 14px;border-bottom:1px solid #eef2f6;text-align:right;
                     font-size:11px;color:#94a3b8;">{esc(d['unit'])}</td>
        </tr>"""

    extras_block = f"""
      <details style="margin-top:12px;">
        <summary style="cursor:pointer;font-size:13px;color:#2563eb;font-weight:600;padding:8px 0;">
          ▽ 나머지 품목 {sum(1 for _, en, _ in LIST_EXTRAS if en in extras)}건 보기</summary>
        <table class="px" style="margin-top:8px;"><tbody>{ex_rows}</tbody></table>
      </details>""" if ex_rows else ""

    recent = (hist or [])[-30:]
    n = len(recent)
    CD = {"labels": [h["date"][5:] for h in recent]}
    for k in ("payable", "regen", "wc_new", "wc_regen", "scrap_cnc",
              "apt_cn", "few", "conc65"):
        CD[k] = [h.get(k) for h in recent]

    def canvas(cid, title, sub):
        return f"""
      <div style="background:#fff;border:1px solid #e2e8f0;border-radius:8px;
                  padding:16px 18px;margin-bottom:14px;">
        <div style="font-size:13px;font-weight:700;">{title}</div>
        <div style="font-size:11px;color:#94a3b8;margin-bottom:10px;">{sub}</div>
        <div style="position:relative;height:230px;"><canvas id="{cid}"></canvas></div>
      </div>"""

    note = ("데이터가 하루치입니다. 며칠 누적되면 추세가 보입니다."
            if n < 3 else f"최근 {n}일 · SMM 증치세 제외 기준")

    charts = f"""
      <div style="font-size:13px;font-weight:700;margin:24px 0 4px;">추이</div>
      <div style="font-size:11px;color:#94a3b8;margin-bottom:12px;">{note}</div>
      {canvas("ch_sp",  "가치사슬 스프레드", "지불률 · 재생가치 (신품 WC 분말 대비 %)")}
      {canvas("ch_wc",  "분말·스크랩",       "USD/kg")}
      {canvas("ch_apt", "APT·합금·원광",     "USD/t")}
      <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
      <script>{_CHART_JS.replace("__CD__", json.dumps(CD, ensure_ascii=False))}</script>"""

    return f"""
  <h2 class="sec">SMM 텅스텐 시세</h2>
  <div style="display:flex;justify-content:space-between;align-items:center;
              flex-wrap:wrap;gap:8px;margin-bottom:10px;">
    <span style="font-size:12px;color:#64748b;">
      {today} · 증치세 제외 · 품목 옆 태그가 시세 기준일 · W 환산은 회수율 미반영 이론치</span>
    <button onclick="dlCsv()" style="font-size:11px;padding:5px 12px;border:1px solid #cbd5e1;
            border-radius:5px;background:#fff;cursor:pointer;color:#475569;">CSV 받기</button>
  </div>
  <div style="margin-bottom:12px;">{_spread_badges(spreads) or '&nbsp;'}</div>
  <table class="px">
    <thead><tr style="background:#f8fafc;">
      <td style="padding:10px 14px;font-size:11px;font-weight:700;color:#475569;
                 border-bottom:2px solid #e2e8f0;">품목</td>
      <td style="padding:10px 14px;font-size:11px;font-weight:700;color:#475569;
                 text-align:right;border-bottom:2px solid #e2e8f0;">고시가</td>
      <td style="padding:10px 14px;font-size:11px;font-weight:700;color:#475569;
                 text-align:right;border-bottom:2px solid #e2e8f0;">W 환산</td>
      <td style="padding:10px 14px;font-size:11px;font-weight:700;color:#475569;
                 text-align:center;border-bottom:2px solid #e2e8f0;">등락</td>
    </tr></thead>
    <tbody>{_price_rows(price_data)}</tbody>
  </table>
  {_spread_table(spreads)}
  {extras_block}
  {charts}
  <script>{_CSV_JS.replace("__DATA__", data_path)}</script>"""


def build_web(data, price_data=None, usd_cny=7.15, hist=None, in_archive=False):
    today        = now_kst().strftime("%Y년 %m월 %d일")
    archive_href = "index.html" if in_archive else "archive/index.html"
    data_path    = "../data/prices.json" if in_archive else "data/prices.json"
    price_web    = build_price_web(price_data, usd_cny, hist or [], data_path)

    by_tag = {}
    for a in data.get("articles", []):
        by_tag.setdefault(a.get("tag", "기타"), []).append(a)

    def card(a, compact=False):
        url = esc(a.get("real_url") or a.get("link", ""))
        pad = "16px 18px" if compact else "20px"
        fs  = "15px" if compact else "16px"
        return f"""
        <div style="border:1px solid #e2e8f0;border-radius:8px;
                    padding:{pad};margin-bottom:10px;background:#fff;">
          <div style="font-size:11px;color:#94a3b8;margin-bottom:6px;">
            <span style="background:#dcfce7;color:#15803d;padding:2px 8px;
                         border-radius:4px;font-weight:700;">{esc(a.get('region', ''))}</span>
            &nbsp;{esc(a.get('source', ''))} · {esc(a.get('date', ''))}</div>
          <a href="{url}" target="_blank" rel="noopener"
             style="font-size:{fs};font-weight:700;color:#0f2744;
                    text-decoration:none;line-height:1.4;">{esc(a.get('title', ''))}</a>
          <p style="font-size:14px;color:#475569;line-height:1.65;margin:8px 0 0;">
            {esc(a.get('summary', ''))}</p>
        </div>"""

    sections = ""
    for tag in TAG_ORDER:
        arts = by_tag.get(tag)
        if not arts:
            continue
        head, rest = arts[:3], arts[3:]
        rest_html = ""
        if rest:
            rest_html = f"""
            <details style="margin-top:4px;">
              <summary style="cursor:pointer;font-size:13px;color:#2563eb;
                              font-weight:600;padding:8px 0;">
                ▽ 나머지 {len(rest)}건 보기</summary>
              <div style="margin-top:10px;">{''.join(card(a, True) for a in rest)}</div>
            </details>"""
        sections += f"""
        <h3 style="font-size:15px;font-weight:700;color:#334155;
                   border-left:3px solid #b45309;padding-left:10px;margin:26px 0 12px;">
          {esc(tag)} <span style="color:#94a3b8;font-weight:400;">({len(arts)})</span></h3>
        {''.join(card(a) for a in head)}{rest_html}"""

    trends = ""
    for i, t in enumerate(data.get("trends", [])):
        trends += f"""
        <div style="border-left:4px solid #b45309;background:#fff;padding:16px 18px;
                    margin-bottom:12px;border-radius:0 6px 6px 0;">
          <div style="font-size:11px;font-weight:800;color:#b45309;">TREND 0{i+1}</div>
          <div style="font-size:15px;font-weight:700;color:#0f2744;margin:6px 0 8px;">
            {esc(t.get('title', ''))}</div>
          <div style="font-size:14px;color:#475569;line-height:1.65;">
            {esc(t.get('body', ''))}</div>
        </div>"""

    insights = ""
    for ins in data.get("insights", []):
        insights += f"""
        <tr>
          <td valign="top" style="width:22px;color:#d97706;font-size:15px;
                                  line-height:1.7;padding-top:2px;">&#9658;</td>
          <td style="font-size:14px;color:#451a03;line-height:1.7;
                     padding-bottom:14px;">{esc(ins)}</td>
        </tr>"""

    weather_js = _WEATHER_JS.replace("__SPOTS__", json.dumps(WEATHER_SPOTS, ensure_ascii=False))

    return f"""<!DOCTYPE html>
<html lang="ko"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Tungsten Recycling Daily Brief — {today}</title>
<style>{_WEB_CSS}</style></head><body>

<div style="background:#1c1917;color:#fff;padding:26px 0;">
  <div class="wrap">
    <div style="font-size:20px;font-weight:700;letter-spacing:.5px;">
      TUNGSTEN RECYCLING DAILY BRIEF</div>
    <div style="color:#a8a29e;font-size:13px;margin-top:6px;">{today}</div>
    <div id="wx" style="margin-top:14px;display:flex;gap:18px;flex-wrap:wrap;
                        font-size:13px;color:#d6d3d1;">불러오는 중…</div>
    <div style="margin-top:12px;">
      <a href="{archive_href}" style="font-size:12px;color:#fbbf24;
         text-decoration:none;">지난 브리핑 보기 &rarr;</a></div>
  </div>
</div>

<div class="wrap">
  {price_web}

  <h2 class="sec">분야별 기사</h2>
  <div style="font-size:12px;color:#94a3b8;margin-bottom:8px;">
    분야별 상위 3건 표시 · 4번째부터는 펼쳐서 확인</div>
  {sections or '<p style="color:#94a3b8;font-size:13px;">오늘은 선별된 기사가 없습니다.</p>'}

  <h2 class="sec">오늘의 산업 흐름</h2>
  {trends}

  <h2 class="sec">리사이클링 사업자 관점 시사점</h2>
  <div style="background:#fefce8;border:1px solid #fde047;border-radius:8px;padding:20px 22px;">
    <table width="100%" cellpadding="0" cellspacing="0">{insights}</table>
  </div>

  <div style="margin-top:44px;padding-top:18px;border-top:1px solid #e2e8f0;
              font-size:12px;color:#94a3b8;line-height:1.6;">
    Tungsten Recycling Daily Brief · {today}<br>
    시세 SMM(metal.com) · 로테르담 APT는 CIF 기준 · W 환산은 회수율 미반영<br>
    &copy; Ben Seo · SungEel HiTech
  </div>
</div>

<script>{weather_js}</script>
</body></html>"""


# ============================================================
# 아카이브
# ============================================================
_ARCHIVE_JS = """
const DATES = __DATES__;
const SET   = new Set(DATES);
let cur;
const pad = n => String(n).padStart(2, '0');
const ym  = d => d.getFullYear() + '-' + pad(d.getMonth() + 1);
function render() {
  const y = cur.getFullYear(), m = cur.getMonth();
  const first = new Date(y, m, 1).getDay();
  const days  = new Date(y, m + 1, 0).getDate();
  let cells = '';
  for (let i = 0; i < first; i++) cells += '<span></span>';
  for (let d = 1; d <= days; d++) {
    const key = y + '-' + pad(m + 1) + '-' + pad(d);
    cells += SET.has(key)
      ? '<a class="on" href="' + key + '.html">' + d + '</a>'
      : '<span class="off">' + d + '</span>';
  }
  document.getElementById('cal').innerHTML = cells;
  document.getElementById('label').textContent = y + '년 ' + (m + 1) + '월';
  document.getElementById('prev').disabled = ym(cur) <= DATES[DATES.length-1].slice(0,7);
  document.getElementById('next').disabled = ym(cur) >= DATES[0].slice(0,7);
}
function move(n) { cur = new Date(cur.getFullYear(), cur.getMonth() + n, 1); render(); }
(function () {
  const latest = DATES[0] || new Date().toISOString().slice(0, 10);
  cur = new Date(+latest.slice(0,4), +latest.slice(5,7) - 1, 1);
  render();
})();
"""

_ARCHIVE_CSS = """
  body { margin:0;background:#f8fafc;font-family:'Apple SD Gothic Neo',
         'Malgun Gothic',Arial,sans-serif;color:#0f2744; }
  .wrap { max-width:520px;margin:0 auto;padding:10px 16px 60px; }
  .card { background:#fff;border:1px solid #e2e8f0;border-radius:10px;
          padding:18px 20px 22px;margin-top:18px; }
  .nav { display:flex;align-items:center;justify-content:space-between;margin-bottom:14px; }
  .nav button { width:32px;height:32px;border:1px solid #e2e8f0;background:#fff;
                border-radius:8px;font-size:15px;color:#334155;cursor:pointer;line-height:1; }
  .nav button:disabled { color:#e2e8f0;cursor:default; }
  .nav b { font-size:15px; }
  .wd, #cal { display:grid;grid-template-columns:repeat(7,1fr);gap:5px; }
  .wd span { text-align:center;font-size:11px;color:#94a3b8;font-weight:700;padding-bottom:6px; }
  #cal a, #cal span { display:flex;align-items:center;justify-content:center;
                      height:38px;border-radius:8px;font-size:14px; }
  #cal a.on { background:#b45309;color:#fff;font-weight:700;text-decoration:none; }
  #cal a.on:hover { background:#92400e; }
  #cal span.off { color:#cbd5e1; }
  details summary { cursor:pointer;font-size:13px;color:#2563eb;font-weight:600;padding:6px 0; }
"""


def build_archive_index():
    files = sorted(glob.glob("docs/archive/*.html"), reverse=True)
    dates = sorted([os.path.basename(f)[:-5] for f in files
                    if re.match(r"\d{4}-\d{2}-\d{2}\.html$", os.path.basename(f))],
                   reverse=True)

    if dates:
        links = "".join(
            f'<a href="{d}.html" style="display:inline-block;padding:6px 10px;'
            f'margin:0 5px 5px 0;background:#fff;border:1px solid #e2e8f0;'
            f'border-radius:6px;font-size:12px;color:#0f2744;'
            f'text-decoration:none;">{d}</a>' for d in dates)
        cal = f"""
  <div class="card">
    <div class="nav">
      <button id="prev" onclick="move(-1)">&lsaquo;</button>
      <b id="label"></b>
      <button id="next" onclick="move(1)">&rsaquo;</button>
    </div>
    <div class="wd"><span>일</span><span>월</span><span>화</span><span>수</span>
      <span>목</span><span>금</span><span>토</span></div>
    <div id="cal"></div>
  </div>
  <details style="margin-top:16px;">
    <summary>전체 목록 ({len(dates)}건)</summary>
    <div style="margin-top:8px;">{links}</div>
  </details>
  <script>{_ARCHIVE_JS.replace("__DATES__", json.dumps(dates))}</script>"""
    else:
        cal = '<p style="color:#94a3b8;font-size:13px;">아직 없습니다.</p>'

    html = f"""<!DOCTYPE html><html lang="ko"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>TRDB 아카이브</title>
<style>{_ARCHIVE_CSS}</style></head><body>
<div style="background:#1c1917;color:#fff;padding:24px 0;">
  <div style="max-width:520px;margin:0 auto;padding:0 16px;">
    <div style="font-size:18px;font-weight:700;">TRDB 아카이브</div>
    <div style="color:#a8a29e;font-size:12px;margin-top:5px;">총 {len(dates)}건</div>
  </div></div>
<div class="wrap">
  <a href="../index.html" style="display:inline-block;margin:18px 0 2px;
     font-size:13px;color:#b45309;text-decoration:none;">← 오늘 브리핑</a>
  {cal}
</div></body></html>"""

    os.makedirs("docs/archive", exist_ok=True)
    with open("docs/archive/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  아카이브 인덱스: {len(dates)}건")


# ============================================================
# 이메일
# ============================================================
def build_email(data, price_data=None, usd_cny=7.15, web_url=""):
    today   = now_kst().strftime("%Y년 %m월 %d일")
    spreads = compute_spreads(price_data) if price_data else {}

    ins_list = data.get("insights", [])
    insights_html = ""
    for i, ins in enumerate(ins_list):
        bb = "border-bottom:1px dashed #fde047;" if i < len(ins_list) - 1 else ""
        pt = "padding-top:16px;" if i > 0 else ""
        pb = "padding-bottom:16px;" if i < len(ins_list) - 1 else ""
        insights_html += f"""
        <tr>
          <td valign="top" style="width:20px;color:#d97706;font-size:16px;line-height:1.6;
              {pt}font-family:'Malgun Gothic',Arial,sans-serif;">&#9658;</td>
          <td style="{bb}{pt}{pb}font-size:14px;color:#451a03;line-height:1.6;
              font-family:'Malgun Gothic',Arial,sans-serif;">{esc(ins)}</td>
        </tr>"""

    price_rows = ""
    if price_data:
        price_rows = f"""
  <tr>
    <td bgcolor="#292524" style="background:#292524;color:#ffffff;font-size:13px;
        font-weight:700;letter-spacing:0.5px;padding:12px 30px;
        font-family:'Malgun Gothic',Arial,sans-serif;">SMM 텅스텐 시세</td>
  </tr>
  <tr>
    <td bgcolor="#ffffff" style="background:#ffffff;padding:20px 12px 16px;">
      <p style="margin:0 0 4px;font-size:12px;color:#64748b;padding:0 6px;
                font-family:'Malgun Gothic',Arial,sans-serif;">
        {now_kst():%Y.%m.%d} · 증치세 제외 · USD/CNY {usd_cny:.2f} ·
        W 환산은 회수율 미반영 이론치</p>
      <p style="margin:0 0 12px;padding:0 6px;">{_spread_badges(spreads) or '&nbsp;'}</p>
      <table width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#ffffff"
             style="font-size:13px;background:#ffffff;border:1px solid #e2e8f0;
                    border-collapse:collapse;">
        <thead><tr bgcolor="#f8fafc" style="background:#f8fafc;">
          <td style="padding:10px 14px;font-size:11px;color:#475569;font-weight:700;
              border-bottom:2px solid #e2e8f0;font-family:'Malgun Gothic',Arial,sans-serif;">품목</td>
          <td style="padding:10px 14px;text-align:right;font-size:11px;color:#475569;font-weight:700;
              border-bottom:2px solid #e2e8f0;font-family:'Malgun Gothic',Arial,sans-serif;">고시가</td>
          <td style="padding:10px 14px;text-align:right;font-size:11px;color:#475569;font-weight:700;
              border-bottom:2px solid #e2e8f0;font-family:'Malgun Gothic',Arial,sans-serif;">W 환산</td>
          <td style="padding:10px 14px;text-align:center;font-size:11px;color:#475569;font-weight:700;
              border-bottom:2px solid #e2e8f0;font-family:'Malgun Gothic',Arial,sans-serif;">등락</td>
        </tr></thead>
        <tbody>{_price_rows(price_data)}</tbody>
      </table>
      {_spread_table(spreads)}
      <p style="margin:10px 6px 0;color:#94a3b8;font-size:11px;text-align:right;
                font-family:'Malgun Gothic',Arial,sans-serif;">
        현물 <b>SMM</b> (metal.com) · 로테르담 APT는 CIF 기준</p>
    </td>
  </tr>"""

    n_art, n_trend = len(data.get("articles", [])), len(data.get("trends", []))

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<!--[if mso]>
<xml><o:OfficeDocumentSettings><o:AllowPNG/>
<o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml>
<![endif]-->
<style type="text/css">
  body {{ margin:0; padding:0; background-color:#f8fafc;
          -webkit-text-size-adjust:100%; -ms-text-size-adjust:100%; }}
  table {{ border-collapse:collapse; mso-table-lspace:0pt; mso-table-rspace:0pt; }}
  a {{ text-decoration:none; }}
</style>
</head>
<body style="margin:0;padding:20px;background-color:#f8fafc;">
<!--[if mso]><table align="center" width="680" cellpadding="0" cellspacing="0" border="0"><tr><td><![endif]-->
<table align="center" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="max-width:680px;margin:0 auto;background-color:#ffffff;
              border:1px solid #e2e8f0;border-collapse:collapse;">

  <tr>
    <td bgcolor="#1c1917" style="background-color:#1c1917;padding:36px 30px;">
      <h1 style="color:#ffffff;font-size:22px;font-weight:700;margin:0 0 10px 0;
                 letter-spacing:0.5px;font-family:'Malgun Gothic',Arial,sans-serif;">
        TUNGSTEN RECYCLING DAILY BRIEF</h1>
      <p style="color:#a8a29e;font-size:14px;margin:0;
                font-family:'Malgun Gothic',Arial,sans-serif;">{today}</p>
    </td>
  </tr>

  {price_rows}

  <tr>
    <td bgcolor="#292524" style="background:#292524;color:#ffffff;font-size:13px;
        font-weight:700;letter-spacing:0.5px;padding:12px 30px;
        font-family:'Malgun Gothic',Arial,sans-serif;">리사이클링 사업자 관점 시사점</td>
  </tr>
  <tr>
    <td bgcolor="#ffffff" style="background:#ffffff;padding:26px 30px;">
      <table width="100%" cellpadding="22" cellspacing="0" border="0" bgcolor="#fefce8"
             style="background-color:#fefce8;border:1px solid #fde047;
                    border-radius:8px;border-collapse:collapse;">
        <tr><td>
          <table width="100%" cellpadding="0" cellspacing="0" border="0"
                 style="border-collapse:collapse;">{insights_html}</table>
        </td></tr>
      </table>
    </td>
  </tr>

  <tr>
    <td bgcolor="#f1f5f9" style="background-color:#f1f5f9;padding:32px 30px 36px;
        text-align:center;border-top:1px solid #e2e8f0;">
      <p style="margin:0 0 4px;font-size:11px;font-weight:800;color:#b45309;
                letter-spacing:1.5px;font-family:'Malgun Gothic',Arial,sans-serif;">
        TODAY'S FULL BRIEF</p>
      <p style="margin:0 0 18px;font-size:15px;font-weight:700;color:#0f2744;
                font-family:'Malgun Gothic',Arial,sans-serif;">
        오늘 선별된 기사 {n_art}건과 산업 흐름 {n_trend}건</p>
      <table cellpadding="0" cellspacing="0" border="0" align="center" style="margin:0 auto;">
        <tr>
          <td bgcolor="#fed7aa" style="background-color:#fed7aa;border-radius:11px;padding:5px;">
            <table cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td bgcolor="#b45309" style="background-color:#b45309;border-radius:7px;">
                  <a href="{web_url}" style="display:inline-block;padding:18px 42px;
                     font-size:16px;font-weight:700;color:#ffffff;text-decoration:none;
                     letter-spacing:0.3px;font-family:'Malgun Gothic',Arial,sans-serif;">
                    분야별 기사 · 산업 흐름 · 시세 차트 &nbsp;&rarr;</a>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
      <p style="margin:16px 0 0;font-size:12px;color:#64748b;
                font-family:'Malgun Gothic',Arial,sans-serif;">
        롱테일 시세 전 품목 · 30일 추이 · CSV 내려받기</p>
    </td>
  </tr>

  <tr>
    <td bgcolor="#1c1917" style="background-color:#1c1917;padding:26px 30px;text-align:center;">
      <p style="color:#a8a29e;font-size:13px;margin:0 0 8px 0;
                font-family:'Malgun Gothic',Arial,sans-serif;">
        Tungsten Recycling Daily Brief &nbsp;|&nbsp; {today}</p>
      <p style="color:#78716c;font-size:12px;margin:0;
                font-family:'Malgun Gothic',Arial,sans-serif;">
        &copy; Ben Seo, SungEel HiTech</p>
    </td>
  </tr>
</table>
<!--[if mso]></td></tr></table><![endif]-->
</body></html>"""


def send_email(html_body):
    today = now_kst().strftime("%Y년 %m월 %d일")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[텅스텐 산업 Daily Brief] {today}"
    msg["From"]    = GMAIL_USER
    msg["To"]      = TO_EMAIL
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASS)
        bcc = [a.strip() for a in BCC_EMAIL.split(',') if a.strip()] if BCC_EMAIL else []
        smtp.sendmail(GMAIL_USER, [TO_EMAIL] + bcc, msg.as_string())
    print(f"발송 완료 → {TO_EMAIL} (BCC {len(bcc)}명)")


# ============================================================
# 메인
# ============================================================
async def main():
    print("=== TRDB 시작 ===")

    usd_cny = get_usd_cny_rate()

    price_data = None
    try:
        price_data = await scrape_prices()
    except Exception as e:
        print(f"! 시세 수집 실패({e}) — 시세 없이 계속")

    if price_data:
        sp = compute_spreads(price_data)
        for k, s in sp.items():
            print(f"  [스프레드] {s['label']}: {s['pct']:.1f}%  ({s['desc']})")

    seen     = load_seen()
    articles = collect_rss(seen)

    if not articles:
        print("신규 기사 없음 — 시세만 발송")
        data = {"articles": [], "trends": [],
                "insights": ["오늘은 신규 선별 기사가 없습니다. 시세 지표만 확인해 주세요."]}
    else:
        articles = await enrich_articles(articles)
        data     = analyze(articles, price_data=price_data, usd_cny=usd_cny)

        from collections import Counter
        arts = data.get("articles", [])
        print(f"\n[선별 결과] {len(arts)}건")
        for t, n in Counter(a.get("tag", "?") for a in arts).most_common():
            print(f"  {t}: {n}건")
        for a in arts:
            print(f"  [{a.get('tag', '?')[:8]:<8}] {a.get('title', '')[:50]}")
        print(f"[트렌드] {len(data.get('trends', []))}개  "
              f"[시사점] {len(data.get('insights', []))}개")

        save_seen(seen, articles)

    hist = append_price_history(price_data, usd_cny,
                                compute_spreads(price_data) if price_data else {})

    os.makedirs("docs/archive", exist_ok=True)
    open("docs/.nojekyll", "w").close()

    stamp = now_kst().strftime("%Y-%m-%d")
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(build_web(data, price_data, usd_cny, hist, in_archive=False))
    with open(f"docs/archive/{stamp}.html", "w", encoding="utf-8") as f:
        f.write(build_web(data, price_data, usd_cny, hist, in_archive=True))
    build_archive_index()
    print(f"웹 저장: docs/index.html, docs/archive/{stamp}.html")

    send_email(build_email(data, price_data=price_data, usd_cny=usd_cny,
                           web_url=f"{WEB_BASE}/archive/{stamp}.html"))
    print("=== 완료 ===")


if __name__ == "__main__":
    asyncio.run(main())
