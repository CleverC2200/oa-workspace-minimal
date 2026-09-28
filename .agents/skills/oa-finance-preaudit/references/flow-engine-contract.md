# 本地 FlowEngine 合同

当前合同覆盖全部六类流程。它们使用同一接口、独立运行目录和各自规则包；包装流程另按规则包直接观察原图，禁止 OCR。

只读对账按 `requestId + received_at` 区分单据轮次。旧轮次只有在完成锁空闲、action/state 完整闭合为 `controlled_write / submitted`，且执行回执、提交与已办后查结果、绑定指纹和冻结审核输入全部复核通过时，才可能归类为 `prior_round_submitted`：当前必须只有一个同流程实时版本，接收时间可解析、晚于旧接收及旧 attempt 创建时间且不在未来，并且已办列表没有同请求冲突。投影以 `current_item_key` 关联新版本；旧文件不变，新版本重新取材、审核和授权，不继承旧决定或 token。缺失流程身份、同请求多版本、时间倒退、活动锁、preview、未闭合、未知写入或任何证据不可信均保留阻断。

本地最新 attempt 仍处于已打开轮次（`mode=controlled_write`、`write_authorized=true`、`execution_state=review_required`、`write_state=authorized`）时，只有同时满足以下条件才归类为 `prior_round_unsubmitted`，只投影不阻断：attempt 目录不存在 `writer-boundary.json`、`execution-receipt.json`、`review-decision.json`、`remark.txt` 或 `submission-envelope.json`；`FlowEngine.resumable_review` 验证 action/state、审核包与冻结材料身份及指纹一致；实时只有一条同流程、同 requestId 的待办，接收时间可解析、严格晚于旧接收时间与旧 attempt 创建时间、不在未来，且已办列表没有同请求冲突。存在任何写入边界证据、review 决定、意见或提交信封，或 identity 多版本、时间不可解析或倒退、preview、未知写入时，仍按 `identity_conflict` 阻断整轮。

## 两动作循环

从项目根执行：

```bash
python3 -m apps.oa_finance_preaudit.cli next-action \
  --workflow WORKFLOW_KEY
```

stdout 只能解析为一个 `schema_version=1`、`kind=preaudit_action` 的 JSON：

- `action=closed`：本轮结束，不取数、不等待。
- `action=review_required`：只读取 `review_packet_file` 及审核包引用的 `material_file`，按审核包顺序完成当前流程的固定审核项。当前登录、会话、执行身份和流程授权可信时，六类财务流程的材料关系、规则口径或业务证据不足仍返回该动作；审核包标为 `material_state=degraded`，且 `required_uncertain_items` 列出必须交财务判断的项目。
- `action=needs_admin`：当前登录/会话、执行身份、流程授权、单据身份、冻结 attempt、动作/状态、审核包完整性、写入授权或目标不再可信；这是退出码为 0 的正常动作结果。停止当前执行并报告 `reason`，不得生成财务意见、自行修复或重取。

包装待办成功读取且为空时，入口另以只读已办结果核对历史材料失败。只有已办命中同一请求、action/state 身份及失败字段一致、`write_state=not_requested`，且无活动完成锁、Writer 边界、执行回执、审核决定、意见或提交信封时，才不再返回该旧失败。旧 attempt 和隔离证据保持不变，不创建新 attempt、不重新采集；没有其他动作时返回无工作 `closed`。已办不可读、未命中、身份冲突、未知写入和其他失败仍保留原阻断，已打开审核动作继续按原合同处理。其他六类财务流程不使用这一包装空队列规则。

失败动作的 `reason` 与 `failure_detail_code` 必须一并保留，输出筛选不得丢弃细码。已授权告警遇到 `material_collection_failed` 且细码属于六类会话错误时，按[错误处理合同](error-handling.md)传递细码，由告警入口复核当前会话；不得把历史细码直接描述为当前登录故障。

默认命令返回的 preview 动作固化 `mode=preview`、`write_authorized=false` 和 `write_state=not_requested`。审核决定不包含运行模式或授权，不能在第二步切换写入。

