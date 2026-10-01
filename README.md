# OA 财务预审与包装文字校对

当前版本：`1.1.0`

本项目以 Codex Skill 为审核入口：Codex 读取表单、附件和权威查询结果，形成六类财务流程的预审意见，以及包装版面的图片文字校对结果；Python 代码只负责稳定取数、材料冻结、确定性校验和受控提交，不生成业务判断。

V1.1 是首次 GitHub 私有发布基线。发布本身不会创建或启动定时任务。经独立授权的生产环境只保留一个外部 Codex 每日 cron：它绑定唯一 `main` 工作树，每次调度创建独立任务，在该任务中调用现行滚动控制器并于当轮结束后退出。

## 范围

- 差旅费报销
- 业务招待费
- 业务支持费
- 小额市场费
- TPM 小额市场费
- 通用费用报销
- 包装新制（更改）及包装版面审批

财务流程只给出预审意见，不代替批准或退回。包装流程只校对图片文字，并在存在 Excel 时逐字符对照，不扩展为法规合规审核。

六类财务流程的固定审核项、判定口径、证据边界和当前限制见[六类财务预审现行审核规则](docs/finance-preaudit-rules.md)。

## 设计边界

- Skill 维护流程规则、证据关系和输出格式。
- `apps/oa_finance_preaudit/` 维护单据材料、任务状态和冻结结果。
- `apps/oa_approval/` 复用 OA、附件、商旅、发票、只读 SQL 和最小提交能力。
- 手工调用默认只读 preview；写入必须有当前明确授权，并使用已冻结、可追溯的同一单据材料。
- 密钥、账号、内部地址、数据库和运行材料只保存在本地环境，不进入 Git。

## 目录

```text
.agents/skills/oa-finance-preaudit/  # Skill 入口、流程规则与证据契约
.agents/skills/oa-finance-preaudit/references/flows/  # 六类隔离规则包和包装规则包
apps/oa_finance_preaudit/           # 单一深模块、JSON CLI、状态与观测
apps/oa_approval/                   # OA 和外部数据的复用连接能力
tests/finance_preaudit/             # 聚焦合同测试与回放测试
docs/agents/                        # Issue、标签和领域文档约定
```

## 本地配置

```bash
mkdir -p ~/.config/oa-workspace-minimal
chmod 700 ~/.config/oa-workspace-minimal
cp -n .env.example ~/.config/oa-workspace-minimal/oa-auth.env
chmod 600 ~/.config/oa-workspace-minimal/oa-auth.env
```

只在工作树外的 `~/.config/oa-workspace-minimal/oa-auth.env` 填写真实 OA 配置；可用进程环境变量 `OA_AUTH_ENV_FILE` 指向另一个绝对位置。运行时优先使用进程环境和稳定配置文件，仓库内被忽略的 `.env`、`.env.oa-auth` 仅作兼容回退。所有配置文件必须是普通文件且禁止组或其他用户访问，否则安全失败。OA 执行人标识只允许通过本地 `OA_UID` 提供，流程 JSON 明确拒绝内嵌账号。数据库连接没有仓库内默认账号或地址；如需从 macOS Keychain 读取密码，需同时配置 `OA_DB_USER` 和 `OA_DB_KEYCHAIN_SERVICE`。完整的数据与凭据边界见[安全说明](SECURITY.md)。

可选的 OA 网页会话告警使用飞书机器人发送。目标会话只配置在上述工作树外的私有环境文件中，通过 `OA_FEISHU_ALERT_CHAT_ID` 指定，仓库不保存真实会话 ID；`OA_FEISHU_ALERT_PROFILE` 默认使用 `default`。机器人必须已加入目标会话并具有发送消息权限。

## 只读 preview

