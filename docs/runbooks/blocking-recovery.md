# 阻断处置与有限恢复

## 安全门和当前单据

生产只读查询使用生产克隆与其 Python：

```bash
status_dir=$(mktemp -d /tmp/oa-status.XXXXXX) &&
.venv/bin/python -m apps.oa_finance_preaudit.cli status \
  --check-live --reconcile --summary --output-file "$status_dir/status.json"
```

启动前、结束后和其他需要实时对账的查询都使用新建目录中的独立结果文件。命令先以仅当前用户可读写的权限保存并回读校验完整 JSON，再输出原值保留的身份、交接、continuation、隔离计数和告警字段；大明细列表显示为空并标记 `display_truncated=true`，计数不变，这不表示没有待办或无需人工处理。`status_file` 给出完整文件路径、字节数和 SHA-256。完整文件包含历史、所有明细和原安全字段；仅在本机保留，不提交仓库或上传。

必须等待原调用 `exit_code=0` 后解析终态 JSON。工具返回 session_id 时持续等待原会话；30 秒是工具等待窗口，不是业务命令超时。文件目标已存在、写入或回读校验失败均非零退出，不返回成功门禁摘要，不覆盖已有文件。失败留下的文件不能当作成功对账证据。

需要逐项核对时，从该次 `status_file.path` 读取并校验字节数和 SHA-256，再用 Python/JQ 按字段或小批量提取明细，读取全部必需项；不要把整个文件再次打印到工具输出，也不要为补齐显示而重复调用实时 status。stdout 仍被宿主截断时，只能在确认原调用成功后从同一已知结果文件提取门禁；原会话或文件不可信仍停止。`display_truncated` 仅影响展示，不能用它放宽门禁或跳过超期、历史未知写入等必要核对。可信活动控制器只跳过重复启动。

OA GET 及已有白名单只读 POST 的网络连接异常最多重试一次，复用相同请求；HTTP 登录拒绝不重试，不自动修复登录。OA 提交不使用此重试路径。

`continuation` 是完整对账结果；领取与对账复用相同历史未知证据校验。历史 Writer 记录须绑定冻结单据、有效签名和可信阶段；旧的零 Writer preview 记录只保留既有兼容分支。未知结果保持未知，不重放。当前未决写入、身份冲突或损坏边界阻断；不能只凭错误码或边界文件存在放行。

`pending_dispositions` 按当前 `received_at` 计龄，输出单据哈希、流程、执行/写入状态、处理人、下一步、恢复次数上限、接管与解决期限。`attempt_created_at` 是首次本地尝试时间，不是 OA 流程创建时间；流程创建时间以 OA 当前权威数据为准。

普通待办超过 2 小时标为超期。隔离默认交给已配置的 OA 告警接收人，接管期限为当前 RPA 接收时间后 4 小时、解决期限为 24 小时；这些是处置目标，未自动改变 OA 状态。

处置记录的 `owner_key` 绑定实际告警目标的脱敏哈希；未配置目标时显示 unconfigured，不能确认接管。旧接管回执保留原目标绑定，配置改变时显式显示变化，不把责任静默转给新目标。未知写入、身份或授权故障另输出 2 小时升级期限。超过升级或处置时限，调用已有告警入口的 `preaudit_pending_overdue` 稳定码；告警仍受去重与 unknown 送达保护，提醒本身不停止可信处理或恢复单据。

## 同一快照的范围汇总

需要汇总本轮完成、各类待办、历史 unknown 及 cron 时，使用已有成功调用的文件，避免为显示重复查询 OA：

```bash
.venv/bin/python -m apps.oa_finance_preaudit.cli status \
  --root /absolute/path/to/finance_preaudit_flows \
  --summary-input-file /absolute/path/to/summary-inputs.json
```

