# html_image_merge_crawler.py

import os
import time
import json
import re
import traceback
from contextlib import contextmanager
from datetime import datetime
from typing import Optional, Dict, List

import base64
from io import BytesIO

import requests
from bs4 import BeautifulSoup
import mysql.connector
from mysql.connector import Error as MySQLError

from openai import OpenAI
from PIL import Image  # 이미지 처리
from datetime import date, datetime
import sys, traceback

from dotenv import load_dotenv
load_dotenv()

# Playwright
try:
    from playwright.sync_api import sync_playwright
    _PLAYWRIGHT_AVAILABLE = True
except Exception:
    _PLAYWRIGHT_AVAILABLE = False


# =========================
# 0) 환경설정
# =========================
BASE_DIR = os.path.abspath(os.getcwd())
OUT_DIR = os.path.join(BASE_DIR, "screenshot")
os.makedirs(OUT_DIR, exist_ok=True)

DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("DB_NAME"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "charset": os.getenv("DB_CHARSET", "utf8mb4"),
    "autocommit": os.getenv("DB_AUTOCOMMIT", "False") == "True",
    "use_pure": os.getenv("DB_USE_PURE", "True") == "True",
    "connection_timeout": int(os.getenv("DB_CONN_TIMEOUT", 20)),
    "raise_on_warnings": os.getenv("DB_WARNINGS", "True") == "True",
}

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY").strip()
client = OpenAI(api_key=OPENAI_API_KEY)
SUMMARIZE_MODEL = "gpt-4o"  
EMBED_MODEL = "text-embedding-3-small"

#################################################################################
# 카테고리 ↔ list_id 매핑
CATEGORIES: Dict[str, str] = {
    "COLLEGE_ENGINEERING": "20013DA1",
    "COLLEGE_HUMANITIES": "human01",
    "COLLEGE_SOCIAL_SCIENCES": "econo01",
    "COLLEGE_URBAN_SCIENCE": "urbansciences01",
    "COLLEGE_ARTS_SPORTS": "artandsport01",
    "COLLEGE_BUSINESS": "20008N2",
    "COLLEGE_NATURAL_SCIENCES": "scien01",
    "COLLEGE_LIBERAL_CONVERGENCE": "clacds01",
    "GENERAL": "FA1",     # TODO
    "ACADEMIC": "FA2",    # TODO
}
#################################################################################

# 목록 페이지에 필요한 추가 파라미터 (있을 때만)
CATEGORY_LIST_PARAMS: Dict[str, Dict[str, str]] = {
    "COLLEGE_ENGINEERING": {"cate_id2": "000010383"},
}

BASE_URL = "https://www.uos.ac.kr/korNotice/view.do"
LIST_URL = "https://www.uos.ac.kr/korNotice/list.do"


# 몇 개 크롤링할 건지 
REQUEST_SLEEP = 1.0
MISSING_BREAK = 3
PLAYWRIGHT_TIMEOUT_MS = 90000
RECENT_WINDOW = 100

SUMMARY_PROMPT = """ 
당신의 임무는 "대학 공지사항" 이미지에서 **명시된 사실 정보만** 추출·정규화하여 자연어 문장들로 요약하는 것입니다. 
원문에 직접적으로 쓰여 있지 않은 정보는 절대 추측/보완/생성하지 마세요. 

[출력 요구] 
- 반드시 여러 문단의 자연스러운 문장으로만 출력 (최대한 길고 상세하게) 
- 불필요한 안내/네비게이션 문구는 제외 
- 해당 공지사항 내에 기재되어 있는 모든 정보에 대해서 빠짐없이 모두 포함하여 요약하여야합니다.
- 다만, 해당 이미지는 공지사항 웹페이지의 '전체' 캡쳐본입니다. 본문의 내용 및 정보는 절대 누락되선 안되지만, 본문 영역 외의 기존 맥락과 다른 불필요한 정보 (예시 : 공지사항 본문 옆과 밑에 있는 '관련있는 게시물'과 서울시립대학교 학교 주소 및 Copywrite 관련 내용)는 제외해도 됩니다. 

[정규화 규칙]  
- 수치는 원문 그대로 보존
- 날짜 및 시간은 원문 그대로 보존
- 기관/부서, 장소, 전화, 메일은 원문 표기 그대로 사용(추측 금지) 
"""

# =========================
# 1) 유틸
# =========================
@contextmanager
def mysql_conn():
    conn = mysql.connector.connect(**DB_CONFIG)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def parse_date_yyyy_mm_dd(text: str) -> Optional[str]:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", text or "")
    return m.group(1) if m else None


