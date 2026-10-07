# 地震台网事件编目与修订

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8307`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8307
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `station`：观测台站；`event`：地震事件及其多个修订版本。
- `subscription`：下游订阅（`subscriber`+`endpoint`），`active`/`disabled`。
- `delivery`：投递对账记录（非实体，存于`deliveries`表），按`事件编号+版本号+订阅方+类型`唯一。

## 投递与撤回对账

- 事件`publish`/`revise`时，按活跃订阅为每家建一条`publish`投递记录，带事件编号和版本号。
- 投递通过`dispatch`推进：`pending`→`delivered`/`failed`；失败后重试沿用同一条记录（`attempts`累加），已送达后再重试是幂等空操作，下游不重复收到。
- `revise`后，未送出（`pending`/`failed`）的旧版本投递立刻作废（`void`），已送达的保留为历史。
- `withdraw`进入`withdrawing`：未送出的发布投递作废，并为每家已送达的订阅方建`withdraw`通知；通知送出（`sent`）后等待回执（`ack`→`acknowledged`），回执齐了事件才转为`withdrawn`（`settle_withdraw`）。
- 同一版本上并发的撤回和修订，晚到的修订被乐观锁或状态机挡回（409）；`station`角色撤回会被拒绝（403）。
- 旧数据没有投递记录：服务启动时执行一次性回填（`meta`表防重），已发布/已撤回事件按当前版本补记为`delivered`，已撤回事件同时补记已确认回执。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/events/<id>/deliveries`：事件的投递对账视图（记录+按状态汇总+撤回是否完成）。
- `GET /api/deliveries/<id>`：读取单条投递记录。
- `POST /api/deliveries/<id>/actions`：提交`{"action":"dispatch","data":{"outcome":"delivered|failed|sent"}}`或`{"action":"ack","data":{"receipt":"..."}}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

事件关联使用简化时间差和距离阈值，不包含完整地震定位、震级标定或台站仪器响应。