旧版 Word `.doc` 附件需要本机 LibreOffice（`soffice` 位于 PATH，或通过 `OA_LIBREOFFICE_BIN` 指定可执行文件）。采集器在独立临时配置中禁用宏、转换为 PDF 并逐页准备图片和确定性文本，保留原附件身份及哈希；单文件转换最多等待 60 秒，结束或超时时回收该转换进程组。转换器不可用、转换失败或超时分别保留 `legacy_doc_converter_unavailable`、`legacy_doc_conversion_failed`、`legacy_doc_conversion_timeout`。失败仍按现有材料降级路径处理，不把未读内容当作已审核；通用内容准备失败保留 `attachment_content_incomplete`。发布涉及此能力时须在生产执行环境核实转换器可用；仅安装 Python 依赖不能提供 LibreOffice。

六类财务流程和包装版面校对已经提供同一个本地深模块两动作接口。先取得指定流程的下一个动作：

```bash
python3 -m apps.oa_finance_preaudit.cli next-action \
  --workflow WORKFLOW_KEY
```

返回 `review_required` 时，只读取 `review_packet_file`，按其固定审核项生成结构化决定，再完成本轮：

```bash
python3 -m apps.oa_finance_preaudit.cli complete-review \
  --action-file /absolute/path/to/action.json \
  --decision-file /absolute/path/to/review-decision.json
```

两条命令默认都是 preview，动作固定 `write_authorized=false`，写入状态固定为 `not_requested`；不会调用 OA 提交。模块和 CLI 共用同一业务实现，大对象通过本地文件传递，stdout 只输出一个版本化 JSON。六类财务流程无法可信取得材料时生成不含可疑证据的降级审核包，由 Agent 输出 `⚠️` 意见交财务判断；流程身份、冻结 attempt、写入授权或目标不一致时才返回 `needs_admin`。包装图片不可读仍停止，避免生成貌似完成的校对意见。七类流程的目录和当前动作投影相互独立；不同单据的材料收集与 Agent 审核可以并发，每个 attempt 使用自己的完成锁，互不等待；所有 OA 写入入口共用一把全局锁并严格串行。

手工运行只使用 `next-action`、`complete-review`、`status`、`prune` 和 `clean` 五个命令；`clean --all-cache` 只清理当前配置的可重建附件缓存，并受控制器和 Writer 空闲门禁保护。六类财务的批量生产由唯一滚动控制器编排同一深模块：按 OA 操作时间选取，审核可并发，提交按认领顺序经过唯一 Writer；包装继续走单据级图片文字校对路径。候选发现、材料收集、冻结校验、单据锁、提交安全门和回执仍隐藏在模块内，没有角色 CLI、底层队列命令或跨任务消息协议。差旅手工受控入口默认拒绝；唯一例外是人工已核实未写入的 `writer_result_unknown` 恢复，它必须绑定精确 requestId，并在无活动控制器和 Writer 时仅创建新冻结 attempt，不直接提交 OA。

## 脱敏运行指标

每个 attempt 的 `observability.json` 只保存 attempt 哈希、流程、阶段、耗时、结果和 Token 状态。阶段拆为机械取数、Agent 审核、提交和回查；协调秒数与 Token 必须为 0。生产 Worker 在调用完成动作前写入绑定 `action_token` 的 `agent-timing.json`，FlowEngine 校验后把 Agent 墙钟与精确 usage 写入审核事件；手工 preview 或 usage 不完整时保持 unknown，不做估算。Worker 只把每次 Codex 进程的事件类型计数汇总为 `codex_events`，不保存 JSONL、提示词或回复正文；任一尝试缺少完整 `turn.completed` 时整体状态保持 unknown，并阻止弹性升档，已观察到的瞬时 `error` 仍如实累计。差旅审核包只在声明 SHA-256 与冻结页面实际字节一致时复用精确重复页；没有可信审核项关系时八项仍分别完整覆盖全部非重复页，不按文件名、页序、金额、日期或关键词裁剪。完成动作逐项校验非重复页引用，并将代表页覆盖投影回重复页及其资源后写入回执。`status` 按单据哈希、流程和 Asia/Shanghai 日期给出 P50/P95 与 Token 完整性。重复事件不会重复计数，冲突值会被拒绝；提交前的必要事件无法保存时停止当前单据，提交后的指标异常不会触发 OA 重放。

