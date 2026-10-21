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

监督回调异常将首个安全错误码、异常类和阶段保存在 `primary_failure`，停止或末尾留证错误另列 `cleanup_failures`；任意异常正文不保存。监督者继续等待原控制器，终态时绑定实际 wait、日志与匹配清单。缺少可信绑定仍为 blocked，不把首错诊断当作根因或业务完成证明。

启动竞争后，只有本次子进程退出0、确认没有本轮镜像且原输出完整验证正常活动控制器门禁时，才记录 result=skipped；此时不能认领他人 run、进入包装或清窗。存在但损坏/不匹配的镜像仍 blocked，不能靠 skipped 输出覆盖坏证据。

## 固定 App 审核槽的原生派单

财务控制器直接发布并轮询 App 请求，不创建 Python 审核等待池；取材仍使用隔离准备进程，完成结果进入唯一 Writer。每轮先收齐当前可见结果并分类共享失败，再按 FIFO 启动新写入。包装沿同一发布和轮询入口，保留实际请求的 Schema。

历史冻结材料的只读容量诊断使用标准库执行器及现役进程登记、原父进程监视和回收入口。每个在途任务独占一个执行器，单项超时或崩溃只回收自己的工作进程和已登记模型，健康同伴继续；报告的 `processes_reaped` 必须来自真实回收检查，无法证实时保持失败并停止接纳新任务。诊断不领取、不完成 OA 单据，不清生产停止门。生产弹性与旧 batch/drain 执行已退场，旧公开入口继续返回稳定拒绝。

新财务 App 请求的审核预算为 1800 秒，控制器 watchdog 使用同一预算加 60 秒收尾余量；保留的 CLI 审核仍为 900 秒。注册 bootstrap 900 秒、父心跳 300 秒、准备 300 秒及领取窗口不变。预算只作用于新请求，不延长已经发布的 `expires_at`。尚未被宿主可信接收的请求到期沿单据隔离入口处理，截止时刻及以后首次交回的决定拒收；共享身份或来源失信仍停止整轮。

宿主在原截止前完成校验并保存绑定请求、分配、决定和原生来源的接收回执后，该决定可在同一有效轮次中等待全局 FIFO。首次轮询、结果读取或写前复验跨过原截止不撤销这次按时接收；只有草稿、文件存在或自报时间不能代替宿主接收。写前仍核验当前授权、冻结材料、账户与进程身份、轮次归属、父租约、停止门、Writer 互斥及未知写入状态。关闭、取消或身份失信后，已接收决定也不能继续写入。

App 等待异常保留主码 `host_review_exchange_failed`；写前完成异常保留主码 `host_review_result_untrusted`，附带 `operation=complete` 和 `failure_phase`。两者均返回安全的 `failure_detail_code`、异常类型及 `failure_recorded`。共享交换失败的首错保存在本轮私有 registry 的 `first_failure`，绑定请求路径和 SHA-256；后续关闭或停止错误不覆盖它。未发布或无法安全留证时明确 `failure_recorded=false`，不保存任意异常正文。历史无细码的轮次保持未知，不追溯补造。

本节只适用于 OpenAI `host_chat`。唯一滚动控制器启动一次后，主对话创建恰好 3 个原生 `collaboration.spawn_agent`，保留其真实 Agent 路径、子会话 UUID 和成功工具回执。每个 Agent 一次只审核一单，返回结果后空闲；下一单调用同一 Agent 的 `followup_task`，本轮不替换身份、不递归创建审核者。DeepSeek 不执行本节。

用户明确要求在当前聊天另建审批监督子 Agent 时，该监督者以自己的真实原生会话 UUID 拥有新轮次，并创建三个直属审核者。登记以可信监督者的原生祖先深度加一核验直属关系，同时核对精确 parent_thread_id、Agent 路径和实际 spawn 回执；不把整个任务树的绝对 depth=1 当成所有轮次的要求。根聊天监督的审核者仍须 depth=1；跳级、同级及损坏祖先拒绝。旧轮拥有者和固定审核槽不重新绑定。

1. 保留原 `execution.json`、`invocation_id` 和工具 `session_id`，按上方入口绑定原 session。用本轮父会话的 3 次真实 spawn 回执登记槽位。来源行必须指向原生工具调用及其已完成输出；来源文件和路径不能猜测。命令中 3 组子会话文件、spawn 行按槽位 0、1、2 排列。

