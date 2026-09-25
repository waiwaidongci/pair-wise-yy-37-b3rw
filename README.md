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
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

许可续期（补续期办理）：

- `POST /api/items/{id}/renewals`：申请人发起续期申请，填写`proposed_capacity`（拟变更产能）和`effective_date`（生效日期）；一个许可单同时只能有一件未结束申请
- `GET /api/items/{id}/renewals`：查询该许可单全部续期申请（含已通过的历史申请）
- `GET /api/renewals/{id}`：申请详情，含材料清单和缺失必备材料
- `POST /api/renewals/{id}/materials`：补充材料，`kind`为`test_report`（检测报告）、`facility_operation_record`（治理设施运行记录）或`other`
- `POST /api/renewals/{id}/amend`：草稿或退回状态下修改拟变更产能和生效日期，必须提交`expected_version`
- `POST /api/renewals/{id}/submit`：提交复核，必须提交`expected_version`；检测报告和治理设施运行记录缺一不能提交
- `POST /api/renewals/{id}/review`：合规经理复核，必须提交`expected_version`；产能超许可量或仍有未关闭事项时退回补充且必须填写`comment`，通过后签发新许可版本和到期日

允许角色：applicant, inspector, compliance_manager, viewer。申报量超过许可量或检查发现高严重度问题时提高优先级；存在未关闭整改时不能批准。续期通过后许可版本号递增，到期日按生效日期顺延5年，历史申请仍可查询。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
