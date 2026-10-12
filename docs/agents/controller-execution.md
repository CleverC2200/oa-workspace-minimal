# 控制器执行回执

显式授权的提前单轮执行使用[人工单轮冷却豁免](../runbooks/cooldown-override.md)，仍由本入口监督。原执行、冷却字节、完整启动对账和当前调用保存在私有豁免回执中；execution.json 绑定其路径和 SHA-256，进度回读核对该绑定。它不改变普通启动、工具会话绑定或真实 wait 合同。

正常已授权调度使用 `python -m scripts.finance_preaudit_run --owner-task-id <当前聊天 UUID> -- <原滚动控制器参数>`。该入口仅监督现有 `scripts.finance_preaudit_concurrency_probe`，确认写入、范围、恢复参数和全部门禁仍由原入口校验；开发验证不传真实业务写入参数。

工具调用者与每轮监督者分开：入口启动一个独立进程组中的本轮监督者，由它创建、真实 wait 原控制器并独占保存 execution.json。它不是常驻 daemon 或额外模型 worker，不重复启动控制器。调用者收到停止信号时转交监督者；调用者或其原工具父进程消失或 PID 身份变化时，监督者请求既有停止门并继续等待 Writer 和控制器收尾，保留真实退出码。启动前和每次 CLI 启动前核对已绑定的 PID、出生时间、进程组及用户，不能仅凭 PID 仍存在放行。

监督者自身被强制终止时，控制器心跳及运行中的审核 worker 每 5 秒核对独立的父链身份，并通过技术停止门有序收尾；CLI 发起前的核验不等待这个间隔。不能强杀可能在途的 Writer。机器断电、所有保护进程同时被强杀、观察前已经发出的请求仍不保证有完整退出回执，不承诺操作系统任意终止下恢复真实 wait。

启动输出给出 `receipt_file`、拥有者和控制器 PID。保持并等待原工具 session；工具返回 `session_id` 而没有 `exit_code` 时继续等待原 session。外层返回原工具 session ID 后，用 `python -m scripts.finance_preaudit_run --owner-task-id <同一任务 UUID> --bind-tool-session <原 ID> --receipt-file <启动给出的 execution.json>` 保存独立 `tool-session.json`，然后继续等待原 session。该元数据入口不启动控制器，只校验本次归属，拒绝冲突绑定，标记来源为 caller_reported_tool_result；不能把自行编造的 ID 当真实工具回执。Python 无法自己取得外层 ID，主执行回执该字段保持 unknown，指向单独绑定文件以避免并发覆盖；未绑定时如实记录缺口。禁止以函数单元的 `Script completed` 替代内部原进程终态。

回执与原 stdout/stderr 保存在 `runtime/control/executions/<invocation>/`，目录独占、权限受限。长时间无输出、低 CPU、浏览器关闭警告均不是结束条件。等待中需要检查健康时，使用原身份匹配的 prepare 采样及只读状态文件，计算采样年龄；保留停止门、Writer、身份、冻结和未知写入门禁。

`state=exited` 只证明监督进程实际 `wait` 得到子控制器退出码；`result=completed` 还需要同 PID、本次开始时间之后的清单为 completed 且 children_reaped=true。终态缺口或不可信结果为 blocked，不证明已安全回收或可以再次启动。进程信号退出记录真实负退出码和信号。监督者被 SIGKILL 或机器断电时，原保存记录可能仍是 waiting；读取入口在可确认监督者丢失时投影 state=supervisor_lost、recorded_state=waiting、status=blocked、terminal=false，保留未知退出码，不改写原证据。新回执用 supervisor_process 核对 PID 复用，历史缺失该字段时只使用原兼容存活检查。子清单收尾或已确认回收不代替真实 wait，按原隔离排障入口核对，不能重放。

启动竞争后，只有本次子进程退出0、确认没有本轮镜像且原输出完整验证正常活动控制器门禁时，才记录 result=skipped；此时不能认领他人 run、进入包装或清窗。存在但损坏/不匹配的镜像仍 blocked，不能靠 skipped 输出覆盖坏证据。

