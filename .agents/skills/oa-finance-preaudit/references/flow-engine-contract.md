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
- `action=review_required`：只读取 `review_packet_file` 及审核包引用的 `material_file`，按审核包顺序完成当前流程的固定审核项。材料采集成功且存在可读取的当前证据时，业务材料缺项、规则口径模糊或证据冲突仍由 Agent 形成 `uncertain` / `fail` 意见。
- `action=needs_admin`：当前登录/会话、执行身份、流程授权、单据身份、冻结 attempt、动作/状态、审核包完整性、写入授权或目标不再可信；这是退出码为 0 的正常动作结果。停止当前执行并报告 `reason`，不得生成财务意见、自行修复或重取。

包装待办成功读取且为空时，入口另以只读已办结果核对历史材料失败。只有已办命中同一请求、action/state 身份及失败字段一致、`write_state=not_requested`，且无活动完成锁、Writer 边界、执行回执、审核决定、意见或提交信封时，才不再返回该旧失败。旧 attempt 和隔离证据保持不变，不创建新 attempt、不重新采集；没有其他动作时返回无工作 `closed`。已办不可读、未命中、身份冲突、未知写入和其他失败仍保留原阻断，已打开审核动作继续按原合同处理。其他六类财务流程不使用这一包装空队列规则。

失败动作的 `reason` 与 `failure_detail_code` 必须一并保留，输出筛选不得丢弃细码。已授权告警遇到 `material_collection_failed` 且细码属于六类会话错误时，按[错误处理合同](error-handling.md)传递细码，由告警入口复核当前会话；不得把历史细码直接描述为当前登录故障。

默认命令返回的 preview 动作固化 `mode=preview`、`write_authorized=false` 和 `write_state=not_requested`。审核决定不包含运行模式或授权，不能在第二步切换写入。

手工 Skill 任务固定使用默认 preview，不得把已有 preview attempt 提升为写入。滚动生产控制器在六类财务流程中按 `received_at + requestId` 选取全局 FIFO；准备容量恒为 1，审核可并发，Writer 严格等待认领序号并保持全局单写。包装不进入该滚动池，继续走单据级图片文字校对路径。该入口校验当前显式确认、本地流程授权清单、停止状态、OA 身份和只读对账后才准备单据；健康窗口只调节并发，不改变提交次序。准备动作生成绑定当前单据和冻结证据的一小时 capability，完成动作再次验签并复核实时待办；任一边界验证失败均不写 OA。手工 `next-action --mode controlled-write` 和受控差旅 `complete-review` 明确拒绝，不能绕过控制器。唯一例外是人工已在 OA 核实未写入的 `writer_result_unknown`：独立确认必须绑定精确 requestId，action/state 均为 `needs_admin + write_state=unknown`，且控制器和所有 Writer 均不活动；入口重新核验实时待办、已办、最新 attempt、身份和执行回执后，仅创建新的受控冻结 attempt，不直接提交 OA，后续仍由滚动控制器和唯一 Writer 处理。

控制器领取已打开动作时，若 capability 已过期或剩余有效期不超过 20 分钟，必须先在原身份、材料和 evidence 指纹绑定下刷新 capability 与提交 token，再开始最长 15 分钟的审核；剩余有效期超过该窗口时保持原 token，不因重复读取破坏幂等。完成宿主在提交前再次执行同一刷新判断；此时保留审核决定绑定的 action token，只轮换 capability 和 submission token。

模型错误的整轮停止范围及 3 小时冷却见[小时生产合同](../../../../docs/runbooks/hourly-production.md)：明确官方限制首错停止；普通写前失败只结束当前单据，不当轮重试，可信留证后保留待办并继续其他单据。人工停止、父链退出以及身份、授权、写入、回收和证据完整性安全门仍可停止整轮。模型启动与停止门的写入、清除共用本地停止控制租约；启动前在租约内复查停止门。模型 CLI 关闭无限连接重试；内置 OpenAI provider 的底层有界重试尚不能据此认定为零。审核期间每 5 秒检查结构化错误事件、停止门及原父进程存活，每次排空该检查开始时可见的事件。stderr 使用短读取，不等满 4 KiB 或进程结束。普通工具输出不用于错误判断。当前审核失败后回收其只读模型进程组；系统失去调度能力或 CLI 尚未输出错误时，不承诺 5 秒内阻断。已进入 Writer 的操作等待原回执，不中断或重放。先保存绑定动作文件摘要的 `review-stop-trigger.json`，其 `process_result=unknown` 不证明退出或未写入。整轮停止时控制器给予审核 worker 6 秒协作收尾窗口，超时再收只读池；完整 `review-failure.json` 依赖 worker 正常收尾，缺失时不得据触发凭据隔离或认定已回收。完整失败凭据绑定 action token、模型、推理档位和逐次错误码；历史两次尝试凭据继续可读，不改写历史。全局停止门不会因本轮收尾自动清除，须人工核对后显式恢复。触发凭据或停止门写入失败时保留原始错误与次级停止错误，不宣称成功阻断。

