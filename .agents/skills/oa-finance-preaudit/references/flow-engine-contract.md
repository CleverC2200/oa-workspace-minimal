# 本地 FlowEngine 合同

当前合同覆盖 `travel_expense`、`business_entertainment`、`business_support_fee` 和 `small_marketing_fee` 的 preview。四类财务流程使用同一接口、独立运行目录和各自规则包；旧角色入口暂时只用于回退，不得与本路径混用。

## 两动作循环

从项目根执行：

```bash
python3 -m apps.oa_finance_preaudit.cli next-action \
  --workflow WORKFLOW_KEY
```

stdout 只能解析为一个 `schema_version=1`、`kind=preaudit_action` 的 JSON：

- `action=closed`：本轮结束，不取数、不等待、不创建其他 Agent。
- `action=review_required`：只读取 `review_packet_file` 及审核包引用的 `material_file`，按审核包顺序完成当前流程的固定审核项。
- `action=needs_admin`：本地冻结材料、审核包或持久状态不再可信；这是退出码为 0 的正常动作结果，并同步写入当前动作和对应 attempt 状态。停止当前单据并报告 `reason`，不得自行修复、重取或改写为业务 `⚠️`。

公开 CLI 返回的 preview 动作都固化 `mode=preview`、`write_authorized=false` 和 `write_state=not_requested`。审核决定不包含运行模式或授权，不能在第二步切换写入。

每个 attempt 只有一个当前动作；同一候选重复调用时返回原动作和 token，不重复取数。积压中还存在未打开候选时，后续 `next-action` 可以打开另一单据，供不同审核 Agent 独立处理。`current-action.json` 只是最近一次返回动作的投影，不是全局锁或事实源。

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

将决定写入新的本地文件后执行：

```bash
python3 -m apps.oa_finance_preaudit.cli complete-review \
  --action-file /absolute/path/to/action.json \
  --decision-file /absolute/path/to/review-decision.json
```

只有返回 `action=closed` 才算当前 preview 闭合。`execution_state`、`business_state` 和 `write_state` 分别表示技术执行、业务结论和 OA 写入；公开 CLI 的本合同下 `write_state` 必须始终为 `not_requested`。业务 `needs_human` 是正常闭合，不重试、不改成技术失败。脚本在闭合前重新验证流程身份、动作、状态、审核包、材料路径和冻结指纹；任一变化返回 `needs_admin`，不生成意见。

受控写入不是审核者可选择的模式。只有宿主同时注入绑定当前 `requestId + received_at + case_key + attempt + material_fingerprint + evidence_fingerprint` 的一次性授权、外部 verifier 和提交适配器时，FlowEngine 才生成写入 token；`complete-review` 提交前会再次调用 verifier。审核决定不能创建、修改或提升授权。

每个 attempt 至少记录 discover、collect、review、close；受控写入还记录 submit、postcheck。没有平台精确 Token 时只能写 `unknown`。提交前事件不能保存时停止；已确认提交后才发生观测异常时，回执必须写 `observability_state=unknown`，禁止为了补日志重复提交。
