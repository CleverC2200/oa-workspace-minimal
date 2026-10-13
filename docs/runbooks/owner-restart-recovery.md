# 宿主重启后的受控停止恢复

用户确认重启或宿主退出原因，并明确授权继续本次审批时，使用 `apps.oa_finance_preaudit.owner_restart_recovery`。原监督者必须已保存真实 `state=exited`、非零退出码、failed 清单和 `children_reaped=true`，且原回执明确 `owner_process_lost=true`、`stop_requested_by_supervisor=true`，无停止转交或等待异常、无收到的停止信号。监督者丢失、退出码未知继续使用[原恢复合同](supervisor-loss-recovery.md)，不适用本入口。

恢复代码按[停机源码发布](production-source-release.md)先发布并同步生产。入口只接受精确 `codex_review_technical_failure`，停止文件修改时间须处于原监督调用的开始与真实完成区间。现场核对原 owner、invocation、真实工具 session、生产干净且同步的唯一 main、暂停的原小时 cron。默认仅预检：

```bash
.venv/bin/python -m apps.oa_finance_preaudit.owner_restart_recovery \
  --target-root "$production_root" \
  --expected-revision "$published_revision" \
  --automation-file "$original_automation_file" \
  --production-project-id "$production_project_id" \
  --expected-schedule "$preserved_schedule" \
  --original-receipt-file "$original_execution_file" \
  --original-owner-task-id "$original_owner_task_id" \
  --original-tool-session-id "$original_tool_session_id"
```

`exit_code=0`、`status=ready` 只证明本次预检通过，停止门仍在。已有本次明确恢复授权时，追加 `--confirm-recovery YES`。确认调用独立重复全部核验，不以此前预检放行。

恢复在 controller/global Writer 租约内等待完整只读 OA 状态，核对当前身份、授权、冻结输入、continuation、原监督者和 controller/独立进程组、登记只读进程及全部模型槽。关闭槽仍核对实际进程消失，运行中、未登记完、回收未知或新官方冷却均拒绝。保留过期冷却，不进行模型可用性探测。原 cron 必须 PAUSED，其他停止原因和环境停止拒绝。

原执行、工具绑定、日志、同轮清单/progress/worker/pool/槽位、原 action/state/冻结资料及提交产物、冷却、授权、当前状态和 cron 保留身份与字节哈希；同内容替换也阻断。`stop_attribution=original_terminal_interval` 是本次对停止文件时间与已封存原调用的核对，不声称保存了当时的 inode 观测。先写私有 `validated` 审计并重新核验，才精确清除绑定停止文件，最后保存 `cleared`。不修改原真实非零退出码，不启动审核、不提交 OA、不创建或恢复 attempt，不改调度状态，不处理历史 unknown。

只有真实退出 0、`status=cleared`、`stop_cleared=true` 且私有审计回读可信，才算恢复完成。清门后的异常仍报告实际 `stop_cleared=true/unknown`，不重复执行。原回执和原业务证据继续保留，未形成有效决定的待办使用下一新监督进程和原滚动控制器重新审核。已提交单据凭回执、后查和待办/已办闭环核验，不重放。正常授权轮次全部完成并核验后，才按用户授权恢复小时调度。
