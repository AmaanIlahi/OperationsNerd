import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi.testclient import TestClient
from db import db as d
from main import app
from packs.validate import check_all
d.init_db(reset=True)
c = TestClient(app)
os.environ.pop("OPERATIONS_API_KEY", None)
assert c.get("/pack-health").status_code == 200 and all(p["ok"] for p in c.get("/pack-health").json())
assert {p["id"] for p in check_all()} >= {"realestate", "healthclub"}
os.environ["OPERATIONS_API_KEY"] = "k1"
assert c.get("/").status_code == 200
assert c.get("/pack-health").status_code == 401
assert c.get("/pack-health", headers={"X-API-Key": "bad"}).status_code == 401
assert c.get("/pack-health", headers={"X-API-Key": "k1"}).status_code == 200
assert c.post("/webhooks/1/x", content=b"{}").status_code == 401  # open to the gate, rejected by signature
os.environ.pop("OPERATIONS_API_KEY")
print("auth+packs OK")