手工 Skill 任务固定使用默认 preview，不得把已有 preview attempt 提升为写入。滚动生产控制器在六类财务流程中按 `received_at + requestId` 选取全局 FIFO；准备容量恒为 1，审核可并发，Writer 严格等待认领序号并保持全局单写。包装不进入该滚动池，继续走单据级图片文字校对路径。该入口校验当前显式确认、本地流程授权清单、停止状态、OA 身份和只读对账后才准备单据；健康窗口只调节并发，不改变提交次序。准备动作生成绑定当前单据和冻结证据的一小时 capability，完成动作再次验签并复核实时待办；任一边界验证失败均不写 OA。手工 `next-action --mode controlled-write` 和受控差旅 `complete-review` 明确拒绝，不能绕过控制器。唯一例外是人工已在 OA 核实未写入的 `writer_result_unknown`：独立确认必须绑定精确 requestId，action/state 均为 `needs_admin + write_state=unknown`，且控制器和所有 Writer 均不活动；入口重新核验实时待办、已办、最新 attempt、身份和执行回执后，仅创建新的受控冻结 attempt，不直接提交 OA，后续仍由滚动控制器和唯一 Writer 处理。

控制器领取已打开动作时，若 capability 已过期或剩余有效期不超过 20 分钟，必须先在原身份、材料和 evidence 指纹绑定下刷新 capability 与提交 token，再开始最长 15 分钟的审核；剩余有效期超过该窗口时保持原 token，不因重复读取破坏幂等。完成宿主在提交前再次执行同一刷新判断；此时保留审核决定绑定的 action token，只轮换 capability 和 submission token。

滚动控制器不得把单个 AI 进程的普通写前失败扩大为长期全局阻断。`codex_process_failed` 或 `codex_process_incomplete` 只有在同一单据已完成两次安全尝试、结果明确为 `write_state=not_requested`，且存在绑定当前 action token、模型、推理档位和两次逐次错误码的原子 `review-failure.json` 时，才进入隔离检查。FlowEngine 在完成锁内复核该凭据、动作仍为 `review_required / authorized`，且不存在 Writer 边界、执行回执、审核决定、签字意见和提交信封后，持久化凭据指纹与 `codex_review_failed / needs_admin / not_requested` 并继续全局 FIFO。收据已写但父控制器在隔离前停止时，后续运行可以复用完全相同且仍绑定当前 action 的收据；内容冲突必须停止。任一条件不满足、隔离落盘失败、未满足下述人工交接条件的进程超时/卡死、宿主 critical 或出现未列入白名单的新错误码时，仍立即停止整轮。历史 `codex_review_failed` 只有在 action/state、收据指纹和零 Writer 痕迹全部复核通过后才计入隔离并参与人工对账；损坏现场按 `review_quarantine_invalid` 技术失败处理，不自动重试。

人工明确授权后，滚动入口可通过精确的 `--recover-action-file` 恢复 `codex_decision_invalid` 或 `codex_review_failed`。恢复前必须核对 action/state 均为 `needs_admin / not_requested`、冻结身份与材料完整、最新 attempt、实时待办身份一致且无已办命中，并确认不存在 Writer 边界、执行回执、审核决定、意见或提交信封；进程失败还必须通过原始失败凭据与指纹校验。只创建新的冻结 attempt，保留原隔离证据，不直接写 OA；不带显式恢复参数时仍跳过历史隔离，不自动重试，未知写入规则不变。

每个 attempt 只有一个当前动作；同一候选重复调用时返回原动作和 token，不重复取数。积压中还存在未打开候选时，后续 `next-action` 可以打开另一单据，供独立审核调用处理。`current-action.json` 只是最近一次返回动作的投影，不是全局锁或事实源。

同一 attempt 的完成转换使用 attempt 锁单飞，后到的重复决定不能覆盖已闭合结果；不同 case 不共享提交锁。完成旧动作时，若投影已指向另一单据，不得回写覆盖该投影。

审核者不得修改 `action_file`、审核包或材料，不得读取其他 attempt，不得自行组织路径、计算指纹、选择 requestId 或调用 OA 写入。

