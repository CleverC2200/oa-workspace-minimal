# 控制器执行回执

显式授权的提前单轮执行使用[人工单轮冷却豁免](../runbooks/cooldown-override.md)，仍由本入口监督。原执行、冷却字节、完整启动对账和当前调用保存在私有豁免回执中；execution.json 绑定其路径和 SHA-256，进度回读核对该绑定。它不改变普通启动、工具会话绑定或真实 wait 合同。

正常已授权调度使用 `python -m scripts.finance_preaudit_run --owner-task-id <当前聊天 UUID> -- <原滚动控制器参数>`。该入口仅监督现有 `scripts.finance_preaudit_concurrency_probe`，确认写入、范围、恢复参数和全部门禁仍由原入口校验；开发验证不传真实业务写入参数。

每轮启动可在分隔符 `--` 之前传入 `--review-provider deepseek` 或 `--review-provider openai`。默认 OpenAI 使用 App 固定子 Agent 对话；显式 DeepSeek 使用 Flash/high 的原 CLI/API 路径。OpenAI 的审核模型和推理级别未观测时记录 unknown，不能用外层调度模型补填。选择只传给本轮新进程及其审核子进程，保存为 `execution.json` 和控制器清单的 `review_configuration`，进入配置指纹；运行中不更换供应商。下一轮可重新选择，无需修改源码或全局 Codex 配置。进度、取回和工具会话绑定入口拒绝混入供应商选择。切换不清冷却、停止门或未知写入，也不授权重新启动已有控制器。

本轮后续包装使用同一选择，OpenAI 进入本轮 App 包装阶段并复用已登记的 3 个子 Agent，DeepSeek 使用 `OA_REVIEW_PROVIDER=deepseek OA_REVIEW_TRANSPORT=cli python -m apps.oa_finance_preaudit.packaging_review --action-file <当前动作>`。财务滚动池仍不包含包装。DeepSeek 使用独立 API 密钥和空运行目录；OpenAI 使用现有原生认证，不接收 DeepSeek、OpenAI 或 Codex 的 API key 环境变量。

自动告警由本轮控制器参数 `--confirm-alerts YES` 独立授权，放在监督入口的 `--` 后。缺省或 `NO` 返回本地 `not_authorized` 回执，不发送消息，也不清改已有告警窗口；审批写入授权、环境中的告警目标和 PAUSED 调度状态均不能代替本轮告警授权。获授权后继续使用现行去重和 unknown 保护。手工单次告警仍要求其专用 `--confirm-send YES`。

工具调用者与每轮监督者分开：入口启动一个独立进程组中的本轮监督者，由它创建、真实 wait 原控制器并独占保存 execution.json。它不是常驻 daemon 或额外模型 worker，不重复启动控制器。调用者收到停止信号时转交监督者；调用者或其原工具父进程消失或 PID 身份变化时，监督者请求既有停止门并继续等待 Writer 和控制器收尾，保留真实退出码。启动前和每次 DeepSeek CLI 启动前核对已绑定的 PID、出生时间、进程组及用户，不能仅凭 PID 仍存在放行。

监督者自身被强制终止时，控制器心跳及运行中的审核 worker 每 5 秒核对独立的父链身份，并通过技术停止门有序收尾；DeepSeek CLI 发起前的核验不等待这个间隔。不能强杀可能在途的 Writer。机器断电、所有保护进程同时被强杀、观察前已经发出的请求仍不保证有完整退出回执，不承诺操作系统任意终止下恢复真实 wait。

启动输出给出 `receipt_file`、拥有者和控制器 PID。保持并等待原工具 session；工具返回 `session_id` 而没有 `exit_code` 时继续等待原 session。外层返回原工具 session ID 后，用 `python -m scripts.finance_preaudit_run --owner-task-id <同一任务 UUID> --bind-tool-session <原 ID> --receipt-file <启动给出的 execution.json>` 保存独立 `tool-session.json`，然后继续等待原 session。该元数据入口不启动控制器，只校验本次归属，拒绝冲突绑定，标记来源为 caller_reported_tool_result；不能把自行编造的 ID 当真实工具回执。Python 无法自己取得外层 ID，主执行回执该字段保持 unknown，指向单独绑定文件以避免并发覆盖；未绑定时如实记录缺口。禁止以函数单元的 `Script completed` 替代内部原进程终态。

回执与原 stdout/stderr 保存在 `runtime/control/executions/<invocation>/`，目录独占、权限受限。长时间无输出、低 CPU、浏览器关闭警告均不是结束条件。等待中需要检查健康时，使用原身份匹配的 prepare 采样及只读状态文件，计算采样年龄；保留停止门、Writer、身份、冻结和未知写入门禁。

