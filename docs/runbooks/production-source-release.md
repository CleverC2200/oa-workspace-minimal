# 生产源码发布门

开发改动合并远端后，生产仍需要独立授权的源码同步。统一入口是 `apps.oa_finance_preaudit.production_release`；默认只读预检，`--confirm-sync YES` 才请求更新源码。各模式都不提交审批、不改停止标记、不启停自动化、不安装依赖、不自动 fetch。

先现场核实目标为生产克隆的唯一干净 `main`，明确预期远端提交、生产项目 ID，以及本轮应保留的实际调度频率。授权范围内 fetch 后，从已审查的开发候选执行下列入口。`--automation-file` 使用本机原自动化 TOML；不得把它、凭据或现场业务证据提交到仓库。

```bash
python -m apps.oa_finance_preaudit.production_release \
  --target-root /Users/synear/HermesWorkspace/profiles/oa/projects/oa-workspace-minimal \
  --expected-revision "$release_revision" \
  --automation-file "$approval_automation_file" \
  --production-project-id "$production_project_id" \
  --expected-schedule "$preserved_schedule"
```

上面的变量必须来自已核实的当前发布和调度配置；不能把待验证值直接复制为期望值。预检通过后，在同一已授权入口追加 `--confirm-sync YES`。程序将重新预检，不依赖上一次缓存回执。

门禁要求生产独立克隆、唯一工作树、干净 `main`，实际 origin 与预期私有仓库相符，实时远端、`origin/main` 和预期发布提交一致，现有 HEAD 可快进。配置检查读取预期提交的 Git 文件内容，避免旧模板漂移阻止修复升级；同步后再检查实际生产树。实际自动化必须符合原生 GPT 6.1 Sol、medium、default、固定 3 槽且禁止弹性扩容、既有六财务流程和已确认项目及调度合同。

整个实时状态检查、源码更新和末尾检查持有目标 runtime 的非阻塞 controller/global Writer 租约。请求 Writer 或旧 Writer 活动时拒绝。状态子进程使用目标自己的 `.venv` 和源码，等原进程终态成功后按完整证据文件的路径、字节数及 SHA-256 复核；不使用缓存 JSON 或候选源码注入生产解释器。租约内只允许本维护持有的 controller/global Writer；上一控制器必须已终态、子进程已回收、支持新进程重启。历史 manifest 保持原样，即使其代码版本早于当前生产源码。

普通发布拒绝所有停止标记与环境停止请求。当前身份、凭据、OA/web 认证及完整对账 continuation 必须可信；当前未知写入或 pending/handled 冲突拒绝。目标自身解释器另用既有只读校验器核验当前 write-policy 对六类财务和包装的授权，以及当前待办本地 attempt 的恢复合同：review_required 核验完整冻结资料，collecting 核验四个采集落盘阶段的原身份、已有材料、恢复 provenance 和未写入边界，可信 item 隔离回执核验失败产物绑定，仅材料收集失败免于重验已损坏材料；生成绑定本次 status SHA 的 current-input proof，不凭 continuation 推断这些条件。可信单据隔离允许保留，不因发布恢复、重试或清理单据。恢复审批资格须另按[停止恢复](blocking-recovery.md)的受控维护合同处理；禁止直接删除标记。业务资格未满足时保留停止门。

## 显式停机维护源码同步

已明确授权保留停止门同步修复源码时，在上述入口追加以下参数。预检仍只读，只有同时提供 `--confirm-sync YES` 才请求快进同步。

```bash
  --stopped-maintenance YES \
  --original-receipt-file "$original_receipt_file" \
  --original-owner-task-id "$original_owner_task_id" \
  --original-tool-session-id "$original_tool_session_id"
```

原执行参数来自原轮监督执行的私有回执和原工具 session；不创建替代 attempt，不使用汇总 JSON 或人工写入的终态。程序使用目标解释器及既有 `read_execution` 只读接口重新核验原 invocation、owner、tool session、manifest 和完整日志哈希。原轮必须确实 exited、终态及 children_reaped 可信，与仍保留的 rolling manifest 相符，原 controller 进程组也必须已退出。目标状态、当前授权及冻结校验的每个只读子进程必须终态并无遗留进程组；异常或取消时阻断，只清理本次入口启动的只读进程组，等待退出并核验组内无遗留进程后才返回原异常。回收无法确认时保留原异常及回收失败，不报告同步就绪。

