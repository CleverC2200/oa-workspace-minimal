# 本地 FlowEngine 合同

当前合同只覆盖 `travel_expense` 的 preview。它是差旅费新路径；旧角色入口暂时只用于回退，不得与本路径混用。

## 唯一动作循环

从项目根执行：

```bash
python3 -m apps.oa_finance_preaudit.cli next-action \
  --workflow travel_expense
```

stdout 只能解析为一个 `schema_version=1`、`kind=preaudit_action` 的 JSON：

- `action=closed`：本轮结束，不取数、不等待、不创建其他 Agent。
- `action=review_required`：只读取 `review_packet_file` 及审核包引用的 `material_file`，完成当前八个固定审核项。
- `action=needs_admin`：本地冻结材料、审核包或持久状态不再可信；这是退出码为 0 的正常动作结果，并同步写入当前动作和对应 attempt 状态。停止当前单据并报告 `reason`，不得自行修复、重取或改写为业务 `⚠️`。

所有动作都固化 `mode=preview`、`write_authorized=false` 和 `write_state=not_requested`。审核决定不包含运行模式或授权，不能在第二步切换写入。

每个流程同一时刻只有一个当前动作，持久化在该流程的 `current-action.json`。已有未闭合 attempt 时，`next-action` 先返回原动作，不因待办列表变化另开单据；多个活动 attempt 属于 `needs_admin`。同一动作的完成转换使用流程锁串行化，后到的重复决定不能覆盖已闭合结果。

审核者不得修改 `action_file`、审核包或材料，不得读取其他 attempt，不得自行组织路径、计算指纹、选择 requestId 或调用 OA 写入。

## 结构化审核决定

审核决定必须是单个 JSON 对象，且只包含以下顶层字段：

```json
{
  "schema_version": 1,
  "kind": "preaudit_review_decision",
  "action_token": "原样复制 action.action_token",
  "workflow_key": "travel_expense",
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

将决定写入新的本地文件后执行：

```bash
python3 -m apps.oa_finance_preaudit.cli complete-review \
  --action-file /absolute/path/to/action.json \
  --decision-file /absolute/path/to/review-decision.json
```

只有返回 `action=closed` 才算当前 preview 闭合。`execution_state`、`business_state` 和 `write_state` 分别表示技术执行、业务结论和 OA 写入；本合同下 `write_state` 必须始终为 `not_requested`。业务 `needs_human` 是正常闭合，不重试、不改成技术失败。脚本在闭合前重新验证动作、状态、审核包、材料路径和冻结指纹；任一变化返回 `needs_admin`，不生成意见。