`state=exited` 只证明监督进程实际 `wait` 得到子控制器退出码；`result=completed` 还需要同 PID、本次开始时间之后的清单为 completed 且 children_reaped=true。终态缺口或不可信结果为 blocked，不证明已安全回收或可以再次启动。进程信号退出记录真实负退出码和信号。监督者被 SIGKILL 或机器断电时，原保存记录可能仍是 waiting；读取入口在可确认监督者丢失时投影 state=supervisor_lost、recorded_state=waiting、status=blocked、terminal=false，保留未知退出码，不改写原证据。新回执用 supervisor_process 核对 PID 复用，历史缺失该字段时只使用原兼容存活检查。子清单收尾或已确认回收不代替真实 wait，按原隔离排障入口核对，不能重放。

启动竞争后，只有本次子进程退出0、确认没有本轮镜像且原输出完整验证正常活动控制器门禁时，才记录 result=skipped；此时不能认领他人 run、进入包装或清窗。存在但损坏/不匹配的镜像仍 blocked，不能靠 skipped 输出覆盖坏证据。

## 固定 App 审核槽的原生派单

财务控制器直接发布并轮询 App 请求，不创建 Python 审核等待池；取材仍使用隔离准备进程，完成结果进入唯一 Writer。每轮先收齐当前可见结果并分类共享失败，再按 FIFO 启动新写入。包装沿同一发布和轮询入口，保留实际请求的 Schema。

历史冻结材料的只读容量诊断使用标准库执行器及现役进程登记、原父进程监视和回收入口。每个在途任务独占一个执行器，单项超时或崩溃只回收自己的工作进程和已登记模型，健康同伴继续；报告的 `processes_reaped` 必须来自真实回收检查，无法证实时保持失败并停止接纳新任务。诊断不领取、不完成 OA 单据，不清生产停止门。生产弹性与旧 batch/drain 执行已退场，旧公开入口继续返回稳定拒绝。

新财务 App 请求的审核预算为 1800 秒，控制器 watchdog 使用同一预算加 60 秒收尾余量；保留的 CLI 审核仍为 900 秒。注册 bootstrap 900 秒、父心跳 300 秒、准备 300 秒及领取窗口不变。预算只作用于新请求，不延长已经发布的 `expires_at`。可信未写请求到期沿单据隔离入口处理，迟到决定拒收；共享身份或来源失信仍停止整轮。

App 等待异常保留主码 `host_review_exchange_failed`，并返回安全的 `failure_detail_code`、异常类型及 `failure_recorded`。共享交换失败的首错保存在本轮私有 registry 的 `first_failure`，绑定请求路径和 SHA-256；后续关闭或停止错误不覆盖它。未发布或无法安全留证时明确 `failure_recorded=false`，不保存任意异常正文。历史无细码的轮次保持未知，不追溯补造。

本节只适用于 OpenAI `host_chat`。唯一滚动控制器启动一次后，主对话创建恰好 3 个原生 `collaboration.spawn_agent`，保留其真实 Agent 路径、子会话 UUID 和成功工具回执。每个 Agent 一次只审核一单，返回结果后空闲；下一单调用同一 Agent 的 `followup_task`，本轮不替换身份、不递归创建审核者。DeepSeek 不执行本节。

用户明确要求在当前聊天另建审批监督子 Agent 时，该监督者以自己的真实原生会话 UUID 拥有新轮次，并创建三个直属审核者。登记以可信监督者的原生祖先深度加一核验直属关系，同时核对精确 parent_thread_id、Agent 路径和实际 spawn 回执；不把整个任务树的绝对 depth=1 当成所有轮次的要求。根聊天监督的审核者仍须 depth=1；跳级、同级及损坏祖先拒绝。旧轮拥有者和固定审核槽不重新绑定。

1. 保留原 `execution.json`、`invocation_id` 和工具 `session_id`，按上方入口绑定原 session。用本轮父会话的 3 次真实 spawn 回执登记槽位。来源行必须指向原生工具调用及其已完成输出；来源文件和路径不能猜测。命令中 3 组子会话文件、spawn 行按槽位 0、1、2 排列。

```bash
python -B -m apps.oa_finance_preaudit.host_chat_review --operation register --owner-task-id <主会话UUID> --source-session-file <父原生会话jsonl> --reviewer-session-file <槽0子会话jsonl> --spawn-line <槽0spawn行> --reviewer-session-file <槽1子会话jsonl> --spawn-line <槽1spawn行> --reviewer-session-file <槽2子会话jsonl> --spawn-line <槽2spawn行> --receipt-file <原execution.json> --invocation-id <原调用ID> --tool-session-id <原session_id>
```

