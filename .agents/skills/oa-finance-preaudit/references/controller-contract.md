# 每日控制台合同

控制台负责实时清单、持久 FIFO、审核槽位、唯一写入器和脱敏运行闭环；不读取业务附件、不代替审核 Agent 判断财务结论。

## 启动与刷新

- 每日仅一个控制台。启动和每个独立小时调度任务发送的 `HOURLY_WAKEUP` 都读取角色 release manifest、冻结 Writer `status`、生产待办身份源、项目 `list-pending`、队列 `status` 与未配对 OPS；两个实时来源均成功后按 requestId 交集刷新并合并 FIFO。消息携带的 `hourly_launcher_thread_id + receipt_id` 是本小时反馈路由，不参与业务身份或队列排序。
- `role_cli controller runtime-snapshot` 必须同时读取并回报 `last_refreshed_intersection`、`actionable`、`blocked`、`candidate_state`、`oldest_actionable_age_seconds`、`queue_age_p50_seconds`、`queue_age_p95_seconds`、`fifo_head_age_seconds`、`fifo_head_lane`、`lane_oldest_preparation_age_seconds`、`prepared_total`、`contiguous_prepared_depth` 以及 `refresh_checkpoint.refreshed_at/age_seconds`；该交集数字只代表最近一次成功刷新。只有紧跟本轮双源成功刷新、刷新时间属于当前 epoch 且 `candidate_state=empty`，才可表述为“实时双源交集为空”；后续 reviewer/writer 回执只能称“最近刷新交集”。`candidate_state=all_candidates_blocked` 时必须保留 `closure=awaiting_ops_resolution`，不得表述为“实时双源交集为空”或“待办已清空”。
- 运行配置中的 Admin threadId 是唯一修改入口；控制台只能上报，不得修改 Skill、代码、automation 或财务规则。
- 每次 claim 前核对当前 controller/reviewer release 指纹。代际变化时停止新 claim，让旧 assignment 自然闭合，再用新指纹继续；控制面文档变更不得让已派发审核误报业务失败。

## 连续排空模式

- `actionable>0` 时保持一个 unfinished Goal，积压未清空时不得因固定时长停止新增 claim。普通 Codex 回合结束由未完成 Goal 自动续回合。
- 新建同日后继控制台的预检/ACK 回合不创建 Goal，只核验角色指纹、队列、生产状态和真实 threadId，并返回 `DAILY_CONTROLLER_ROLLOVER_ACK`。dispatcher 更新精确指针后发送 `CONTROL_START`；后继收到后第一步创建 fresh unfinished Goal，并在同一回合立即刷新、补槽、等待和排空，不得先发送 final。每次后续自动续回合先调用 `get_goal`。
- `get_goal.status=blocked` 不是 unfinished Goal，也不能在同一任务内用 `create_goal` 替换；此时不得在 blocked Goal 下继续 claim、派发 reviewer 或启动新的 writer。立即发出 `CONTROLLER_ROLLOVER_REQUIRED|failure_code=controller_goal_blocked`，停止新增 claim 和新提交，只让已经活动的 reviewer 与唯一 writer 自然闭合；随后生成 `DAILY_CONTROLLER_ROLLOVER_READY`，由当前小时调度任务执行同日控制台顺序换班。该换班必须立即发生，不得依赖下一小时调度任务。
- 每次 reviewer 或 writer 回执后读取 `role_cli controller runtime-snapshot`。只要 `refresh_checkpoint.due=true`，必须在下一次 claim 前建立滚动检查点，完整刷新双源并合并 FIFO；该机器字段按“距上次成功刷新 30 分钟”或“刷新后已闭合 10 个 attempt”先到者触发，成功刷新后归零。检查点、阶段回执、小时调度和跨日交接都不是 Goal 完成条件。
- 控制台读取 `role_cli controller status` 的 `pool.lanes`：差旅使用 `role_cli controller claim --lane travel`，业务招待费、业务支持费和小额市场费使用 `--lane finance_nontravel`，包装使用 `--lane packaging`。每次 claim 同时传入本轮 canonical reviewer manifest 返回的 `--reviewer-release-sha256`；角色入口缺少或拒绝该字段时不得创建 Reviewer。差旅与非差旅财务同时有积压时各保留至少一个准备槽；不得用无 lane 的 claim 把非差旅保留槽领取成差旅。
- 普通财务 0 条不建槽、1 条使用 1 个、2–19 条保持 2 个；达到 20 条且 `contiguous_prepared_depth < 2` 时，第 3 槽才具备补位资格。连续 prepared 前缀已达到 2 条时保持 2 个普通槽，让唯一 Writer 消化缓冲；每次 reviewer 或 writer 回执后重新读取 `reviewer_pool.boost_eligible/reason_codes/lane_priority`，不得凭旧积压数保留第 3 槽。包装待办另建立 `packaging_accuracy` 协调位，不计入普通财务 3 槽上限。平台拒绝创建时记录真实 capacity rejection，等待任一活动审核结束后再补槽；不得重复占用同一容量快照，也不得因容量不足跳过已经命中风险条件的必需复核。
- 每次刷新、Reviewer 回执或 Writer 回执后，按同一个最新 pool 快照连续 claim 并派发，进入第一次等待前补满所有当前可执行 lane，直到 `active_workers >= desired_workers`、没有对应 lane 的 queued 项，或平台明确拒绝当前一次创建。不得在只领取一条后提前等待。若 `active_workers < desired_workers` 且 backlog 仍大于 0，连续两个不同快照都没有新增活动 Reviewer、prepared 前缀也未达到门限，则立即记录 `reviewer_pool_underfilled` 并再次按最新快照补位；只有真实 capacity rejection 才作为容量原因。
- 审核与附件严格按 `requestId + received_at + attempt` 独立。普通财务 Reviewer 和唯一 Writer 都使用原生子 Agent：控制台调用 `spawn_agent` 并设置 `fork_turns="none"`，再用 `wait_agent`、`send_message` 和 agent path 管理；不得用 `create_thread` 创建普通财务 Reviewer 或 Writer。审核 Agent 一次只持有一个 assignment。包装关键文字通道例外使用独立可见任务：默认由协调者直接主审，只有命中风险条件时才用原生子 Agent 做一次独立复核，增强模式才启动 A/B/C；其他单据不得进入该上下文。返回后立即按通道补充空槽。OA 提交始终只有一个唯一写入器并按全局 FIFO 串行。
- 等待活动任务采用事件驱动的有界等待（单次不超过 60 秒）。有 backlog、无活动审核且无写入时，必须补槽或上报容量故障，禁止空转。
- 领取、派发或补满 reviewer 只是本轮中间动作：只要任一 reviewer/writer 活动或 `actionable>0`，本回合就禁止发送 final。每次派发或补槽后的下一动作必须对原生子 Agent 调用 `wait_agent`；只有独立包装任务使用 `wait_threads`。任一任务返回后按“回执 → 串行提交可提交队首 → 检查 `refresh_checkpoint` → 补槽 → 再次等待”循环。不得用“审核已启动”“等待回执”作为 final 文本，也不得依赖下一小时调度任务把本轮接回来。
- 收到带 `hourly_launcher_thread_id + receipt_id` 的小时调度消息后，必须把同一 receipt_id 绑定到本小时 epoch。首次真实双源刷新、首个审核派发、首个提交/阻断或本轮排空时，向该小时调度任务回传至少一项脱敏 `PROGRESS`、`SUBMIT`、`BLOCK` 或 `EPOCH_RECEIPT`；消息发送成功才算本小时控制面反馈闭合。普通 Reviewer、Writer 和观察者仍全部是本控制台的原生子 Agent，不为本小时内每个流程创建可见任务。