该模式只接受本用户持有、私有普通文件中的精确 `stop_requested:codex_review_technical_failure` 标记。手动停止、未知原因、Writer 未知写入停止、官方错误冷却停止及环境停止请求仍拒绝。实际原 cron 必须是 `$CODEX_HOME/automations/oa-skill/automation.toml`，未配置 CODEX_HOME 时使用 `~/.codex`；文件内 ID 必须为 `oa-skill` 且状态为 PAUSED，拒绝副本和链接替换。项目、调度、模型、提示词及并发合同仍须通过原配置门。源码、身份、授权、冻结、continuation 和 controller/global/request Writer 门禁均保持。

同一维护租约内，在原执行回读、实时状态、配置检查和源码快进前后重复核验停止门与原 cron 的身份、权限、内容哈希；同内容替换文件也拒绝。程序不写这两个文件、不恢复调度、不清停止门、不调用审批模型、不提交 OA。成功回执记录 `stopped_controls_before/after`、`stop_preserved=true`、`automation_preserved=true`、`automation_state=PAUSED`，并明确 `next_process_ready=false`、`write_state=not_requested`、`production_acceptance=not_verified`。停止门继续阻断审批，即使源码已更新也不表示恢复资格。

任一末尾检查失败，回执保留实际更新状态及新鲜的停止门和 cron 观测。文件不可信时明确 untrusted，调度无法核实时明确 unknown；不自动回滚、重试、改标记或恢复调度。

各发布模式的成功回执均区分 `ready` 和 `synced`，记录更新前后 SHA、完整状态证据引用及 continuation，明确 `write_state=not_requested`、`production_acceptance=not_verified`。若快进尝试后的检查失败，API 和命令均返回 blocked、原细码、before_revision、after_revision、requested_revision，以及 source_sync_state=updated/not_updated/unknown；读取 HEAD 失败时明确 unknown。next_process_ready=false、production_acceptance=not_verified 保持，不能把失败当作未更新，也不能自动回滚、重试或提交。普通发布只有源码同步和末尾检查通过，下一轮新进程才具备加载条件；停机维护始终保持 next_process_ready=false。实际生产恢复仍须独立授权、该轮版本、运行及提交后查证据。

## 失去监督后仅同步源码

原监督者退出且真实 wait 无法恢复时，不得修改 waiting 回执或补造退出码。只有当前明确授权保留未知终态、暂停调度、停止门与历史对账阻断的源码维护，才在上述停机参数之外追加：

```bash
  --supervisor-loss-source-only YES
```

该参数必须与 `--stopped-maintenance YES` 同时使用；不提供时仍执行原有真实终态门禁。目标自己的原回执读取器必须核验 owner、invocation、真实 tool session，以及同轮 rolling/mirror manifest 的 failed、children_reaped 和代码修订。原监督者 PID、同轮 controller PID/进程组、登记 readonly worker/pool 和全局模型槽位都必须已退出，未登记完的启动意图也拒绝。标记 closed 的模型槽位还要核对实际 PID/进程组，不能仅凭槽位状态证明回收。官方冷却记录存在时拒绝，不清冷却、不做可用性探测。

固定只读桥接仅使用已安装在目标中的校验器，绑定同轮 progress 的项目、控制器身份和 action 列表。另从目标的最新本地 state 独立核验下述历史采集取消，不把它们补入原 progress。原 execution、tool-session、rolling/mirror manifest、stdout/stderr、progress、worker/pool、模型槽位、同轮 action/state/manual receipt 及这些历史取消的 action/state/manual receipt 的文件身份和哈希在快进前后必须相同。同内容替换也阻断。日志哈希标为 `observed_after_supervisor_loss`，表示丢失监督后的现场观测，不冒充原监督者封存的 wait 留证；不输出日志正文。

