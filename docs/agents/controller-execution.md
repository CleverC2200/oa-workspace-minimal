# 控制器执行回执

正常已授权调度使用 `python -m scripts.finance_preaudit_run --owner-task-id <当前聊天 UUID> -- <原滚动控制器参数>`。该入口仅监督现有 `scripts.finance_preaudit_concurrency_probe`，确认写入、范围、恢复参数和全部门禁仍由原入口校验；开发验证不传真实业务写入参数。

启动输出给出 `receipt_file`、拥有者和控制器 PID。保持并等待原工具 session；工具返回 `session_id` 而没有 `exit_code` 时继续等待原 session。外层返回原工具 session ID 后，用 `python -m scripts.finance_preaudit_run --owner-task-id <同一任务 UUID> --bind-tool-session <原 ID> --receipt-file <启动给出的 execution.json>` 保存独立 `tool-session.json`，然后继续等待原 session。该元数据入口不启动控制器，只校验本次归属，拒绝冲突绑定，标记来源为 caller_reported_tool_result；不能把自行编造的 ID 当真实工具回执。Python 无法自己取得外层 ID，主执行回执该字段保持 unknown，指向单独绑定文件以避免并发覆盖；未绑定时如实记录缺口。禁止以函数单元的 `Script completed` 替代内部原进程终态。

回执与原 stdout/stderr 保存在 `runtime/control/executions/<invocation>/`，目录独占、权限受限。长时间无输出、低 CPU、浏览器关闭警告均不是结束条件。等待中需要检查健康时，使用原身份匹配的 prepare 采样及只读状态文件，计算采样年龄；保留停止门、Writer、身份、冻结和未知写入门禁。

`state=exited` 只证明监督进程实际 `wait` 得到子控制器退出码；`result=completed` 还需要同 PID、本次开始时间之后的清单为 completed 且 children_reaped=true。终态缺口或不可信结果为 blocked，不证明已安全回收或可以再次启动。进程信号退出记录真实负退出码和信号；监督进程自身被 SIGKILL 或机器断电时可能保留 waiting，此时终态未知，按原隔离排障入口核对，不能重放。

启动竞争后，只有本次子进程退出0、确认没有本轮镜像且原输出完整验证正常活动控制器门禁时，才记录 result=skipped；此时不能认领他人 run、进入包装或清窗。存在但损坏/不匹配的镜像仍 blocked，不能靠 skipped 输出覆盖坏证据。

回执不授予恢复、清警报或新的 OA 写入权限。原始输出仅本地留存，不提交 Git 或上传 Issue。父 Bug #325 的历史退出触发者仍待证实；本地回归不代替发布及正常生产轮次验收。
