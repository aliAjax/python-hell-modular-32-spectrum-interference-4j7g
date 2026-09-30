# 无线电频谱干扰调查与协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8332`。

模块结构：`app.py` 负责组装，`src/domain.py` 定义字段和错误，`src/rules.py` 负责评估、定位、授权和状态机，`src/repository.py` 管理 SQLite、版本和审计链，`src/service.py` 编排权限，`src/http_api.py` 提供接口，`src/audit.py` 生成审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8332
python3 -m unittest discover -s tests -v
```

使用 `X-User-Id`、`X-Role`、`X-Region` 请求头。接口为 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/measurements`（测量批次上报，同站点/频点/观测时刻自动归并，`batch_number` 支持失败重试不新增记录）、`POST /api/items/<id>/batches/<batch_id>/merge`（并发修改的字段级合并，未冲突字段保留、冲突项列出待选）、`POST /api/items/<id>/actions`（含跨区 `submit_opinion` 处置意见）和 `GET /api/items/<id>/audit`。测试覆盖完整调查流程、测量更正、重复事件、跨区越权、定位置信度、版本冲突、批次归并与失效、并发三向合并和批次号幂等重试。协议接入、真实无线电传播模型和执法权限仍需由外部系统实现。
