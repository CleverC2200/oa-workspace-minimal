# 人工单轮冷却豁免

只有用户在当前聊天明确要求忽略历史冷却并运行一轮，且重新确认 Desktop 账号、个人工作区和未切号后，才使用此能力。默认启动继续遵守三小时冷却。授权不证明模型已经恢复，也不代替 OA 身份核验；不调用模型作可用性探针，不切换账号，不恢复小时调度。

在已发布并同步的生产唯一干净 main 中使用现行 Python。传入当前真实聊天 UUID，以及此前失败轮次的原 execution.json、原 owner 和实际工具会话 ID：

```bash
.venv/bin/python -m scripts.finance_preaudit_run \
  --owner-task-id <当前聊天真实UUID> \
  --review-provider deepseek \
  --ignore-cooldown-once YES \
  --original-receipt-file <原execution.json绝对路径> \
  --original-owner-task-id <原聊天真实UUID> \
  --original-tool-session-id <原实际工具会话ID> \
  -- --rolling --fixed-capacity --capacity 3 --max-capacity 3 \
  --confirm-write YES --recover-confirmed-no-write-once YES \
  --workflow travel_expense --workflow business_entertainment \
  --workflow business_support_fee --workflow small_marketing_fee \
  --workflow tpm_small_marketing_fee --workflow general_expense
```

入口仅接受上述六类固定范围和参数顺序，拒绝只读、绑定、缺少原回执或不同并发参数的混用。启动前核对生产远端、代码、唯一 main、原实际退出、同轮只读进程回收和可信未写入中断；在控制器和全局 Writer 租约内运行一次完整 status，以私有文件保留字节数和 SHA-256，并使用现行校验器核对身份、授权、冻结、严格 continuation 和当前写入状态。不能使用旧状态文件放行。

全部门禁通过后，入口保存 0600 的 cooldown-overrides 回执和原冷却字节副本，再精确清除已绑定的官方冷却停止标记。原 cooldown.json 保持身份和字节；人工、损坏、未知原因或新的停止门不能清除。豁免仅在当前任务、单次 invocation、原冷却指纹和原调用进程身份均匹配且仍存活时有效，不能传给另一监督调用或写入自动化配置。原进程退出后普通启动重新遵守现有冷却。

主聊天官方错误可能在上一审批退出后生成 `run_id=null` 的冷却。入口从目标本机原生只读历史重新核验该项目的官方失败事件、错误类型、时间及每个来源摘要，要求当前冷却不早于原审批真实退出。上轮审批的关闭证明继续绑定它自己的原冷却和未写入中断，当前主聊天事件另以 `cooldown_attribution=project_notice` 留证。来源缺失、历史不可读、时间不符或其他审批 run 仍阻断；不得补造 run 归属或改写旧关闭证明。新鲜核验与消费期间，当前冷却和原审批证据均须保持不变。

私有执行目录以独占创建消费这次豁免，execution.json 绑定实际监督者和控制器的 PID、出生时间、进程组与用户。开始执行后只允许该监督者、控制器及同 run/instance 的已登记只读 worker 使用；复制环境变量、原调用仍存活或冷却自然到期均不能复用该 invocation。终态回读验证原绑定，保留真实退出码。

每五秒重复观测同一历史首错不撤销本次豁免。出现新的官方错误时，立即停止新增 CLI 和本轮审批，并以新事件时间开始新的三小时冷却；后续同轮错误不延长期限。旧冷却字节及旧首错证据保留，普通技术失败继续只停止当前单据、不重试。

启动输出包含 audit_file、startup_status_file、stop_cleared；清门成功只表示维护动作完成。随后按[原执行回执](../agents/controller-execution.md)绑定实际返回的工具会话 ID并等待同一会话，运行中只读原回执和本地五秒进程采样。真实退出后重新取得一次完整结束对账，按实际 Writer、后查和待办消失分别证明提交结果。失败不补跑，不自动进入包装或恢复调度。

清门后发生启动或审计异常时，保留私有回执并核对实际停止状态，不重复执行命令或声称未清门。工程检查、生产同步与真实单轮业务验收分别记录。
