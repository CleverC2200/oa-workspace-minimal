# 隔离排障与恢复

适用于用户要求排查、修复或清理隔离项、历史技术失败的开发任务。生产自动调度隔离失败项并继续其他可信待办；开发排障必须接着查明失败、验证修复并处理恢复结果。生产的“不自动重试”不能成为开发排障的结束条件，也不要求另开任务或重复索取已有同范围授权。

## 从当前事实确定范围

在开发克隆修改代码和运行离线回归；生产只读对账及已授权恢复均使用生产克隆的明确 `workdir` 和 `.venv/bin/python`。每次真实操作核对工作目录、模块来源及生产修订，遵守[开发与生产隔离及发布](../../CONTRIBUTING.md#开发与生产隔离及发布)。不要因任务起于开发项目就在开发目录运行真实审批。

从生产拥有的只读入口 `python -m apps.oa_finance_preaudit.cli status --check-live --reconcile` 读取当前身份、待办/已办、`reconciliation.manual_review`、`continuation` 和 `controller_handoff`。按脱敏单据键定位原 attempt，保留旧 action/state、失败凭据、冻结材料和已有写入回执。

| 对账结果 | 开发排障动作 |
| --- | --- |
| 精确仍在待办、未命中已办，建议为 `confirmed_no_write_recovery_candidate` | 查明失败阶段和原因，验证修复后列入显式恢复范围；候选建议不是写入授权或已恢复证明 |
| 当前仍在待办，但证据不完整、身份冲突或恢复校验失败 | 继续调查缺口，必要时修复读取或校验缺陷并回测；不能删证据、改状态或把错误码加入允许列表来放行 |
| `write_state=unknown` | 核对真实写入结果及既有回执，保留未知；不因“清理隔离”而认定未写入或纳入普通恢复 |
| 已办或当前不可见 | 核对历史与当前状态，按留存入口处理可归档项；没有精确待办身份时不重新提交 |

已被严格更晚的同请求轮次取代、且可证明从未进入 Writer 的已打开轮次不算缺口：只读对账归类为 `prior_round_unsubmitted` 并只投影、不阻断整轮，旧 attempt 与证据字节不变，新轮次仍按新 case 重新取材、审核和授权。其余身份冲突仍按上表继续调查，不能删证据、改状态或加白名单放行。

`findings` 和 `manual_review.items` 使用同一恢复建议。候选建议复用 `FlowEngine.recoverable_no_write` 校验原 action/state、冻结材料和适用的失败或 Writer 边界证据；仅有失败标签不足以证明未写入。实际恢复入口还会在执行时重新检查最新 attempt、实时唯一待办、流程身份和已办结果。只读查询不创建 attempt，`automatic_retry` 保持 `false`。

## 诊断、修复和回测

按真实失败阶段分组，区分业务意见、材料问题、审核进程失败和写入边界故障。对能复现的程序缺陷先建立经过原调用路径的失败回归，再修复并验证；暂时无法确定的原因标为未知，不能凭一次恢复成功宣称底层故障根因已消除。

审核进程正常退出但最终输出不能解析成 JSON 或不是对象时，宿主在当前 attempt 留存 `review-output-<原输出 SHA-256>.txt`，文件权限为 `0600`；拒绝回执仍只包含脱敏摘要。该原输出只用于本地排障，不是业务证据或可提交决定，不进入 Git、Issue、PR 或终端汇报；后续审核不得读取它。旧记录未留存原输出时，不补造历史格式原因。已有原输出被改动或留存失败时返回诊断失败，保持未写入。

新失败留存会将实际 Schema 与输入指纹保存在私有上下文中，并在拒绝摘要中关联其指纹；旧记录不补造。排查具体 JSON/合同阶段时先用[失败输出离线回放](offline-review-diagnosis.md)，解析复现、完整合同、业务质量和生产验收分开报告。

除业务或采集故障本身，还要检查为什么既有恢复入口未被使用：候选是否漏报、指令是否把生产隔离当作排障终点、人工清单是否丢失具体下一步。发现此类缺陷应在本次任务修复并关联 Issue，不能只补跑当前批次后结束。

## 按授权执行显式恢复

修复验证通过且已有本次范围内的恢复/OA 写入授权时，继续完成恢复；不要再次询问用户是否处理已明确要求处理的隔离项。仅要求只读排查、代码修复或发布，不自行扩展为 OA 写入授权。缺少执行授权时先完成可独立进行的诊断、回归和精确恢复清单，再说明缺少的具体授权。

六类财务使用生产同一个滚动入口，逐项传入精确旧 `action.json` 的 `--recover-action-file`；命令及门禁见 [README 恢复入口](../../README.md#滚动恢复隔离项)和[FlowEngine 合同](../../.agents/skills/oa-finance-preaudit/references/flow-engine-contract.md)。包装使用其现有单据入口和图片审核规则，不能塞入财务滚动队列。

恢复创建新 attempt，重新采集、审核和受控提交，原 attempt 保持不变。缺少显式恢复参数的普通控制器仍跳过隔离项。不要通过删除隔离、清除告警、改冻结状态、重复启动控制器或使用未知写入确认参数代替修复。正在执行时等待同一会话终态；滚动入口也可能接收新待办，须把指定恢复项与正常新增项分别核算。

## 已成功提交后由用户撤回

仅当用户明确确认已撤回并要求继续审批时，使用本分支。原 `closed/submitted`、成功后查和全部材料保留；当前单据须唯一仍待办、未命中已办、流程及账户归属一致。接收时间未更新时不推造新时间，不使用“确认未写入”恢复参数。

若原成功项重新待办已阻断安装修复，先按[已确认撤回的源码维护](../runbooks/production-source-release.md#已确认撤回后的源码维护)安装已审查版本，保留 raw continuation 和旧成功记录。源码维护不代表审批就绪。

在已安装版本、生产 cwd 和生产解释器中调用单据级入口。缺少任一确认参数时只做只读预检，不创建 attempt：

```bash
.venv/bin/python -B -m apps.oa_finance_preaudit.withdrawn_submission \
  --action-file "$withdrawn_action" \
  --confirm-withdrawal YES --confirm-write YES
```

入口持有 Controller/global Writer 租约，重新验证原成功及完整当前待办、已办、账户、节点、身份、授权和其余冻结输入，然后创建下一 attempt 重新取材并获取新授权。它不调用模型、不提交 OA。恢复回执为版本 2，记录 `source_outcome=submitted_then_withdrawn`，与版本 1 的未写入恢复分开；读取回执不授权再次提交。

等待原工具真实终态，保存返回 action 和实际状态。材料技术失败仍生成可信 item 隔离；旧意见不复制到新 attempt。完成后重新执行完整启动门，通过后由新的真实监督身份和原生审核槽进入正常审批。旧 attempt、回执或对账被改动、已有下一 attempt、当前已办或归属冲突时保持阻断，不重复执行恢复入口。

## 完成证据

已明确授权的生产小时调度可以使用[有限恢复与人工接管入口](../runbooks/blocking-recovery.md)，一次跨轮恢复可信未写入的首个 attempt。代码修复或发布本身不启用该授权；调度提示词与参数必须显式采用它。既有显式单据恢复仍供已授权的开发排障使用。人工接管回执只记录处理责任，不读写 OA，也不把隔离或未知结果改为业务通过。

指定的每个隔离项都必须有以下结果之一：

- 新 attempt 已闭合：分别核验业务结果、实际 `write_state`、提交/后查回执和实时待办/已办；预览闭合不等于生产提交。
- 经当前状态核对无需重提：给出已办或不可见证据及留存结果，不伪称已恢复提交。
- 仍未解决：记录具体原因、缺少证据或授权、下一步；保持缺陷未完成，不以“继续隔离”作为修复结论。

最后核对原证据未被改写、生产控制器/Writer 空闲及子进程已回收。`completed_with_quarantine`、普通可行动队列为空、清除告警、代码合并或生产同步均不能单独证明隔离排障完成。分别报告本批恢复结果、根因修复与回归、实际发布修订及剩余项；指令静态审计不代表后续新任务已验证遵循。

## 控制器被中断后遗留 running 清单

材料采集收到明确 `oa_run_stopped` 且本地停止门已置位时，只有原 collecting claim 对象未变、未出现 Writer 边界、执行回执、决定、意见或提交信封，才保留 collecting/not_requested 并向调用者传播停止，不创建材料失败隔离。下次仍须经过启动门和当前身份检查，重新调用材料采集；停止门不因此清除，不能复用未完成材料形成意见。相同字符串但没有停止门、claim 已变化或出现写入/决定证据，仍按原安全失败合同阻断。

旧版将停止记为 material_collection_failed / failure_detail_code=oa_run_stopped / scope=controller 的记录保持原字节。其 pending_failure_not_isolated 不能靠升级代码、改分类白名单或删除失败回执解除；只读定位后仍须使用另行授权的既有恢复合同。新行为只避免再次把可信的未写入采集取消记成材料故障。

控制器收尾捕获 Python 中断和退出异常，记录 failed 并保留实际回收状态；进程被强制终止仍可能遗留 running。不能仅凭无标准输出、CPU 空闲或某一刻没有子进程判断卡死；采集和线程内等待可能没有子进程，先检查阶段进度、超时边界和原调用回执。

发布后可在已授权的生产维护范围使用 `python -m apps.oa_finance_preaudit.controller_recovery --confirm-children-reaped YES`。执行前必须核对原控制器进程树（包括独立进程组）已全部退出；死 PID 本身不足以确认子进程回收。入口持有控制器与全局请求 Writer 租约，重查身份和对账，拒绝存活 PID、脏代码、停止标记、活跃请求 Writer 及未决写入。数量一致、无阻断原因的历史隔离项不阻断清单恢复，仍须通过显式单据恢复入口处理。原清单保存为同目录 `orphan-controller-<run_id>.json`，当前清单仅闭合为 failed/degraded，不恢复单据、不启动审批、不清除告警。执行后复核生产交接状态；发布和生产操作仍需当前授权。

### 运行中监测与失败清单恢复

`status` 的 `controller_handoff.health` 是清单记录的运行结果，`health_scope=run_result`；运行期间 `unknown` 仅表示尚未收尾，不是实时主机健康。锁与运行清单一致时 `monitoring_action=wait_for_current_run`，继续等待原调用；`children_reaped=false` 和 `ready_for_start=false` 在此时也是预期值。锁身份不一致返回 blocked，必须调查。`check_start_gates` 只是进入完整启动核验的提示，不是 OA 写入授权。

实时主机数据来自 `apps.oa_finance_preaudit.concurrency_control.sample_local_health()`；滚动控制器每个心跳自行采样，未知采样暂停新增领取，critical 走受控停止，准备与审核各有超时 watchdog。不得因交接 health=unknown、无标准输出或单次无子进程快照发送 Ctrl-C/kill。故障判断先核对实际主机数据、阶段证据、超时与原调用回执；确有停止条件时使用现有停止门让控制器停止领取并完成 Writer/子进程收尾，保留原进程等待其终态，不用中断代替有序停止。

已发生中断且清单为 failed、children_reaped=false 时，完成原进程树及独立进程组退出确认后，使用 `python -m apps.oa_finance_preaudit.controller_recovery --mode failed --confirm-children-reaped YES`。该模式要求已知 `controller_cycle_stop`，拒绝手工停止、环境停止、变更中的停止标记及不可信实时对账；控制器和 Writer 租约覆盖全部核验、原清单与停止证据留存及恢复操作。只将失败清单标记为已确认回收，清理本次已知周期停止门；保留失败状态、告警和业务证据，不创建 attempt 或执行审批。默认 orphaned 模式仍只处理无停止门的 running 孤立清单。

### 原调用归属与准备进程父生命周期

调度调用滚动入口时必须单独执行，完整保留 `exec_command` 的 `session_id`、`exit_code` 和输出；不要只传递 `output`。存在 `session_id` 就通过同一个 `write_stdin` 会话等待至终态，不再调用滚动入口。当前任务已经启动的控制器即使在另一次状态查询中显示 active，也仍归当前任务收尾；只有启动前确认由其他任务拥有的活动控制器，才可直接跳过本轮。原调用尚未终态时不得结束或归档任务，也不执行结束后的 prune。

材料准备进程是独立进程组，初始化时监听原父进程的 multiprocessing sentinel。父进程已退出时，准备进程回收自身只读进程组；该保护不覆盖 Writer、不将清单改成成功，也不替代有序停止。被强制终止的主控制器仍可能留下 running 清单，必须走现有显式孤立运行恢复门。排查此类故障时同时记录原工具调用终态与进程组证据；工具 exit_code=-1 不能独自证明操作系统终止信号或发起者。

### 准备超时的阶段与栈证据

准备进程按约 1 秒间隔保存最近一次脱敏采样，阶段切换只在内存中更新。每个 dispatcher 使用独立 `trace_id`，每次准备使用独立 `task_id`；生产 `runtime/control/prepare-progress-<trace_id>.json` 只保留该 dispatcher 的最新快照。阶段包括待办读取、引擎建立、领取/取材、等待前一完成操作和返回动作验证；`stage_totals_seconds` 累计同一准备任务各阶段耗时。栈只保留最多 16 个仓库内 Python 函数位置，不包含参数、局部变量、源代码、异常正文、业务身份或外部绝对路径。

准备 watchdog 超时完成原有受控停止、全部执行器关闭及回收核验后保存 `prepare-timeout-<trace_id>.json`，关联 `run_id`、`controller_instance_id`、`task_id` 和 `task_index`。仅接受同次准备的完整快照；缺失、损坏或属于上一准备任务的样本记为 `sample_status=unavailable`。`sample_age_seconds` 是最近样本距记录超时的时间，包含进程回收耗时；采样线程也可能被阻塞，所以最近栈不保证是超时瞬间的栈，不能单凭旧采样断言根因。

正常完成不会生成 timeout 文件；progress 文件仅表示历史采样，不证明准备仍在运行。诊断读写失败保留原始超时错误和回收流程；这些诊断不改变 300 秒 watchdog、FIFO、Writer、授权和恢复门禁，也不能代替真实提交回执。按同一运行关联诊断后，先建立复现再修复底层问题，不用调大超时或重复审批代替取证。