FlowEngine 的隔离和恢复仍须在完成锁内复核失败凭据、动作、冻结输入及零 Writer 痕迹。历史 `codex_review_failed` 只有在 action/state、收据指纹和零 Writer 痕迹全部复核通过后才计入隔离并参与人工对账；损坏现场按 `review_quarantine_invalid` 技术失败处理，不自动重试。异常阻断不证明历史单据已隔离、未写入或已完成。

人工明确授权后，滚动入口可通过精确的 `--recover-action-file` 恢复 `codex_decision_invalid` 或 `codex_review_failed`。恢复前必须核对 action/state 均为 `needs_admin / not_requested`、冻结身份与材料完整、最新 attempt、实时待办身份一致且无已办命中，并确认不存在 Writer 边界、执行回执、审核决定、意见或提交信封；进程失败还必须通过原始失败凭据与指纹校验。只创建新的冻结 attempt，保留原隔离证据，不直接写 OA；不带显式恢复参数时仍跳过历史隔离，不自动重试，未知写入规则不变。

每个 attempt 的当前动作由 `state.json` 派生；新 `action.json` 是绑定身份、路径与原始字节摘要的不可变引用。同一候选重复调用时返回原动作和 token，不重复取数。积压中还存在未打开候选时，后续 `next-action` 可以打开另一单据，供独立审核调用处理。存储与历史兼容边界见 [ADR 0012](../../../../docs/adr/0012-canonical-attempt-state.md)。

同一 attempt 的完成转换使用 attempt 锁单飞，后到的重复决定不能覆盖已闭合结果；不同 case 不共享提交锁。当前入口停止写入 `current-action.json`，已有文件保留原字节。

审核者不得修改 `action_file`、审核包或材料，不得读取其他 attempt，不得自行组织路径、计算指纹、选择 requestId 或调用 OA 写入。

六类财务的自动采集、下载或读取失败返回 `needs_admin / material_collection_failed`，稳定细码及脱敏计数保留在失败回执；不生成可提交审核包或“全部未完成核对”的意见。采集返回成功但申报字段、资源、权威资料及准备内容全空时，同样停止。旧 `material_state=degraded` 冻结包只用于历史验真，重新领取及完成入口均在 Writer 前阻断，原材料、审核包和已提交回执保持原字节。正常材料上的业务 `fail` / `uncertain` 继续按原合同提交 AI 意见。包装图片不可读仍按其原技术失败合同处理。

### 审核未完成的人工交接

资源等待与 AI 审核失败分开处理。CPU 高负载本身属于 `pressure`，不单独触发 `critical`。当前原生生产审核固定最多 3 个全局模型槽位，不自适应扩容；材料准备及 Writer 均保持 1 槽，原全局 FIFO 不变。历史录制诊断算法保留自适应并发规则，不用于原生生产启动。

宿主健康采样未知时，控制器标记 `resource_paused=true`，暂停新领取、准备完成后的审核派发及排队 Writer；已在途操作仍受原超时和安全校验约束。周期性复测恢复可信后继续原队列，排队等待不启动 AI 审核计时。等待期间仍响应显式停止，已进入 Writer 的操作不能被重放。可用内存低于 12% 仍为 `critical`，即使 CPU 采样缺失也立即停止。资源繁忙或等待不生成人工交接意见。

当前滚动审核遇到可信写前 `codex_process_timeout` 时只结束当前单据，保留待办且不当轮重试，不自动生成或提交行政交接意见。进程回收或失败留证不可信时仍按安全门停止。历史 `preaudit_manual_handoff` 是说明 AI 未形成有效业务结论、请财务人工审核的行政记录，不是 `preaudit_review_decision`，不含 `items`，不计为完成业务审核覆盖；原固定项目规则只适用于有效业务决定。包装与手工 preview 不使用此分支。

历史人工交接的校验继续保留。超时须确认审核进程组退出并保存绑定当前 action token 的失败凭据；宿主在同一完成锁内复核动作、冻结输入、当前授权及零 Writer 痕迹，身份、待办、begin-submit、终态回执与后置校验仍全部执行。历史意见只说明审核超时，不包含原始错误内容，也不生成业务通过/不通过结论；保留这些读取与校验能力不授权当前自动提交。

