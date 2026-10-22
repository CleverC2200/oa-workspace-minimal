# 小时生产调度手册

本手册与 `config/automations/oa-skill-prompt.txt` 是待发布的调度配置候选。启用前须按 [发布约定](../../CONTRIBUTING.md#开发与生产隔离及发布) 完成代码及手册发布、生产同步和只读核验，再通过原生 automation_update 更新现有自动化并回读。海外阶段的提示词可先更新仍暂停的原 cron；入口与独立授权未部署时保持暂停，并在能力检查处阻断，不将配置更新视为生产能力已上线。变更默认供应商时，先按[默认供应商迁移](production-source-release.md#默认供应商迁移)更新仍暂停的原 cron 提示词，使同步前的候选配置门可核验；源码同步失败时保持暂停。当前本地文件不表示生产已经加载。保留实际外层模型、推理级别、频率、状态、通知偏好、项目和目录；业务审核默认 model=host-current-chat reasoning_effort=unknown model_provider=openai service_tier=unknown transport=host_chat。外层调度保留 scheduler_model=gpt-6.1-sol scheduler_reasoning_effort=medium。OpenAI 审核由本轮固定 3 个 App 子 Agent 承载，复用原对话，不启动 OpenAI 审核 CLI；显式选择 DeepSeek 时通过 Responses API 使用 OA_REVIEW_API_KEY，requires_openai_auth=false，CODEX_HOME 按调用隔离。不调用 codex login、不复制认证、不改全局配置。外层调度继续使用现有原生 OpenAI 认证。历史回执保留原供应商、模型和推理身份，不改写为当前配置。

每轮必读“每轮启动与实时对账”；结束必读“结束对账与超期”和“清窗与留存”。其他章节仅在对应条件出现时读取，且原安全边界始终适用。

## 每轮启动与实时对账

首先读取项目 AGENTS.md、现行财务 Skill 以及 docs/runbooks/blocking-recovery.md。生产不得开发、创建工作树、切换分支、修改代码、提交、推送或自行拉取版本。核对唯一 main、工作区干净、HEAD=origin/main、依赖及已发布入口、本地授权清单包含本次流程；缺口停止并保留原稳定码。

自动化运行记忆只使用 /Users/synear/.codex/automations/oa-skill/memory.md，确认在工作树之外且可写。不要把界面显示的 $CODEX_HOME 路径按字面当作相对路径。不可写使用 automation_memory_path_invalid 停止；不回退到工作树。

每次启动前及原控制器真实退出后的实时对账，均按 docs/runbooks/blocking-recovery.md 的文件输出方式执行一次。运行中采用下述原回执监视，不把启动门禁用于尚在提交的控制器。先新建私有临时目录，再在同一 exec_command 运行：
status_dir=$(mktemp -d /private/tmp/oa-status.XXXXXX) &&
.venv/bin/python -m apps.oa_finance_preaudit.cli status --check-live --reconcile --summary --output-file "$status_dir/status.json"
完整保留原 session_id、exit_code 和输出；运行中使用原 ID 的 write_stdin 等待，不把工具等待窗口当业务超时。仅在 exit_code=0 后解析终态：stdout 保留原安全门和计数，完整 JSON 已保存到 status_file.path，并返回字节数和 SHA-256。明细的 display_truncated=true 只表示未在界面展开，不代表待办或人工核对项为零；按需校验同一文件的字节数/哈希，再分字段、小批量读取全部必要明细，不将整份文件重新打印。stdout 若仍被宿主截断，从本次已知且确认成功的文件提取门禁，不为补读重复查询 OA。原会话、文件或校验结果不可信仍停止，不用部分数据放行。所有身份、授权、continuation、锁、Writer 和交接安全判断保持原要求。

启动前发现正常活动控制器：只有 running 清单、当前代码一致、锁所有者一致、restart 已验证且 monitoring_action=wait_for_current_run 时，跳过本轮，不发故障提醒、不进入包装或海外、不清故障窗口。已经由本任务启动的控制器仍归本任务收尾，后续 active/skipped 不能当成他人的运行。

继续须凭据、OA 接口与专用浏览器会话正常、执行身份 verified、授权与冻结可信、所有控制器和 Writer 空闲、上一轮终态及子进程回收可信。continuation 须 status=ready、blocking_reasons={}、isolated_pending为非负整数且等于pending_quarantined、scope与计数一致。code_matches_current=false 若只表示已终态旧版本清单，且当前 Git干净同步、ready_for_start=true、children_reaped=true、restart_verified=true，其余门禁可信，可由 fresh process加载新版；不复用旧进程。不因笼统 degraded 或可信隔离数量停止其他待办。

普通轮次按 OA 当前待办重新取材。`historical_continuation` 和 `historical_quarantine` 保留旧本地状态的诊断，启动使用当前 `continuation`；`deferred_pending` 是本轮暂缓核对的请求，不计为已提交。未进入 Writer 的可信旧尝试可重新采集，旧决定不复用；提交已开始、结果未知或身份/边界不可信的请求暂缓，其余可信待办继续。当前提交仍逐单验证本轮冻结、授权和目标。

生产审核固定 3 槽，OpenAI 每轮只创建 3 个 App 子 Agent 并复用对话，控制器直接发布并轮询请求；DeepSeek 沿用独立 API key 的 CLI 工具承载。生产弹性和旧批次执行已退场；材料准备和全局 Writer 均为 1。每次启动产生独立 `run_id`，后续 pending、heartbeat、派单、包装和收尾命令都必须显式带同一 `--run-id`；旧轮证据只按 legacy 只读路径核验，不能回退到旧轮。门禁通过后只调用一次：

```bash
.venv/bin/python -m scripts.finance_preaudit_run --owner-task-id <当前任务真实UUID> --review-provider openai --review-transport host_chat -- --rolling --fixed-capacity --capacity 3 --max-capacity 3 --confirm-write YES --confirm-alerts YES --workflow travel_expense --workflow business_entertainment --workflow business_support_fee --workflow small_marketing_fee --workflow tpm_small_marketing_fee --workflow general_expense
```

上述参数选择本轮 OpenAI App 审核；其中 `--confirm-alerts YES` 只适用于本轮明确授权自动告警的调度任务。手动单轮无告警授权时省略此参数或显式传 `NO`，按[执行回执](../agents/controller-execution.md)保留本地失败及历史告警窗口。用户要求某轮改用 DeepSeek 时，同时把 `--review-provider` 改为 `deepseek`、`--review-transport` 改为 `cli`；审核型号与推理由现行供应商配置成对选择，原门禁继续有效。每轮回执记录实际配置，不改历史证据。不在原控制器未退出时发起另一个供应商的控制器。后续独立包装调用通过 `OA_REVIEW_PROVIDER` 沿用该轮选择，具体见[执行回执](../agents/controller-execution.md)。

### 原运行监视

OpenAI 原控制器启动后立即执行[固定 App 审核槽原生派单](../agents/controller-execution.md#固定-app-审核槽的原生派单)。主对话创建并登记本轮恰好 3 个原 Agent，注册成功后按执行回执入口独立续首次父心跳，再读带 `--run-id <本轮run_id>` 的 pending；逐单 `followup_task` 复用；在 30 秒以内续父任务心跳，读取原执行回执和本轮 pending，先真实派单再 assign。子 Agent 的完成只表示审核返回，Writer 仍串行提交。保持原 session 的等待与派单同时推进，不等控制器退出才处理请求。身份不能替换，不递归创建审核者，不回退 OpenAI CLI。

入口在开始本轮审核及启动任何 DeepSeek CLI 前读取审批冷却记录以及审批 run/instance、PID 启动时间、进程组和心跳。其他任务拥有的可信正常活动轮返回 `approval_startup_gate/skipped`，本轮不叠加控制器。本任务仍拥有活动原轮时拒绝另起一轮，继续等待原工具 session 并完成原轮收尾。45 秒以上没有可信心跳只返回 blocked，不以静默判断卡死；明确阶段超时或已失去原控制器且进程归属仍可信时，只收尾登记的审批只读进程。登记不完整、PID 被复用、进程组回收未知或 Writer 活动均阻断，不猜 PID、不批量杀 Codex。旧版本缺少归属登记时，即使传入人工确认回收参数，也不能绕过此启动门。发起前未登记成功的 CLI 槽位保持占用，须人工核验。

worker 与外层监督进程每 5 秒观察本地原生错误，不产生模型 token。OpenAI App 的限流与服务过载按下节有限重试处理。官方用量上限、模型不可用或账号认证拒绝仍立即封住新审核，结束当前审批并沿现行 3 小时冷却；其他安全停止门保持。DeepSeek CLI 仍按首次官方错误停止及原冷却合同处理，不读取 OpenAI Desktop 错误。旧冷却与原始错误证明保留，不因新策略改写。官方主对话尚未落盘的错误和 CLI 输出首次警告前的内部行为不保证可观测。定时任务的首次模型唤醒发生在本地入口之前，不能由此阻止；本地门控制的是审批链。

#### 限流和过载的有限重试

本节只适用于 OpenAI `host_chat`。同轮首次和第二次可信限流或服务过载允许模型审核重试；第三次结束当前轮次，等待下一次独立授权执行，不新增三小时冷却。财务和后续包装共用本轮计数。财务监督者每 5 秒观察，包装阶段由父对话每次心跳观察。三槽共享预算，同一原生错误来源重复扫描只计一次；只计本轮开始后、属于本轮父会话或已登记审核者的错误。认证、身份、冻结、父租约、回收及未知写入仍独立核验。

父对话每次心跳读取 `transient_retries`，并查看对应原 Agent 的可见执行状态。发现新增限流或过载时，先确认当前单据未被宿主接收、没有提交或 Writer 边界，原请求仍在截止前，原 Agent 的失败调用已结束。每条新增错误最多唤醒一次原 Agent，传入同一个当前请求路径和逐附件读取要求，继续形成新决定；原分配保持，不再次 assign，不延长原请求期限。普通重连、I/O 错误及无进展不是官方限流的证明，按当前单据异常处置。

第三次错误沿 `codex_transient_retry_exhausted` 有序停止。控制器只在只读进程已回收、Writer 空闲且停止来源与同轮计数证明可信后，撤销可信未写入动作的原授权并封存中断证明。原轮已失败终态后仅清理这一精确轮次停止标记，App 原三槽仍须统一收尾 verified。下轮从新 attempt 重新取材；有决定、Writer 边界或未知写入的单据保持原保护，不使用此路径恢复。包装阶段保留已完成的财务清单，关闭当前交换并完成 App 统一收尾，核验原执行终态、预算及 Writer 空闲后只清理同一精确停止标记。定时任务状态不因此改变。


明确的普通网络重连警告允许同一原生 CLI 继续，不触发停止或冷却，也不授权审批层重试或另建 CLI。已核实的原生 WebSockets 切换至 HTTPS 提示按同一警告处理，原 deadline 和执行次数不变；只识别精确提示前缀及传输信号，不按泛化的 fallback 文案放行。官方限制优先于“重连/稍后重试/传输切换”措辞；原生 `codexErrorInfo`、顶层 `code` 和 HTTP 状态中的已知官方限制均按对应模型传输的停止或有限重试合同处理。警告豁免须同时满足可信事件类型和各代码、状态字段的传输合同，不能用一个已知字段遮住另一个未知字段。

官方错误的停止与重试范围按上述合同执行。普通失败终态、未知原生错误、损坏事件、CLI 非零退出、超时或缺少可信完成/决定只停止当前单据；不能当作成功，也不当轮重试。可信写前失败留证后保留待办，继续其他单据；不因连续失败或达到第三个失败轮次停止整轮或自动永久隔离。`blocking` 表示原生事件阻断当前审核，不等同于全局停止门。人工停止、主对话退出收尾以及身份、授权、回收、写入结果或证据完整性不可信的安全门仍保留，不能用本规则豁免。

每个 launch 单独保留不可覆盖的首次观测、首次阻断和首次官方限制证据，普通警告不会遮住后续官方错误。`review-notice-*.json` 绑定 run/instance/launch 的 SHA-256、action 指纹、原生事件类型、分类及观测时间。新 schema_version=2 保存有格式和长度限制的原生错误码、有效 HTTP 状态及来源，不再因旧白名单而丢弃未识别枚举或 502/504；字段缺失与形状不可信被省略分别标注。这些详情只用于追溯，不修改停止或冷却分类。

新首错文件为0600，只采集原生错误信封的 message，不留存普通消息、工具输出或整个响应。`error_message` 是技术词白名单过滤后的文本，不是完整原文；凭据、Cookie、URL、邮箱、路径、引号内内容、数字、非ASCII及非技术词用占位标记替换。正文最多2048字符，超过8192字符的输入只保留占位标记；原文摘要、字节数、脱敏和截断标志明确保留。原生未提供 message 时正文及摘要为 null；非 JSON stderr 的首个观测可能只是片段。不能据此承诺所有上游错误都有明确根因，也不能把缺少限制码解释为账号无封控。旧 schema_version=1 仍可只读校验，历史缺口不补造。

`review-process-*.json` 保留原诊断，并分别记录实际停止请求与进程退出的观测时间，未观测到的时间保留 null。旧诊断和早先 launch 的证据不改写；文件冲突或失信保持阻断。

只有所有登记的只读进程已回收、Writer 空闲、控制器维护租约可信，才撤销本轮未写入动作的旧授权并保存中断证据，OA 待办不变。下一独立轮次通过适用的冷却门及原身份、授权、待办/已办和冻结门后，用新 attempt 重新读取当前材料；不续跑旧审核，不复用旧决定。Writer 边界、决定/提交文件或未知写入存在时不自动恢复。此技术中断恢复独立于原“首 attempt 一次”隔离恢复，后者限制不变。到期只清除精确的官方冷却停止标记，不清人工、损坏或未知写入停止门。skipped/blocked/官方冷却停止不进入包装、海外、清窗或程序修复派单。

保留原工具会话并等待真实终态。需要进度时，按[原执行回执](../agents/controller-execution.md#只读进度等待与终态取回)使用同一 owner_task_id、invocation_id、receipt_file 的 `finance_preaudit_run --progress`；已绑定工具 ID 时同时精确核对。它不查询 OA、不启动或停止控制器，也不授权包装、恢复、清窗或留存清理。回执等待、清单未收尾与 children_reaped=false 是运行期状态，不按固定静默时长判断故障。归属、锁、代码、进程或回执证据失信仍转人工核对，不认领或启动另一控制器。

`status --check-live --reconcile` 依次读取待办、已办和本地状态，不能表示运行中的原子快照；输出的 `reconciliation.usage` 明确标记 startup_or_terminal、non_atomic 和 original_execution_progress。raw continuation 仍保持严格阻断，不能用活动控制器豁免 unknown、身份冲突、submitting 或 submitted。运行中不执行这次完整对账，也不根据旧待办与新本地提交状态的组合停止原运行。原控制器继续执行材料、身份、授权、冻结、Writer 未知结果及轮次后严格对账的安全检查；等待其真实退出后，才取得一次新鲜完整结束对账。持续冲突仍阻断，不重复查询直到放行，原回执和所有已取观测文件保留。

## 停止隔离与有限恢复

用户在当前聊天明确要求提前运行一轮时，可使用[人工单轮冷却豁免](cooldown-override.md)。该能力仅豁免绑定的历史冷却等待，不进入小时调度配置；新官方首错、身份、授权、冻结、未知写入和回收安全门保持有效。

停止标记只按项目现行受控恢复入口处理。普通 controller_cycle_stop 在确认可靠回收后由控制器收尾；残余、人工、环境或带具体失败原因的停止门不得手工删除、改文件或用清告警代替恢复。身份、待办已办冲突、损坏冻结/边界、当前未知写入、活动 Writer或未回收进程继续硬停止。

控制器 failed、当前未知写入、身份/授权/冻结/隔离不可信、整个 worker 看护超时或进程回收不明、本机critical或停止门触发时，立即停止新增领取和写入，等待原调用收尾，保存首个错误和已提交证据。普通 CLI 失败不属于这些整轮安全停止条件。已有 notification 不重复发送。业务 fail/uncertain 为正常意见；completed_with_quarantine 可继续其他可信单据，但须重新完整对账。

确已确认的外部对账门禁故障需要受控停止时，使用现有 `runtime_control.request_stop("production_reconciliation_blocked")`，并留存触发它的原对账回执。停止文件首次写入后不覆盖；原真正首错优先，具体原因贯通控制器 primary_error_code 和单次终态通知。普通人工、环境、损坏或未知停止保留原阻断和通用告警语义，只有可信 controller_cycle_stop 能按原回收门清理。不得因混合运行快照写停止标记。

普通轮次不再由“首 attempt 一次恢复”限制决定是否处理 OA 当前待办。同轮已领取或失败的身份保持排除，下一轮重新查询 OA，再对可信未提交项重新采集。精确指定目标、旧显式恢复及请求排除仍使用原绑定合同；`--recover-confirmed-no-write-once` 仅为旧请求排除范围保留兼容，不改变普通轮次的选择。未知写入不重试，旧证据不重写。

## 包装待审核

财务原工具 session 与原回执均可信正常终态、children_reaped=true 且新鲜结束门禁可信、Writer 空闲后才处理包装。OpenAI 主对话先 `host_chat_review --operation enter-packaging --owner-task-id <本轮主会话UUID> --run-id <本轮run_id>`，保留同一 3 个 Agent。随后使用原 `cli next-action --workflow packaging_layout_approval --mode controlled-write --confirm-write YES`。closed/not_available 不生成决定或调用完成。

review_required 只读取当前冻结包装材料，逐页直接看原图，不用 OCR、不跨版本拼证据。按[包装只读审核](../agents/packaging-review.md)调用原 packaging_review；OpenAI 加本轮 owner-task-id 并持续原生派单/心跳，按需独立复核使用另一原槽，真实观察和父语义确认通过后走 App complete 包装入口。DeepSeek 沿用独立 API key/空 CODEX_HOME、原工具回执和调用者语义门，确认后才使用原 complete-review。供应商或来源失信不回退，不重提已提交意见。

包装完成后按[统一收尾](../agents/controller-execution.md#app-统一收尾)保存本轮 3 个 Agent 的原生中断及终态来源、原工具退出和 Writer 空闲证据，取得 `status=verified` 后再进入海外阶段。取消时停止派单并收尾原控制器，不进入海外；迟到决定拒绝，未知写入保留原回执和人工处置。

## 海外采购付款

财务原 session 及回执可信正常终态、包装完成，且 OpenAI 已按[统一收尾](../agents/controller-execution.md#app-统一收尾)取得 `status=verified`（DeepSeek 对应原进程已回收）、全部 Controller/Writer 空闲、停止门与新鲜结束门禁可信后，主对话进入[海外采购发票技能](../../.agents/skills/oa-overseas-invoice/SKILL.md)。这是与六类财务队列分开的串行阶段，不新增审核槽。

先运行 `scripts.overseas_invoice status` 核对四项入口能力及独立授权；`write_enabled`、`no_invoice_enabled` 均应为 true。缺少能力或授权时报告待部署/待启用，不通过 UI、直接接口或改材料绕过。使用 `pending` 返回的当前身份和 FIFO 顺序逐单采集；已有回执的待办先保留并报告冲突，不重新发送。用户已授权该流程有发票时识别回填，无发票时保持字段并提交当前 RPA 节点；具体识别、空来源和命令只以[执行说明](../overseas-invoice.md)为准。

成功要求字段回读一致、已办命中且待办消失。`unknown`、中断或回读冲突停止海外阶段，保留原回执，仅使用只读 `postcheck` 核对，不重试。流程节点配置错误需要管理员处理，不替换流程节点或操作者。海外字段、提交及后查按独立回执统计；财务完成、网页关闭或待办减少均不代替海外成功证据。结束清窗前核对本阶段无未知写入或未完成处置。

## 结束对账与超期

结束按前述文件输出方式重新取得完整实时对账，分别统计有实际提交/后查回执的完成、恢复、人工交接、隔离、未领取、当前未决写入与不支持流程。pending_dispositions 按当前 RPA received_at计龄；已有后续失败转 manual_takeover，默认责任目标绑定现有告警接收人。未经处理人实际确认，不调用 case_recovery --confirm-takeover YES、不假称已接管。超出 escalation_due_at 或 resolution_due_at、4小时仍无人接管、同类整轮故障连续2轮，报告处理目标、具体下一步与期限；存在真实超期时调用已发布 session_alert --error-code preaudit_pending_overdue --confirm-send YES 一次。提醒不阻断其他可信处理、不恢复单据。owner unconfigured 或绑定配置变化必须明确报告，不能编造负责人或改配新接收人。

## 故障通知

实际故障告警用项目现行有限稳定码：基线 production_baseline_invalid；授权 production_authorization_invalid；环境 production_runtime_unavailable；身份/冻结/对账 production_reconciliation_blocked；交接/回收 production_handoff_blocked；记忆路径 automation_memory_path_invalid；主机critical production_health_critical；已识别审核、材料、Writer、登录错误保持原码，无法映射的实际程序阻断用 controller_failed。review_decision_invalid 映射已支持 codex_decision_invalid，保留本地原细码。材料 reason与failure_detail_code都保留；细码为六类会话码时通过 --failure-detail-code 传递并由入口重查，返回 not_required 不再用通用码补发。同一分支只调用一次，unknown/started不代表送达，不自动重试、重发、安装或修复消息工具。只读状态本身不发消息，状态查询不能清故障。使用 session_alert --status核对，sent仅表示服务端发送确认。

## 清窗与留存

只有财务、包装、海外、结束门禁全部可信，无当前隔离、未决写入或超期处置时，才用现行 --clear-production --confirm-recovered YES结束生产故障窗口。清除只改变窗口，不改变单据或回执。prune 仅在原控制器终态和全部Writer空闲后执行；retention_plan_invalid保留证据，按controller_failed映射提醒，不手工删除运行记录。

## 程序故障登记与汇总

程序故障默认登记或更新 GitHub Issue 并汇总。按[Bug 记录规范](../agents/bug-ledger.md)和[Issue 追踪](../agents/issue-tracker.md)查找同范围问题，记录脱敏故障码、本轮来源任务、证据位置、未完成项与人工下一步。保留历史记录；原因未明时只记录相关现象，不能把隔离或普通队列为空当成修复完成。

审批父聊天负责监督、审核派单和收尾。故障登记不自动创建开发修复任务，也不自动推进开发、推送、PR、合并或生产同步。已有当前同范围的明确授权继续有效，各动作按[发布约定](../../CONTRIBUTING.md#开发与生产隔离及发布)执行；需要开发时在保存的开发克隆内处理，生产审批与修复分开留证。

已有修复任务沿用 `repair_dispatch status` 查询归属，再用 Codex 任务工具核对真实进度。`reserved` 先确认原创建结果，`running` 检查绑定任务，2 小时无进展升级；通知 unknown、只有 clientThreadId 或创建结果不明时保持待确认，不重复创建。每阶段由原 reservation_id/thread_id 记录进度，真实终态确认后才受控 finish，保留窗口历史。另有明确派发授权时，按[阻断恢复](blocking-recovery.md)的原派单入口处理；登记故障不会新增该授权。

重大业务规则或授权变化、未知写入判断、放宽门禁、删除证据、无法确定原因或需要新凭据时，保留具体原因和人工下一步。原身份、授权、冻结、Writer、未知写入及停止策略保持；审批父聊天继续完成原会话等待与安全收尾，不能用开发任务代替这些责任。

故障归属按本轮实际 run_id/controller_instance_id/来源任务关联，告警窗口只是通知历史，旧日期不得代替本轮。换窗口键前查询当前及历史派单记录的同轮归属；已有任务或归属不明不创建第二个任务。收到技术退出码前不按固定静默时长或浏览器关闭警告认定卡死。
