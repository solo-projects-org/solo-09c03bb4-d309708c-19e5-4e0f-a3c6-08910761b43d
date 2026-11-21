"""核心业务：用电补报/更正、因子维护、预览快照、确认发布。

预览快照（payload）只包含「该省该月」的发布输入：
  - 当期逐表用电明细（表号、千瓦时）
  - 所用因子的来源（省级/全国）与数值
确认发布时重算当前 payload 与预览快照逐字节比对：
  - 不一致 -> 必须重新预览（哪怕排放总量相同）
  - 其他省份/月份/年份的数据变动不影响本快照，无需重新预览

事务约定（配合 db.tx）：
  - create_preview：读用电、读因子、存快照在同一个 IMMEDIATE 事务内完成，
    预览的电量与所用因子必然对应同一个已提交状态；
  - publish：校验（重算比对）与入库在同一个 IMMEDIATE 事务内完成，
    提交前其他写者无法插入，杜绝「校验通过后、入库前被更正钻空」；
  - 事务只在单次请求内存活（毫秒级），不跨请求持锁，
    核算某省月期间其他省月的补报/因子维护照常入库（WAL 下读不阻塞写）。
"""
import json
import re
from datetime import datetime
from sqlite3 import Connection

from fastapi import HTTPException

from .db import tx

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
EMISSIONS_DIGITS = 6  # kg CO2 保留小数位，保证快照可稳定比对


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def validate_month(month: str) -> str:
    if not MONTH_RE.match(month or ""):
        raise HTTPException(422, f"月份格式应为 YYYY-MM：{month!r}")
    return month


def year_of(month: str) -> int:
    return int(month[:4])


# ---------- 用电明细 ----------

def upsert_usage(conn: Connection, rows: list[dict]) -> dict:
    """按 (省份, 月份, 表号) 覆盖写入：新键为补报，已有键为更正。整批单事务。"""
    inserted, updated = 0, 0
    ts = now_iso()
    with tx(conn):
        for r in rows:
            cur = conn.execute(
                "UPDATE usage_records SET kwh=?, updated_at=? "
                "WHERE province=? AND month=? AND meter_no=?",
                (r["kwh"], ts, r["province"], r["month"], r["meter_no"]),
            )
            if cur.rowcount:
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO usage_records(province, month, meter_no, kwh, updated_at)"
                    " VALUES (?,?,?,?,?)",
                    (r["province"], r["month"], r["meter_no"], r["kwh"], ts),
                )
                inserted += 1
    return {"inserted": inserted, "updated": updated}


def list_usage(conn: Connection, province: str | None, month: str | None) -> list[dict]:
    sql = "SELECT province, month, meter_no, kwh, updated_at FROM usage_records"
    cond, args = [], []
    if province:
        cond.append("province=?"); args.append(province)
    if month:
        cond.append("month=?"); args.append(month)
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY province, month, meter_no"
    return [dict(r) for r in conn.execute(sql, args)]


# ---------- 排放因子 ----------

def upsert_factor(conn: Connection, scope: str, province: str, year: int, factor: float) -> dict:
    if scope not in ("province", "national"):
        raise HTTPException(422, "scope 须为 province 或 national")
    if scope == "province" and not province:
        raise HTTPException(422, "省级因子必须指定省份")
    if scope == "national":
        province = ""
    if not (2000 <= year <= 2100):
        raise HTTPException(422, "年份超出合理范围")
    if factor <= 0:
        raise HTTPException(422, "排放因子必须为正数")
    with tx(conn):
        conn.execute(
            "INSERT INTO emission_factors(scope, province, year, factor, updated_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(scope, province, year) DO UPDATE SET factor=excluded.factor, updated_at=excluded.updated_at",
            (scope, province, year, factor, now_iso()),
        )
    return {"scope": scope, "province": province, "year": year, "factor": factor}


def list_factors(conn: Connection, year: int | None = None) -> list[dict]:
    sql = "SELECT scope, province, year, factor, updated_at FROM emission_factors"
    args: list = []
    if year:
        sql += " WHERE year=?"; args.append(year)
    sql += " ORDER BY year, scope, province"
    return [dict(r) for r in conn.execute(sql, args)]


def delete_factor(conn: Connection, scope: str, province: str, year: int) -> bool:
    if scope == "national":
        province = ""
    with tx(conn):
        cur = conn.execute(
            "DELETE FROM emission_factors WHERE scope=? AND province=? AND year=?",
            (scope, province, year),
        )
    return cur.rowcount > 0


def resolve_factor(conn: Connection, province: str, year: int) -> dict | None:
    """优先同年省级因子，其次同年全国因子；都缺返回 None。"""
    row = conn.execute(
        "SELECT factor FROM emission_factors WHERE scope='province' AND province=? AND year=?",
        (province, year),
    ).fetchone()
    if row:
        return {"source": "province", "value": row["factor"], "year": year}
    row = conn.execute(
        "SELECT factor FROM emission_factors WHERE scope='national' AND year=?",
        (year,),
    ).fetchone()
    if row:
        return {"source": "national", "value": row["factor"], "year": year}
    return None


# ---------- 预览与发布 ----------