def extract_main_text_from_html(html: str, max_chars: int = 12000) -> str:
    """
    공지의 '본문' 컨테이너에서 텍스트만 추출.
    사이드/푸터/관련글/주소/카피라이트 등은 제거하고,
    길이가 너무 길면 max_chars로 잘라 모델 입력을 안정화.
    """
    soup = BeautifulSoup(html, "html.parser")

    # 본문 후보 셀렉터 (사이트 맞게 필요시 추가)
    candidates = [
        "div.vw-cnt", "div.vw-con", "div.vw-bd", "div.board-view",
        "article", "div#content", "div#contents", "main"
    ]
    main = None
    for sel in candidates:
        node = soup.select_one(sel)
        if node and node.get_text(strip=True):
            main = node
            break
    if main is None:
        main = soup.body or soup

    # 불필요 영역 제거
    kill_selectors = [
        ".related", ".relate", ".attach", ".file", ".files",
        ".prev", ".next", "footer", "#footer", ".sns", ".share",
        ".copyright", ".copy", ".address", ".addr"
    ]
    for ks in kill_selectors:
        for n in main.select(ks):
            n.decompose()

    text = main.get_text("\n", strip=True)

    # 흔한 푸터/주소/카피라이트 문구 제거
    drop_patterns = [
        r"서울시립대학교\s*.+?\d{2,3}-\d{3,4}-\d{4}",
        r"Copyright.+?All rights reserved\.?",
        r"이전글.*", r"다음글.*", r"관련\s?게시물.*",
    ]
    for pat in drop_patterns:
        text = re.sub(pat, "", text, flags=re.I | re.S)

    # 공백 정리
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    # 과도한 길이 제한
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[... 본문 일부 생략 ...]"

    return text


# =========================
# 2) Playwright로 HTML → 이미지 캡처
# =========================
def html_to_images_playwright(
    url: str,
    viewport_width: int = 1200,
    slice_height: int = 1920,
    timeout_ms: int = PLAYWRIGHT_TIMEOUT_MS,
    debug_full_image_path: Optional[str] = None,  # 전체 페이지 1장 저장 경로
    full_image_format: str = "png",               # "png"|"jpeg"
) -> List[Image.Image]:
    """
    페이지 전체를 full_page 스크린샷으로 찍은 뒤,
    slice_height 간격으로 끝까지 전부 잘라서 반환.
    (max_slices 제한 없음)
    debug_full_image_path가 주어지면 전체 스크린샷 원본을 파일로 저장.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        print("❌ Playwright 미설치/임포트 실패")
        return []

    imgs: List[Image.Image] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=[
                "--disable-web-security",
                "--hide-scrollbars",
            ])
            page = browser.new_page(
                viewport={"width": viewport_width, "height": slice_height},
                device_scale_factor=2.0,
            )
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

            try:
                page.wait_for_selector("div.vw-tibx", timeout=timeout_ms)
            except Exception:
                pass

            for _ in range(6):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(700)

            page.wait_for_load_state("networkidle", timeout=timeout_ms)
            page.wait_for_timeout(800)

            # 전체 페이지 스크린샷
            if full_image_format.lower() == "png":
                buf = page.screenshot(full_page=True, type="png")
            else:
                buf = page.screenshot(full_page=True, type="jpeg", quality=85)
            browser.close()

        # 전체 페이지 한 장 저장(테스트/디버그)
        if debug_full_image_path:
            try:
                with open(debug_full_image_path, "wb") as f:
                    f.write(buf)
                print(f"💾 Full screenshot saved: {debug_full_image_path}")
            except Exception as e:
                print(f"⚠️ Full screenshot save failed: {e}")

        # 슬라이스 분할
        full_img = Image.open(BytesIO(buf)).convert("RGB")
        W, H = full_img.size
        y = 0
        while y < H:
            crop = full_img.crop((0, y, W, min(y + slice_height, H)))
            imgs.append(crop)
            y += slice_height

    except Exception as e:
        print(f"❌ HTML→이미지 캡처 실패: {e}")

    return imgs


# =========================
# 3) OpenAI: 이미지/텍스트 요약 + 임베딩
# =========================
def pil_to_data_url(pil_image: Image.Image, fmt="JPEG", quality=80) -> str:
    bio = BytesIO()
    pil_image.save(bio, format=fmt, quality=quality, optimize=True)
    b64 = base64.b64encode(bio.getvalue()).decode("utf-8")

    return f"data:image/{fmt.lower()};base64,{b64}"

def summarize_with_text_and_images(html_text: str, images: List[Image.Image]) -> str:
    """
    HTML 본문 텍스트를 우선 근거로 삼고,
    이미지(포스터/표 등)에만 있는 누락 정보를 보강하도록 지시.
    """
    merge_prompt = f"""
