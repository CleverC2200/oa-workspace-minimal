---
name: oa-finance-preaudit
description: 使用 Codex 对 Weaver E9 六类财务报销和包装版面流程执行预审：读取 FlowEngine 冻结证据，按当前流程固定审核项形成结构化决定；财务只给意见，包装只校对文字。
---

# OA 财务预审

目标是用最短路径完成一张单据的一类流程审核：FlowEngine 负责发现、取数、冻结和状态；Codex 只读取证据并形成业务决定。

手工任务固定为 `preview-only`；本 Skill 不批准、不退回，也不改变 OA 状态。
六类财务的批量受控写入统一由项目滚动生产控制器按 OA 操作时间编排；审核可并发，提交按认领顺序全局串行。包装继续走单据级图片文字校对路径。差旅费不开放两条手工受控 CLI 命令；所有生产写入继续要求当前授权、本地流程授权清单和显式写入确认。

## 1. 取得当前动作

从项目根执行：

```bash
python3 -m apps.oa_finance_preaudit.cli next-action \
  --workflow WORKFLOW_KEY
```

按返回动作处理：

- `closed`：本轮完成。
- `needs_admin`：仅用于登录/会话/执行身份、冻结、授权、目标或写入结果等技术边界不可信；报告脱敏 `reason` 后停止当前执行，不生成财务意见。
- `review_required`：继续下一步；只读取 `review_packet_file` 及其引用的 `material_file`。

动作、材料和审核包均为只读。完整的动作字段、技术失败和闭合语义见[本地 FlowEngine 合同](references/flow-engine-contract.md)。

## 2. 读取证据

先确认审核包的 `workflow_key`，再从[共享结果接口与流程路由](references/result-contract.md)只加载对应的一个流程规则包。完整读取[证据阅读规则](references/observation-contract.md)，并满足以下完成条件：

- 表单明细、相关资源和准备页全部覆盖；
- 每条采用的证据同时证明资源归属和审核项关系；
- 权威字段优先，图片不可见或关系不明的事实保持未知；
- 审核包为 `material_state=degraded` 时，不使用任何候选材料；`collection_gaps` 只表示自动读取没有完成，不能据此声称申请人未填写字段、未上传附件或单据没有发票。按 `required_uncertain_items` 将对应项目写成简短的 `uncertain` 结论，说明自动读取失败、未完成核对并请财务查看原单，继续完成预审。
- 当前登录、会话和执行身份可信时，规则口径模糊、材料关系不明或证据不足只影响业务判断；使用 `uncertain` / `needs_human` 继续完成预审，不得升级为技术阻断。

包装流程同时调用项目的中文图片文字校对 Skill，直接观察原图；存在 Excel 时逐字符对照。包装只校对关键文字，不扩展为法规审核。

## 3. 形成结构化决定

决定必须遵守[本地 FlowEngine 合同](references/flow-engine-contract.md)中的 `preaudit_review_decision` 结构：

- `items` 与审核包的固定项目数量、顺序和 `item_key` 完全一致；
- 每项包含 `status`、简明 `conclusion`、`evidence_refs` 和 `missing_evidence`；
- `pass`、`fail` 必须有可回查证据，`uncertain` 必须写明缺口；
- `required_uncertain_items` 中的项目只能是 `uncertain`，结论要用财务可理解的语言说明具体缺口和需其判断的事项，不得只复制错误码或套用单一固定句；
- 任一项目为 `fail` 或 `uncertain` 时，`overall=needs_human`；全部为 `pass` 时，`overall=pass`；
- 包装决定增加合同规定的 `excel_comparison`，并覆盖每个冻结图片页。

业务 `fail` 和 `uncertain` 是正常预审结果，不代表批准、退回或技术失败。

## 4. 完成审核

把决定写入新的本地 JSON 文件，然后执行：

```bash
python3 -m apps.oa_finance_preaudit.cli complete-review \
  --action-file /absolute/path/to/action.json \
  --decision-file /absolute/path/to/review-decision.json
```

只有返回 `action=closed` 才完成当前单据。返回 `needs_admin` 时报告脱敏原因并停止；不得修改冻结文件后重试。

## 5. 输出与反馈

向用户展示的意见遵守[共享结果接口](references/result-contract.md)：整体结论先行，状态前置，固定项目完整且不增加第二套摘要。

用户明确指出误报、漏报或财务口径时，把脱敏后的可复现纠正写入[已确认反馈清单](references/confirmed-feedback.md)；可重放错误写入[错误回归案例](references/regression-cases.json)。正常审核不读取回归答案，保持盲审。