2. 注册回执成功后，在下一次独立原生 `functions.exec` 中续首次父任务心跳，再读取 pending。注册与首次心跳不得合并在同一次原生调用中；其来源时间早于注册时，心跳拒绝且不会续租。控制器在可信首次心跳前保持自身心跳，不取材或发布请求；注册与首次心跳共用原清单开始时间的 900 秒宽限，轮询不重新计时。等待期间 pending 为空不代表队列已完成。随后每次取得可信进度后读取当前请求，并在 60 秒以内续一次父任务心跳。App 父租约独立为 300 秒，用于容忍原生派单和工具调度超过 45 秒；控制器自身的 45 秒进程心跳门保持原值。父租约到期阈值为 5 分钟，到期后的下一次既有检查拒绝派单和结果，控制器观察器再请求停止。原进程身份丢失、显式停止及单据截止时间仍在原检查点生效。心跳必须来自主会话本次已落盘的原生调用，父会话 UUID 和实际调用时间均需可信。可在当前原生 exec 内调用 `source_anchor(owner=owner, operation="heartbeat")` 取得当前来源，再调用 `heartbeat(owner=owner, **anchor)`；重复旧来源不延长租约。维持原工具 session 的等待；不得等待直到控制器退出才派单。心跳到期后，新增派单和结果接收在下一次检查时拒绝，不把等待超时当审批重试授权。

```bash
python -B -m apps.oa_finance_preaudit.host_chat_review --operation heartbeat --owner-task-id <主会话UUID> --source-session-file <父原生会话jsonl> --source-line <本次heartbeat工具行>
python -B -m apps.oa_finance_preaudit.host_chat_review --operation pending --owner-task-id <主会话UUID>
python -B -m apps.oa_finance_preaudit.host_chat_review --operation status --owner-task-id <主会话UUID> --run-id <本轮run_id>
```

3. 请求文件名绑定本轮 owner，保留旧轮同 attempt 的请求、决定和提交证据。pending 只发现本轮注册表登记且属于当前 progress 的请求；派单使用返回的绝对路径，不拼接固定文件名。对 pending 返回的本轮请求，调用空闲原 Agent 的 `followup_task`，只传请求绝对路径和当前单据边界。保留该次真实工具调用来源；它完成后执行 assign，把请求绑定到原槽位。一个槽位有未交回请求时不能再派单。dispatch 的原生时间、请求指纹、子会话和来源必须匹配。

pending 的 round 及独立 status 返回本轮原 3 槽、可用容量和失败请求。失败槽在新鲜原生空闲证明前保持封存；全部封存时控制器停止领取，保留未处理单据并等待统一收尾，不替换 Agent。

主线程使用原执行回执、pending 和原生工具进度监督审核，向用户汇报并持续父心跳。审核槽完成当前取证、必要验算和决定发布；仅在发生阻断或需要澄清时交回中间消息，避免为纯进度回复增加一次携带累计图片的模型请求。

派单来源必须是登记过的父会话文件，同一个原生 call_id 或来源行只能绑定一个请求；同请求、同槽、同来源的重复 assign 保持幂等。明文 message 仍核对请求路径。App 原生记录使用当前已识别的有界 opaque 格式时，只记录 dispatch_message_readable=false，不宣称正文或当前请求已送达；当前请求归属必须继续由已登记子会话真实 write-draft 调用及其专用回执中的相同 request SHA-256 确认。缺确认、不同请求、错槽、过期或已关闭均拒绝，不读取密钥或解密日志。

```bash
python -B -m apps.oa_finance_preaudit.host_chat_review --operation assign --owner-task-id <主会话UUID> --request-file <本轮request.json> --slot <0或1或2> --source-session-file <父原生会话jsonl> --source-line <本次followup行>
```

财务请求过期时，控制器只在原请求、冻结输入和未写入证据可信后撤销该项授权并隔离；其他槽位按原 FIFO 继续，失败项不在本轮重领。失败槽位仍占用，主线程先中断该原 Agent，再取得失败发生后的原生 `list_agents` 空闲、完成或已中断状态，调用 `release-failed-slot` 验证并释放。只有释放成功才给该槽位下一单。来源、共享身份或未知写入不可信时仍停止整轮。

```bash
python -B -m apps.oa_finance_preaudit.host_chat_review --operation release-failed-slot --owner-task-id <主会话UUID> --run-id <本轮run_id> --request-file <失败请求绝对路径> --source-session-file <父原生会话jsonl> --source-line <失败后list_agents行>
```

