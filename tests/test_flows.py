"""端到端流程测试：补报/更正、因子优先级、预览-确认一致性、发布可见性。"""
import os
import sqlite3
import threading

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


# ---------- 并发交错：预览/确认与补报、因子更正 ----------

def _raw_write(sql_and_params):
    """模拟另一人用独立连接提交写入；短超时，拿不到写锁即报错。"""
    conn = sqlite3.connect(os.environ["APP_DB"], timeout=0.2)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for sql, params in sql_and_params:
            conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def test_preview_reads_single_committed_snapshot(client, monkeypatch):
    """另一人「先改因子再补报电量」插在预览的两次读之间：
    预览不得出现旧因子配新电量，快照必须对应同一个已提交状态。"""
    upload(client, CSV1)
    add_factor(client, "national", 2026, 0.5)

    from app import services
    original = services.resolve_factor
    outcome = {"blocked": None}

    def interfering_resolve(conn, province, year):
        result = original(conn, province, year)  # 预览已读因子、尚未读电量
        if outcome["blocked"] is None:
            try:
                _raw_write([  # 另一人的两次提交：改因子 + 补报新表
                    ("UPDATE emission_factors SET factor=0.9 WHERE scope='national' AND year=2026", ()),
                    ("INSERT INTO usage_records VALUES('江苏','2026-03','M03',50,'t')", ()),
                ])
                outcome["blocked"] = False
            except sqlite3.OperationalError:
                outcome["blocked"] = True  # 写锁在预览事务手里，并发提交进不来
        return result

    monkeypatch.setattr(services, "resolve_factor", interfering_resolve)
    p = preview(client)

    assert outcome["blocked"] is True  # 预览事务期间并发写被挡在事务外
    assert [d["meter_no"] for d in p["details"]] == ["M01", "M02"]  # 没有混入新表
    assert {d["factor_value"] for d in p["details"]} == {0.5}       # 仍是旧因子

    # 预览事务结束后，另一人的提交正常入库，新预览看到一致的新状态
    _raw_write([
        ("UPDATE emission_factors SET factor=0.9 WHERE scope='national' AND year=2026", ()),
        ("INSERT INTO usage_records VALUES('江苏','2026-03','M03',50,'t')", ()),
    ])
    p2 = preview(client)
    assert [d["meter_no"] for d in p2["details"]] == ["M01", "M02", "M03"]
    assert {d["factor_value"] for d in p2["details"]} == {0.9}


def test_publish_validate_and_insert_are_atomic(client, monkeypatch):
    """更正赶在确认发布的「校验」与「入库」之间提交：
    不得让旧结果误发为最新版。"""
    upload(client, CSV1)
    add_factor(client, "national", 2026, 0.5)
    p1 = preview(client)

    from app import services
    original = services.compute_payload
    outcome = {"blocked": None}

    def interfering_compute(conn, province, month):
        payload = original(conn, province, month)  # 校验通过（与预览一致）
        if outcome["blocked"] is None:
            try:
                _raw_write([  # 更正赶在入库前试图提交
                    ("UPDATE usage_records SET kwh=999 WHERE province='江苏' AND month='2026-03' AND meter_no='M01'", ()),
                ])
                outcome["blocked"] = False
            except sqlite3.OperationalError:
                outcome["blocked"] = True
        return payload

    monkeypatch.setattr(services, "compute_payload", interfering_compute)
    r = publish(client, p1["preview_id"])
    assert r.status_code == 200
    assert outcome["blocked"] is True  # 校验与入库同一事务，更正进不来

    # 发布完成后再提交的更正正常入库；已发布内容不被修改
    upload(client, "省份,月份,表号,千瓦时\n江苏,2026-03,M01,999\n")
    pub = client.get("/api/published", params={"province": "江苏", "month": "2026-03"}).json()
    assert {d["meter_no"]: d["kwh"] for d in pub["details"]} == {"M01": 100, "M02": 200}

    # 更正已入库，原预览再确认必须 409
    r = publish(client, p1["preview_id"])
    assert r.status_code == 409
    assert "usage" in r.json()["detail"]["changed"]


def test_concurrent_previews_always_consistent(client):
    """压力交错：写线程反复「先改因子、再改电量」（两次独立提交），
    读线程连续预览；任何预览都不得出现旧因子配新电量。"""
    upload(client, CSV1)
    add_factor(client, "national", 2026, 1.0)

    from app import services
    from app.db import get_conn
    stop = threading.Event()
    errors = []

    def writer():
        i = 1
        while not stop.is_set():
            i += 1
            with get_conn() as c:  # 第一次提交：改因子为第 i 轮
                services.upsert_factor(c, "national", "", 2026, float(i))
            with get_conn() as c:  # 第二次提交：更正电量为第 i 轮
                services.upsert_usage(c, [
                    {"province": "江苏", "month": "2026-03", "meter_no": "M01", "kwh": 100.0 + i}])

    def reader():
        try:
            for _ in range(200):
                with get_conn() as c:
                    p = services.create_preview(c, "江苏", "2026-03")
                d = {x["meter_no"]: x for x in p["details"]}
                f_round = int(d["M01"]["factor_value"])      # 因子所属轮次
                u_round = int(d["M01"]["kwh"] - 100.0)       # 电量所属轮次
                if f_round < u_round:  # 旧因子配新电量：从未作为已提交状态存在
                    errors.append(f"因子第{f_round}轮 × 电量第{u_round}轮")
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    t = threading.Thread(target=writer)
    t.start()
    reader()
    stop.set()
    t.join()
    assert errors == []


def test_other_province_uploads_during_accounting(client):
    """逐表核算（预览/确认循环）期间，其他省月的补报照常入库。"""
    upload(client, CSV1)
    add_factor(client, "national", 2026, 0.5)
    errors = []

    def accountant():
        try:
            for _ in range(30):
                r = publish(client, preview(client)["preview_id"])
                assert r.status_code == 200, r.text
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    def reporter():
        try:
            for i in range(30):
                r = upload(client, f"省份,月份,表号,千瓦时\n浙江,2026-03,M{i:02d},{i}\n")
                assert r.status_code == 200, r.text
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    t1, t2 = threading.Thread(target=accountant), threading.Thread(target=reporter)
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert errors == []
    rows = client.get("/api/usage", params={"province": "浙江", "month": "2026-03"}).json()["rows"]
    assert len(rows) == 30  # 30 笔补报全部入库
