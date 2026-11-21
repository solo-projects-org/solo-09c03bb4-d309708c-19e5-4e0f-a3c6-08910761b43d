# 集团购电碳排放盘查发布

面向集团碳盘查员的网页工具：按省份和月份发布购电排放明细。FastAPI + SQLite，Docker Compose 一键启动。

## 启动

```bash
docker compose up --build
# 打开 http://localhost:8000 （自动跳转「预览与发布」）
```

SQLite 数据持久化在命名卷 `carbon-data`（容器内 `/data/app.db`，可用环境变量 `APP_DB` 改路径）。

本地开发（不用 Docker）：

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload
.venv/bin/python -m pytest tests/   # 跑测试
```

## 功能与规则

- **用电补报/更正**（`/upload`）：上传 CSV，表头 `省份,月份,表号,千瓦时`（月份 YYYY-MM，UTF-8）。同一 省份+月份+表号 重复上传即更正，新表号即补报；有错行则整批拒绝。
- **排放因子维护**（`/factors`）：省级因子按 省份+年度、全国因子按 年度维护（单位 kg CO₂/kWh），可增改删。
- **预览与发布**（`/preview`）：选省份和月份，预览逐表排放与所用因子。
  - 因子优先级：**同年省级因子 → 同年全国因子 → 两者都缺则提示补录、暂缓发布**（确认按钮禁用，API 返回 422）。
  - **确认一致性**：预览时服务端保存快照（当期逐表用电明细 + 所用因子来源/数值）。确认时重算比对，只要用电明细或所用因子的来源、数值与预览不符即拒绝（HTTP 409），要求重新预览——**即使排放总量相同**（例如补录了与全国因子等值的省级因子，来源由全国变省级，也必须重新预览）。其他省份/月份/年份的改动不影响本快照，无需重新预览。
- **已发布查询**（`/published`）：查看每期最新发布的逐表排放与所用因子。发布为单事务写入新快照，旧发布记录不被修改，**新结果发布成功前原已发内容照常可查**。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/usage/upload` | 上传用电 CSV（multipart，字段名 `file`） |
| GET | `/api/usage?province=&month=` | 查询当前用电明细 |
| POST | `/api/factors` | 新增/更新因子 `{scope, province, year, factor}` |
| GET | `/api/factors?year=` | 因子列表 |
| DELETE | `/api/factors?scope=&province=&year=` | 删除因子 |
| POST | `/api/preview` | 生成预览 `{province, month}`，返回 `preview_id` |
| POST | `/api/publish` | 确认发布 `{preview_id}`；409=须重新预览，422=缺因子暂缓 |
| GET | `/api/published?province=&month=` | 该期最新发布明细 |
| GET | `/api/published/list` | 全部已发布期次 |

## 目录结构

```
app/
  main.py        # 路由：页面 + JSON API，CSV 解析
  services.py    # 业务：补报/更正、因子优先级、预览快照、确认发布
  db.py          # SQLite 连接与建表
  templates/     # 四个页面
tests/test_flows.py  # 端到端流程测试
Dockerfile / docker-compose.yml
```