아래는 대학 공지사항의 'HTML 본문 텍스트'입니다. 이 텍스트를 **우선 근거**로 삼고,
추가로 제공되는 '페이지 전체 캡처 이미지들'에서만 보이는 표/포스터/스캔된 문장 등 누락 정보를 **보완**하여
내용을 덧붙여주세요.

- 본문과 무관한 사이드/푸터/주소/카피라이트/관련 게시물 등은 제외하세요.
- 수치는 원문 그대로 보존
- 날짜 및 시간은 원문 그대로 보존
- 기관/부서, 장소, 전화, 메일은 원문 표기 그대로 사용(추측 금지) 

[HTML 본문 텍스트 시작]
{html_text}
[HTML 본문 텍스트 끝]
""".strip()

    contents = [{"type": "input_text", "text": merge_prompt}]
    for img in images:
        contents.append({
            "type": "input_image",
            "image_url": pil_to_data_url(img, fmt="JPEG", quality=75)  # JPEG로 압축
        })
    try:
        resp = client.responses.create(
            model=SUMMARIZE_MODEL,   # "gpt-4o" 권장
            input=[{"role": "user", "content": contents}],
            temperature=0.2,
        )
        return (resp.output_text or "").strip()
    except Exception as e:
        print(f"❌ 텍스트+이미지 요약 실패: {type(e).__name__}: {e}")
        traceback.print_exc(limit=2, file=sys.stdout)
        return ""

def embed_text(text: str) -> list:
    if not text:
        return []
    try:
        er = client.embeddings.create(input=[text], model=EMBED_MODEL)
        return er.data[0].embedding
    except Exception as e:
        print(f"⚠️ 임베딩 실패(무시하고 진행): {e}")
        return []


# =========================
# 4) HTML 파싱 (상세)
# =========================
CONNECT_TIMEOUT = 10    # 서버 TCP 연결까지 기다릴 최대 시간
READ_TIMEOUT    = 20   # 실제 응답(HTML)을 받는 시간

def fetch_notice_html(list_id: str, seq: int) -> Optional[str]:
    try:
        params = {
            "list_id": list_id,
            "seq": str(seq),
            "sort": "1",
            "pageIndex": "1",
            "searchCnd": "",
            "searchWrd": "",
            "cate_id": "",
            "viewAuth": "Y",
            "writeAuth": "Y",
            "board_list_num": "10",
            "lpageCount": "12",
            "menuid": "",
        }
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(BASE_URL, params=params, headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        if r.status_code != 200:
            print(f"❌ HTTP {r.status_code} for seq={seq}")
            return None
        return r.text
    except Exception as e:
        print(f"❌ 요청 실패 seq={seq}: {e}")
        return None


def parse_notice_fields(html: str, seq: int) -> Optional[dict]:
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one("div.vw-tibx h4") if soup else None
    title = title_el.get_text(strip=True) if title_el else ""
    if not title:
        return None  # 게시물 없음

    spans = soup.select("div.vw-tibx div.zl-bx div.da span")
    department = spans[1].get_text(strip=True) if len(spans) >= 3 else ""
    date_text = spans[2].get_text(strip=True) if len(spans) >= 3 else ""
    dt = parse_date_yyyy_mm_dd(date_text) or datetime.now().strftime("%Y-%m-%d")

    post_number_el = soup.select_one("input[name=seq]")
    post_number = int(post_number_el["value"]) if post_number_el and post_number_el.get("value") else int(seq)

    content_text = soup.get_text("\n", strip=True)
    return {
        "title": title,
        "department": department,
        "posted_date": dt,
        "post_number": post_number,
        "content_text": content_text,
    }


# =========================
# 5) DB 업서트
# =========================
UPSERT_SQL = """
INSERT INTO notice
    (category, post_number, title, link, summary, embedding_vector, posted_date, department)
VALUES
    (%s, %s, %s, %s, %s, %s, %s, %s) AS new
ON DUPLICATE KEY UPDATE
    title = new.title,
    link = new.link,
    summary = new.summary,
    embedding_vector = new.embedding_vector,
    posted_date = new.posted_date,
    department = new.department
