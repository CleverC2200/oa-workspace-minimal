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

## 一次跨轮恢复

用户已明确授权该范围后，唯一小时调度使用：

```bash
.venv/bin/python -m scripts.finance_preaudit_concurrency_probe \
  --rolling --adaptive --capacity 3 --max-capacity 6 --confirm-write YES \
  --recover-confirmed-no-write-once YES
```

选择时同时验证本地授权清单、完整对账、实时唯一待办、未命中已办、单据级可信未写入回执及现有恢复契约。允许审核进程失败、无效决定、允许的材料局部失败和经过可信边界确认未写入的 Writer 失败。

Writer 被停止门拦在提交之前时，仅精确签名边界为 `writer_preflight_failed / stop_requested`、终态 `preflight_failed`、未出现 `submit_started` 且无执行回执的记录可进入单据隔离。原冻结文件和绑定须仍有效；其他预检细码保持原控制器分类。该分类不清理停止门，活动停止门仍由原受控收尾处理。旧版生成的控制器级派生回执需在已授权维护、完全空闲和证据复核后重建；原回执先留存，action/state/Writer 边界和冻结文件保持原字节。

每个单据轮次只对首个 attempt 恢复一次，新 attempt 重新采集、审核、授权、提交和后查，原证据保留。已有后续 attempt 的失败转人工。即使恢复字段被移除，attempt 大于 1 也不再次选择。不能与精确 `--recover-action-file` 参数混用，不能在只读探针使用。普通未授权调用仍跳过历史隔离。

显式启用此策略时，同一审核的安全进程尝试耗尽后立即按可信凭据隔离，不再沿用旧的额外三轮 retry_pending；无效决定的原有一次定向重审核规则保持。真实准备/审核超时或停止结果始终优先报告为本轮失败，不能被等待重试项覆盖为完成。材料准备超时仍保留现场与回收证据，按当前门禁调查或继续可信动作，不生成未经审核的意见。

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

## 程序产生的无原因停止维护

`stop_requested\n` 只说明停止标记没有原因码，不能据此判断是用户主动停止。若原定时任务因 `production_reconciliation_blocked` 写入该标记，先追查原冲突、等待原控制器终态并完成子进程回收。当前冲突消失只满足恢复检查，不证明原根因已修复。模型资格和已授权恢复范围由本轮维护负责人核实，历史 unknown 保留，不重放单据或伪造人工接管。

新增维护入口 `apps.oa_finance_preaudit.manual_stop_recovery` 只写私有审计回执并精确清除绑定的普通停止标记。它不执行 OA 提交，不改 attempt、授权、冻结或控制器历史。带原因码的停止继续使用匹配的既有处置入口，其中 `writer_result_unknown` 使用原 `stop_recovery` 合同。

引导维护可以从已审查、CI 通过且合并到远端 main 的干净候选运行。候选 `HEAD`、本地已核实的 `origin/main` 和明确指定的发布 SHA 必须相同。维护显式指定生产根，使用候选自身 Python 执行维护代码，并由生产已有 `.venv/bin/python`、生产 cwd 执行现行官方只读 status 子进程。不得用 PYTHONPATH 导入候选到生产 Python，不链接生产依赖或 runtime 到开发树，也不得绕过停止门先同步生产代码。

维护证据包为当前用户所有的 0600 JSON，`schema_version=1`、`kind=preaudit_manual_stop_recovery_binding`。以下字段均必填：

- `target_root`、`target_revision` 绑定唯一、干净、可核实的生产 main。
- `stop` 包含原标记的 `device`、`inode`、`mtime_ns` 和原始字节 `sha256`。不能用维护时重新读取的标记身份替代原记录。
- `manifest` 和 `original_manifest` 均包含绝对 `path`、原始字节 `sha256`，分别指向当前 rolling-controller-manifest 和原执行目录 controller-manifest。两份内容必须同轮终态且 children_reaped=true。
- `original_conflict_status` 绑定原 blocked、存在 pending_handled_conflict 的完整状态文件；`original_status` 绑定原控制器终态后的完整收尾状态文件。两者均使用 `path`、`sha256`，不能用当前新快照替代。
- `original_exit` 使用 `path`、`sha256` 绑定 `preaudit_original_controller_exit` 记录。该记录必须包含实际 `exit_code`、`process_session_id`、`run_id`、`terminal=true`、`children_reaped=true`，以及原会话 `source_file` 的 `path/sha256`、`source_line`、`source_call_id`。
- `stop_source` 包含原停止调用的 `line`、`call_id`，引用同一原会话文件。维护核对原 `request_stop()` 调用、实际退出 0 的停止结果、无原因码和对应冲突文件；再核对原会话 cwd、任务 id，以及实际 write_stdin 进程退出与嵌套执行回执的轮次、实例、PID、修订和终态。保留各阶段原时间，严格核对带时区的 manifest.completed_at ≤ supervisor.completed_at ≤ 原工具输出 timestamp ≤ 原收尾 status.generated_at；这些阶段无须同时结束。`Script completed` 不作为进程退出证据。原会话只读源允许当前用户所有且组和其他用户不可写，私有维护证据仍须 0600。

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
