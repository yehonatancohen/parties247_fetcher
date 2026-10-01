"""Print the producer ranking: python producer_report.py"""
import os

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
from producers import get_producer_report

db = MongoClient(os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or os.getenv("MONGODB_URL"))[os.getenv("MONGODB_DB_NAME")]
rep = get_producer_report(db, limit=25)
print(f"unknown-producer tickets: {rep['unknown_tickets']}")
for r in rep["rows"]:
    print(f"{r['tickets']:>5} {r['events']:>4} {r['revenue']:>8.0f}  {r['producer']}")