降级审核包的 `material_file` 不引用采集失败前的任何候选字段或资源，只保留脱敏 `collection_gaps`。审核者必须根据缺口和当前流程审核项生成可读的自然语言结论；脚本验证所有 `required_uncertain_items` 均为 `uncertain` 后，仍可正常闭合为 `business_state=needs_human`。OA 登录失败、OA 会话过期、当前 RPA/CDP 会话缺失/失效/不可用或执行身份不可信时，不使用降级包，直接返回 `needs_admin`，且不得生成材料、审核包、决定、意见或写入请求。包装图片材料不可读时也继续返回 `needs_admin`，不使用降级包。

### 审核未完成的人工交接

资源等待与 AI 审核失败分开处理。CPU 高负载本身属于 `pressure`，不再单独触发 `critical`；弹性审核默认从 6 槽开始，持续压力按每次心跳逐级降到 2、1 槽，保留在途审核，不为降档杀进程。恢复后仍需连续两个完整健康窗口才按 `1 → 2 → 6 → 8 → 10 → 12` 升一级。材料准备及 Writer 均保持 1 槽，原全局 FIFO 不变。

宿主健康采样未知时，控制器标记 `resource_paused=true`，暂停新领取、准备完成后的审核派发及排队 Writer；已在途操作仍受原超时和安全校验约束。周期性复测恢复可信后继续原队列，排队等待不启动 AI 审核计时。等待期间仍响应显式停止，已进入 Writer 的操作不能被重放。可用内存低于 12% 仍为 `critical`，即使 CPU 采样缺失也立即停止。资源繁忙或等待绝不生成下述人工交接意见；只有实际 AI 超时继续按下述独立规则处理。

六类财务当前受控 attempt 遇到 `codex_process_timeout` 时，宿主可生成 `preaudit_manual_handoff` 行政交接记录，说明 AI 未形成有效业务结论，请财务人工审核。它不是 `preaudit_review_decision`，不含 `items`，不计为完成业务审核覆盖；原固定项目规则只适用于有效业务决定。包装与手工 preview 不使用此分支。

超时须确认审核进程组退出并保存绑定当前 action token 的失败凭据。宿主在同一完成锁内复核动作仍为 `review_required / authorized`、冻结输入、当前授权及零 Writer 痕迹，再将特别说明交给唯一串行 Writer；身份、待办、begin-submit、终态回执与后置校验仍全部执行。此分支不再请求 AI 重试，成功闭合为 `needs_human / submitted` 后继续 FIFO。意见只说明审核超时，不包含原始错误内容，也不生成业务通过/不通过结论。

`codex_decision_invalid` 不得生成 `preaudit_manual_handoff`，也不得进入 Writer。有效 JSON 对象在父 Writer 的结构校验失败时，由调度器重新发起一次独立 AI 审核；第二次仍无效则把当前 attempt 隔离为 `codex_decision_invalid / needs_admin / not_requested`，继续全局 FIFO。原始输出不是单个有效决定对象时不重试，直接按同一 no-write 结果隔离当前 attempt 并继续 FIFO。两种路径都保存脱敏拒绝凭据，禁止把“审核结果格式异常”写入 OA；最终人工交接校验边界也必须拒绝该失败码。

登录失效、身份/材料/授权不可信、未知写入、当前停止门、本机 critical 或健康状态未知仍停止；进程回收未确认时不得转交接。历史隔离项不自动重试，已办意见不追改。普通进程失败和无有效对象的进程未完成仍按原隔离合同处理。

审核子进程另保存脱敏阶段诊断：动作令牌摘要、固定事件计数、最后观察阶段、耗时、退出码和标准错误固定信号。不保存标准错误原文、事件正文或审核决定。固定信号只表示观察到相关关键字，不单独证明网络或服务故障根因。无完成用量事件时，汇总状态为 `unknown`，数值累计字段不代表实际消耗为零。

## 结构化审核决定

宿主可以使用 `python -m apps.oa_finance_preaudit.review_decision --action-file ... --input ... --output ...` 从当前冻结动作装配缺少的协议绑定字段。它只补 schema、kind、动作 token、流程键和规则版本，不生成或修改业务项、页面分类、证据或结论；已有冲突绑定保留并由完成边界拒绝。输出必须是 attempt 外的新临时文件，冻结资料改变时停止。普通 Worker 使用同一装配函数；历史只读容量复验保留专用 token，不进入装配或写入。

