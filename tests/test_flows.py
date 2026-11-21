"""端到端流程测试：补报/更正、因子优先级、预览-确认一致性、发布可见性。"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB", str(tmp_path / "test.db"))
    from app.main import app
    with TestClient(app) as c:
        yield c


def upload(client, text):
    return client.post("/api/usage/upload", files={"file": ("u.csv", text.encode("utf-8"), "text/csv")})


def add_factor(client, scope, year, factor, province=""):
    return client.post("/api/factors", json={
        "scope": scope, "province": province, "year": year, "factor": factor})


def preview(client, province="江苏", month="2026-03"):
    r = client.post("/api/preview", json={"province": province, "month": month})
    assert r.status_code == 200, r.text
    return r.json()


def publish(client, preview_id):
    return client.post("/api/publish", json={"preview_id": preview_id})


CSV1 = "省份,月份,表号,千瓦时\n江苏,2026-03,M01,100\n江苏,2026-03,M02,200\n"


# ---------- 页面 ----------

def test_pages_render(client):
    assert client.get("/").status_code in (200, 307)
    for path in ("/upload", "/factors", "/preview", "/published"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "集团购电碳排放盘查" in r.text


# ---------- 上传：补报与更正 ----------

def test_upload_supplement_and_correction(client):
    r = upload(client, CSV1)
    assert r.status_code == 200 and r.json() == {"ok": True, "inserted": 2, "updated": 0}
    # 更正 M01，补报 M03
    r = upload(client, "省份,月份,表号,千瓦时\n江苏,2026-03,M01,150\n江苏,2026-03,M03,50\n")
    assert r.json() == {"ok": True, "inserted": 1, "updated": 1}
    rows = client.get("/api/usage", params={"province": "江苏", "month": "2026-03"}).json()["rows"]
    assert [(x["meter_no"], x["kwh"]) for x in rows] == [("M01", 150), ("M02", 200), ("M03", 50)]


def test_upload_bad_csv_rejected_atomically(client):
    r = upload(client, "省份,月份,表号,千瓦时\n江苏,2026-13,M01,100\n江苏,2026-03,M02,200\n")
    assert r.status_code == 422
    assert client.get("/api/usage").json()["rows"] == []  # 有错则整批不写入


# ---------- 因子优先级与缺失 ----------

def test_missing_factors_block_publish(client):
    upload(client, CSV1)
    p = preview(client)
    assert p["publishable"] is False
    assert len(p["missing_factors"]) == 2  # 省级、全国都缺
    r = publish(client, p["preview_id"])
    assert r.status_code == 422 and "暂缓发布" in r.json()["detail"]["error"]


def test_factor_priority_provincial_over_national(client):
    upload(client, CSV1)
    add_factor(client, "national", 2026, 0.5)
    p = preview(client)
    assert {d["factor_source"] for d in p["details"]} == {"national"}
    add_factor(client, "province", 2026, 0.8, province="江苏")
    p = preview(client)
    assert {d["factor_source"] for d in p["details"]} == {"province"}
    assert p["details"][0]["factor_value"] == 0.8
    assert p["total_emissions"] == round(300 * 0.8, 6)


# ---------- 核心情形：预览后补录等值省级因子再确认 ----------

def test_preview_then_add_equal_provincial_factor_requires_repreview(client):
    upload(client, CSV1)
    add_factor(client, "national", 2026, 0.5)
    p1 = preview(client)
    assert p1["publishable"] and p1["total_emissions"] == 150.0
    assert {d["factor_source"] for d in p1["details"]} == {"national"}

    # 补录与全国因子等值的省级因子：总量不变，但所用因子来源变了
    add_factor(client, "province", 2026, 0.5, province="江苏")
    r = publish(client, p1["preview_id"])
    assert r.status_code == 409
    assert "factor" in r.json()["detail"]["changed"]

    # 重新预览后确认成功，已发明细使用省级因子
    p2 = preview(client)
    assert {d["factor_source"] for d in p2["details"]} == {"province"}
    r = publish(client, p2["preview_id"])
    assert r.status_code == 200
    pub = client.get("/api/published", params={"province": "江苏", "month": "2026-03"}).json()
    assert {d["factor_source"] for d in pub["details"]} == {"province"}
    assert pub["total_emissions"] == 150.0  # 总量相同，来源已变


def test_same_total_usage_correction_requires_repreview(client):
    upload(client, CSV1)
    add_factor(client, "national", 2026, 0.5)
    p1 = preview(client)
    # 更正为 150/150：逐表变了、总量仍是 300
    upload(client, "省份,月份,表号,千瓦时\n江苏,2026-03,M01,150\n江苏,2026-03,M02,150\n")
    r = publish(client, p1["preview_id"])
    assert r.status_code == 409
    assert "usage" in r.json()["detail"]["changed"]


def test_unrelated_changes_do_not_invalidate_preview(client):
    upload(client, CSV1)
    add_factor(client, "national", 2026, 0.5)
    p1 = preview(client)
    # 其他省份、其他月份、其他年份因子的改动不影响本次确认
    upload(client, "省份,月份,表号,千瓦时\n浙江,2026-03,M09,999\n江苏,2026-04,M01,777\n")
    add_factor(client, "national", 2025, 0.9)
    add_factor(client, "province", 2026, 0.6, province="浙江")
    r = publish(client, p1["preview_id"])
    assert r.status_code == 200


# ---------- 发布可见性 ----------

def test_old_publication_visible_until_new_one_succeeds(client):
    upload(client, CSV1)
    add_factor(client, "national", 2026, 0.5)
    publish(client, preview(client)["preview_id"])
    pub_v1 = client.get("/api/published", params={"province": "江苏", "month": "2026-03"}).json()
    assert {d["meter_no"]: d["kwh"] for d in pub_v1["details"]} == {"M01": 100, "M02": 200}

    # 更正用电并生成新预览，但尚未确认发布：已发明细仍是旧版
    upload(client, "省份,月份,表号,千瓦时\n江苏,2026-03,M01,999\n")
    p2 = preview(client)
    pub = client.get("/api/published", params={"province": "江苏", "month": "2026-03"}).json()
    assert pub["id"] == pub_v1["id"]
    assert {d["meter_no"]: d["kwh"] for d in pub["details"]} == {"M01": 100, "M02": 200}

    # 确认新发布后，查询切换到新版
    publish(client, p2["preview_id"])
    pub = client.get("/api/published", params={"province": "江苏", "month": "2026-03"}).json()
    assert pub["id"] != pub_v1["id"]
    assert {d["meter_no"]: d["kwh"] for d in pub["details"]} == {"M01": 999, "M02": 200}


def test_factor_value_change_invalidates_preview(client):
    upload(client, CSV1)
    add_factor(client, "national", 2026, 0.5)
    p1 = preview(client)
    add_factor(client, "national", 2026, 0.55)  # 同来源、数值变了
    r = publish(client, p1["preview_id"])
    assert r.status_code == 409
    assert "factor" in r.json()["detail"]["changed"]
