# evaluate_chatbot.py
import os
import re
import json
import time
import uuid
import difflib
import pandas as pd
import requests
from pathlib import Path

API_URL = "http://localhost:8001/chat"
EXCEL_PATH = r"./validation set.xlsx"  # 파일 경로 조정 가능
SHEET_NAME = "Sheet1"
OUTPUT_CSV = "./validation_results.csv"

# --- 텍스트 전처리(정규화) ---
def normalize_title(s: str) -> str:
    if s is None:
        return ""
    s = str(s)
    # 따옴표/대괄호/공백/특수기호 정리
    s = s.strip().strip('"').strip("'")
    s = re.sub(r"\s+", " ", s)
    # 대소문자, 괄호/하이픈/콜론 등 제거(비교를 느슨하게)
    s = s.lower()
    s = re.sub(r"[\[\]\(\)\"'“”‘’·:：\-–—_/\\|,.!?]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def fuzzy_equal(a: str, b: str, threshold: float = 0.90) -> bool:
    # 완전일치 먼저 체크
    if a == b:
        return True
    # 유사도(SequenceMatcher)로 느슨한 일치 허용
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return ratio >= threshold

def post_chat(query: str, session_id: str, timeout=20):
    payload = {"query": query, "session_id": session_id}
    r = requests.post(API_URL, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()

def main():
    # 1) 엑셀 로드 (헤더 없는 형태라 가정)
    df = pd.read_excel(EXCEL_PATH, sheet_name=SHEET_NAME, header=None)
    # 2컬럼(정답 제목, 질문)이라고 가정하고 이름 부여
    if df.shape[1] < 2:
        raise ValueError("엑셀에 최소 2개 컬럼(정답제목, 질문)이 필요합니다.")
    df = df.iloc[:, :2]
    df.columns = ["gold_title", "query"]

    results = []
    n = len(df)

    for i, row in df.iterrows():
        gold_title_raw = str(row["gold_title"])
        query_raw = str(row["query"])

        # 질문 문자열에 양쪽 큰따옴표가 들어가 있으면 제거
        query = query_raw.strip().strip('"').strip("'")

        session_id = f"val-{uuid.uuid4().hex[:12]}"
        try:
            resp = post_chat(query, session_id=session_id)
        except Exception as e:
            results.append({
                "idx": i,
                "query": query_raw,
                "gold_title": gold_title_raw,
                "pred_title": "",
                "match_exact": False,
                "match_fuzzy": False,
                "score": "",
                "error": f"request_failed: {e}"
            })
            # 잠깐 쉬었다 재시도 방지
            time.sleep(0.1)
            continue

        # API 스키마에 맞춰 파싱
        pred_title_raw = ""
        score = ""
        try:
            rec = resp.get("recommended_notice") or {}
            pred_title_raw = rec.get("title", "") or ""
            score = rec.get("score", "")
        except Exception:
            pass

        gold_norm = normalize_title(gold_title_raw)
        pred_norm = normalize_title(pred_title_raw)

        exact = (gold_norm == pred_norm and gold_norm != "")
        fuzzy = fuzzy_equal(gold_norm, pred_norm, threshold=0.90) if (gold_norm and pred_norm) else False

        results.append({
            "idx": i,
            "query": query_raw,
            "gold_title": gold_title_raw,
            "pred_title": pred_title_raw,
            "match_exact": exact,
            "match_fuzzy": fuzzy,
            "score": score,
            "error": ""
        })

        # API에 부담 줄이기
        time.sleep(0.05)

    out_df = pd.DataFrame(results)
    out_df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    # 요약 지표
    total = len(out_df)
    exact_cnt = int(out_df["match_exact"].sum())
    fuzzy_cnt = int(out_df["match_fuzzy"].sum())
    exact_acc = exact_cnt / total if total else 0.0
    fuzzy_acc = fuzzy_cnt / total if total else 0.0

    print("==== Validation Summary ====")
    print(f"Samples           : {total}")
    print(f"Exact matches     : {exact_cnt}  (accuracy = {exact_acc:.3f})")
    print(f"Fuzzy matches     : {fuzzy_cnt}  (accuracy = {fuzzy_acc:.3f})")
    print(f"Saved details to  : {Path(OUTPUT_CSV).resolve()}")
    print()
    # 틀린 케이스 상위 10개만 미리 보기
    wrong = out_df[~out_df["match_fuzzy"]].head(10)
    if not wrong.empty:
        print("---- Examples of mismatches ----")
        for _, r in wrong.iterrows():
            print(f"[{r['idx']}] Q: {r['query']}")
            print(f"   Gold: {r['gold_title']}")
            print(f"   Pred: {r['pred_title']}\n")

if __name__ == "__main__":
    main()