```bash
uv run --quiet python -X utf8 -B -m apps.oa_finance_preaudit.host_chat_review --operation register --owner-task-id <主会话UUID> --source-session-file <父原生会话jsonl> --reviewer-session-file <槽0子会话jsonl> --spawn-line <槽0spawn行> --reviewer-session-file <槽1子会话jsonl> --spawn-line <槽1spawn行> --reviewer-session-file <槽2子会话jsonl> --spawn-line <槽2spawn行> --receipt-file <原execution.json> --invocation-id <原调用ID> --tool-session-id <原session_id>
```

2. 注册回执成功后，在下一次独立原生 `functions.exec` 中续首次父任务心跳，再读取 pending。注册与首次心跳不得合并在同一次原生调用中；其来源时间早于注册时，心跳拒绝且不会续租。控制器在可信首次心跳前保持自身心跳，不取材或发布请求；注册与首次心跳共用原清单开始时间的 900 秒宽限，轮询不重新计时。等待期间 pending 为空不代表队列已完成。随后每次取得可信进度后读取当前请求，并在 60 秒以内续一次父任务心跳。App 父租约独立为 300 秒，用于容忍原生派单和工具调度超过 45 秒；控制器自身的 45 秒进程心跳门保持原值。父租约到期阈值为 5 分钟，到期后的下一次既有检查拒绝派单和结果，控制器观察器再请求停止。原进程身份丢失及显式停止仍在原检查点生效；单据截止时间约束首次可信接收，已按时接收的决定继续遵守上述 FIFO 与写前门禁。心跳必须来自主会话本次已落盘的原生调用，父会话 UUID 和实际调用时间均需可信。心跳 CLI 默认从已登记父会话自动定位当前原生调用，续期与 pending 快照在同次调用完成；重复旧来源不延长租约。维持原工具 session 的等待；不得等待直到控制器退出才派单。心跳到期后，新增派单和结果接收在下一次检查时拒绝，不把等待超时当审批重试授权。

```bash
uv run --quiet python -X utf8 -B -m apps.oa_finance_preaudit.host_chat_review --operation heartbeat --include-pending --owner-task-id <主会话UUID> --run-id <本轮run_id>
uv run --quiet python -X utf8 -B -m apps.oa_finance_preaudit.host_chat_review --operation status --owner-task-id <主会话UUID> --run-id <本轮run_id>
```

`heartbeat --include-pending` 返回 requests、round、下一次续期期限 `renew_by`、实际到期时间 `expires_at`、有界等待建议 `max_wait_seconds` 和可原样调用的 `next_invocation`。等待最多使用建议秒数，返回后先执行下一次心跳，再处理派单和结果。建议为零时立即续期；旧来源不会获得新的期限。显式 source 文件与行号必须同时提供，只有心跳允许引用当前尚未返回的 exec。

父会话只监督取回、派单和提交，不在活动轮次内展开业务复核或长篇故障分析。需要较长分析时先按统一协议收尾。以上入口减少漏续步骤，不能保证模型或桌面在 300 秒内再次调度；没有新鲜真实父调用时继续停止，不通过后台进程、旧来源或延长租约维持运行。

3. 请求文件名绑定本轮 owner，保留旧轮同 attempt 的请求、决定和提交证据。pending 只发现本轮注册表登记且属于当前 progress 的请求；派单使用返回的绝对路径，不拼接固定文件名。对 pending 返回的本轮请求，调用空闲原 Agent 的 `followup_task`，只传请求绝对路径和当前单据边界。保留该次真实工具调用来源；它完成后执行 assign，把请求绑定到原槽位。一个槽位有未交回请求时不能再派单。dispatch 的原生时间、请求指纹、子会话和来源必须匹配。

pending 的 round 及独立 status 返回本轮原 3 槽、可用容量和失败请求。失败槽在新鲜原生空闲证明前保持封存；全部封存时控制器停止领取，保留未处理单据并等待统一收尾，不替换 Agent。

主线程使用原执行回执、pending 和原生工具进度监督审核，向用户汇报并持续父心跳。审核槽完成当前取证、必要验算和决定发布；仅在发生阻断或需要澄清时交回中间消息，避免为纯进度回复增加一次携带累计图片的模型请求。

