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
- `action=review_required`：只读取 `review_packet_file` 及审核包引用的 `material_file`，按审核包顺序完成当前流程的固定审核项。四类财务流程的材料采集不可信时仍返回该动作，但审核包标为 `material_state=degraded`，且 `required_uncertain_items` 列出必须交财务判断的项目。
- `action=needs_admin`：当前单据身份、冻结 attempt、动作/状态、审核包完整性、写入授权或目标不再可信；这是退出码为 0 的正常动作结果。停止当前执行并报告 `reason`，不得自行修复或重取。

默认命令返回的 preview 动作固化 `mode=preview`、`write_authorized=false` 和 `write_state=not_requested`。审核决定不包含运行模式或授权，不能在第二步切换写入。

手工 Skill 任务固定使用默认 preview，不得把已有 preview attempt 提升为写入。滚动生产控制器在四类财务流程中按 `received_at + requestId` 选取全局 FIFO；准备容量恒为 1，审核可并发，Writer 严格等待认领序号并保持全局单写。包装不进入该滚动池，继续走单据级图片文字校对路径。该入口校验当前显式确认、本地流程授权清单、停止状态、OA 身份和只读对账后才准备单据；健康窗口只调节并发，不改变提交次序。准备动作生成绑定当前单据和冻结证据的一小时 capability，完成动作再次验签并复核实时待办；任一边界验证失败均不写 OA。手工 `next-action --mode controlled-write` 和受控差旅 `complete-review` 明确拒绝，不能绕过控制器。唯一例外是人工已在 OA 核实未写入的 `writer_result_unknown`：独立确认必须绑定精确 requestId，action/state 均为 `needs_admin + write_state=unknown`，且控制器和所有 Writer 均不活动；入口重新核验实时待办、已办、最新 attempt、身份和执行回执后，仅创建新的受控冻结 attempt，不直接提交 OA，后续仍由滚动控制器和唯一 Writer 处理。

控制器领取已打开动作时，若 capability 已过期或剩余有效期不超过 20 分钟，必须先在原身份、材料和 evidence 指纹绑定下刷新 capability 与提交 token，再开始最长 15 分钟的审核；剩余有效期超过该窗口时保持原 token，不因重复读取破坏幂等。完成宿主在提交前再次执行同一刷新判断；此时保留审核决定绑定的 action token，只轮换 capability 和 submission token。

每个 attempt 只有一个当前动作；同一候选重复调用时返回原动作和 token，不重复取数。积压中还存在未打开候选时，后续 `next-action` 可以打开另一单据，供独立审核调用处理。`current-action.json` 只是最近一次返回动作的投影，不是全局锁或事实源。

同一 attempt 的完成转换使用 attempt 锁单飞，后到的重复决定不能覆盖已闭合结果；不同 case 不共享提交锁。完成旧动作时，若投影已指向另一单据，不得回写覆盖该投影。

审核者不得修改 `action_file`、审核包或材料，不得读取其他 attempt，不得自行组织路径、计算指纹、选择 requestId 或调用 OA 写入。

降级审核包的 `material_file` 不引用采集失败前的任何候选字段或资源，只保留脱敏 `collection_gaps`。审核者必须根据缺口和当前流程审核项生成可读的自然语言结论；脚本验证所有 `required_uncertain_items` 均为 `uncertain` 后，仍可正常闭合为 `business_state=needs_human`。包装图片材料不可读时继续返回 `needs_admin`，不使用降级包。

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

提交适配器在 OA POST 前写入带 HMAC 完整性证明的 `writer-boundary.json`，其中只保留流程、attempt、请求身份哈希、冻结 job 哈希、提交 token 哈希、阶段和失败码。`writer_preflight_failed` 与 `submission_target_changed` 明确表示尚未发出 OA POST，状态为 `not_requested`；`submit_started` 之后缺少可信终态则保持 `unknown`，同一 attempt 禁止重放。`postcheck_verified` 可在宿主中断后恢复成功结果，但仍由 FlowEngine 生成和验证正式执行回执。边界缺失、篡改、身份不一致或非法阶段转换一律按 `writer_result_unknown` 止损。

每个 attempt 至少记录 discover、collect、review、close；受控写入还记录 submit、postcheck。FlowEngine 不把“审核包已生成到结果写回”的墙钟时间直接当成等待或 Agent 活动；只有宿主提供绑定当前 item 的 exact timing receipt 时，验收报告才覆盖 `active_seconds` 与 `waiting_seconds`，否则两者保持 unknown 并阻止等待比例调优。coordination 秒数和 Token 固定为 0。当前运行的 Token 状态固定写 `unknown`。终态回执只保存审核项、资源、页面的 covered/total/applicable 计数和格式是否合规，不保存证据正文；资源 covered 必须来自决定中的具体资源引用，不能按材料存在自动填满。提交前事件不能保存时停止；已确认提交后才发生观测异常时，回执必须写 `observability_state=unknown`，禁止为了补日志重复提交。