输入是本机 JSON。`status_file` 直接复制原成功 status stdout 的路径、bytes、sha256；原调用及文件来源须先核实。可选 `overseas_status_file` 绑定[海外只读 status](../overseas-invoice.md)的完整原输出，`cron_file` 绑定指定 cron TOML，两者使用相同文件元数据格式。cron 仅展示 provided_configuration 的 id 和状态，未校验唯一 OA 调度的项目及目录绑定时，OA cron 状态仍为 unknown。元数据只证明内容完整性，不证明调用来源或快照仍是当前状态。

```json
{
  "status_file": {"path": "/absolute/status.json", "bytes": 123, "sha256": "原回执摘要"},
  "completed": {"owner_task_id": "原宿主聊天UUID", "run_ids": ["本轮已封存run_id"]},
  "excluded_workflows": [{"workflow_id": "已明确排除的精确ID", "workflow_name": "对应精确名称"}]
}
```

`completed` 只选择本轮原生封存记录，计数重新经过 [completed-receipts](../agents/controller-execution.md#已封存轮次的完成数量)的原始证据校验；不能输入已完成总数。缺少、无效和排除回执分别保留。财务、包装、海外始终属于处理范围，排除映射不能覆盖它们；其他待办只有精确匹配调用者明确选择的 ID 和名称才计为范围外，未分类项保持 unclassified。旧快照没有流程明细时计数保持 unknown，不能用总数相减猜测。

输出保存原 handoff 与 continuation 阻断项，并列出文件摘要、快照时间及年龄。缺少海外、cron、完成选择或来源不完整时保留 unknown。`all_clear=false` 和 `startup_authorized=false` 表示本入口只汇总历史快照；源码可用、配置授权、生产部署、cron 配置与实际业务验收分别报告。启动仍需新一轮完整实时门禁；海外实际验收见[独立流程证据要求](../overseas-invoice.md)，App 只读业务回归见[原生回放](finance-native-replay.md)。

## 一次跨轮恢复

用户已明确授权该范围后，唯一小时调度使用：

```bash
.venv/bin/python -m scripts.finance_preaudit_concurrency_probe \
  --rolling --capacity 3 --max-capacity 3 --confirm-write YES \
  --recover-confirmed-no-write-once YES
```

选择时同时验证本地授权清单、完整对账、实时唯一待办、未命中已办、单据级可信未写入回执及现有恢复契约。允许审核进程失败、无效决定、允许的材料局部失败和经过可信边界确认未写入的 Writer 失败。

Writer 被停止门拦在提交之前时，仅精确签名边界为 `writer_preflight_failed / stop_requested`、终态 `preflight_failed`、未出现 `submit_started` 且无执行回执的记录可进入单据隔离。原冻结文件和绑定须仍有效；其他预检细码保持原控制器分类。该分类不清理停止门，活动停止门仍由原受控收尾处理。旧版生成的控制器级派生回执需在已授权维护、完全空闲和证据复核后重建；原回执先留存，action/state/Writer 边界和冻结文件保持原字节。

每个单据轮次只对首个 attempt 恢复一次，新 attempt 重新采集、审核、授权、提交和后查，原证据保留。已有后续 attempt 的失败转人工。即使恢复字段被移除，attempt 大于 1 也不再次选择。不能与精确 `--recover-action-file` 参数混用，不能在只读探针使用。普通未授权调用仍跳过历史隔离。

该参数只选择历史已隔离且可信确认未写入的首个 attempt，不改变当前单据的失败策略。当前普通审核失败、无效决定和 CLI 超时在可信写前留证后保留待办，不当轮重试，继续其他单据；不因失败轮次累积自动永久隔离。官方错误的有限重试、整轮停止和适用冷却见[小时生产合同](hourly-production.md#限流和过载的有限重试)。整个 worker 或材料准备超时、回收不明和安全停止仍优先报告为本轮失败，不能被等待重试项覆盖为完成。材料准备超时保留现场与回收证据，不生成未经审核的意见。

同一单据的收集或审核在新 attempt 打开后中断，下轮依据现行冻结和安全门继续该动作，不创建第三个恢复 attempt。登录、冻结、隔离或 Writer 证据不可信时停止。

## 人工接管

处理人实际接管后，记录到对应失败 attempt 的旁路回执：

```bash
.venv/bin/python -m apps.oa_finance_preaudit.case_recovery \
  --action-file /absolute/path/to/action.json --confirm-takeover YES
```

该命令只确认最新失败 attempt 的责任归属，默认处理人为当前告警接收人；不访问或提交 OA，不恢复单据，不修改 action/state。回执绑定 state 指纹，重复确认不延长期限；实际接管后解决期限为确认时间后 24 小时。原状态改变后不复用旧接管回执。逾期必须报告处理人、原因和下一步，不能只报告隔离数量。

无效决定不得自动生成行政交接意见。AI 超时仅按现行特定交接合同处理。未知写入先由处理人核对原提交与 OA 已办、待办和签字记录；没有可信终态不得重提。

## 协议装配

业务决定完成后，可在 attempt 外的新临时目录装配冻结协议字段，再走原完成入口：

```bash
.venv/bin/python -m apps.oa_finance_preaudit.review_decision \
  --action-file /absolute/path/to/action.json \
  --input /absolute/path/to/draft.json --output /absolute/path/to/new-decision.json
```

宿主只补绑定字段，保留模型已有冲突；业务项、证据、包装页面分类和版本关系仍由审核者观察，Writer 前完整校验。包装视觉合同拒绝保存独立细码。

## 修复任务占位

生产任务使用项目修复占位入口；入口本身不创建任务、不发消息、不执行 OA。

1. `repair_dispatch status` 查询活动归属。`reserved` 检查已发起创建是否有结果；`running` 用 Codex 任务工具查询已绑定任务进度。2 小时无更新须检查与升级，不以时间删除占位。
2. `repair_dispatch reserve --window-key ... --source-thread-id ...` 先持久化窗口和活动归属，返回 reservation_id。已有窗口不重复创建任务；已有活动占位则明确报告归属和下一步。
3. 创建工具确认返回真实 threadId 后，用 `repair_dispatch bind --reservation-id ... --thread-id ...` 绑定。创建结果不明、只有临时 clientThreadId 或旧任务归属未知时保持等待，不再调用创建工具。
4. 修复任务每阶段通过 `repair_dispatch progress --reservation-id ... --thread-id ...` 记录进度。只更新自身归属。
5. 已经从任务工具确认终态后，使用 `repair_dispatch finish --reservation-id ... --thread-id ... --result completed|failed|waiting_for_user --confirm-terminal YES`。它保存窗口终态并归档该活动目录，历史窗口记录保留。同一窗口不自动重新创建任务；未完成处置给出处理人与期限。

现存无归属的空 active 目录，只有操作人已核实没有活动修复任务时，才使用 `repair_dispatch quarantine-orphan --confirm-no-active-task YES` 原子归档。非空、符号链接或未确认归属拒绝，不自动删除任何记录。

## 每轮收尾

分别报告指定恢复项、实际提交回执与后查、人工接管、隔离、未领取、当前未决写入以及进程/Writer 回收。连续两轮相同技术故障须检查同一修复任务的进度，不重复发起失败处理。告警只使用已有授权渠道和稳定错误码，原细码落盘，unknown/started 不表示送达、不自动重发。

发布同步及新进程门禁、未知写入和原证据留存见[隔离排障与恢复](../agents/quarantine-repair.md)。

原监督者丢失、旧退出码无法恢复，且保留技术停止门与可信未写入采集取消时，另行授权后使用[失去监督后的受控停止恢复](supervisor-loss-recovery.md)。源码维护的 ready/synced 不授予该恢复资格。

## 程序产生的无原因停止维护

### Writer 后查未知的停止维护

`writer_result_unknown` 停止标记可以绑定原 `writer_postcheck_timeout` 单据；已安装的 `stop_recovery` 仅在签名 Writer 边界可信时接受该具体错误。仍待办或对账阻断时拒绝，原 unknown 不因此成为成功或未写入，禁止重提。

修复尚未安装时，从已审查、CI 通过、合并且 `HEAD == origin/main` 的干净候选运行 `python -m apps.oa_finance_preaudit.writer_stop_maintenance`。显式提供 `--target-root`、`--bind-action-file`、`--expected-candidate-revision`、原执行的 `--original-receipt-file` / `--original-owner-task-id` / `--original-tool-session-id`、原调度的 `--automation-file` / `--production-project-id` / `--expected-schedule`，以及本次授权的 `--confirm-reconciled YES`。

维护复用 Controller/global Writer 租约，以目标自己的解释器核验本轮行动归属、冻结和签名边界，并取得新鲜完整状态与当前输入证明。只接受精确 `handled_not_pending`，要求原执行真实终态、已回收，原 cron 保持 PAUSED，生产唯一干净 main 可快进到候选。维护保存私有审计，仅按原身份清除停止标记；不改任何旧单据文件或 OA。必须同时读取 `status`、`stop_cleared` 和 `receipt_file`；结束留证失败时保留实际清除状态，不重复运行。随后按普通源码发布门同步，再启动独立审批新轮。

`stop_requested\n` 只说明停止标记没有原因码，不能据此判断是用户主动停止。若原定时任务因 `production_reconciliation_blocked` 写入该标记，先追查原冲突、等待原控制器终态并完成子进程回收。当前冲突消失只满足恢复检查，不证明原根因已修复。模型资格和已授权恢复范围由本轮维护负责人核实，历史 unknown 保留，不重放单据或伪造人工接管。

新增维护入口 `apps.oa_finance_preaudit.manual_stop_recovery` 只写私有审计回执并精确清除绑定的普通停止标记。它不执行 OA 提交，不改 attempt、授权、冻结或控制器历史。带原因码的停止继续使用匹配的既有处置入口，其中 `writer_result_unknown` 使用原 `stop_recovery` 合同。

引导维护可以从已审查、CI 通过且合并到远端 main 的干净候选运行。候选 `HEAD`、本地已核实的 `origin/main` 和明确指定的发布 SHA 必须相同。维护显式指定生产根，使用候选自身 Python 执行维护代码，并由生产已有 `.venv/bin/python`、生产 cwd 执行现行官方只读 status 子进程。不得用 PYTHONPATH 导入候选到生产 Python，不链接生产依赖或 runtime 到开发树，也不得绕过停止门先同步生产代码。

维护证据包为当前用户所有的 0600 JSON，`schema_version=1`、`kind=preaudit_manual_stop_recovery_binding`。以下字段均必填：

- `target_root`、`target_revision` 绑定唯一、干净、可核实的生产 main。
- `stop` 包含原标记的 `device`、`inode`、`mtime_ns` 和原始字节 `sha256`。不能用维护时重新读取的标记身份替代原记录。
- `manifest` 和 `original_manifest` 均包含绝对 `path`、原始字节 `sha256`，分别指向当前 rolling-controller-manifest 和原执行目录 controller-manifest。全局入口为定位指针时，须绑定原 execution 的 owner、invocation、run 和 controller instance；旧完整全局清单仍与原 execution 内容一致。权威清单必须同轮终态且 children_reaped=true。
- `original_conflict_status` 绑定原 blocked、存在 pending_handled_conflict 的完整状态文件；`original_status` 绑定原控制器终态后的完整收尾状态文件。两者均使用 `path`、`sha256`，不能用当前新快照替代。
- `original_exit` 使用 `path`、`sha256` 绑定 `preaudit_original_controller_exit` 记录。该记录必须包含实际 `exit_code`、`process_session_id`、`run_id`、`terminal=true`、`children_reaped=true`，以及原会话 `source_file` 的 `path/sha256`、`source_line`、`source_call_id`。
- `stop_source` 包含原停止调用的 `line`、`call_id`，引用同一原会话文件。维护核对原 `request_stop()` 调用、实际退出 0 的停止结果、无原因码和对应冲突文件；再核对原会话 cwd、任务 id，以及实际 write_stdin 进程退出与嵌套执行回执的轮次、实例、PID、修订和终态。保留各阶段原时间，严格核对带时区的 manifest.completed_at ≤ supervisor.completed_at ≤ 原工具输出 timestamp ≤ 原收尾 status.generated_at；这些阶段无须同时结束。`Script completed` 不作为进程退出证据。原会话只读源允许当前用户所有且组和其他用户不可写，私有维护证据仍须 0600。

### 已闭合提交未决与首次维护身份观测

`schema_version=2` 仅扩展原阻断为 `pending_write_state_unresolved` 的受控恢复，不改变版本 1 的原身份合同。保留上述字段，并新增 `original_submissions`、`stop_identity_observation`、`original_stop_payload`：

- 原完整对账须 read_only，continuation 为 blocked/controller，isolated_pending=0，唯一阻断码为 pending_write_state_unresolved。正整数计数必须精确对应 pending_not_handled 且本地 review_required/submitting 或 closed/submitted 的原项；原 unknown、额外阻断、重复项或数量不符均拒绝。
- `original_submissions` 每项只能含 `state`、`receipt`，分别是原同 attempt 的 state.json、execution-receipt.json 的绝对 path/sha256。路径严格绑定生产流程目录和 workflow/item_key/attempt。原终态状态与维护时新鲜完整对账中，同项均须唯一 closed/submitted/handled_not_pending；不可见、下一 attempt 或当前仍待办不能代替闭环。状态中的 receipt_fingerprint 须命中原回执，controlled_write、提交及 done/postcheck 布尔均须为 true。保留原证据，不重放单据。
- 有原身份记录时，`stop_identity_observation=null`、`original_stop_payload=null`，继续按原 stop 元数据绑定。缺少当时 inode 记录时，可采用有限出生证据模式：前者绑定私有 `preaudit_stop_identity_observation` 文件，后者绑定此前已保存的普通停止原字节文件，两者均使用 path/sha256。保存样本只证明原字节，不能把复制件 inode 当作原标记 inode。
- 观测文件包含 `observed_at`、原停止路径、device/inode/mtime_ns/ctime_ns/birthtime/sha256，诚实记录首次维护观测，不声称是当时 inode 观测。实际文件出生及最后修改均须严格落在原停止工具调用与实际输出的时间区间内，ctime_ns==mtime_ns，出生不晚于修改，首次观测不早于原工具输出。文件须当前用户所有、私有、无符号链接；文件系统无 birthtime、后来改动、替换、区间不符或元数据漂移均拒绝。维护租约内反复核对本次观测的精确 identity 与出生/修改证据，审计回执标记 `stop_identity_source=maintenance_observation_with_original_birth`。
- 出生模式仅接受已核实的混合原工具形式：同一 exec 先运行固定 `.venv/bin/python -c` 三语句（导入 runtime_control、无参数 request_stop、打印 stop_requested/source/status_file），随后一次精确原会话 write_stdin。停止结果必须真实 exit_code=0 且 source=production_reconciliation_blocked；后续真实终态与原退出回执须按顺序对应。额外 Python 动作、命令、等待、错会话或输出不符均拒绝，不进行通用 eval 或执行历史代码。旧独立停止与纯等待形式保持原合同。

版本 2 同样只清除已授权且证据完整的绑定普通停止标记，写私有维护审计；不写 OA、不改原业务意见、不放宽未知写入、授权、冻结、回收或实时门禁。工程回归、证据格式预检与真实生产恢复分别记录，代码合并本身不是恢复证明。

证据与原工具终态、文件 bytes/SHA 已核实后，按当前明确授权运行：

```bash
python -m apps.oa_finance_preaudit.manual_stop_recovery \
  --target-root "$production_root" \
  --evidence-file "$private_binding_file" \
  --expected-candidate-revision "$reviewed_release_sha" \
  --confirm-reconciled YES
```

维护持有生产非阻塞 controller 和 global Writer 租约，拒绝环境停止、路径覆盖、活动请求 Writer、不可信权限、原标记替换或原证据改变。锁内等待生产只读 status 实际退出 0，并核对完整文件回执。目标解释器另调用既有只读策略及冻结校验器，要求当前 write-policy 覆盖六财务和包装、当前 review_required 冻结资料完整；collecting 按既有采集恢复合同校验身份与未写入边界，覆盖初始 claim、材料/packet 先落盘、state 先保存冻结字段、action 先转待审的四个阶段；已有材料按目标校验器核验，恢复 provenance 和原 receipt 必须可信。可信 item 隔离回执绑定失败产物，仅材料收集失败的可信 item 隔离免于重验已损坏材料，其他隔离仍走既有冻结校验。证明绑定本次 status SHA。原收尾状态保留旧字段兼容，不补造新证明。原证据读取仅兼容 macOS 系统所有的 `/tmp` 根别名，原调用路径字符串保持不变，其他目录或文件软链及文件替换仍拒绝。当前身份、认证、授权、冻结和 continuation 必须可信，当前未知写入或 pending/handled 冲突阻断；可信单项隔离可保留。通过后先 fsync 保存 0600 validated 回执，复查原证据与标记，再精确清除并保存 cleared 结果。

结果必须同时读取 `status`、`stop_cleared`、`receipt_file` 和 `write_state`。`maintenance_completion_failed` 或 `audit_completion_failed` 表示维护结束有异常，清除可能已经发生；先核对回执和实际停止状态，不重复执行或声称仍未清除。恢复标记成功不代表生产审核验收成功。随后使用统一生产发布预检，同步通过后只有下一独立进程才加载新版本。

## 宿主重启且原执行已真实退出

用户已确认重启并授权继续，原回执为 owner_process_lost、监督者已停止原控制器且真实 failed/reaped 时，使用[宿主重启受控恢复](owner-restart-recovery.md)。它只清精确技术停止门，不改真实退出码或单据，不启动审核和调度；其他停止原因、未知 wait、未决写入及不可信证据仍阻断。

## App 异常收尾后残留周期停止门

原 App 轮次已真实失败退出、子进程回收且统一收尾 `verified`，但收尾调用留下精确 `controller_cycle_stop` 时，使用 `apps.oa_finance_preaudit.shutdown_stop_recovery`。此入口仅在当前明确恢复授权内清除绑定标记，默认只读。

从已审查、完整 CI 通过并合并的干净同步候选 `main` 执行。生产须保持唯一干净 `main`，其修订精确等于原执行修订，且可快进至候选远端版本。这是安装前的引导恢复；清门后立即使用[统一源码发布门](production-source-release.md)，同步核验通过后才启动新业务进程。

```bash
python -m apps.oa_finance_preaudit.shutdown_stop_recovery \
  --target-root "$production_root" \
  --expected-candidate-revision "$reviewed_release_sha" \
  --original-receipt-file "$original_receipt_file" \
  --original-owner-task-id "$original_owner_task_id" \
  --original-tool-session-id "$original_tool_session_id" \
  --begin-shutdown-line "$original_begin_line" \
  --automation-file "$approval_automation_file" \
  --production-project-id "$production_project_id" \
  --expected-schedule "$preserved_schedule"
```

原生来源必须完整匹配原 `begin-shutdown`，可随后包含精确绑定的只读 `--retrieve-result`；任意额外命令均拒绝。目标解释器重新核对原真实 wait、三个审核槽收尾、停止时间归属、进程身份及进程组、当前身份认证、授权、冻结和完整 continuation；原 cron 必须 PAUSED。确认执行追加 `--confirm-recovery YES`，在租约内复验并保存私有审计。历史证据、旧意见和隔离保持原样。

读取结果的 `status`、`stop_cleared` 和 `receipt_file`。只有 `cleared` 才完成恢复；末尾审计失败时清门可能已发生，先核实际状态和回执，不重复执行。清门本身不表示源码已同步或审批已恢复。