派单来源必须是登记过的父会话文件，同一个原生 call_id 或来源行只能绑定一个请求；同请求、同槽、同来源的重复 assign 保持幂等。明文 message 仍核对请求路径。App 原生记录使用当前已识别的有界 opaque 格式时，只记录 dispatch_message_readable=false，不宣称正文或当前请求已送达；当前请求归属必须继续由已登记子会话真实 write-draft 调用及其专用回执中的相同 request SHA-256 确认。缺确认、不同请求、错槽、过期或已关闭均拒绝，不读取密钥或解密日志。

```bash
uv run --quiet python -X utf8 -B -m apps.oa_finance_preaudit.host_chat_review --operation assign --owner-task-id <主会话UUID> --run-id <本轮run_id> --request-file <本轮request.json> --slot <0或1或2> --source-session-file <父原生会话jsonl> --source-line <本次followup行>
```

真实 followup 完成后，可用下列入口定位已登记父会话中本请求、原槽位的调用。它按原生工具类型和配对输出校验，不把 exec 中提到 followup 的文本当作派单；未完成调用、失败输出或错轮次拒绝。返回 `source`、`source_line` 及 `next_invocation`（固定 cmd 与绝对 workdir），可直接交给 `tools.exec_command` 执行 assign；assign 仍复验请求、截止、槽位和幂等，定位本身不分配请求。

```bash
uv run --quiet python -X utf8 -B -m apps.oa_finance_preaudit.host_chat_review --operation source-anchor --anchor-operation followup_task --owner-task-id <主会话UUID> --run-id <本轮run_id> --request-file <本轮request.json> --slot <0或1或2>
```

财务请求过期时，控制器只在原请求、冻结输入和未写入证据可信后撤销该项授权并隔离；其他槽位按原 FIFO 继续，失败项不在本轮重领。失败槽位仍占用，主线程先中断该原 Agent，再取得失败发生后的原生 `list_agents` 空闲、完成或已中断状态，调用 `release-failed-slot` 验证并释放。只有释放成功才给该槽位下一单。来源、共享身份或未知写入不可信时仍停止整轮。

```bash
uv run --quiet python -X utf8 -B -m apps.oa_finance_preaudit.host_chat_review --operation release-failed-slot --owner-task-id <主会话UUID> --run-id <本轮run_id> --request-file <失败请求绝对路径> --source-session-file <父原生会话jsonl> --source-line <失败后list_agents行>
```

4. 子 Agent 读取当前请求提示词、规则和冻结材料，形成本单决定或包装草稿。材料使用请求提示中的完整读取命令；它绑定当前解释器的绝对路径，并显式使用 UTF-8，不依赖 PATH 中的裸 python。附图工具导航沿用同一解释器。

发布直接使用本次请求提示中的原生 `functions.exec` 模板，将 `JSON.stringify(本单原始JSON对象)` 的占位内容替换为完整 JSON 对象。JSON 只填写一次；由模板在本次调用中编码和 shell 引用后交给专用 write-draft。不再运行 Python heredoc 生成长命令，也不再转抄第二份证据引用。`draft_invocation` 是该模板的唯一生成器。

模板绑定本轮 owner、请求、尚不存在的私有决定文件、当前源码目录、controller lock 和 stop file。发布通过 uv 固定选择 Python 3.11，并显式使用 `-X utf8`；相对或其他 workdir 拒绝。整个原生调用仅含该单表达式，JSON 对象以外不添加变量、分支、注释或工具。校验器只静态解码受限模板，不执行 JavaScript。

既有规范字面量发布和历史 bare uv 来源继续只读接受，原请求、配对输出及决定字节指纹仍必须一致。新审核按请求模板发布。子 Agent 交回专用回执及决定路径后返回空闲；来源行由主会话在已登记子会话定位。

模型 JSON 与发布 JSON 逐字段核对，数组顺序及引用逐字一致。JSON number 的等值表示如 120.0 与 120 可以匹配，布尔值不等于数值，整数精度变化仍拒绝。实际发布文件必须保持专用接口的规范字节，并与原生配对回执的 SHA-256 完全一致。

等该次原工具输出实际结束、exit_code=0，且 stdout 的专用发布回执匹配当前 request_id、request_sha256、目标文件和决定字节 SHA-256，再用其来源行 submit。仅文件存在、调用中提到路径、另一个命令成功或自行打印的成功对象均不构成证据。正在执行的 submit 不能充当自己的已完成发布证据。子 Agent 只交回决定，不调用 OA 完成动作。主对话保持心跳并等待原控制器，Writer 仍按认领顺序管理。

