# src/uosai/indexer/index.py
import os, sys, time, traceback
from datetime import datetime

# 공통 유틸
from uosai.common.utils import fetch_all_rows, row_to_doc, split_docs, upsert_docs

BATCH_SIZE = int(os.getenv("BATCH_SIZE", "200"))
BATCH_SLEEP_SEC = float(os.getenv("BATCH_SLEEP_SEC", "0.8"))  # 레이트리밋 대응

def log(msg: str) -> None:
    print(f"[indexer {datetime.now():%Y-%m-%d %H:%M:%S}] {msg}")

def main() -> int:
    log("Full rebuild start")
    rows = fetch_all_rows()
    if not rows:
        log("No rows found")
        return 0

    docs = split_docs([row_to_doc(r) for r in rows])
    log(f"Rows={len(rows)} → Chunks={len(docs)}")

    total = 0
    for i in range(0, len(docs), BATCH_SIZE):
        batch = docs[i:i+BATCH_SIZE]
        # 첫 배치만 전체 삭제
        n = upsert_docs(batch, rebuild=(i == 0))
        total += n
        log(f"Upsert batch {i//BATCH_SIZE+1}: {n} chunks (cum {total})")
        if i + BATCH_SIZE < len(docs) and BATCH_SLEEP_SEC > 0:
            time.sleep(BATCH_SLEEP_SEC)

    log(f"Full rebuild done: chunks={total}")
    return total

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)