当前身份、OA/web 认证、授权、其他冻结材料、Controller/Writer 空闲和配置门禁保持。仅源码维护允许保留已验证的历史采集取消：`material_collection_failed`、`failure_detail_code=oa_run_stopped`，完整且验证通过的 controller-scope manual receipt，write_state=not_requested，无 Writer、执行、意见、提交产物及冻结材料。不要求原 progress 登记，也不据时间相近推断属于原轮；回执将 `historical_stopped_actions` 与原 `actions` 分列，并标明 `historical_stop_attribution=not_verified`。只有这些取消造成的 `pending_failure_not_isolated` 可以留作源码维护例外；数量须与当前待办隔离及 continuation 相符。其他 controller 失败、未知写入或对账阻断仍拒绝。程序不改变原 continuation、失败 state、隔离回执或 attempt，也不把取消列入单据隔离白名单。

成功回执增加 `supervisor_loss_source_only=true`、`original_wait_verified=false`、`approval_recovery_allowed=false`、`original_evidence_preserved=true`。原 exit_code 仍为 null，原 execution 仍非 terminal，业务 continuation 仍可 blocked。源码同步不闭合原执行，也不解除历史业务阻断。末尾留证无法验证时 `original_evidence_preserved=unknown`，同时保留源码是否已经更新的真实状态。恢复审批需要独立授权和独立恢复合同，不能使用本源码维护回执代替。

## 官方冷却停止后仅同步源码

已明确授权保留官方停止门安装修复源码时，在停机维护参数之外追加：

```bash
  --official-stop-source-only YES
```

该参数必须与 `--stopped-maintenance YES` 同时使用，不能与失去监督模式并用。普通发布和原技术停机模式仍拒绝官方停止门。本模式只接受精确 `stop_requested:codex_official_error_cooldown`，原 `oa-skill` 必须 PAUSED。冷却无需提前到期，因为维护不调用模型、不提交 OA；本模式不清门、不更新冷却、不恢复审批资格。

目标自己的原回执读取器必须核验真实 exited 终态、原工具 session、非零退出码、同轮 failed manifest 和 children_reaped。原监督者、controller、登记只读 worker/pool 及全局模型槽位须已退出；closed 槽位仍核验实际 PID 和进程组，启动意图或归属未知拒绝。现有关闭只读证明须绑定同轮 progress、控制器身份、项目根和精确官方冷却记录；当前中断单据必须经目标现有 `item_failure.isolated` 校验，同轮且可信未写入。停止门、原 cron 与冷却文件固定 inode、权限和字节哈希；原 execution、tool-session、manifest、日志、progress、worker/pool、模型槽位、关闭证明、首错文件及同轮 action/state/中断回执的身份和哈希在同步前后必须一致，同内容替换也阻断。

旧版对账可能计入 `isolated_pending`，但未将可信中断计入 `pending_quarantined`。仅源码维护允许逐项解释这一缺口：当前完整只读对账必须 ready/item、blocking_reasons 为空，隔离计数差值必须精确等于本次新鲜输入证明中、同轮待办且经目标现有校验器确认未计入隔离的中断数。原 status 文件及 continuation 不改写。其他计数差异、缺失中断证明、身份冲突、Writer 产物、未知写入和业务阻断仍拒绝；当前身份、认证、授权、冻结、Controller/Writer 租约及发布配置门保持。

成功回执增加 `official_stop_source_only=true`、`original_wait_verified=true`、`cooldown_preserved=true`、`original_evidence_preserved=true`、`approval_recovery_allowed=false` 和同步前完整 `pre_sync_status_file` 引用。源码安装后重新从目标生成完整只读状态和输入证明，不复用升级前的计数解释。`next_process_ready=false`、`write_state=not_requested`、`production_acceptance=not_verified` 保持；只有冷却到期后的独立恢复门禁和新进程验收才可证明审批恢复。同步后校验失败仍报告真实源码更新状态，停止门或冷却留证失信时明确未保留或 unknown，不自动重试、回滚或启动审批。

上一审批真实退出后产生的主聊天 `run_id=null` 冷却也可保留用于源码维护。按[单轮豁免的来源合同](cooldown-override.md)分别核验目标本机官方历史和上一审批闭合，不把主聊天事件归为原审批错误。回执以 `cooldown_attribution=project_notice` 保存当前事件来源，上轮审批关闭证明及所有原证据不变。维护仍不清门、不恢复调度；提前运行必须有当前单轮豁免授权并经独立入口消费。