内部离线验收报告固定检查审核项/资源/页面/格式/golden replay 100%、重复提交/跨单据混用/unknown 自动重试/Agent 消息 0，以及 P95 小于 900 秒、等待比例小于 20%、技术错误率不高于 2%。单变量实验报告同时输出裁剪前后的 P50/P95 和 Token 总量；任一闭合样本缺少精确 usage 时 Token 状态保持 `unknown`，不输出估算总量。每流程不足 20 个闭合脱敏样本时不建议调参。真实 `python -m` 入口会把五命令的成功、参数、JSON 合同和文件传递结果写入本地 30 天脱敏 CLI 日志；进程尚未启动时的失败和人工复现由本地 launcher/harness 补充。日志不含 argv、stderr 或路径；status/prune/clean 每次只记一条 global 调用，按流程展示时投影影响范围，但 overall 不重复计数。未知值不能推断为 0。只有 CLI 故障严格多于其他技术故障且证据完整时，报告才允许提出独立 stdio MCP 后续 Issue；本项目当前不实现或安装 MCP。

OA 提交只通过冻结写入入口执行。授权证明、token、冻结指纹和终态回执均绑定单个 `requestId + received_at + attempt` 及其材料版本：同一 attempt 单飞并幂等重放；不同单据的取数和审核可以并发，滚动控制器按 `received_at + requestId` 认领，OA 写入再按认领序号经过全局单写锁。差旅费只接受滚动控制器的一次入口确认，准备动作生成一小时绑定 capability，完成动作再次验签；除下述人工确认 unknown 未写入恢复外，手工受控 CLI 对差旅明确拒绝。其他流程的单据级手工受控操作仍要求两步命令各自显式确认：

```bash
python3 -m apps.oa_finance_preaudit.cli next-action \
  --workflow NON_TRAVEL_WORKFLOW_KEY \
  --mode controlled-write \
  --confirm-write YES

python3 -m apps.oa_finance_preaudit.cli complete-review \
  --action-file /absolute/path/to/action.json \
  --decision-file /absolute/path/to/review-decision.json \
  --confirm-write YES
```

流程授权清单只保存在被 Git 忽略的 `runtime/control/write-policy.json`。授权源用本地 OA 应用密钥对单据版本、attempt 和材料指纹生成一小时有效的绑定证明；提交前 verifier 再验签、复核流程授权清单、冻结文件和当前 OA 待办版本。测试只使用模拟接口，不会写真实 OA。

## 单流程运行

每次手工 `next-action` 只处理参数指定的一类流程，规则包、状态目录和当前单据相互隔离，且固定为 preview。批量生产处理只允许从滚动控制器进入；差旅旧的受控手工 CLI 和旧批次/drain 入口明确拒绝。仓库合并不会创建或启用生产 cron；每类流程仍需单独完成真实环境验收和当前授权后，才能建立并启用自己的定时入口。

内部并发使用有界、可复用的 Python 进程池，保持同一流程一个控制入口。子进程只执行一个单据动作；普通异常、进程崩溃、单任务超时、批次超时和人工停止分别输出稳定错误码，不携带异常正文，也不自动重试。每 15 秒只采集可用内存、按 CPU 归一化负载、宿主线程数、活动 Worker 和最老任务年龄；单任务 960 秒仍未返回时终止该进程池和任务拥有的独立 Codex 进程组，下一轮只使用全新进程池。进程池容量、Codex 会话槽和商旅浏览器资源槽分别配置和测量；资源槽继续提供跨进程硬上限。

