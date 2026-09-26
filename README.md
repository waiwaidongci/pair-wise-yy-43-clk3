# 溢油应急响应与任务追踪

围控、回收、岸线保护和废弃物处置任务，按证据和监测结果闭环。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：演示页（事件占用、资源台账、调派/撤收）。
- `tests/`：完整流程、规则、失败、调派台账和HTTP测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8320
```

默认端口为`8320`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`（含`active_resource_count`和`active_callsigns`，即每单当前占用）
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

调派台账：

- `POST /api/resources`：登记资源，字段`callsign`（全局唯一呼号）、`resource_type`（containment_vessel围控船/recovery_team回收组/monitoring_personnel监测人员）、`home_area`（值守区域）。
- `GET /api/resources`：资源视图，含`occupied`、`current_area`、`current_item_id`。
- `POST /api/items/{id}/assignments`：调派，字段`callsign`、`work_area`、`person_in_charge`（负责人）、`task`（现场任务）、`planned_start`/`planned_end`（计划起止，ISO8601）、可选`external_ref`（幂等号；误报重复调派冲突时不会写入半条记录）。
- `GET /api/items/{item}/assignments`：该单调派台账（在岗与撤收历史均保留），可`?status=active|released`过滤；`GET /api/assignments`为全局视图，可按`item_id`、`status`过滤。
- `POST /api/assignments/{id}/release`：撤收，字段`actual_minutes`（实际用时，分钟；留空按调派时刻自动计算）和可选`release_note`。

台账规则（见`src/rules.py`）：同一资源同一时段只能绑定一个在岗调派（SQLite部分唯一索引`ux_resource_active_assignment`强约束，未撤收改派返回409）；跨区域调派时`current_area`立即变为任务区域、原区域视为释放，撤收后恢复归属区域；撤收记录保留，撤收后可再次调派；重复撤收返回409。事件转`closed`时若仍有资源在岗，返回409，响应体`details.callsigns`列出未撤收呼号（`details.reason=resources_still_active`）。登记/调派/撤收均写入审计链。

允许角色：observer, response_commander, operations, viewer。资源登记允许 response_commander/operations，调派仅 response_commander，撤收允许 response_commander/operations。估算油量、海况和未完成任务数影响响应等级；关闭前必须完成回收和岸线监测记录、且所有资源已撤收。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
