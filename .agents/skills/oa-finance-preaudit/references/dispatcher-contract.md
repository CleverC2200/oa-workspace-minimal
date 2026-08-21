# 小时调度任务合同

本合同只约束每小时 cron 自动创建的独立可见调度任务。当前小时调度任务只负责登记、归档补偿、控制台发现、唤醒和脱敏回执；不得 claim、不得读取附件、不得执行审核或写 OA。本小时轮内的普通 Reviewer、Writer 和观察者由当天控制台创建为原生子 Agent，不创建侧栏任务。

## 当前任务登记与上一轮归档

1. 从当前 cron 运行的权威顶层身份取得当前 `thread_id`，立即持久化 `THREAD_REGISTER|role=hourly_launcher|thread_id=...|date=...|hour=...|generation=...`，再把标题设为 `OA财务预审小时调度 YYYY-MM-DD HH:00`。标题设置失败不得阻断登记、归档补偿或控制台发现；记录 `thread_title_unconfirmed` 后继续。只有无法取得当前精确身份时才记录 `hourly_launcher_identity_unavailable` 并进入本节第 3 条的无调度终态，不猜测身份。
2. 读取 automation memory 中全部未配对 `ARCHIVE_PENDING`，按登记时间从旧到新逐一补偿，不只检查紧邻的前一小时。只有其最新结束回执与 `ARCHIVE_PENDING` 引用同一个 `HOURLY_DISPATCH_RECEIPT`、任务没有活动回合、生产没有活动写入且异常已经持久化时，才调用 `set_thread_archived(archived=true)` 并追加 `ARCHIVE_RECEIPT`。单条归档失败记录 `OPEN_OP` 后继续检查其他 pending，也不阻断本小时控制台发现。
3. 当前小时调度任务不得在自身回合结束前归档自己。无论正常反馈、反馈超时、身份缺失还是控制台发现失败，只要当前精确 `thread_id` 已取得，结束前仍须生成 `HOURLY_DISPATCH_RECEIPT`；未发送控制消息时固定使用 `dispatch=not_sent`、`feedback=对应failure_code`、`write_state=confirmed_no_write`、`closure=awaiting_ops_resolution`。随后登记 `ARCHIVE_PENDING`，引用该 receipt_id；下一小时调度任务核验终态后完成归档。若下一小时到达时前一任务仍有活动回合，只保留 pending，不中断、不重复唤醒该任务。

## 当天控制台发现与启动

1. 读取 automation memory 中当天控制台的精确 `thread_id` 与 generation，并按精确身份查询任务顶层状态和最新权威回执。
2. 控制台存在且可用时先执行启动缺口恢复：若最新结束回执为 `DAILY_HANDOFF_ACK`、`DAILY_CONTROLLER_ROLLOVER_ACK` 或 `CONTROL_START_REQUIRED`，且其后没有滚动 Goal 的权威进度，则只发送一次 `CONTROL_START`，本次不得改发 `HOURLY_WAKEUP`，也不得等待下一小时；否则发送一次 `HOURLY_WAKEUP`，要求刷新实时双源候选并合并当前 FIFO。两种消息都必须携带当前 `hourly_launcher_thread_id`、hour、generation 和 receipt_id，供控制台回传本小时反馈。
3. 当天控制台经权威任务查询确认不存在时，才创建一个题为 `OA财务预审控制台 YYYY-MM-DD` 的控制台并登记精确 threadId。新控制台完成 `DAILY_HANDOFF_ACK` 后立即发送 `CONTROL_START`；ACK 前不得 claim 或写 OA。
4. 控制台发现未知或超时时记录 `controller_discovery_unavailable`，不得据此推断空队列、创建替代控制台、claim 或写 OA。

启动状态必须同时看任务顶层状态和最新回合。顶层状态为 `active` 或当前存在 `inProgress` 回合时，表示控制台已在运行；此时发送 `HOURLY_WAKEUP` 合并新候选，不得再次发送 `CONTROL_START`。若精确回合读取超时，但任务列表按精确登记 `thread_id` 唯一命中当天控制台，顶层任务状态足以发送 `HOURLY_WAKEUP`，不得记录 `controller_discovery_unavailable`。控制台尚未启动时返回 `CONTROL_START_REQUIRED`；当前小时调度任务在同一回合有界回读，并在收到 `CONTROL_START_REQUIRED` 后立即补发一次 `CONTROL_START`。Admin 发送的 `ROLE_RELEASE` 回合只返回 `ROLE_RELEASE_ACK`，不得顺带执行调度动作。

若精确读取与列表恢复都超时，但 automation memory 中已登记的当日精确控制台指针在同日已有真实 `PROGRESS`、`SUBMIT`、`BLOCK` 或 `EPOCH_RECEIPT`，说明该控制台已经启动；此时直接调用 `send_message_to_thread` 向该精确 `thread_id` 发送一次 `HOURLY_WAKEUP`。返回的 `threadId` 必须与登记值一致，匹配即视为本小时消息已交付并继续等待同 receipt_id 反馈；发送失败或返回不一致才记录 `controller_discovery_unavailable`。该降级只投递到已登记且已证明启动的同日指针，不按标题替换指针，也不得重复创建控制台。

## 本小时反馈闭环

发送 `CONTROL_START` 或 `HOURLY_WAKEUP` 后，使用权威任务等待能力有界等待当前控制台回传同一 receipt_id。至少取得一项真实 `PROGRESS`、`SUBMIT`、`BLOCK` 或 `EPOCH_RECEIPT`，才能生成：

`HOURLY_DISPATCH_RECEIPT|receipt_id=...|date=...|hour=...|generation=...|controller_thread_id=...|dispatch=CONTROL_START或HOURLY_WAKEUP|feedback=PROGRESS或SUBMIT或BLOCK或EPOCH_RECEIPT|write_state=...|closure=...`

控制台已经持续排空时，本小时只合并新候选，不终止旧 Goal、不创建平行 Goal。若反馈等待超时，持久化 `OPEN_OP|failure_code=hourly_feedback_unavailable`，回执使用 `closure=awaiting_ops_resolution`；不得把消息已发送当成运行闭环，也不得声称队列为空。

## 同日控制台顺序换班

控制台返回 `DAILY_CONTROLLER_ROLLOVER_READY` 且原任务没有活动 reviewer/writer、生产 `active_run=false` 时，当前小时调度任务立即创建同日后继控制台。ACK 前不创建 Goal；后继返回 `DAILY_CONTROLLER_ROLLOVER_ACK` 前不得 claim 或写 OA。ACK 后先更新精确控制台指针，再发送一次 `CONTROL_START`。随后旧控制台进入归档补偿：登记 `ARCHIVE_PENDING`，并由本小时或下一小时调度任务精确归档。任何阶段都不得并行存在两个 claim 控制台或两个 writer。

## 调度载荷

只发送当前日期、hour、generation、项目与生产工作树、Admin 指针、当前小时调度任务精确 threadId、dispatcher 角色指纹和 `role_cli controller capture-live-inputs` 刷新指令；不发送附件、意见正文、人员或公司信息。
