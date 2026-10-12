# 失去监督后的受控停止恢复

原监督者已丢失、真实 OS wait 无法恢复，且上一轮 failed/reaped 时，只有另行明确授权才使用 `apps.oa_finance_preaudit.supervisor_loss_recovery`。本入口保留原 waiting、未知退出码及历史单据归属未验证，不将旧轮标为成功。源码维护回执不能代替本恢复。

本入口仅处理精确的 `codex_review_technical_failure` 停止门和一条显式选定的历史采集取消。它不运行审核、不写 OA、不创建 attempt、不启停调度，也不改变自动归档。工程测试、代码发布、停止恢复及真实审批验收分别留证。

## 预检与确认

恢复代码先按现行发布门合并和同步生产。现场核实生产唯一、干净、同步的 `main`、预期远端修订、原执行的 owner/invocation/tool session，以及真实原 `oa-skill` 的项目和计划。原调度必须仍为 PAUSED。以下变量来自该次现场证据，不使用猜测值。

```bash
.venv/bin/python -m apps.oa_finance_preaudit.supervisor_loss_recovery \
  --target-root "$production_root" \
  --expected-revision "$published_revision" \
  --automation-file "$original_automation_file" \
  --production-project-id "$production_project_id" \
  --expected-schedule "$preserved_schedule" \
  --original-receipt-file "$original_execution_file" \
  --original-owner-task-id "$original_owner_task_id" \
  --original-tool-session-id "$original_tool_session_id" \
  --recover-action-file "$selected_old_action_file"
```

默认仅预检。实际进程退出 0 且 `status=ready` 表示本次验证通过，`stop_cleared=false`，没有恢复审批。确认清门需要独立当前授权，并追加 `--confirm-recovery YES`。确认调用重新核验全部条件，不使用前次 ready 结果放行。

Controller/global Writer 租约覆盖原回执、完整实时状态、授权和冻结材料校验、审计及清门。目标自己的解释器和已安装校验器读取原 owner、invocation、真实 tool-session、同轮 rolling/mirror、progress、进程登记、全局模型槽和历史证据。原监督者、控制器进程组及登记只读/模型进程均须实际消失，回收和未登记完的启动意图未知仍拒绝。官方冷却记录存在或近三小时同项目有可识别的原生官方终态错误时拒绝，数据库不可读也拒绝。观测仅读，不写冷却或停止记录，不调用模型探测。

当前 OA/web 身份和认证、流程授权、冻结输入均须可信。普通 continuation 只能因这一条可信采集取消阻断。选定单据须是当前最新首 attempt、唯一待办且未命中已办，有验证通过的 controller-scope manual receipt、`oa_run_stopped`、`not_requested`，无 Writer、执行、意见、提交或冻结产物。复用现有精确未写入恢复合同验证，不新增错误白名单。其他当前未知写入、身份冲突或阻断均拒绝。

停止门、实际暂停调度、授权文件、原执行与历史证据、状态文件及当前待办产物绑定文件身份和 SHA，并在审计落盘后、清门前再次核验。同内容替换也拒绝。审计文件在私有唯一目录中保存为 0600；先保存 validated，再通过 stop-control 租约精确清除原标记，最后保存 cleared。原 execution、失败 attempt、raw continuation 和调度字节不改。

## 结果与下一轮

确认调用只有实际退出 0、`status=cleared`、`stop_cleared=true` 且独立审计文件可信，才证明此次停止恢复完成。`original_wait_verified=false`、`original_exit_code=null` 和 `historical_stop_attribution=not_verified` 始终保留。审计列出唯一 `selected_recovery_action`、原执行绑定、当前状态证据和文件指纹，不能用它证明旧轮有真实 wait。

`status=blocked` 不授权审批。特别是 `stop_cleared=true` 或 `unknown` 的失败，清门可能已经发生；保存原输出并核对实际停止状态及审计，不重复执行，不假称门仍在。审计仍为 validated 也不能冒充 cleared。

后续另行授权的审批必须使用[原监督入口](../agents/controller-execution.md)创建全新执行，明确传入这份审计选定的 `--recover-action-file`，重新采集材料。不能与 `--recover-confirmed-no-write-once` 混用，不续跑旧进程或复用旧决定。全局 3 槽、5 秒原生错误观测、首个官方错误停止整轮并冷却 3 小时，以及原身份、授权、Writer、冻结和退出回执门禁保持不变。

清门不关闭历史 raw continuation，也不自动恢复小时调度。精确恢复后，实际提交、后查、待办/已办闭环和新监督者真实退出须分别验证，才讨论恢复小时调度。Codex 桌面与审核 CLI 的当前账号资格仍需在实际执行前另行核对；本维护的 OA 身份与本地错误记录不能证明客户端同号，也不保证官方尚未落盘的通知可被观测。