4. 子 Agent 读取当前请求提示词、规则和冻结材料，形成本单决定或包装草稿。保存只使用专用 `write-draft`，目标必须是尚不存在的私有文件。先在本轮有效控制环境中调用 `host_chat_review.draft_command(Path(request_file), Path(decision_file), payload, owner=owner)`，取得固定发布命令。命令在 `uv run --quiet python -B -m ... --operation write-draft` 前携带 `/usr/bin/env` 及本轮 controller lock、stop file 的绝对路径，使独立原生 exec 使用相同控制目录；仅传这两项路径，核验时须与本轮完全一致。已有原生记录中的规范 bare uv 命令仍可核验。此仓库无 pyproject.toml，不使用会产生项目警告的 no-sync；发布只返回一个 JSON 回执。payload 是审核者形成的原 JSON；不手工拼接 shell 参数，不使用通用写文件脚本，不覆盖已有文件。

将该完整命令用 JSON 字符串字面量填入下方 cmd，再执行一个独立原生 exec。整个调用只含这一条表达式，不附加变量、注释、分支或其他工具。此示例中的占位文字必须替换为 draft_command 返回值，不能照字面执行：

准备命令时用标准 JSON 编码器生成整条原生调用，再原样执行输出，避免手工转义或模板字符串破坏来源合同。下面只生成文本，不发布草稿；`command` 是上一步 `draft_command` 的完整返回值：

```python
print('text(await tools.exec_command(' + json.dumps({'cmd': command}, ensure_ascii=False) + '));')
```

草稿发布后的来源行由主聊天从已登记子会话定位；子 Agent 交回专用回执及决定路径即可返回空闲。

默认工具目录与审核工作树不同的时候，可在 exec_command 的同一个 JSON 对象中增加 workdir。其值必须是当前审核项目根目录的完整绝对路径，与宿主 PROJECT_ROOT 字符串完全一致；其他目录、相对路径及其他工具参数仍拒绝。cmd 和专用发布回执的校验保持相同。

```javascript
text(await tools.exec_command({"cmd":"完整规范命令字符串"}));
```

等该次原工具输出实际结束、exit_code=0，且 stdout 的专用发布回执匹配当前 request_id、request_sha256、目标文件和决定字节 SHA-256，再用其来源行 submit。仅文件存在、调用中提到路径、另一个命令成功或自行打印的成功对象均不构成证据。正在执行的 submit 不能充当自己的已完成发布证据。子 Agent 只交回决定，不调用 OA 完成动作。主对话保持心跳并等待原控制器，Writer 仍按认领顺序管理。

原生日志可能在一次图片观察输出中内嵌多张大图。来源扫描按小块读取，保持原行号，跳过指定来源调用之前超过 32 MiB 的非目标记录；父心跳查找也跳过这些不可用作来源的记录。会话头、被引用调用及配对输出仍须在原 32 MiB 上限内，并按原字节计算指纹。调用之后、配对结果之前遇到超限记录时，无法确认它是否为目标输出，仍拒绝接收。原日志不改写；逐页业务观察要求不变。

```bash
python -B -m apps.oa_finance_preaudit.host_chat_review --operation submit --owner-task-id <主会话UUID> --request-file <本轮request.json> --decision-file <本单私有决定json> --source-session-file <已登记子会话jsonl> --source-line <已完成决定写入行>
```

财务原工具 session 和原回执均确认 completed、children_reaped=true，且新鲜结束门禁可信、Writer 空闲后，执行 `host_chat_review --operation enter-packaging --owner-task-id <主会话UUID>`。该操作只推进同轮阶段，不重启控制器，不增加 Agent；包装请求继续使用上方 pending、followup、assign、submit。包装业务确认和完成动作见[包装审核](packaging-review.md)。包装主审请求还必须由 App 完成入口产生可信 app-completion.json，绑定语义确认、决定、闭合 action/state、执行回执和文件指纹；独立复核请求只是只读结果，不代替 OA 完成。仅主审 submitted 或 draft_ready 时 normal close 拒绝，不能直接把本轮报完成。包装及结束对账完成后，执行下面的统一收尾。

### 本轮精确暂缓未提交单据

用户允许先跳过拿不准的单据时，在下一轮滚动入口重复传入 `--defer-action-file /absolute/path/to/action.json`。参数只缩小本次选择范围，不改变原单状态，不重采材料；不提供参数的后续轮次会重新选择该单。运行中不修改本轮选择，已有控制器先按下节完成收尾。