## 机械接口

- 使用 `role_cli controller release-manifest --project-root PROJECT_ROOT` 获取本角色复合指纹；角色由入口固定，不手工传 `--role`。
- 使用 `role_cli controller runtime-snapshot` 获取脱敏队列、生产、OPS 与角色指纹快照。
- 每次实时刷新和启动 Writer 前，都调用一次 `role_cli controller capture-live-inputs --snapshot-dir ABSOLUTE_UNIQUE_DIR --production-root PRODUCTION_ROOT --project-root PROJECT_ROOT`。该确定性入口独占创建 snapshot 目录，内部依次调用只读 `role_cli controller list-production-pending --output ...` 和项目 `apps.oa_finance_preaudit.cli list-pending --output ...`；前者只投影 requestId、received_at、workflow identity 与 support status，不读取附件或生成审核判断。只有 written 回执、final schema、绝对路径、计数和 SHA-256 全部闭合后才原子发布 `live-inputs.json`；已有目录拒绝复用。控制台只把该 manifest 交给 `role_cli controller refresh --live-inputs-manifest ...` 或 Writer，不再手工传递两个 source 路径，不得把 stdout、半成品目录或不存在的路径交给下游。manifest 必须在采集后 300 秒内消费；超时或采集时间异常时必须重新采集，不得复用旧快照。
- claim 后把完整 payload 原样交给全新审核 Agent，并把 payload 内嵌的绝对 `assignment_file` 明确标为唯一 canonical 命令路径。控制台可保存 claim stdout 或 epoch assignment 副本，但归档副本只用于审计记录；所有 reviewer 命令都不得传入 epoch 归档副本。审核完成只接受 `role_cli reviewer seal-review --assignment-file ...` 的状态机回执；采集只接受 `role_cli reviewer collect-assignment`。仅当原采集进程已确认结束而 stdout 丢失时，允许用只读 `role_cli reviewer collection-status` 恢复同一 attempt 的材料或脱敏失败回执；若误在进程退出前调用，CLI 必须以 `collection_process_active` 硬阻断，控制台继续等待原进程，不得把它转换为 unavailable 或结束 Reviewer。控制台与 reviewer 都不手工拼接采集路径、租约、封套或指纹参数。
- `prepared` 只交给[唯一写入器合同](writer-contract.md)；单据级技术失败安全闭合后继续 FIFO。写入状态不明、并行写入、登录失效或串单迹象停止新 claim 和 writer，并立即上报 Admin。

## 结束门禁与闭环

- 只有实时双源刷新成功且 `actionable=0`、没有活动 reviewer/writer 时，才完成调度 Goal并发送 final。阶段回执、达到 50 分钟、小时边界、“下一项已 prepared”或“审核已派发”不是 Goal 或当前回合的完成条件。
- Goal 已是 `blocked` 且 `actionable>0` 时，只允许以 `DAILY_CONTROLLER_ROLLOVER_READY` 作为当前控制台的顺序换班回执；不得把它表述为队列完成或等待下轮唤醒。后继控制台 ACK、精确指针切换和旧控制台归档补偿由 dispatcher 完成。
- `open_ops>0` 时可在 `actionable=0` 后结束调度，但回执必须 `closure=awaiting_ops_resolution`；所有事件配对、状态迁移及恢复后进度验证完成才写 `closure=closed`。
- 每个 attempt 从持久 `timing.json` 汇总 collection、observation、judgment、prepared_wait、writer_total、end_to_end；每轮输出实际槽位峰值、均值和 P95，不凭聊天估时。
- 跨日先停止新 claim、自然闭合活动任务并写 `DAILY_HANDOFF`；下一日控制台 ACK 后才登记旧控制台归档。当前任务异常仍须先实时上报并持久化，不能等下轮归档。