离线稳定性门固定覆盖容量 `1/2/4/8/10/12`、进程复用、失败隔离、任务超时回收、强制停止和重复单据单飞。弹性审核从 6 槽开始，连续两个完整健康窗口才按 `1 → 2 → 6 → 8 → 10 → 12` 提升一级；持续资源压力每次心跳降一级，最低 1 槽；CPU 负载高本身不再判为 critical，可用内存低于 12% 仍立即停止；健康采样未知时保留队列并暂停新工作，恢复后继续，资源等待不转人工；技术失败、容量完成数不一致或本机 critical 立即止损。材料领取与采集仍由单调度器串行，OA 提交仍由全局锁固定为 1，因此提升的是 AI 审核槽，不是浏览器或写入并发。离线通过不代表生产写入已启用。完整决策和递增/回退规则见 [ADR 0010](docs/adr/0010-bounded-process-pool.md)。

生产滚动控制器不属于五个手工 JSON CLI 命令，也不创建定时任务。它先对六类财务流程完成只读对账，再由单调度器按 OA 操作时间串行发现和冻结材料；包装不进入该滚动池。每个 Codex 工作进程只审核一单，宿主按认领顺序在完成边界再次验证单据 capability 后通过全局单写提交。使用时仍要求本地流程授权清单和当前人工确认：

```bash
python3 -m scripts.finance_preaudit_concurrency_probe \
  --capacity 4 \
  --confirm-write YES
```

容量诊断优先使用只读探针。它复核已有冻结证据，只要求 Agent 输出结构化决定，不领取单据、不访问实时待办、不完成动作、不提交 OA，也不会清除生产止损文件：

```bash
python3 -m scripts.finance_preaudit_concurrency_probe \
  --read-only \
  --capacity 12 \
  --samples 20
```

只读弹性验证从 6 槽开始，每档至少经过两个健康窗口：

```bash
python3 -m scripts.finance_preaudit_concurrency_probe \
  --read-only --adaptive \
  --capacity 6 \
  --samples 20 \
  --waves 6
```

首轮真实写入递增验证中，容量 `1/2/4/6` 均完成；旧版容量 `8` 出现一次 Codex 进程退出码为 0、但业务动作未完成的 `codex_process_incomplete`。结构化只读复验使用同一组 20 个冻结样本，容量 `8/10/12` 均为 20/20 有效决定，证明旧故障不是 8 槽资源上限，而是旧版让 Agent 同时承担判断和动作完成时的语义完成缺陷。当前只读审核验证上限为 `12`，尚未触达边界；这不代表材料采集或 OA 写入已验证到 12 并发，OA 写入仍固定串行。真实停止文件不会由测试或代码自动清除。

生产弹性滚动入口仍受当前授权、写策略、停止文件和显式确认约束。它只把 AI 审核槽从 6 逐级提升，单调度器和单写锁不变：

```bash
python3 -m scripts.finance_preaudit_concurrency_probe \
  --rolling --adaptive \
  --capacity 6 \
  --confirm-write YES
```

若上轮控制器已失败且终态清单仅因瞬时回收误报保留
`children_reaped=false`，不得自动改写或清除止损。操作人必须先用只读状态与系统进程核实：
停止原因为 `controller_cycle_stop`，原控制器 PID、全局 Writer、请求 Writer 和全部子进程均已退出，
实时身份、授权、冻结与对账门禁可信，且当前代码工作树干净；随后才能在同一次受控滚动入口增加
`--confirm-stale-children-reaped YES`。入口只修复这一份过期终态交接记录并继续正常门禁，
不会恢复 `needs_admin`、历史技术失败或任何 `write_state=unknown` 项；条件不完整时仍返回稳定阻断码。

只读对账的 `continuation` 将继续条件与 `failure_attention` 分开：有可信失败回执、无写入边界且未请求写入的 `codex_review_failed` 仅列为 `scope=item`，继续精确排除该单据版本；人工关注状态仍保留。`status=ready` 且 `isolated_pending == quarantine.pending_quarantined` 才可通过隔离门。当前未知或未闭合写入、身份/待办已办冲突以及未经验证的其他失败返回 `scope=controller` 和脱敏 `blocking_reasons`。普通周期停止恢复使用这一门禁，仍核验身份、锁和进程回收；不会重试隔离项或清除未知写入证据。统一失败回执也支持经过允许列表验证的材料失败、无效审核决定和经签名边界确认的提交拒绝。已打开且冻结证据完整、尚未进入 Writer 的审核动作可继续由 FlowEngine 重新核验授权；明确指定的未写入恢复项只在首轮门禁按精确身份匹配，其他阻塞项仍停止整轮。调度必须采用相同字段；仅发布代码不会改写工作树外的自动化提示词。

