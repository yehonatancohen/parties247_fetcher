"""Manual backfill: python producer_sweep.py [--all]. Logic lives in producers.py."""
import os
import sys

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
from producers import ensure_producers

db = MongoClient(os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or os.getenv("MONGODB_URL"))[os.getenv("MONGODB_DB_NAME")]
print(ensure_producers(db, refetch_all="--all" in sys.argv))
