# 空气污染源许可与合规检查

管理设施、排放口、治理设备、现场检查、整改和许可续期。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8314
```

默认端口为`8314`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/records/{record_id}/close`，关闭整改/检查事项
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

许可续期：

- `POST /api/items/{id}/renewals`：申请人发起续期，填写`proposed_capacity`（拟变更产能）、`effective_date`（生效日期，YYYY-MM-DD）、`materials`（材料清单，每项含`kind`/`detail`），一个许可单只允许一件未结束申请（draft/submitted/returned）。
- `POST /api/renewals/{id}/update`：仅draft或returned状态可补充修改，必须提交`expected_version`。
- `POST /api/renewals/{id}/submit`：提交复核，材料清单中必须同时含`test_report`（检测报告）和`facility_operation`（治理设施运行记录），否则退回409。
- `POST /api/renewals/{id}/review`：合规管理员（compliance_manager）复核，必须提交`expected_version`。拟变更产能超过许可量（threshold）或存在未关闭事项时退回`returned`，`comment`为必填意见；否则通过`approved`，生成新许可版本和到期日（生效日期+5年），见`GET /api/items/{id}/permits`。
- `GET /api/items/{id}/renewals`、`GET /api/renewals/{id}`：续期申请全程可查，已结束的旧申请保留。
- 续期状态机：`draft → submitted →（returned → submitted）* → approved`，申请人负责发起/补充/提交，合规管理员负责复核。

允许角色：applicant, inspector, compliance_manager, viewer。申报量超过许可量或检查发现高严重度问题时提高优先级；存在未关闭整改时不能批准。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
