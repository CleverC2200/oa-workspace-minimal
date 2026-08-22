# 本地 FlowEngine 合同

当前合同覆盖全部五类流程。它们使用同一接口、独立运行目录和各自规则包；包装流程另按规则包直接观察原图，禁止 OCR。

## 两动作循环

从项目根执行：

```bash
python3 -m apps.oa_finance_preaudit.cli next-action \
  --workflow WORKFLOW_KEY
```

stdout 只能解析为一个 `schema_version=1`、`kind=preaudit_action` 的 JSON：

- `action=closed`：本轮结束，不取数、不等待。
- `action=review_required`：只读取 `review_packet_file` 及审核包引用的 `material_file`，按审核包顺序完成当前流程的固定审核项。
- `action=needs_admin`：本地冻结材料、审核包或持久状态不再可信；这是退出码为 0 的正常动作结果，并同步写入当前动作和对应 attempt 状态。停止当前单据并报告 `reason`，不得自行修复、重取或改写为业务 `⚠️`。

默认命令返回的 preview 动作固化 `mode=preview`、`write_authorized=false` 和 `write_state=not_requested`。审核决定不包含运行模式或授权，不能在第二步切换写入。

受控生产任务仍使用相同的 `next-action` 和 `complete-review`，但必须由已经单独授权的流程定时入口在两个动作上都显式传入 `--confirm-write YES`，并由运行环境提供只包含该流程的本地授权清单。手工 Skill 任务固定使用默认 preview，不得把已有 preview attempt 提升为写入。

每个 attempt 只有一个当前动作；同一候选重复调用时返回原动作和 token，不重复取数。积压中还存在未打开候选时，后续 `next-action` 可以打开另一单据，供独立审核调用处理。`current-action.json` 只是最近一次返回动作的投影，不是全局锁或事实源。

同一 attempt 的完成转换使用 attempt 锁单飞，后到的重复决定不能覆盖已闭合结果；不同 case 不共享提交锁。完成旧动作时，若投影已指向另一单据，不得回写覆盖该投影。

审核者不得修改 `action_file`、审核包或材料，不得读取其他 attempt，不得自行组织路径、计算指纹、选择 requestId 或调用 OA 写入。

## 结构化审核决定

审核决定必须是单个 JSON 对象，且只包含以下顶层字段：

```json
{
  "schema_version": 1,
  "kind": "preaudit_review_decision",
  "action_token": "原样复制 action.action_token",
  "workflow_key": "原样复制 action.workflow_key",
  "rule_version": "原样复制 review_packet.rule_version",
  "overall": "pass 或 needs_human",
  "items": []
}
```

`items` 必须与审核包中的固定项目数量、顺序和 `item_key` 完全一致。每项只包含：

```json
{
  "item_key": "原样复制",
  "status": "pass、fail 或 uncertain",
  "conclusion": "简明业务结论",
  "evidence_refs": ["可回查的材料字段或资源定位"],
  "missing_evidence": ["缺失的事实或关系"]
}
```

- `pass` 和 `fail` 至少有一个 `evidence_refs`。
- `uncertain` 至少有一个 `missing_evidence`。
- 任一项目为 `fail` 或 `uncertain` 时，`overall` 必须为 `needs_human`；全部为 `pass` 时必须为 `pass`。
- 不增加 requestId、人员、公司、写入模式、意见文本或其他字段。

包装流程的决定还必须增加一个 `excel_comparison`。未提供 Excel 时固定为 `{"status":"not_provided","resource_id":null,"mappings":[]}`。存在 Excel 时，`mappings` 必须按四项顺序逐项给出 `item_key`、可解析到同一结构化 Excel `key_text` 索引的 `cell_refs`，以及 `match`、`different` 或 `uncertain` 比较结果；相同 cell ref 还要进入对应项目的 `evidence_refs`。脚本逐条验证资源、索引、项目状态和四项映射后才渲染 Excel 对照尾行，不根据附件存在自行推断完成。

包装决定还必须用 `material:prepared.images.<resource_id>.page-<number>` 在 `evidence_refs` 或 `missing_evidence` 中覆盖审核包引用材料的每一页。脚本会先验证图片可读、manifest 页数与准备页一致，再验证决定的全页覆盖；采集脚本不得生成图片观察或校对结论。

将决定写入新的本地文件后执行：

```bash
python3 -m apps.oa_finance_preaudit.cli complete-review \
  --action-file /absolute/path/to/action.json \
  --decision-file /absolute/path/to/review-decision.json
```

只有返回 `action=closed` 才算当前 preview 闭合。`execution_state`、`business_state` 和 `write_state` 分别表示技术执行、业务结论和 OA 写入；公开 CLI 的本合同下 `write_state` 必须始终为 `not_requested`。业务 `needs_human` 是正常闭合，不重试、不改成技术失败。脚本在闭合前重新验证流程身份、动作、状态、审核包、材料路径和冻结指纹；任一变化返回 `needs_admin`，不生成意见。

受控写入不是审核者可选择的模式。只有宿主同时注入绑定当前 `requestId + received_at + case_key + attempt + material_fingerprint + evidence_fingerprint` 的一小时授权、外部 verifier 和提交适配器时，FlowEngine 才生成写入 token；`complete-review` 提交前会再次验签并复核本地流程授权清单。提交适配器还要重新确认当前 OA 待办中的 `requestId + received_at` 完全一致。审核决定不能创建、修改或提升授权。

每个 attempt 至少记录 discover、collect、review、close；受控写入还记录 submit、postcheck。FlowEngine 不把“审核包已生成到结果写回”的墙钟时间直接当成等待或 Agent 活动；只有宿主提供绑定当前 item 的 exact timing receipt 时，验收报告才覆盖 `active_seconds` 与 `waiting_seconds`，否则两者保持 unknown 并阻止等待比例调优。coordination 秒数和 Token 固定为 0。当前运行的 Token 状态固定写 `unknown`。终态回执只保存审核项、资源、页面的 covered/total/applicable 计数和格式是否合规，不保存证据正文；资源 covered 必须来自决定中的具体资源引用，不能按材料存在自动填满。提交前事件不能保存时停止；已确认提交后才发生观测异常时，回执必须写 `observability_state=unknown`，禁止为了补日志重复提交。