`codex_decision_invalid` 不得生成 `preaudit_manual_handoff` 或触发 OA 写入。无论原始输出不是有效对象，还是有效 JSON 对象在写前结构校验失败，当前生产调度都保存脱敏拒绝凭据与失败轮次，保留待办且不当轮重审，继续其他单据；不按连续失败或第三个失败轮次自动永久隔离。无法可信留证时仍走安全隔离或阻断。禁止把“审核结果格式异常”写入 OA；最终人工交接校验边界也必须拒绝该失败码。

登录失效、身份/材料/授权不可信、未知写入、当前停止门、本机 critical 或健康状态未知仍停止；进程回收未确认时不得转交接。历史隔离项不自动重试，已办意见不追改。外层监督器收到停止信号或发现直接父进程退出后请求控制器停止；等待超过 30 秒保存 `controller_stop_timeout`，查询返回 blocked，仍等待实际退出和在途 Writer，不伪造退出码或成功。监督器死亡而回执仍为 starting/waiting 时，只读查询返回 `controller_supervisor_lost`，原回执不改写。主聊天结束但宿主进程仍活着时，尚无可靠事件接线，不将进程存活检测等同聊天生命周期检测。

审核子进程另保存脱敏阶段诊断：动作令牌摘要、固定事件计数、最后观察阶段、耗时、退出码和标准错误固定信号。首错留证另保存原生错误码、HTTP 状态及有界脱敏错误正文；缺失字段明确标未知，不推测填值。具体字段与读取边界见[小时生产合同](../../../../docs/runbooks/hourly-production.md)。不保存完整标准错误原文或审核决定到错误凭据。固定信号只表示观察到相关关键字，不单独证明网络或服务故障根因。无完成用量事件时，汇总状态为 `unknown`，数值累计字段不代表实际消耗为零。

## 结构化审核决定