### 滚动恢复隔离项

已明确批准有限恢复的生产调度，可在唯一滚动入口增加 `--recover-confirmed-no-write-once YES`。宿主在租约内核对实时状态，只对当前授权流程、可信单据级确认未写入的首个 attempt 执行一次跨轮恢复；已有后续尝试进入人工处置，未知写入仍不重试。默认入口继续跳过历史隔离。处理人、当前 RPA 等待时长、期限、恢复上限及修复占位收尾见[阻断处置手册](docs/runbooks/blocking-recovery.md)。

生产调度使用 `status --check-live --reconcile --summary --output-file <新文件>`：一次查询完整保存 JSON，stdout 只展示原安全门、计数和带字节数/SHA-256 的文件回执，避免历史明细挤满工具输出。文件必须尚不存在，保存失败不返回成功门禁；明细显示截断时从同一文件分批读取，不重复查询 OA。具体命令见[阻断处置与有限恢复](docs/runbooks/blocking-recovery.md#安全门和当前单据)。不指定 `--output-file` 时保留原行为，`--summary` 的明细展示上限仍为 200 项。

开发隔离排障按[隔离排障与恢复](docs/agents/quarantine-repair.md)继续诊断、回测和显式恢复，不能以生产已隔离或普通队列为空作为修复完成。只读对账的 `findings` 与 `manual_review.items` 对通过现有未写入证据校验、精确仍在待办且未命中已办的项统一给出 `confirmed_no_write_recovery_candidate`，覆盖审核进程失败和无效审核决定；缺少有效证据时保留核对建议。候选不是写入授权，查询和普通调度均不会自动恢复。

六类财务历史 `material_collection_failed`、`codex_review_failed` 等已确认未写入的隔离项，只能把精确旧
`action.json` 交给同一个滚动控制器恢复；参数可重复。恢复仍在容量为 1、300 秒
硬超时且可回收的 prepare 进程中完成，只创建新 attempt，后续审核和提交继续经过
同一 FIFO 与唯一 Writer：

```bash
python3 -m scripts.finance_preaudit_concurrency_probe \
  --rolling --capacity 6 --confirm-write YES \
  --recover-action-file /absolute/path/to/first/action.json \
  --recover-action-file /absolute/path/to/second/action.json
```

提交结果按可恢复性拆分：`writer_submit_rejected`、`writer_preflight_failed` 和 `submission_target_changed` 是确认未写入；`writer_transport_unknown`、`writer_postcheck_timeout`、`writer_pending_conflict` 都保持 unknown 并触发止损。Writer 在 OA POST 前持久化带完整性证明的脱敏阶段边界；一旦进入 `submit_started` 而没有可信终态，同一 attempt 永不重放。已确认成功的边界可恢复本地回执，确认未写入也必须走下述新 attempt 恢复入口。

## 留存与清理

通用费用报销新采集的 PDF 准备页保持原有渲染分辨率；超过 256 KiB 的页面仅在质量 95 的 JPEG 更小时采用 JPEG，减少多页扫描图片累计导致的模型请求过大。其他流程和较小页面沿用 PNG，源 PDF、页面数量、资源归属与原始源文件指纹保留；新页面按实际字节重新计算指纹，不改写旧冻结材料。这是体积优化，不保证任意页数都能放入一次请求。原图内容模糊时仍按证据缺口处理。审核进程遇到 HTTP 413 时，脱敏诊断记录 `payload_too_large_signal`，包括 `turn.failed` 中的错误；现有失败码、隔离及写入门禁保持不变。

结构化日志、单据级脱敏指标、冻结材料和临时附件保留 30 天；不含单据哈希的日/流程聚合保留 180 天。每类流程只保留最新一轮可见任务，旧的已闭合任务先归档，严格超过 30 天后才删除。未闭合任务或写入结果 `unknown` 的恢复证据始终保留，但超过 30 天不进入原始分析窗口。清理以创建时间和最后事件时间中较晚者计算年龄，避免老任务刚闭合就被删除。

人工、Codex 和后续定时任务使用同一个只读状态入口；输出只有脱敏哈希、计数、年龄、三维状态和失败码：

```bash
python3 -m apps.oa_finance_preaudit.cli status \
  --root /absolute/path/to/finance_preaudit_flows
```

需要同时核验稳定凭据、OA API 认证、RPA 专用浏览器当前可见登录状态、服务端网页会话、执行身份绑定和实时待办映射时，在同一命令增加 `--check-live`。浏览器探针检查当前 OA 页面，并调用同源、只读、需要登录的菜单接口确认服务端会话；它不刷新页面、不调用 OA heartbeat，也不输出 Cookie、页面正文或地址，只返回脱敏健康状态。页面、CDP、网络或服务端结果无法确认时均硬停止。命令不领取、冻结或提交单据：

```bash
python3 -m apps.oa_finance_preaudit.cli status --check-live
```

状态入口本身保持只读，不发送消息。生产滚动入口会对本轮材料失败、审核失败、未知写入、授权失败、控制器失败及降级结果发出脱敏提醒；其他未识别的控制器失败使用 `controller_failed` 汇总，不发送原始异常正文。登录类告警在发送前复查当前会话，避免历史登录错误误报；其他故障不以登录正常作为恢复依据。同一连续故障窗口最多尝试提醒三次，单次最多等待 15 秒；发送结果不确定时不自动重试。配置或发送失败保留本地 `runtime/control/oa-session-alert.json` 回执，恢复清除去重窗口时仍保留上次发送状态和次数，供查询送达情况。财务控制器成功仅清登录窗口；整轮财务、包装及结束检查无错误后才清生产故障窗口；提醒不改变审批、停止门或未知写入保护。真实发送必须显式确认：

prepare（领取/准备）阶段的通用失败除稳定码 `production_probe_prepare_failed` 外，还会落一份有界、脱敏的诊断回执：已解析出 attempt 身份时写在该 attempt 目录下的 `prepare-failure-<digest>.json`，领取或发现阶段失败写在 `runtime/control/prepare-failure-<digest>.json`。回执只含固定 `stage`、稳定错误码、异常类名、最内层 traceback 函数名、停止动作（`cycle_stop`/`stop`）、`run_id`/控制器实例和可解析出的单据身份（`workflow_key`/`case_key`/`attempt`/`action_file`），不含异常正文；诊断写入失败不改变原有停止门与运行结果。

```bash
python3 -m apps.oa_finance_preaudit.session_alert \
  --error-code oa_session_expired \
  --confirm-send YES
```

材料失败动作必须保留 `failure_detail_code`。细码为六类会话错误时，已授权告警调用增加 `--failure-detail-code <会话细码>`，例如 `--error-code material_collection_failed --failure-detail-code oa_session_expired --confirm-send YES`；入口复核当前会话，正常时不发送旧会话故障提醒。其他材料故障继续使用通用码，不能用会话细码覆盖 Writer 故障。旧调用不自动获得细码，生产调用方需按更新后的[错误处理合同](.agents/skills/oa-finance-preaudit/references/error-handling.md)传递，发布不会更改定时配置。

包装实时待办为空时，入口只对已办命中、身份一致且无写入痕迹的历史材料失败停止重复返回；旧记录保持不变。不可见或已办读取失败、未知写入及未完成审核继续保留原保护，具体见[FlowEngine 合同](.agents/skills/oa-finance-preaudit/references/flow-engine-contract.md)。

人工重新登录且下一次 `--check-live` 成功后，外部定时任务使用 `--clear` 仅清除会话类告警窗口；仍生效的生产故障窗口保留，待无错误完成轮次再清除：

```bash
python3 -m apps.oa_finance_preaudit.session_alert --clear
```

飞书发送失败或结果不确定只影响告警回执，不得覆盖原始 OA 会话错误、放宽停止门禁或触发审批重试。入口不会自动登录、处理验证码，也不会恢复历史 `needs_admin` 或未知写入。

启动或恢复前可在同一只读命令增加 `--reconcile`。它将本地最新 attempt 与实时待办、已办对账，区分 `handled_not_pending`、`pending_not_handled`、双边冲突、身份冲突、已提交的旧轮次 `prior_round_submitted`、被新轮次取代且未进入 Writer 的旧轮次 `prior_round_unsubmitted` 和不可见，只生成脱敏建议，不领取或重提：

对账结果的 `manual_review.items` 提供脱敏人工核对清单，包含单据摘要键、流程、失败码、写入状态及回执验证状态。新失败在原 attempt 中保留 `manual-review-receipt.json`；历史缺失回执显示 `missing_or_invalid`，不补造证明。明确提交拒绝须经现有签名边界本地核实才可隔离并继续同轮其他单据；未知写入仍停止并保留人工核对，不自动重试。清单可供财务查看，不代表已发送通知或已完成业务审核。 每轮控制器的实时快照都校验 `continuation`，无停止标记也不能绕过未知写入、仍需人工核对的身份冲突或不可信隔离；已被严格更晚轮次取代且从未进入 Writer 的旧轮次只投影、不阻断。可信材料失败、无效审核结果和签名验证通过的提交拒绝与既有可信审核进程失败共同纳入单据隔离；只读对账保留人工关注信息。


```bash
python3 -m apps.oa_finance_preaudit.cli status --reconcile
```

普通人工恢复只允许 `material_collection_failed`、`writer_submit_rejected`、`writer_preflight_failed` 或 `submission_target_changed` 且 `write_state=not_requested`。普通 `--confirm-recovery` 仍拒绝所有 unknown。仅当人工已在 OA 核实未写入时，`writer_result_unknown + needs_admin + write_state=unknown` 才能使用独立确认：命令必须绑定精确 requestId，确认控制器和所有 Writer 均不活动，并重新核验同一身份唯一仍在待办、未出现在已办、旧 attempt 为最新且无执行回执。恢复保留旧现场，只创建新的受控冻结 attempt，不直接写 OA：

```bash
python3 -m apps.oa_finance_preaudit.cli next-action \
  --workflow travel_expense \
  --mode controlled-write \
  --confirm-write YES \
  --recover-action-file /absolute/path/to/action.json \
  --confirm-recovery YES \
  --confirm-unknown-no-write YES \
  --confirm-unknown-request-id EXACT_REQUEST_ID
```

已确认写入落地的历史未知写入不会再触发止损（见下文 `prior_round` 与历史未知写入规则），但上一轮失败留下的 `stop_requested:<reason>` 停止标记仍需人工处理。操作人在控制器与全局 Writer 空闲、实时身份与对账可信、且只读对账把绑定的 attempt 判为已办或不可见（非实时待办）后，可按精确原因清除该标记，入口会保留停止证据哈希、绑定 attempt 指纹与对账快照为回执：

```bash
python3 -m apps.oa_finance_preaudit.stop_recovery \
  --confirm-reconciled YES \
  --expected-reason writer_result_unknown \
  --bind-action-file /absolute/path/to/action.json
```

该入口只接受允许列表内的失败原因并逐字节核对标记内容，不删除业务证据、不重放 attempt、不写 OA；仍待办的未知写入、不可信实时身份或不可信对账一律返回 `writer_stop_recovery_blocked`。

受控清理使用同一 JSON CLI。它在同一维护锁和事件锁内先计算脱敏日聚合，完整校验计划与所有目标，再以不跟随符号链接的原子写入落盘，最后才归档或删除原始 attempt：

```bash
python3 -m apps.oa_finance_preaudit.cli prune \
  --root /absolute/path/to/finance_preaudit_flows
```

`prune` 要求运行根已存在专用 `.oa-retention-root` 标记，不会自动标记目录。未标记根、符号链接、绝对目标或越界目标会在任何变更前整体失败。仓库不会自动清理现有生产目录，也不连接真实任务系统；生产启用需要另行验收。

## 离线质量回放

离线基线包含七类流程的正常、业务异常、证据不足和技术失败场景，并为六类财务流程增加材料采集降级场景，共 34 个脱敏样本。回放比较固定审核项、状态/停止码、格式、耗时和精确 Token，不需要 OA 登录：

```bash
python3 -m pytest tests/finance_preaudit/test_flow_evals.py -v
```

离线样本不包含原始附件或业务身份，性能改善不得以准确性和格式回归为代价。回放实现属于开发质量模块，不是正常运行入口。

## 代码质量门

GitHub PR 的 `quality` 检查在无业务凭据环境运行 Skill 校验、文本格式、敏感文件/密钥扫描、Markdown 链接、PR 单 Issue 合同、Python 语法和完整离线测试。贡献者可在本地运行：

```bash
python3 -m scripts.quality skill
python3 -m scripts.quality format
python3 -m scripts.quality hygiene
python3 -m scripts.quality links
python3 -m pytest -v
```

仓库继续保持私有，不配置 GitHub 服务端分支保护。`main` 的普通变更仍按本地维护规则通过质量门和 PR 合并，但这不是平台强制；具体见[main 本地管理规则](docs/agents/branch-protection.md)。CI 不读取真实 OA 凭据，也不启用生产自动化。

## 验证

```bash
python3 -m scripts.quality skill
python3 -m pytest -v
git diff --check
```

贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。排错入口为 [Bug 快速索引](docs/bugs/README.md)，记录、复发与关闭规则见 [Bug 记录规范](docs/agents/bug-ledger.md)。V1.2 的流程拆分、并发提交、监控、留存与代码质量工程在 GitHub Issues 中分票推进；已合并能力只代表代码和测试完成，不代表生产已经启用。


生产启动门禁失败也使用 `apps.oa_finance_preaudit.session_alert` 的有限稳定错误码：代码基线、流程授权、运行环境、实时对账、控制器交接、运行记忆路径和本机 critical。正常已有控制器运行只跳过重复启动，不当作故障。提醒保持同一窗口最多三次，发送不确定保留本地回执且不自动重试。

`python3 -m apps.oa_finance_preaudit.session_alert --status` 只读查询脱敏发送结果；不访问飞书、不发送、不重置窗口。身份恢复仍只清登录故障。本轮财务与包装处理均成功、结束对账及所有生产门禁重新可信、无当前隔离或未决写入后，可显式调用 `--clear-production --confirm-recovered YES` 结束生产故障窗口，保留最近发送状态、次数和时间；这不会恢复任何单据。定时任务的启动前触发路径由独立授权的调度提示词使用这些入口，代码发布本身不修改自动化。


统一查询使用 `apps.oa_finance_preaudit.cli status --check-live --reconcile`：`workflows[].write_states` 显示本地提交状态，`reconciliation.manual_review` 列出人工核对项，`continuation` 与 `controller_handoff` 显示整轮阻断，`notification` 显示最近发送结果。`recoveries.items` 显示显式恢复的来源/目标 attempt、未写入依据、准备结果和证据校验状态；缺失或被改动的历史回执标为 `missing_or_invalid`，不补造证明。

恢复仍复用 `next-action --recover-action-file` 的明确确认、实时待办/已办与精确身份门，Writer 类未写入标签还必须有匹配冻结 job 的签名边界。新 attempt 的 `recovery-receipt.json` 在准备前记为 started，准备后记为 prepared 或 preparation_failed；恢复准备不代表已提交 OA。历史未知写入不自动重试，人工确认未知写入未发生的既有入口仍要求逐单明确确认。旧 attempt 保持不变，查询不会触发恢复、发送或写入。