有限跨轮恢复是生产宿主的独立显式授权：`--recover-confirmed-no-write-once YES` 只选择当前授权流程和可信单据级确认未写入的允许失败类别，且只对首个 attempt 恢复一次。候选选择不构成提交授权；后续仍执行恢复入口实时复核、单据 capability、唯一 Writer 和后置核验。已有后续 attempt 或恢复失败时给出人工接管与期限，不无限恢复。默认调用与未知写入规则保持原合同。

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

- `pass` 和 `fail` 至少有一个 `evidence_refs`；唯一例外是 V2 包装流程确认全部页面均为 `non_packaging` 的跳过分支，内部四项不绑定图片证据。
- `uncertain` 至少有一个 `missing_evidence`。
- 差旅完整材料采用 `full_coverage_without_trusted_item_relation` 路由时，页面引用只记录覆盖范围；`uncertain` 的 `missing_evidence` 除页面引用外还必须包含当前审核项具体缺失的事实或关系，只有页面引用的决定无效。
- 任一项目为 `fail` 或 `uncertain` 时，`overall` 必须为 `needs_human`；全部为 `pass` 时必须为 `pass`。
- 不增加 requestId、人员、公司、写入模式、意见文本或其他字段。

包装流程的决定还必须增加一个 `excel_comparison`。未提供 Excel 时固定为 `{"status":"not_provided","resource_id":null,"mappings":[]}`。存在 Excel 时，`mappings` 必须按四项顺序逐项给出 `item_key`、可解析到同一结构化 Excel `key_text` 索引的 `cell_refs`，以及 `match`、`different` 或 `uncertain` 比较结果；相同 cell ref 还要进入对应项目的 `evidence_refs`。脚本逐条验证资源、索引、项目状态和四项映射后才渲染 Excel 对照尾行，不根据附件存在自行推断完成。

`packaging_layout_approval:2` 的包装决定还必须增加 `visual_review`：

```json
{
  "outcome": "reviewed 或 all_non_packaging",
  "pages": [
    {
      "page_ref": "原样复制 packaging_visual_contract.page_refs 中对应值",
      "classification": "packaging_layout、non_packaging 或 uncertain",
      "package_version_id": "仅包装页填写稳定、脱敏的版本 ID；其他页为 null",
      "item_keys": ["该包装页实际覆盖的固定审核项 key；其他页为空数组"]
    }
  ]
}
```

- `pages` 是按 `packaging_visual_contract.page_refs` 原始顺序排列的页面-版本观察列表，必须覆盖每个原始页面。一个原图同时承载多个包装版本时，可为不同 `package_version_id` 连续重复该 `page_ref`；同页同版本不得重复，同页分类必须一致，`non_packaging` 或 `uncertain` 页不得重复。重复观察不能替代其他原始页面，页面覆盖计数仍按唯一原始页计算。
- `packaging_layout` 观察必须填写版本 ID；同一版本所有页面观察的 `item_keys` 并集必须精确覆盖四个固定项。观察声明覆盖某项时，该页引用必须进入对应项目的 `evidence_refs` 或 `missing_evidence`。
- `non_packaging` 和 `uncertain` 页的版本 ID 必须为 `null`，`item_keys` 必须为空；`non_packaging` 页引用也不得进入任一固定项目的 `evidence_refs` 或 `missing_evidence`。
- `non_packaging` 的业务语义包括仅含储运信息、且不承载固定四项内容的运输外箱版面，以及包装规则包中已确认的“正面展示标签例外”；除此例外，不得仅因零售包装缺少固定四项就归入该类。此语义由审核 Agent 直接观察原图判断，FlowEngine 只验证分类后的结构与输出分支。
- 只要存在 `packaging_layout` 就使用 `outcome=reviewed` 并审核包装页；存在 `uncertain` 时也必须使用 `reviewed`，整体结论为 `needs_human`，且不确定页引用必须进入至少一个 `uncertain` 项的 `missing_evidence`。
- 只有全部页面均为 `non_packaging` 时允许 `outcome=all_non_packaging`。此时四项保留为内部 `pass` 结构以维持统一执行与回执合同，但 `evidence_refs` 和 `missing_evidence` 均为空；`excel_comparison.status` 使用 `not_applicable`，OA 意见只渲染 `AI审核结论：非包装版面，跳过审核。`。
- V1 冻结包装审核包仍按原决定合同闭合；新审核包会在 action、state 和 review packet 中同时固定 V2 标记，任一标记缺失或不一致均停止，不能把 V2 决定降级成 V1 绕过视觉覆盖校验。

