import os
import time
import json
import re
import traceback
from contextlib import contextmanager
from datetime import datetime
from typing import Optional, Dict

import requests
from bs4 import BeautifulSoup
import mysql.connector
from mysql.connector import Error as MySQLError

from openai import OpenAI

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
OUT_DIR = os.path.join(BASE_DIR, "notices_pdf")
os.makedirs(OUT_DIR, exist_ok=True)

# DB 연결 정보
DB_CONFIG = {
    "host": "uoscholar.cdkke4m4o6zb.ap-northeast-2.rds.amazonaws.com",
    "user": "admin",
    "password": "dongha1005!",
    "database": "uoscholar_db",
    "port": 3306,
    "charset": "utf8mb4",
    "autocommit": False,
    "use_pure": True,
    "connection_timeout": 10,
    "raise_on_warnings": True,
}

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)
SUMMARIZE_MODEL = "gpt-4o-mini"
EMBED_MODEL = "text-embedding-3-small"

# 카테고리 ↔ list_id 매핑
CATEGORIES: Dict[str, str] = {
    "COLLEGE_ENGINEERING": "20013DA1",
    "COLLEGE_HUMANITIES": "humna01",
    "COLLEGE_SOCIAL_SCIENCES": "econo01",
    "COLLEGE_URBAN_SCIENCE": "urbansciences01",
    "COLLEGE_ARTS_SPORTS": "artandsport01",
    "COLLEGE_BUSINESS": "20008N2",
    "COLLEGE_NATURAL_SCIENCES": "scien01",
    "COLLEGE_LIBERAL_CONVERGENCE": "clacds01",
    "GENERAL": "general",     # TODO
    "ACADEMIC": "academic",   # TODO
}

BASE_URL = "https://www.uos.ac.kr/korNotice/view.do"
REQUEST_SLEEP = 1.0
MISSING_BREAK = 3
PLAYWRIGHT_TIMEOUT_MS = 45000  # 페이지 로딩/네트워크 안정화 대기

SUMMARY_PROMPT = (
    "이 문서는 대학 공지사항입니다. 사람이 바로 활용할 수 있게 핵심만 간결히 정리해 주세요.\n"
    "- 제목, 주최/부서, 기간(모집/행사), 장소, 대상/자격, 문의(전화/메일)와 관련하여, 문서의 전체 내용을 한 단락으로 정리\n"
    "- 날짜는 YYYY-MM-DD 형식으로 정규화, 수치는 그대로 보존, 불필요한 안내/네비게이션 문구는 제외\n"
)


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


# =========================
# 2) Playwright로 PDF 생성 (성공 시에만 다음 단계 진행)
# =========================
def render_pdf_playwright(url: str, out_pdf: str, timeout_ms: int = PLAYWRIGHT_TIMEOUT_MS) -> bool:
    if not _PLAYWRIGHT_AVAILABLE:
        print("❌ PDF 생성 실패: Playwright가 설치/임포트되지 않았습니다.")
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--disable-web-security"])
            page = browser.new_page()
            # 로딩 → networkidle로 안정화 → 추가 2초
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
            page.wait_for_timeout(2000)
            pdf_bytes = page.pdf(format="A4", print_background=True)
            with open(out_pdf, "wb") as f:
                f.write(pdf_bytes)
            browser.close()
        print(f"📄 PDF 생성 성공: {out_pdf}")
        return True
    except Exception as e:
        print(f"❌ PDF 생성 실패: {e.__class__.__name__}: {e}")
        tb = traceback.format_exc(limit=3)
        print(f"↳ Traceback(요약):\n{tb}")
        return False


# =========================
# 3) OpenAI 요약/임베딩
# =========================
def summarize_with_file(pdf_path: str) -> str:
    try:
        f = client.files.create(file=open(pdf_path, "rb"), purpose="assistants")
        resp = client.responses.create(
            model=SUMMARIZE_MODEL,
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": SUMMARY_PROMPT},
                    {"type": "input_file", "file_id": f.id}
                ]
            }],
            temperature=0.2
        )
        return (resp.output_text or "").strip()
    except Exception as e:
        print(f"❌ 요약 실패: {e.__class__.__name__}: {e}")
        tb = traceback.format_exc(limit=3)
        print(f"↳ Traceback(요약):\n{tb}")
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
# 4) HTML 파싱
# =========================
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
        r = requests.get(BASE_URL, params=params, headers=headers, timeout=10)
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

EXISTS_SQL = "SELECT 1 FROM notice WHERE category=%s AND post_number=%s LIMIT 1"


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


def exists_notice(category: str, post_number: int) -> bool:
    with mysql_conn() as conn:
        cur = conn.cursor()
        cur.execute(EXISTS_SQL, (category, post_number))
        exists = cur.fetchone() is not None
        cur.close()
        return exists


# =========================
# 6) 파이프라인: 한 건 처리
#    반환값: "stored" | "not_found" | "skipped_error"
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
    if exists_notice(category_key, post_number):
        print(f"Seq {seq} (post_number={post_number}) 이미 존재 → 스킵")
        return "stored"

    # 5) PDF 생성 (성공해야만 다음 단계)
    out_pdf = os.path.join(OUT_DIR, f"{category_key}_{seq}.pdf")
    ok_pdf = render_pdf_playwright(link, out_pdf)
    if not ok_pdf:
        print(f"↳ Seq {seq}: PDF 생성 실패로 저장 건너뜀 → 다음 seq")
        return "skipped_error"

    # 6) 요약 (PDF 입력만 허용)
    summary = summarize_with_file(out_pdf)
    if not summary:
        print(f"↳ Seq {seq}: 요약 실패로 저장 건너뜀 → 다음 seq")
        return "skipped_error"

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
        print(f"✅ 저장 완료: [{category_key}] seq={seq}, post_number={post_number}, title={title[:30]}...")
        return "stored"
    except MySQLError as e:
        print(f"❌ DB 저장 실패: {e.__class__.__name__}({getattr(e,'errno',None)}): {e}")
        tb = traceback.format_exc(limit=3)
        print(f"↳ Traceback(요약):\n{tb}")
        return "skipped_error"


# =========================
# 7) 카테고리 크롤 
#   - 게시물 '없음'만 miss_count로 계산하여 중단
#   - PDF/요약 실패는 단순 스킵 후 다음 seq 진행
# =========================
def crawl_category(category_key: str, list_id: str, start_seq: int, missing_break: int = MISSING_BREAK):
    seq = start_seq
    miss_count = 0

    while True:
        status = process_one(category_key, list_id, seq)

        if status == "stored":
            miss_count = 0
        elif status == "not_found":
            miss_count += 1
            print(f"Seq {seq}: 존재하지 않습니다. 미존재 카운트 누적 :  {miss_count}/{missing_break}")
        else:
            # "skipped_error": 실패 사유는 process_one에서 이미 출력
            pass

        if miss_count >= missing_break:
            print(f"[{category_key}] 연속 {missing_break}건 존재 X → 중단")
            break

        seq += 1
        time.sleep(REQUEST_SLEEP)


# =========================
# 8) 실행 예시
# =========================
if __name__ == "__main__":
    starts = {
        "COLLEGE_ENGINEERING": 15410,
        # 필요시 다른 카테고리 추가
    }

    for cat, start_seq in starts.items():
        list_id = CATEGORIES.get(cat)
        if not list_id or "TODO" in list_id.lower():
            print(f"⏭️  {cat}: list_id 미설정 → 건너뜀")
            continue
        print(f"==== [{cat}] list_id={list_id}, start={start_seq} ====")
        crawl_category(cat, list_id, start_seq)