原生日志可能在一次图片观察输出中内嵌多张大图。来源扫描按小块读取，保持原行号，跳过指定来源调用之前超过 32 MiB 的非目标记录；父心跳查找也跳过这些不可用作来源的记录。会话头、被引用调用及配对输出仍须在原 32 MiB 上限内，并按原字节计算指纹。调用之后、配对结果之前遇到超限记录时，无法确认它是否为目标输出，仍拒绝接收。原日志不改写；逐页业务观察要求不变。

```bash
uv run --quiet python -X utf8 -B -m apps.oa_finance_preaudit.host_chat_review --operation submit --owner-task-id <主会话UUID> --run-id <本轮run_id> --request-file <本轮request.json> --decision-file <本单私有决定json> --source-session-file <已登记子会话jsonl> --source-line <已完成决定写入行>
```

财务原工具 session 和原回执均确认 completed、children_reaped=true，且新鲜结束门禁可信、Writer 空闲后，执行 `host_chat_review --operation enter-packaging --owner-task-id <主会话UUID>`。该操作只推进同轮阶段，不重启控制器，不增加 Agent；包装请求继续使用上方 pending、followup、assign、submit。包装业务确认和完成动作见[包装审核](packaging-review.md)。包装主审请求还必须由 App 完成入口产生可信 app-completion.json，绑定语义确认、决定、闭合 action/state、执行回执和文件指纹；独立复核请求只是只读结果，不代替 OA 完成。仅主审 submitted 或 draft_ready 时 normal close 拒绝，不能直接把本轮报完成。包装及结束对账完成后，执行下面的统一收尾。

### 定向恢复与逐目标验收

在已获单据级授权的下一轮，监督入口的 `--` 后传入 `--targets-only`，并重复使用 `--recover-action-file <隔离动作绝对路径>` 或 `--resume-action-file <已打开动作绝对路径>`。恢复沿原校验创建一次新 attempt，续审复用原 attempt。必须提供非空目标；等价输入去重，方式冲突、旧 attempt、坏路径及冻结不可信拒绝。此模式不能与自动选择恢复或暂缓参数混用，未开启时保持原全队列行为。

选择固定到精确单据版本，范围内按操作时间 FIFO；新增待办和范围外历史状态不取材、不恢复、不提交。全量身份、停止、unknown、Writer 与进程回收门仍先检查，目标范围不授予写入资格。

