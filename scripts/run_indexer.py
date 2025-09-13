# scripts/run_indexer.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from uosai.indexer.index import main

if __name__ == "__main__":
    main()