def compute_payload(conn: Connection, province: str, month: str) -> dict:
    """计算当期发布输入的规范快照。因子缺失时逐表因子/排放为 null，publishable=False。"""
    validate_month(month)
    year = year_of(month)
    factor = resolve_factor(conn, province, year)
    usage = conn.execute(
        "SELECT meter_no, kwh FROM usage_records WHERE province=? AND month=? ORDER BY meter_no",
        (province, month),
    ).fetchall()
    details = []
    total_kwh = 0.0
    total_emissions = 0.0
    for u in usage:
        kwh = float(u["kwh"])
        emissions = round(kwh * factor["value"], EMISSIONS_DIGITS) if factor else None
        total_kwh += kwh
        total_emissions += emissions or 0.0
        details.append({
            "meter_no": u["meter_no"],
            "kwh": kwh,
            "factor_source": factor["source"] if factor else None,
            "factor_value": factor["value"] if factor else None,
            "factor_year": year if factor else None,
            "emissions": emissions,
        })
    missing = []
    if usage and not factor:
        missing = [
            {"scope": "province", "province": province, "year": year},
            {"scope": "national", "province": "", "year": year},
        ]
    return {
        "province": province,
        "month": month,
        "year": year,
        "details": details,
        "total_kwh": round(total_kwh, EMISSIONS_DIGITS),
        "total_emissions": round(total_emissions, EMISSIONS_DIGITS),
        "missing_factors": missing,
        "publishable": bool(usage) and not missing,
    }


def _canonical(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def create_preview(conn: Connection, province: str, month: str) -> dict:
    # 单个 IMMEDIATE 事务内完成「读用电 + 读因子 + 存快照」：
    # 预览的电量与所用因子对应同一个已提交状态，
    # 期间其他连接的改因子/补报提交不进来，不会出现「旧因子算新电量」。
    with tx(conn):
        payload = compute_payload(conn, province, month)
        cur = conn.execute(
            "INSERT INTO previews(province, month, payload, publishable, created_at)"
            " VALUES (?,?,?,?,?)",
            (province, month, _canonical(payload), int(payload["publishable"]), now_iso()),
        )
        preview_id = cur.lastrowid
    return {"preview_id": preview_id, **payload}


def _change_reasons(old: dict, new: dict) -> list[str]:
    reasons = set()
    old_map = {d["meter_no"]: d for d in old["details"]}
    new_map = {d["meter_no"]: d for d in new["details"]}
    if set(old_map) != set(new_map):
        reasons.add("usage")
    for meter in set(old_map) & set(new_map):
        o, n = old_map[meter], new_map[meter]
        if o["kwh"] != n["kwh"]:
            reasons.add("usage")
        if (o["factor_source"], o["factor_value"]) != (n["factor_source"], n["factor_value"]):
            reasons.add("factor")
    if old.get("missing_factors") != new.get("missing_factors"):
        reasons.add("factor")
    return sorted(reasons)


def publish(conn: Connection, preview_id: int) -> dict:
    # 校验与入库在同一个 IMMEDIATE 事务：自 BEGIN 起其他写者无法提交，
    # 因此「确认时比对的状态」就是「发布入库时的状态」，
    # 赶在确认前后提交的更正要么被看到（409 要求重新预览）、要么等发布完成后才入库。
    with tx(conn):
        row = conn.execute("SELECT * FROM previews WHERE id=?", (preview_id,)).fetchone()
        if not row:
            raise HTTPException(404, "预览不存在，请重新预览")
        old_payload = json.loads(row["payload"])
        current = compute_payload(conn, row["province"], row["month"])
        if _canonical(current) != row["payload"]:
            # 当期用电明细或所用因子来源/数值已变化（与总量是否相同无关）
            raise HTTPException(409, detail={
                "error": "预览后数据已变化，须重新预览后再确认",
                "changed": _change_reasons(old_payload, current),
            })
        if not current["publishable"]:
            year = current["year"]
            raise HTTPException(422, detail={
                "error": f"缺少 {current['province']} {year} 年省级及全国排放因子，请先补录，暂缓发布",
                "missing_factors": current["missing_factors"],
            })
        # 单事务写入新发布快照；旧发布记录不被修改，提交前旧数据照常可查
        cur = conn.execute(
            "INSERT INTO publications(province, month, preview_id, total_kwh, total_emissions, published_at)"
            " VALUES (?,?,?,?,?,?)",
            (current["province"], current["month"], preview_id,
             current["total_kwh"], current["total_emissions"], now_iso()),
        )
        pub_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO publication_details"
            "(publication_id, meter_no, kwh, factor_source, factor_value, factor_year, emissions)"
            " VALUES (?,?,?,?,?,?,?)",
            [(pub_id, d["meter_no"], d["kwh"], d["factor_source"],
              d["factor_value"], d["factor_year"], d["emissions"]) for d in current["details"]],
        )
    return {"publication_id": pub_id, **current}


def latest_publication(conn: Connection, province: str, month: str) -> dict | None:
    # 单读事务：发布表头与逐表明细取自同一快照
    with tx(conn, "DEFERRED"):
        pub = conn.execute(
            "SELECT * FROM publications WHERE province=? AND month=? ORDER BY id DESC LIMIT 1",
            (province, month),
        ).fetchone()
        if not pub:
            return None
        details = conn.execute(
            "SELECT meter_no, kwh, factor_source, factor_value, factor_year, emissions"
            " FROM publication_details WHERE publication_id=? ORDER BY meter_no",
            (pub["id"],),
        ).fetchall()
    return {**dict(pub), "details": [dict(d) for d in details]}


def list_publications(conn: Connection) -> list[dict]:
    """每个 省+月 的最新一条发布。"""
    rows = conn.execute(
        "SELECT p.* FROM publications p"
        " JOIN (SELECT province, month, MAX(id) AS max_id FROM publications GROUP BY province, month) t"
        " ON p.id = t.max_id ORDER BY p.month DESC, p.province"
    ).fetchall()
    return [dict(r) for r in rows]
