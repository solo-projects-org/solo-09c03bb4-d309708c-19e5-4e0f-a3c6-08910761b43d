"""FastAPI 入口：网页 + JSON API。"""
import csv
import io
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import services
from .db import get_conn, init_db

PROVINCES = [
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江",
    "上海", "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南",
    "湖北", "湖南", "广东", "广西", "海南", "重庆", "四川", "贵州",
    "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆",
]

# CSV 表头别名 -> 内部字段
HEADER_ALIASES = {
    "省份": "province", "province": "province",
    "月份": "month", "month": "month",
    "表号": "meter_no", "电表号": "meter_no", "meter_no": "meter_no", "meter": "meter_no",
    "千瓦时": "kwh", "用电量": "kwh", "kwh": "kwh",
}

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="集团购电碳排放盘查发布", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def page(request: Request, name: str):
    return templates.TemplateResponse(request, name, {"provinces": PROVINCES})


# ---------------- 页面 ----------------

@app.get("/", response_class=HTMLResponse)
def index():
    return RedirectResponse("/preview")


@app.get("/upload", response_class=HTMLResponse)
def page_upload(request: Request):
    return page(request, "upload.html")


@app.get("/factors", response_class=HTMLResponse)
def page_factors(request: Request):
    return page(request, "factors.html")


@app.get("/preview", response_class=HTMLResponse)
def page_preview(request: Request):
    return page(request, "preview.html")


@app.get("/published", response_class=HTMLResponse)
def page_published(request: Request):
    return page(request, "published.html")


# ---------------- 用电明细 ----------------

def parse_usage_csv(raw: bytes) -> list[dict]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(422, "CSV 须为 UTF-8 编码")
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        raise HTTPException(422, "CSV 为空")
    header = [HEADER_ALIASES.get(c.strip()) for c in rows[0]]
    needed = {"province", "month", "meter_no", "kwh"}
    if not needed.issubset({h for h in header if h}):
        raise HTTPException(422, "表头须包含：省份,月份,表号,千瓦时")
    out, errors = [], []
    for i, r in enumerate(rows[1:], start=2):
        rec = dict(zip(header, [c.strip() for c in r]))
        rec = {k: v for k, v in rec.items() if k}
        try:
            province, meter_no = rec["province"], rec["meter_no"]
            month = rec["month"]
            if not services.MONTH_RE.match(month):
                raise ValueError(f"月份格式应为 YYYY-MM：{month!r}")
            if not province or not meter_no:
                raise ValueError("省份/表号不能为空")
            kwh = float(rec["kwh"])
            if kwh < 0:
                raise ValueError("千瓦时不能为负")
        except (KeyError, ValueError) as e:
            errors.append(f"第{i}行：{e}")
            continue
        out.append({"province": province, "month": month, "meter_no": meter_no, "kwh": kwh})
    if errors:
        raise HTTPException(422, detail={"error": "CSV 存在错误行，未写入任何数据", "rows": errors})
    if not out:
        raise HTTPException(422, "CSV 没有数据行")
    return out


@app.post("/api/usage/upload")
async def upload_usage(file: UploadFile):
    rows = parse_usage_csv(await file.read())
    with get_conn() as conn:
        result = services.upsert_usage(conn, rows)
    return {"ok": True, **result}


@app.get("/api/usage")
def api_list_usage(province: str | None = None, month: str | None = None):
    with get_conn() as conn:
        return {"rows": services.list_usage(conn, province, month)}


# ---------------- 排放因子 ----------------

class FactorIn(BaseModel):
    scope: str = Field(pattern="^(province|national)$")
    province: str = ""
    year: int
    factor: float = Field(gt=0)


@app.post("/api/factors")
def api_upsert_factor(body: FactorIn):
    with get_conn() as conn:
        return {"ok": True, **services.upsert_factor(
            conn, body.scope, body.province.strip(), body.year, body.factor)}


@app.get("/api/factors")
def api_list_factors(year: int | None = None):
    with get_conn() as conn:
        return {"rows": services.list_factors(conn, year)}


@app.delete("/api/factors")
def api_delete_factor(scope: str, year: int, province: str = ""):
    with get_conn() as conn:
        if not services.delete_factor(conn, scope, province, year):
            raise HTTPException(404, "因子不存在")
    return {"ok": True}


# ---------------- 预览 / 发布 ----------------

class PreviewIn(BaseModel):
    province: str
    month: str


@app.post("/api/preview")
def api_preview(body: PreviewIn):
    if not body.province:
        raise HTTPException(422, "请选择省份")
    with get_conn() as conn:
        return services.create_preview(conn, body.province, body.month)


class PublishIn(BaseModel):
    preview_id: int


@app.post("/api/publish")
def api_publish(body: PublishIn):
    with get_conn() as conn:
        return services.publish(conn, body.preview_id)


@app.get("/api/published")
def api_published(province: str, month: str):
    with get_conn() as conn:
        pub = services.latest_publication(conn, province, month)
    if not pub:
        raise HTTPException(404, "该期尚未发布")
    return pub


@app.get("/api/published/list")
def api_published_list():
    with get_conn() as conn:
        return {"rows": services.list_publications(conn)}
