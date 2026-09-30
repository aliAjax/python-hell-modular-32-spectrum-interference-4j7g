# 无线电频谱干扰调查与协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8332`。

模块结构：`app.py` 负责组装，`src/domain.py` 定义字段和错误，`src/rules.py` 负责评估、定位、授权和状态机，`src/batches.py` 负责测量批次归并、三向合并和失效联动，`src/repository.py` 管理 SQLite、版本和审计链，`src/service.py` 编排权限，`src/http_api.py` 提供接口，`src/audit.py` 生成审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8332
python3 -m unittest discover -s tests -v
```

使用 `X-User-Id`、`X-Role`、`X-Region` 请求头。

## 案件接口

- `GET /health`、`GET /api/state`
- `POST /api/items`：立案；带 `batch_no` 时从测量批次带入站点、频点、强度和带宽，可再带 `target_region` 指定目标辖区
- `GET /api/items`、`GET /api/items/<id>`、`GET /api/items/<id>/audit`
- `POST /api/items/<id>/sources`
- `POST /api/items/<id>/actions`，body 中 `action` 取值：
  - `assess`、`locate`（置信度需 ≥ 0.6）、`correct_measurement`
  - `suspend` / `resolve`：**只能由目标辖区**（`target_region`，缺省为测量辖区）的 `coordinator`/`regulator` 确认，需 `expected_version`
  - `coordinate` / `propose` / `cancel`：由**原辖区**（`region`）发起；`propose` 仅追加处置意见，不占用版本号，也不要求 `expected_version`
  - 越权/越区提交不改动原记录，只在审计链追加 `action_denied` 留痕

## 测量批次接口

- `POST /api/batches/reports`：监测站上报。按 `station_id + frequency_mhz + observed_at` 归并，重复上报（`reporter + strength_dbm + note` 相同）原样返回 `outcome=duplicate`，不升版本、不触发失效；自然键命中且内容不同时归入同一批次并触发失效，`outcome` 为 `created`/`merged`/`retried`/`duplicate`
  - 写入失败后可用原 `batch_no` 重试，服务端原样返回已有批次（`outcome=retried`），重复重试不新增记录
- `GET /api/batches`、`GET /api/batches/<batch_no>`、`GET /api/batches/<batch_no>/audit`
- `POST /api/batches/<batch_no>/edits`：修改可编辑字段 `strength_dbm`、`bandwidth_mhz`、`region`、`note`，需 `base_version`
  - 两个值班员同时修改时，以共同基准版本做三向合并：未冲突字段各自保留；同字段冲突返回 `409 merge_conflict`，body 含冲突字段的 base/current/incoming 值与 `merge_token`
- `POST /api/merges/<token>/resolve`：对每个冲突字段提交 `current` 或 `incoming` 的选择；会话一次性有效
- 批次的任何更新（归并上报、直接编辑、合并解决）都会在同一事务内：
  - 失效所有关联案件的定位、停用授权和结案，归档到 `invalidated_artifacts`
  - 案件状态回退到 `assessed` 并刷新评估，需重新定位、由目标辖区重新确认停用和结案；批次审计链与案件 `measurement_invalidated` 事件可查
- 历史上没有批次的案件（不带 `batch_no`）继续按原测量调查，状态机不变

测试覆盖完整调查流程、测量更正、重复事件、跨区越权与留痕、定位置信度、版本冲突、批次归并与幂等重试、失效联动与重新确认、并发字段合并与冲突选择。协议接入、真实无线电传播模型和执法权限仍需由外部系统实现。
