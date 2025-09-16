# scripts/run_crawler.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from uosai.crawler.notice_crawler import main

if __name__ == "__main__":
    raise SystemExit(main())