控制器输出及监督回读的 `target_outcome` 为每个目标保留脱敏单据键、来源/实际 attempt、执行/业务/写入状态与证据分类。`complete` 要求全部目标可信提交并后查，或已办且有可信无需重提证据；`partial` 表示门禁可信但仍有未解决目标；安全阻断为 `blocked`。提交的 `needs_human` 可以完成，preview、模型结束或不可见不能当提交成功。进程退出码和原 `result` 含义不变，进程 completed 仍可能目标 partial；原历史回执不改写。真实生产逐目标验收仍需独立授权，见 [#350](https://github.com/CleverC2200/oa-workspace-minimal/issues/350)。

### 本轮精确暂缓未提交单据

用户允许先跳过拿不准的单据时，在下一轮滚动入口重复传入 `--defer-action-file /absolute/path/to/action.json`。参数只缩小本次选择范围，不改变原单状态，不重采材料；不提供参数的后续轮次会重新选择该单。运行中不修改本轮选择，已有控制器先按下节完成收尾。

暂缓对象必须是本轮流程范围内的最新 attempt，保持可信 `review_required / controlled_write / authorized`，身份、冻结和授权原状态校验通过，且不存在规范决定、意见、Writer 边界、执行回执或提交信封。域外路径、符号链接、重复路径、旧 attempt、与恢复选择重叠或任何校验失败均拒绝。私有草稿不等于已提交决定，不自动复制到新轮次。

全局对账和 Writer 门禁先按原规则通过，之后才从可行动数量中扣除当前待办的暂缓项，其余单据保持 FIFO。暂缓不能掩盖身份冲突、未知写入或损坏冻结，也不能代替隔离恢复。结果中的 `deferred_action_files` 和 `deferred_selected_count` 只说明本次选择；暂缓不计作提交或隔离。只剩暂缓项时本轮可结束，仍须明确报告这些待办没有完成，不把本轮结束称为全部待办已清空。

### App 登记前失败

原控制器已失败退出、三个原生审核者尚未登记时，使用 `finish-unregistered-shutdown`。保留原 execution、清单、日志和 progress；取得原 session 的真实 terminal wait、原三个 spawn、interrupt 和最后 list_agents 来源，再将这些来源交给该入口。它只接受没有 action、准备/审核执行及 Writer 活动的失败轮，核对原进程已退出、回收、身份和时间顺序，独占新增 `pre-registration-shutdown.json`；原部分回执保持原字节，业务结果继续为失败。

```bash
uv run --quiet python -X utf8 -B -m apps.oa_finance_preaudit.host_chat_review --operation finish-unregistered-shutdown --owner-task-id <原父会话UUID> --receipt-file <原execution.json> --invocation-id <原调用ID> --tool-session-id <原session_id> --source-session-file <原父会话jsonl> --reviewer-session-file <槽0子会话jsonl> --spawn-line <槽0spawn行> --reviewer-session-file <槽1子会话jsonl> --spawn-line <槽1spawn行> --reviewer-session-file <槽2子会话jsonl> --spawn-line <槽2spawn行> --interrupt-line <槽0interrupt行> --interrupt-line <槽1interrupt行> --interrupt-line <槽2interrupt行> --source-line <最终list_agents行> --wait-line <原session终态wait行>
```

新 App 轮次启动时复核原 execution 的收尾，包含未登记的失败轮；全局 ready、进程消失或待办为空不能代替该回执。来源缺失或存在取材、审核、写入时保持阻断，沿隔离排障入口处理。

#### 清单建立之前的失败

原执行没有清单、真实退出 1 且保留 `children_reaped=unknown` 时，不补造清单。仅原错误为 `production_rolling_reconciliation_invalid`、原生启动命令属于同一监督入口、原生完整终态与原 execution 一致，且旧权威指针和旧清单的修改及变更时间均早于该次启动时，使用 `apps.oa_finance_preaudit.pre_manifest_shutdown`。它还要求关键调度源码与原生产提交一致且在启动后未修改、旧轮终态及回收可信、原进程身份和进程组已退出、原三个审核槽中断和空闲来源完整。合法相对 Agent 名只按已验证的直属父子身份解析。缺少任一证据继续阻断。

从已审查、完整 CI 通过、合并并同步远端的干净候选 `main` 操作；生产仍保持唯一干净 `main`，可快进到该候选，原 cron 保持暂停。此维护可先于源码安装，只新增原 execution 旁的审计，不写 OA，不改停止门或原业务文件。

1. 用 `pre_manifest_shutdown.command` 传入原 `target`、`binding`、`reviewers`、父 `source`、三个 `interrupt_lines`、最终 `status_line`、真实终态 `wait_line` 和原启动 `launch_line`，取得 `cmd/workdir`。来源均取实际原生记录，不猜路径。
2. 将返回值用标准 JSON 编码器生成 `text(await tools.exec_command(<返回的 JSON 对象>));`，作为独立原生调用执行。只允许该表达式及 `cmd/workdir`。等待真实退出 0、`kind=preaudit_app_pre_manifest_observation` 和 `status=ready`；该步只读，尚未封存。
3. 用同一模块的 `--operation seal --target-root <生产绝对路径> --expected-revision <候选 SHA> --owner-task-id <原父 UUID> --source-session-file <原父 jsonl> --source-line <上一步 observe 原生工具行>` 确认。它重新核对原证据、观察来源和未变化的权威指针，在 Controller/global Writer 租约内独占创建 `pre-manifest-shutdown.json`。只有 `status=verified` 才完成该执行的可信收尾。

新审计明确记录 `controller_started=false`、`original_children_reaped=unknown` 和当前进程退出观察，不把原监督回执改成已回收。以后启动复验原生观察、退出和三槽来源；新的正常轮次可以更新权威指针，已封存观察不因此变成新事实。当前身份、授权、冻结、未知写入和完整实时对账仍独立核验。新监督执行额外留存启动前指针快照；旧执行没有该字段时仍须满足上述现存证据，不能补填快照。

### 历史已取材但未登记的失败轮

历史启动门命中未登记失败轮，且原轮已有准备动作时，先核对原执行、原生来源和后续单据归属。有当前明确维护授权后，使用 `python -m apps.oa_finance_preaudit.legacy_app_shutdown --help` 的单轮入口，提供原 execution、owner、invocation、tool session、真实父会话、三个原子会话及 spawn/interrupt、最终 list_agents 和原工具终态 wait 来源。默认预检返回 `status=ready`，不封存；确认调用增加 `--confirm-seal YES`，只有 `status=verified` 才取得历史封存。

该分支要求原监督回执已完整封存为失败，原生工具终态中的完整 execution 与原文件一致，配对终态输出时间不早于原退出及 wait 调用时间。wait 可以在退出前开始，配对输出须在退出后返回。实际进程组已回收、三槽已中断且空闲，原轮审核执行与 Writer 计数为零。原 action 可以存在或已由后续轮次处理，封存始终保留 `historical_action_outcomes=not_verified`，不恢复、重提或重判这些单据。当前未知写入仍由新轮完整对账阻断。

历史 owner 与真实父会话不一致时，分别提供原 owner 与 `--source-owner-task-id`；完整原工具结果必须绑定同一原 execution，原 owner 字节保持不变，封存明确记录身份差异。历史 wait 曾发送中断字符时，仅在原监督回执记录 SIGTERM、真实工具已终态且完整结果匹配时，接受该历史退出来源；普通 App 收尾仍只接受空 stdin 的轮询。原始证据、来源或回收不可信时继续阻断。

入口在 Controller/global Writer 租约内新增 `legacy-app-shutdown.json`，原执行及业务文件保持原字节。后续启动重新验证封存全部来源；失败轮继续是失败，当前身份、冻结、授权、停止门及实时对账仍按新轮检查。

### App 统一收尾

正常完成、人工取消、官方限制、子 Agent 异常和父任务主动退出均执行同一协议。主对话进入 final 前必须取得 `preaudit_app_shutdown` 的 `status=verified` 回执。父 final 不会可靠地中断仍运行的子 Agent。DeepSeek 保持原收尾路径。

1. 调用 `begin-shutdown`，reason 选择 completed、cancelled、reviewer_failed 或 parent_lost。它冻结本轮派单、续租及迟到结果，返回原 3 个 `interrupt_targets` 和原工具 session ID。非正常原因关闭 exchange；只有原执行终态与回收可信，且 Controller、全局及请求 Writer 均空闲时，才省略周期停止请求。不发信号终止 Writer。begin 与在途包装完成共用原轮锁，可能等待已进入的 Writer 完成后才返回；不承诺抢占写入。单独的 exchange cancelled/closed 不代表统一收尾已完成。
2. 对返回的每个原 Agent 调用一次原生 `collaboration.interrupt_agent`，即使它当前已经空闲，也保留实际配对回执。不得新增或替换 Agent。随后调用原生 `collaboration.list_agents`，确认本轮 3 槽全部为 idle、completed 或 interrupted；仍 running、缺失、工具失败或来源不可验证时保持 pending。
3. 继续等待原工具 session 和 Writer。原工具必须有实际终态 `write_stdin` 回执，调用单独使用下方形状；只轮询、不写 stdin。财务阶段已经取得的同一原 session 退出回执可复用。进程回执必须 terminal、children_reaped=true，退出码与原生等待一致，Writer 和请求 Writer 均空闲。非零退出只可封存为取消/异常收尾，不能报业务完成；unknown 写入及封存失败保持原阻断，不重放。
4. 调用 `finish-shutdown`，提供同一父原生来源文件、按槽位顺序的 3 个 interrupt 行、最终 list_agents 行以及原 write_stdin 行。它检查时间顺序和配对输出摘要，保留精简来源证明。正常完成额外执行财务及包装原闭合门禁。缺证据时保持 pending，可继续监督并补齐证据。若正常计划期间发生取消，重新 begin-shutdown 以已记录的取消原因降为异常计划，原冻结时点保持不变；不能升级回正常计划。此时不能把它当成功或重启原审批。verified 后才结束父回合。

```bash
uv run --quiet python -X utf8 -B -m apps.oa_finance_preaudit.host_chat_review --operation begin-shutdown --owner-task-id <主会话UUID> --reason <原因>
```

```javascript
text(await tools.write_stdin({"session_id":12345,"chars":"","yield_time_ms":1000}));
```

等待调用继续优先使用上方 JSON 引号键。收尾读取器也接受同一白名单属性的无引号 JavaScript 名称，值限 JSON 字符串或非负整数字面量；变量、计算属性、表达式、重复键及附加语句均拒绝。历史调用在唯一原 session 末尾 wait 前至多顺序执行两个固定字面量调用，也可兼容核验。前置调用可为 `text(await tools.exec_command({...}));`，参数限 cmd、workdir、max_output_tokens、yield_time_ms；或其他不同 session 的空 stdin `write_stdin`，且其真实结果必须已终态、退出 0。原 session 不得提前出现，前置轮询的 session 也不得重复。每个调用均须有按序配对的真实终态结果，数量必须一致，最终退出只取原 session 的末尾 wait。原 session 重复 wait、前置轮询非零或仍等待、尾随命令、动态参数、额外或缺失结果继续拒绝。兼容只解析原始调用，不执行 JavaScript，不改原生来源或配对输出；原会话、真实退出、回收及 Writer 校验保持。

```bash
uv run --quiet python -X utf8 -B -m apps.oa_finance_preaudit.host_chat_review --operation finish-shutdown --owner-task-id <主会话UUID> --source-session-file <父原生会话jsonl> --interrupt-line <槽0interrupt行> --interrupt-line <槽1interrupt行> --interrupt-line <槽2interrupt行> --source-line <最终list_agents行> --wait-line <原session真实终态wait行>
```

CLI 的旧 `close` 操作也走相同收尾核验，不能省略证明参数。Python 内部 `close()` 只负责 exchange 止损，供父链失效观察器等使用，不构成主回合退出许可。突然失去整个父会话时仍由原父链门和300秒租约止损；没有活着的 App 主对话就无法补发原生中断。机器断电、App崩溃或所有保护进程同时被强杀不保证完整收尾，结果缺失保持 unknown。

## 控制器清单与历史证据

每次 execution 目录中的 `controller-manifest.json` 是该轮唯一完整可变清单。全局 `rolling-controller-manifest.json` 只保存版本与 owner、invocation、run、controller instance 的定位绑定，读取时同时核对指针和执行清单的原字节、文件身份与路径。owner 是原生父会话，invocation 是监督执行目录，run 是控制器轮次，controller instance 是进程身份；四者分别核验。指针或目标损坏、丢失、冲突及符号链接均阻断，不能当作未运行。

旧完整全局清单保持只读兼容。显式新启动通过原门禁后，先保留原字节再激活新指针；显式旧轮恢复也先归档原件，只更新该轮执行权威。有原 execution 原件时须核身份与内容兼容，冲突拒绝。归档不改写原 execution receipt、退出码、日志或原工具 wait，历史取回仍按原 invocation 与原回执指纹读取。恢复后的当前清单不替原回执补造 terminal、children_reaped 或原生等待证据。

progress、execution receipt 和原生工具收尾各自保留原合同；指针不代替阶段心跳、实际进程回收、Writer 空闲或业务完成证明。

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

### 已封存轮次的完成数量

汇总同一主聊天的一个或多个明确轮次时，使用审批 CLI 的 `completed-receipts --owner-task-id <主会话UUID> --run-id <run_id> --run-id <另一run_id> --output-file <新私有JSON文件>`；需要读取独立开发夹具时传入 `--root <流程运行根目录>`。它只读取所选轮次，复验原生收尾、冻结材料与实际执行回执，并按 action 去重。标准输出保留计数和有界缺口，完整明细只保存在新建的 0600 文件中。

异常轮次也逐项核验已完成单据。原生收尾可信时，关闭及收尾开始前已派单、已接收的决定，仍须通过同一 action 的实际提交、后查和来源校验才计数。原轮次继续保留异常状态，未提交或迟到的决定不因汇总而恢复执行资格。

`completed` 仅表示已提交并后查的技术闭环，`business_acceptance=not_verified` 保留业务准确性的独立验收边界。草稿、preview、未闭合、缺失或损坏证据不会计作完成。此汇总不替代上方三槽中断和原 session wait，也不替代新鲜 OA 对账；有 invalid 或 excluded 时查看私有明细，不根据计数补造旧记录。