暂缓对象必须是本轮流程范围内的最新 attempt，保持可信 `review_required / controlled_write / authorized`，身份、冻结和授权原状态校验通过，且不存在规范决定、意见、Writer 边界、执行回执或提交信封。域外路径、符号链接、重复路径、旧 attempt、与恢复选择重叠或任何校验失败均拒绝。私有草稿不等于已提交决定，不自动复制到新轮次。

全局对账和 Writer 门禁先按原规则通过，之后才从可行动数量中扣除当前待办的暂缓项，其余单据保持 FIFO。暂缓不能掩盖身份冲突、未知写入或损坏冻结，也不能代替隔离恢复。结果中的 `deferred_action_files` 和 `deferred_selected_count` 只说明本次选择；暂缓不计作提交或隔离。只剩暂缓项时本轮可结束，仍须明确报告这些待办没有完成，不把本轮结束称为全部待办已清空。

### App 统一收尾

正常完成、人工取消、官方限制、子 Agent 异常和父任务主动退出均执行同一协议。主对话进入 final 前必须取得 `preaudit_app_shutdown` 的 `status=verified` 回执。父 final 不会可靠地中断仍运行的子 Agent。DeepSeek 保持原收尾路径。

1. 调用 `begin-shutdown`，reason 选择 completed、cancelled、reviewer_failed 或 parent_lost。它冻结本轮派单、续租及迟到结果，返回原 3 个 `interrupt_targets` 和原工具 session ID。非正常原因关闭 exchange；只有原执行终态与回收可信，且 Controller、全局及请求 Writer 均空闲时，才省略周期停止请求。不发信号终止 Writer。begin 与在途包装完成共用原轮锁，可能等待已进入的 Writer 完成后才返回；不承诺抢占写入。单独的 exchange cancelled/closed 不代表统一收尾已完成。
2. 对返回的每个原 Agent 调用一次原生 `collaboration.interrupt_agent`，即使它当前已经空闲，也保留实际配对回执。不得新增或替换 Agent。随后调用原生 `collaboration.list_agents`，确认本轮 3 槽全部为 idle、completed 或 interrupted；仍 running、缺失、工具失败或来源不可验证时保持 pending。
3. 继续等待原工具 session 和 Writer。原工具必须有实际终态 `write_stdin` 回执，调用单独使用下方形状；只轮询、不写 stdin。财务阶段已经取得的同一原 session 退出回执可复用。进程回执必须 terminal、children_reaped=true，退出码与原生等待一致，Writer 和请求 Writer 均空闲。非零退出只可封存为取消/异常收尾，不能报业务完成；unknown 写入及封存失败保持原阻断，不重放。
4. 调用 `finish-shutdown`，提供同一父原生来源文件、按槽位顺序的 3 个 interrupt 行、最终 list_agents 行以及原 write_stdin 行。它检查时间顺序和配对输出摘要，保留精简来源证明。正常完成额外执行财务及包装原闭合门禁。缺证据时保持 pending，可继续监督并补齐证据。若正常计划期间发生取消，重新 begin-shutdown 以已记录的取消原因降为异常计划，原冻结时点保持不变；不能升级回正常计划。此时不能把它当成功或重启原审批。verified 后才结束父回合。

```bash
python -B -m apps.oa_finance_preaudit.host_chat_review --operation begin-shutdown --owner-task-id <主会话UUID> --reason <原因>
```

```javascript
text(await tools.write_stdin({"session_id":12345,"chars":"","yield_time_ms":1000}));
```

等待调用继续优先使用上方 JSON 引号键。收尾读取器也接受同一白名单属性的无引号 JavaScript 名称，值限 JSON 字符串或非负整数字面量；变量、计算属性、表达式、重复键及附加语句均拒绝。兼容只解析原始调用，不执行 JavaScript，不改原生来源或配对输出；原会话、真实退出、回收及 Writer 校验保持。

```bash
python -B -m apps.oa_finance_preaudit.host_chat_review --operation finish-shutdown --owner-task-id <主会话UUID> --source-session-file <父原生会话jsonl> --interrupt-line <槽0interrupt行> --interrupt-line <槽1interrupt行> --interrupt-line <槽2interrupt行> --source-line <最终list_agents行> --wait-line <原session真实终态wait行>
```

CLI 的旧 `close` 操作也走相同收尾核验，不能省略证明参数。Python 内部 `close()` 只负责 exchange 止损，供父链失效观察器等使用，不构成主回合退出许可。突然失去整个父会话时仍由原父链门和300秒租约止损；没有活着的 App 主对话就无法补发原生中断。机器断电、App崩溃或所有保护进程同时被强杀不保证完整收尾，结果缺失保持 unknown。

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