## 只读进度、等待与终态取回

小时调度的运行中监视使用本节原回执入口，并继续等待原工具会话；完整 `status --check-live --reconcile` 用于启动前和原控制器真实退出后。待办、已办及本地状态分次读取，不是活动 Writer 的原子快照。不得把运行中混合快照的 pending_write_state_unresolved 当作已确认故障去停止原控制器，也不能放宽 raw continuation 或给 submitting/submitted 自动豁免。原控制器内部身份、冻结、授权、Writer 未知结果及轮次后对账保护保持有效。

从启动回执复制 `receipt_file`、`owner_task_id` 和父目录的 `invocation_id`。在同一源码工作树执行以下命令。
读取入口不加载 `.env`、认证或刷新会话。控制目录有自定义配置时，须沿用原运行的
`OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE` 环境变量；不能靠读取业务认证配置猜测目录。

```bash
python3 -B -m scripts.dev_environment --json preflight
python3 -B -m scripts.finance_preaudit_run --owner-task-id <原任务 UUID> --receipt-file <原 execution.json 路径> --invocation-id <原 invocation_id> --progress
python3 -B -m scripts.finance_preaudit_run --owner-task-id <原任务 UUID> --receipt-file <原 execution.json 路径> --invocation-id <原 invocation_id> --progress --wait-seconds 30
python3 -B -m scripts.finance_preaudit_run --owner-task-id <原任务 UUID> --receipt-file <原 execution.json 路径> --invocation-id <原 invocation_id> --retrieve-result
```

`--wait-seconds` 只轮询这份原回执，范围为0到60秒，默认0。它不启动、停止、重试、重新认领或回收控制器。
原工具绑定已保存时可追加 `--tool-session-id <原工具 ID>`，要求精确匹配；未绑定且不指定时如实报告 unknown，
不把 caller_reported_tool_result 当独立的工具服务证明。中断后仍可读取，但 waiting 不代表进程仍存活，
也不授权新启动；未知终态继续按[隔离排障与恢复](quarantine-repair.md)处理。

入口验证拥有者、调用 ID、清单 PID/轮次/开始时间、修订、回收状态和原 stdout/stderr 的 SHA-256。
新回执还保存镜像清单指纹；历史回执没有该字段时继续核对原有字段和日志指纹，不补写历史证据。
符号链接、非普通文件、重复 JSON 字段、损坏或漂移证据均返回非零；缺清单、未退出或未回收不能报告 completed。
完成轮次返回0；仍等待返回2；不可信、失败或未回收返回1。可信 skipped 返回0，只表示原监督进程已退出且未接管，
其 `terminal=true` 不证明另一个控制器已终态，`children_reaped` 保持 unknown。

`--progress` 仅输出执行摘要，不带单据、客户或原日志正文。`--retrieve-result` 在可信终态返回 `controller_stdout`；
原输出超过1 MiB时返回 `controller_stdout_file`、已验证指纹、大小及 `output_omitted=size_limit`。
日志按64 KiB块计算哈希，每份最多64 MiB；超过该上限或读取中漂移时拒绝取回。控制 JSON 各限1 MiB。
这些限制约束本地读取工作量，不是业务运行超时。文件指针需保留为本地证据，后续读取仍应核对给出的 SHA-256，
原始结果可能含业务内容，不上传、提交 Git 或贴进 Issue。取回结果不加载审核规则，也不修改历史业务判断。

确已确认的外部对账失信采用有限停止理由 production_reconciliation_blocked；已写停止原因不覆盖，原真正首错优先。终态原输出的 primary_error_code 与通知保留具体原因，取回入口只验证并返回原结果，不改写它。普通停止或无法验证的原因保留通用失败语义，不能自动清除；普通周期停止仍按原回收门处理。

回执不授予恢复、清警报或新的 OA 写入权限。原始输出仅本地留存，不提交 Git 或上传 Issue。父 Bug #325 的历史退出触发者仍待证实；本地回归不代替发布及正常生产轮次验收。