"""


UPSERT_SQL = UPSERT_SQL.replace("new.posted_date", "new.posted_date")

EXISTS_SQL = "SELECT posted_date FROM notice WHERE category=%s AND post_number=%s LIMIT 1"

def get_existing_posted_date(category: str, post_number: int) -> Optional[str]:
    with mysql_conn() as conn:
        cur = conn.cursor()
        cur.execute(EXISTS_SQL, (category, post_number))
        row = cur.fetchone()
        cur.close()
        return row[0] if row else None

def upsert_notice(row: dict):
    with mysql_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            UPSERT_SQL,
            (
                row["category"],
                row["post_number"],
                row["title"],
                row["link"],
                row.get("summary") or None,
                row.get("embedding_vector") or None,
                row["posted_date"],
                row.get("department") or None,
            ),
        )
        cur.close()


def exists_notice(category: str, post_number: int, posted_date: Optional[str]) -> bool:
    with mysql_conn() as conn:
        cur = conn.cursor()
        cur.execute(EXISTS_SQL, (category, post_number, posted_date))
        exists = cur.fetchone() is not None
        cur.close()
        return exists

def _ymd(x: Optional[object]) -> Optional[str]:
    if x is None:
        return None
    if isinstance(x, (datetime, date)):
        return x.strftime("%Y-%m-%d")
    s = str(x).strip()
    return s[:10]

# =========================
# 6) 파이프라인: 한 건 처리 (HTML 텍스트 + 이미지 동시 요약)
# =========================
def process_one(category_key: str, list_id: str, seq: int) -> str:
    # 1) HTML
    html = fetch_notice_html(list_id, seq)
    if not html:
        print(f"⚠️ Seq {seq}: HTML 로드 실패 → 스킵")
        return "skipped_error"

    # 2) 파싱
    parsed = parse_notice_fields(html, seq)
    if not parsed:
        print(f"Seq {seq}: 게시물 없음")
        return "not_found"

    post_number = parsed["post_number"]
    title = parsed["title"]
    department = parsed["department"]
    posted_date = parsed["posted_date"]

    # 3) 링크
    link = f"{BASE_URL}?list_id={list_id}&seq={seq}"

    # 4) 중복 체크
    prev_dt_raw = get_existing_posted_date(category_key, post_number)
    prev_dt = _ymd(prev_dt_raw)
    curr_dt = _ymd(posted_date)

    if prev_dt:
        if prev_dt == curr_dt:
            # 날짜까지 동일 → 스킵
            print(f"Seq {seq} (post_number={post_number}) 이미 존재 (posted_date={curr_dt}) → 스킵")
            return "stored"
        else:
            # 날짜가 다름 → 수정된 게시물로 간주
            print(f"Seq {seq} (post_number={post_number}) 날짜 변경 {prev_dt} → {curr_dt}, 업데이트 진행")

    # 4-1) HTML 본문 텍스트 추출
    html_text = extract_main_text_from_html(html)

    # 5) HTML → 전체 이미지 캡처 (슬라이스 포함)
    full_path = os.path.join(OUT_DIR, f"{category_key}_{seq}_FULL.png")
    imgs = html_to_images_playwright(
        link,
        viewport_width=1200,
        slice_height=1800,
        debug_full_image_path=full_path,     # 전체 1장 저장
        full_image_format="png",
    )
    if not imgs:
        print(f"↳ Seq {seq}: 이미지 캡처 실패 → 스킵")
        return "skipped_error"

    # 6) 텍스트 + 이미지 동시 요약
    summary = summarize_with_text_and_images(html_text, imgs)
    if not summary:
        print(f"↳ Seq {seq}: 텍스트+이미지 요약 실패 → 스킵")
        return "skipped_error"

    print(summary)

    # 7) 임베딩 (실패해도 저장은 진행)
    embedding = embed_text(summary)
    embedding_str = json.dumps(embedding) if embedding else None

    # 8) DB 업서트
    row = {
        "category": category_key,
        "post_number": post_number,
        "title": title,
        "link": link,
        "summary": summary,
        "embedding_vector": embedding_str,
        "posted_date": posted_date,
        "department": department,
    }
    try:
        upsert_notice(row)
        print(f"✅ 저장 완료: [{category_key}] seq={seq}, post_number={post_number}, posted_date={posted_date}, title={title[:30]}...")
        return "stored"
    except MySQLError as e:
        print(f"❌ DB 저장 실패: {e.__class__.__name__}({getattr(e,'errno',None)}): {e}")
        tb = traceback.format_exc(limit=3)
        print(f"↳ Traceback(요약):\n{tb}")
        return "skipped_error"


# =========================
# 7) 목록 HTML에서 seq 추출
# =========================
from collections import OrderedDict

from bs4 import BeautifulSoup
import re

def extract_seqs_skip_pinned(html: str) -> List[int]:
    """
    목록에서 '공지' 배지가 붙은 고정글을 제외하고 seq만 추출.
    - 고정글 마크업: <p class="num"><span class="cl">공지</span></p>
    - 일반글: <p class="num">1506</p> 처럼 숫자 표시
    """
    soup = BeautifulSoup(html, "html.parser")
    seqs: List[int] = []

    # li 단위로 훑되, p.num 안에 span.cl(=공지) 있으면 skip
    for li in soup.select("li"):
        num = li.select_one("p.num")
        if num and (num.select_one("span.cl") or "공지" in num.get_text(strip=True)):
            continue  # 🔸 고정글 스킵

        # li 안에서 view.do 링크 찾고 seq 추출
        hrefs = [a.get("href", "") for a in li.select("a[href]")]
        found = False
        for href in hrefs:
            m = re.search(r"(?:\?|&|&amp;)seq=(\d+)", href)
            if m:
                seqs.append(int(m.group(1)))
                found = True
                break
        if found:
            continue

        # href에 없으면 onclick 계열에서 보조 추출 (예: goDetail('xxx','15583') or goDetail('xxx',15583))
        txt = li.decode()
        m = re.search(r"\(\s*['\"][^'\"]*['\"]\s*,\s*'(\d+)'\s*\)", txt)
        if not m:
            m = re.search(r"\(\s*['\"][^'\"]*['\"]\s*,\s*(\d+)\s*\)", txt)
        if m:
            seqs.append(int(m.group(1)))

    # 순서 유지한 중복 제거
    from collections import OrderedDict
    return list(OrderedDict.fromkeys(seqs))


def extract_seqs_from_list_html(html: str) -> List[int]:
    seqs: List[int] = []
    for m in re.finditer(r"view\.do[^\"'>]*(?:\?|&|&amp;)seq=(\d+)", html):
        seqs.append(int(m.group(1)))
    for m in re.finditer(r"\(\s*['\"][^'\"]*['\"]\s*,\s*'(\d+)'\s*\)", html):
        seqs.append(int(m.group(1)))
    for m in re.finditer(r"\(\s*['\"][^'\"]*['\"]\s*,\s*(\d+)\s*\)", html):
        seqs.append(int(m.group(1)))
    return list(OrderedDict.fromkeys(seqs))


def collect_recent_seqs(list_id: str,
                        extra_params: Optional[Dict[str, str]] = None,
                        limit: int = RECENT_WINDOW,
                        max_pages: int = 10) -> List[int]:
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.uos.ac.kr/"}
    collected: List[int] = []
    seen = set()

    for page in range(1, max_pages + 1):
        params = {"list_id": list_id, "pageIndex": str(page), "searchCnd": "", "searchWrd": ""}
        if extra_params:
            params.update(extra_params)

        r = requests.get(LIST_URL, params=params, headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        if r.status_code != 200:
            print(f"❌ 목록 HTTP {r.status_code} (list_id={list_id}, page={page}, params={params})")
            break

        if page == 1:
            page_seqs = extract_seqs_skip_pinned(r.text)
        else:
            page_seqs = extract_seqs_from_list_html(r.text)

        new_count = 0
        for s in page_seqs:
            if s not in seen:
                seen.add(s)
                collected.append(s)
                new_count += 1
                if len(collected) >= limit:
                    return collected

        if new_count == 0:
            break

        time.sleep(0.2)

    return collected


# =========================
# 8) 실행부
# =========================
print(f"Screenshot directory: {OUT_DIR}")
if __name__ == "__main__":
    # 여기 카테고리 추가하면 크롤링
    targets = [
        "COLLEGE_ENGINEERING" ,
        "COLLEGE_HUMANITIES",
        "COLLEGE_SOCIAL_SCIENCES",
        "COLLEGE_URBAN_SCIENCE",
        "COLLEGE_ARTS_SPORTS",
        "COLLEGE_BUSINESS",
        "COLLEGE_NATURAL_SCIENCES",
        "COLLEGE_LIBERAL_CONVERGENCE",
        "GENERAL",
        "ACADEMIC"
    ]

    for cat in targets:
        list_id = CATEGORIES.get(cat)
        if not list_id or "TODO" in list_id.lower():
            print(f"⏭️  {cat}: list_id 미설정 → 건너뜀")
            continue

        extra = None
        seqs = collect_recent_seqs(list_id, extra_params=extra, limit=RECENT_WINDOW, max_pages=10)

        if not seqs:
            print(f"⚠️ {cat}: 목록에서 seq를 찾지 못해 건너뜀")
            continue

        print(f"==== [{cat}] list_id={list_id}, {len(seqs)}개 수집됨 (목록 노출 항목만) ====")
        for seq in reversed(seqs):
            process_one(cat, list_id, seq)
            time.sleep(REQUEST_SLEEP)


