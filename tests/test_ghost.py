from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from ghost.discover import discover
from ghost.probe import probe_all
from ghost.report import to_dict

app = FastAPI()


@app.get("/items/{item_id}", responses={404: {"description": "missing"}})
def get_item(item_id: int):
    if item_id == 1:
        return {"id": 1}
    raise HTTPException(404, "nope")


@app.get("/boom")
def boom():
    raise HTTPException(500, "bad")


@app.post("/items")
def create(item: dict):
    return item


def _client():
    return TestClient(app, base_url="http://t")


def test_discover_finds_endpoints():
    with _client() as c:
        eps = discover("http://t", c)
    assert {(e.method, e.path) for e in eps} == {
        ("GET", "/items/{item_id}"), ("GET", "/boom"), ("POST", "/items"),
    }


def test_probe_skips_writes_and_flags_5xx():
    with _client() as c:
        results = probe_all("http://t", discover("http://t", c), c, samples=2)
    report = {(e["method"], e["path"]): e for e in to_dict(results)["endpoints"]}
    assert report[("POST", "/items")]["skipped"]
    assert "5XX" in report[("GET", "/boom")]["flags"]
    assert report[("GET", "/items/{item_id}")]["observed_codes"] == {"200": 2}
