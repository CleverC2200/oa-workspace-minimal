# 小时生产调度手册

本手册与 `config/automations/oa-skill-prompt.txt` 是待发布的调度配置候选。启用前须按 [发布约定](../../CONTRIBUTING.md#开发与生产隔离及发布) 完成代码及手册发布、生产同步和只读核验，再通过原生 automation_update 更新现有自动化并回读。当前本地文件不表示生产已经加载。保留实际外层模型、推理级别、频率、状态、通知偏好、项目和目录；审核默认 model=gpt-6.1-sol reasoning_effort=medium model_provider=openai service_tier=default。使用现有原生 OpenAI 认证，普通服务档，不开加速；不调用 codex login、不复制认证、不改全局配置。历史 DeepSeek 回执保留原供应商身份，不改写为当前配置。

每轮必读“每轮启动与实时对账”；结束必读“结束对账与超期”和“清窗与留存”。其他章节仅在对应条件出现时读取，且原安全边界始终适用。

## 每轮启动与实时对账

首先读取项目 AGENTS.md、现行财务 Skill 以及 docs/runbooks/blocking-recovery.md。生产不得开发、创建工作树、切换分支、修改代码、提交、推送或自行拉取版本。核对唯一 main、工作区干净、HEAD=origin/main、依赖及已发布入口、本地授权清单包含本次流程；缺口停止并保留原稳定码。

自动化运行记忆只使用 /Users/synear/.codex/automations/oa-skill/memory.md，确认在工作树之外且可写。不要把界面显示的 $CODEX_HOME 路径按字面当作相对路径。不可写使用 automation_memory_path_invalid 停止；不回退到工作树。

每次启动前、结束后或复查的实时对账，均按 docs/runbooks/blocking-recovery.md 的文件输出方式执行一次。先新建私有临时目录，再在同一 exec_command 运行：
status_dir=$(mktemp -d /tmp/oa-status.XXXXXX) &&
.venv/bin/python -m apps.oa_finance_preaudit.cli status --check-live --reconcile --summary --output-file "$status_dir/status.json"
完整保留原 session_id、exit_code 和输出；运行中使用原 ID 的 write_stdin 等待，不把工具等待窗口当业务超时。仅在 exit_code=0 后解析终态：stdout 保留原安全门和计数，完整 JSON 已保存到 status_file.path，并返回字节数和 SHA-256。明细的 display_truncated=true 只表示未在界面展开，不代表待办或人工核对项为零；按需校验同一文件的字节数/哈希，再分字段、小批量读取全部必要明细，不将整份文件重新打印。stdout 若仍被宿主截断，从本次已知且确认成功的文件提取门禁，不为补读重复查询 OA。原会话、文件或校验结果不可信仍停止，不用部分数据放行。所有身份、授权、continuation、锁、Writer 和交接安全判断保持原要求。

启动前发现正常活动控制器：只有 running 清单、当前代码一致、锁所有者一致、restart 已验证且 monitoring_action=wait_for_current_run 时，跳过本轮，不发故障提醒、不进入包装、不清故障窗口。已经由本任务启动的控制器仍归本任务收尾，后续 active/skipped 不能当成他人的运行。

继续须凭据、OA 接口与专用浏览器会话正常、执行身份 verified、授权与冻结可信、所有控制器和 Writer 空闲、上一轮终态及子进程回收可信。continuation 须 status=ready、blocking_reasons={}、isolated_pending为非负整数且等于pending_quarantined、scope与计数一致。code_matches_current=false 若只表示已终态旧版本清单，且当前 Git干净同步、ready_for_start=true、children_reaped=true、restart_verified=true，其余门禁可信，可由 fresh process加载新版；不复用旧进程。不因笼统 degraded 或可信隔离数量停止其他待办。

生产审核初始 3 槽、最高 6 槽，六类财务共享该池，材料准备和全局 Writer 均为 1。门禁通过后只调用一次：

```bash
.venv/bin/python -m scripts.finance_preaudit_run --owner-task-id <当前任务真实UUID> -- --rolling --adaptive --capacity 3 --max-capacity 6 --confirm-write YES --recover-confirmed-no-write-once YES --workflow travel_expense --workflow business_entertainment --workflow business_support_fee --workflow small_marketing_fee --workflow tpm_small_marketing_fee --workflow general_expense
```

## 停止隔离与有限恢复

停止标记只按项目现行受控恢复入口处理。普通 controller_cycle_stop 在确认可靠回收后由控制器收尾；残余、人工、环境或带具体失败原因的停止门不得手工删除、改文件或用清告警代替恢复。身份、待办已办冲突、损坏冻结/边界、当前未知写入、活动 Writer或未回收进程继续硬停止。

控制器 failed、当前未知写入、身份/授权/冻结/隔离不可信、有同轮证据确认的进程故障、本机critical或停止门触发时，立即停止新增领取和写入，等待原调用收尾，保存首个错误和已提交证据。已有 notification 不重复发送。业务 fail/uncertain 为正常意见；completed_with_quarantine 可继续其他可信单据，但须重新完整对账。

有限恢复保持原控制器在租约内按授权与可信未写入选择首个 attempt 一次；已存在后续 attempt 不自动再恢复，不使用 unknown 确认参数，未知写入不重试。指定恢复与普通新增分别核算。具体选择及失败类别见[阻断处置](blocking-recovery.md#一次跨轮恢复)，不自行挑旧路径、删隔离或重写冻结状态。

## 包装待审核

财务终态及结束门禁可信后，才处理包装：.venv/bin/python -m apps.oa_finance_preaudit.cli next-action --workflow packaging_layout_approval --mode controlled-write --confirm-write YES。closed/not_available 即包装无工作，不看图、不生成决定、不调用完成入口。review_required 按当前冻结包逐页直接观察原图，不用OCR；承载单位/地址/许可证等固定项的外箱也需审核，不根据文件名、扩展名或字段自动分类，不跨版本拼证据。按 Skill 的必要条件独立复核；原生 OpenAI 认证或配置失信时停止当前包装，不改用其他供应商。调用 `.venv/bin/python -m apps.oa_finance_preaudit.packaging_review --action-file <当前动作>`，等待其原 session 实际终态，按 [包装只读审核](../agents/packaging-review.md) 核对草稿、原始工具回执、逐页覆盖及必要复核。draft_ready 不表示已看图或已提交；缺真实工具证据或语义未确认时停在人工处置，禁止靠模型自述放行。宿主已仅绑定冻结协议，业务字段不改；核对后才使用原 complete-review --action-file ... --decision-file <回执的决定文件> --confirm-write YES。装配不改变业务结论，无效决定不写OA；包装缺失/不可读材料停止当前包装。不重提已提交意见。

## 结束对账与超期

结束按前述文件输出方式重新取得完整实时对账，分别统计有实际提交/后查回执的完成、恢复、人工交接、隔离、未领取、当前未决写入与不支持流程。pending_dispositions 按当前 RPA received_at计龄；已有后续失败转 manual_takeover，默认责任目标绑定现有告警接收人。未经处理人实际确认，不调用 case_recovery --confirm-takeover YES、不假称已接管。超出 escalation_due_at 或 resolution_due_at、4小时仍无人接管、同类整轮故障连续2轮，报告处理目标、具体下一步与期限；存在真实超期时调用已发布 session_alert --error-code preaudit_pending_overdue --confirm-send YES 一次。提醒不阻断其他可信处理、不恢复单据。owner unconfigured 或绑定配置变化必须明确报告，不能编造负责人或改配新接收人。

## 故障通知

实际故障告警用项目现行有限稳定码：基线 production_baseline_invalid；授权 production_authorization_invalid；环境 production_runtime_unavailable；身份/冻结/对账 production_reconciliation_blocked；交接/回收 production_handoff_blocked；记忆路径 automation_memory_path_invalid；主机critical production_health_critical；已识别审核、材料、Writer、登录错误保持原码，无法映射的实际程序阻断用 controller_failed。review_decision_invalid 映射已支持 codex_decision_invalid，保留本地原细码。材料 reason与failure_detail_code都保留；细码为六类会话码时通过 --failure-detail-code 传递并由入口重查，返回 not_required 不再用通用码补发。同一分支只调用一次，unknown/started不代表送达，不自动重试、重发、安装或修复消息工具。只读状态本身不发消息，状态查询不能清故障。使用 session_alert --status核对，sent仅表示服务端发送确认。

## 清窗与留存

只有财务、包装、结束门禁全部可信，无当前隔离、未决写入或超期处置时，才用现行 --clear-production --confirm-recovered YES结束生产故障窗口。清除只改变窗口，不改变单据或回执。prune 仅在原控制器终态和全部Writer空闲后执行；retention_plan_invalid保留证据，按controller_failed映射提醒，不手工删除运行记录。

## 程序故障派单

程序故障开发派发保持一窗口一次、一轮修复约束，优先采用已发布 repair_dispatch入口，不能因通知unknown重复派发：
1. status查询活动归属；reserved先确认之前创建结果，running用Codex任务工具检查绑定任务，2小时无进展升级，不能只看目录存在就永久等待。
2. reserve --window-key <告警window_id；缺失时原error_code+first_seen稳定摘要> --source-thread-id <本任务ID>。已有当前或旧格式窗口记录不创建第二个任务；已有活动占位报告其任务和进度。无归属占位需人工核实，调度不能 quarantine-orphan、删除或盲目重建。
3. 只有 reserve确认为reserved且尚未创建，才 list_projects核对保存的 oa-workspace-dev 路径 /Users/synear/HermesWorkspace/profiles/oa/projects/oa-workspace-dev，并通过 create_thread一次创建local开发修复任务；不在生产建工作树、不用shell启动隐藏任务。创建确认真实threadId后bind；仅clientThreadId或结果不明保持待确认，不重试。真实绑定任务已被任务工具确认终态后，按相同reservation_id/thread_id finish --result completed|failed|waiting_for_user --confirm-terminal YES受控归档活动占位，窗口历史保留。
4. 已确认未写入的隔离恢复失败属于人工接管处置，不另派重复开发任务；明确程序缺陷才派发。通知/创建工具缺失或失败均保留具体原因并报告人工处置，不绕过门禁。

新开发修复任务提示词只含脱敏故障码、窗口、来源任务和回执位置，并包含：用户授权本故障一轮诊断、必要修改、聚焦回归、必要完整测试、Standards/Spec双路审查及一次问题修正；读AGENTS、CONTRIBUTING和隔离恢复手册，保留无关脏工作；只在保存的开发克隆内隔离开发，沿用uv、RTK。重大业务规则/授权变化、未知写入判定、放宽门禁、删证据、无法确定原因或需要新凭据时停止并给出具体人工下一步，不自动派生另一修复任务。符合小范围验证与审查时可本地提交、推送origin、Draft PR、CI通过转Ready并合并、确认Issue关闭；生产持控制器/Writer租约、空闲干净main后ff同步，新进程只读核验。发布不启动审批验收或恢复历史，正常授权审批由唯一cron负责。每阶段用自己的reservation_id/thread_id progress记录进度；派发任务无权限发起第二个修复任务、改频率或启停自动化。

故障归属按本轮实际 run_id/controller_instance_id/来源任务关联，告警窗口只是通知历史，旧日期不得代替本轮。换窗口键前查询当前及历史派单记录的同轮归属；已有任务或归属不明不创建第二个任务。收到技术退出码前不按固定静默时长或浏览器关闭警告认定卡死。