V1 包装决定必须用 `material:prepared.images.<resource_id>.page-<number>` 在 `evidence_refs` 或 `missing_evidence` 中覆盖每一页；V2 改由 `visual_review.pages` 完成逐页分类覆盖，并把包装页声明的固定项逐项绑定到对应证据或缺口。脚本会先验证图片可读、manifest 页数与准备页一致，再验证视觉分类、版本四项矩阵和证据绑定；采集脚本不得生成图片观察、分类或校对结论。

`small_marketing_fee:2` 在原五项末尾增加“支付公司一致”，并在 action、state 和审核包中显式冻结规则版本。发布前未带显式版本的 V1 冻结包只接受原五项决定；V2 只接受六项决定。两版兼容只用于完成各自已经冻结的审核包，不得删改版本字段把 V2 决定降级为 V1。

将决定写入新的本地文件后执行：

```bash
python3 -m apps.oa_finance_preaudit.cli complete-review \
  --action-file /absolute/path/to/action.json \
  --decision-file /absolute/path/to/review-decision.json
```

只有返回 `action=closed` 才算当前 preview 闭合。`execution_state`、`business_state` 和 `write_state` 分别表示技术执行、业务结论和 OA 写入；公开 CLI 的本合同下 `write_state` 必须始终为 `not_requested`。业务 `needs_human` 是正常闭合，不重试、不改成技术失败。脚本在闭合前重新验证流程身份、动作、状态、审核包、材料路径和冻结指纹；任一变化返回 `needs_admin`，不生成意见。

受控写入不是审核者可选择的模式。只有宿主同时注入绑定当前 `requestId + received_at + case_key + attempt + material_fingerprint + evidence_fingerprint` 的一小时授权、外部 verifier 和提交适配器时，FlowEngine 才生成写入 token；`complete-review` 提交前会再次验签并复核本地流程授权清单。提交适配器还要重新确认当前 OA 待办中的 `requestId + received_at` 完全一致。审核决定不能创建、修改或提升授权。

提交适配器在 OA POST 前写入带 HMAC 完整性证明的 `writer-boundary.json`，其中只保留流程、attempt、请求身份哈希、冻结 job 哈希、提交 token 哈希、阶段和失败码。`writer_preflight_failed` 与 `submission_target_changed` 明确表示尚未发出 OA POST，状态为 `not_requested`；`submit_started` 之后缺少可信终态则保持 `unknown`，同一 attempt 禁止重放。`postcheck_verified` 可在宿主中断后恢复成功结果，但仍由 FlowEngine 生成和验证正式执行回执。边界缺失、篡改、身份不一致或非法阶段转换一律按 `writer_result_unknown` 止损。

轮次开始时已经存在的历史 `writer_result_unknown`（包括存在 Writer 边界但无执行回执的未确认提交）只有在只读对账已证明同一身份不再属于实时待办时，才由滚动入口显式列入 `skip_unknown_failures`：这类项保持原样、只留在人工核对清单，不重放也不补造回执；仍待办的未知写入由 continuation 门阻断整轮，运行期间新发生的未知写入不在该集合内，仍立即止损。

每个 attempt 至少记录 discover、collect、review、close；受控写入还记录 submit、postcheck。FlowEngine 不把“审核包已生成到结果写回”的墙钟时间直接当成等待或 Agent 活动；只有宿主提供绑定当前 item 的 exact timing receipt 时，验收报告才覆盖 `active_seconds` 与 `waiting_seconds`，否则两者保持 unknown 并阻止等待比例调优。coordination 秒数和 Token 固定为 0。当前运行的 Token 状态固定写 `unknown`。终态回执只保存审核项、资源、页面的 covered/total/applicable 计数和格式是否合规，不保存证据正文；资源 covered 必须来自决定中的具体资源引用，不能按材料存在自动填满。提交前事件不能保存时停止；已确认提交后才发生观测异常时，回执必须写 `observability_state=unknown`，禁止为了补日志重复提交。