TPM 新包若含 `company_comparison_contract.version=1`，决定顶层还须包含 `company_comparison={"observed_refs": [...], "result": "match/mismatch/unknown", "reason": "各已读原值及角色冲突的业务判断"}`。合同由冻结材料整体指纹和 `state.company_comparison_contract_version=1` 绑定；第四项 `entity_consistency` 的状态分别对应 `pass/fail/uncertain`。通过结论保留实际报销公司、支付公司与购买方全称及一致性结果。宿主核验原值引用覆盖与结构，不按公司名称生成业务状态。角色与缺口规则见 [TPM 规则包](flows/tpm-small-marketing-fee.md#公司比较观察合同)。历史无标记包保持原合同，整体降级且无公司事实时不添加该观察。

宿主可以使用 `python -m apps.oa_finance_preaudit.review_decision --action-file ... --input ... --output ...` 从当前冻结动作装配缺少的协议绑定字段。它只补 schema、kind、动作 token、流程键和规则版本，不生成或修改业务项、页面分类、证据或结论；已有冲突绑定保留并由完成边界拒绝。输出必须是 attempt 外的新临时文件，冻结资料改变时停止。普通 Worker 使用同一装配函数；历史只读容量复验保留专用 token，不进入装配或写入。

有限跨轮恢复是生产宿主的独立显式授权：`--recover-confirmed-no-write-once YES` 只选择当前授权流程和可信单据级确认未写入的允许失败类别，且只对首个 attempt 恢复一次。候选选择不构成提交授权；后续仍执行恢复入口实时复核、单据 capability、唯一 Writer 和后置核验。已有后续 attempt 或恢复失败时给出人工接管与期限，不无限恢复。默认调用与未知写入规则保持原合同。

已提交后由用户明确撤回的单据使用[撤回恢复](../../../../docs/agents/quarantine-repair.md#已成功提交后由用户撤回)。它保留原 `closed/submitted` 及成功回执，创建下一 attempt 重新取材，不属于 `confirmed_no_write`。原接收时间不变时，只有明确撤回确认、可信原成功提交及当前唯一归属验证才能恢复；普通控制器仍保留防重复提交门。恢复入口只准备资料和新授权，实际意见仍由新轮审核者生成并由 Writer 提交。


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
- 任一固定项目或包装补充项为 `fail` 或 `uncertain` 时，`overall` 必须为 `needs_human`；固定项目全部为 `pass` 且无包装补充异常时必须为 `pass`。
- 不增加 requestId、人员、公司、写入模式、意见文本或其他字段。

### 按次油补观察

新冻结差旅材料中，主表 `sfdc`（是否带车）已读取为“按次”时，材料、action、state 和审核包绑定 `fuel_trip_contract={"version":1,"mode_field_refs":["原字段path"]}`。材料指纹保护该升级标记；删除协议文件中的标记不能把新包变成旧包。旧冻结材料没有标记时按旧决定合同兼容，不修改历史材料、决定或意见。单一区间没有该标记，保留原决定结构。

带标记的决定增加顶层 `fuel_trip_observation`，实际 Worker 使用同一 schema。下例只说明结构，读数、配对与业务状态由审核者依据原材料形成：

```json
{
  "fuel_trip_observation": {
    "outcome": "grouped",
    "groups": [
      {
        "trip_basis": "该次实际带车行程及配对依据",
        "basis_refs": ["能够回查配对依据的字段或原图引用"],
        "start": {"reading_km": 10000, "locator": "起始日期或可定位行程", "image_ref": "原始页引用"},
        "end": {"reading_km": 10120, "locator": "结束日期或可定位行程", "image_ref": "原始页引用"},
        "distance_km": 120
      }
    ],
    "total_km": 120
  }
}
```

- `grouped` 至少一组，`total_km` 等于每组 `distance_km` 的合计；每组差值等于模型所读结束里程减起始里程。仪表原单位不是公里时，由审核者记录原单位及换算依据后填写公里值。宿主不读图取数、不按文件名生成读数、不生成行程配对或费用 verdict。
- `mode_field_refs`、所有 `basis_refs`、起止原图引用均须进入油补项 `evidence_refs`，引用必须属于当前冻结材料。每端的 `locator` 保留日期或可回查行程；相同起止照片对、完全相同照片的重复上传不产生新的组。申报里程按发票类型分行不构成行程配对依据。
- 配对未确认时用 `outcome=uncertain`、`total_km=null`，可保留已确认的组，也可使用空组；油补项必须为 `uncertain`，并给出具体缺口。无油补申报用 `outcome=not_applicable`、`groups=[]`、`total_km=null`，由审核者在油补意见中明确不适用原因。
- 分组与算式还要简记在油补 `conclusion`，不能只保存在观察对象或工作笔记中。金额是否合规、与申报是否一致仍由模型判断；宿主只核验结构、归属引用、组内减法及合计算术，不根据字符串或读数自行生成财务意见。

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

包装 V2 的四项之外异常使用 `supplementary_checks`，新只读审核草稿始终提供此数组，无异常时为 `[]`。已冻结的旧决定未携带该字段时按空数组兼容；V1 和六类财务不接受此字段。

```json
{
  "supplementary_checks": [
    {
      "page_ref": "原样复制对应冻结页面引用",
      "status": "fail 或 uncertain",
      "conclusion": "版本或区域定位及实际疑点，保留单行"
    }
  ]
}
```

每个异常位置单独记录，来源必须是本次已观察的包装页；分类为 `uncertain` 的页面只允许 `uncertain` 补充项，`non_packaging` 页不提供补充证据。空结论、重复位置与结论、额外字段及不可解析的页引用在写入前拒绝。补充异常独立影响整体结论，固定四项数量、顺序和状态不变。宿主仅校验和渲染审核者的观察，在四项之后合并为唯一一行 `补充校对`，逐条保留定位及疑点；Excel 对照保持末行。全部非包装分支只接受空数组并保持原单句意见。

V1 包装决定必须用 `material:prepared.images.<resource_id>.page-<number>` 在 `evidence_refs` 或 `missing_evidence` 中覆盖每一页；V2 改由 `visual_review.pages` 完成逐页分类覆盖，并把包装页声明的固定项逐项绑定到对应证据或缺口。脚本会先验证图片可读、manifest 页数与准备页一致，再验证视觉分类、版本四项矩阵和证据绑定；采集脚本不得生成图片观察、分类或校对结论。

`small_marketing_fee:2` 在原五项末尾增加“支付公司一致”，并在 action、state 和审核包中显式冻结规则版本。发布前未带显式版本的 V1 冻结包只接受原五项决定；V2 只接受六项决定。两版兼容只用于完成各自已经冻结的审核包，不得删改版本字段把 V2 决定降级为 V1。

`tpm_small_marketing_fee:4` 保持四项，在第一项增加制作单价判断。适用科目的材料含宿主冻结的 `tpm_price_reference`，包括参考表来源 SHA-256、目录 SHA-256、原表行号及 H 列含税指导价；缺失或不可读以状态保留。它是共同价格基准，不计作当前单据附件，不证明费用发生。价格证据通过 `material:tpm_price_reference.rows.<索引>` 引用；通过但没有有效行引用时只降级第一项，不修改第三项金额。材料整体指纹同时保护冻结价格，后续配置更新不影响已冻结材料。旧 V3 包仅回查，新工作进程不续审旧版本。

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
