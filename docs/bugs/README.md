# Bug 快速索引

本页只导航：按关键词找 Issue 和回归入口，不维护第二份状态表。当前状态、根因、每次发生及验收证据以 GitHub Issue 为准；同一现象不自动认定同一根因。记录规范见 [Bug 记录、复发与验收](../agents/bug-ledger.md)。

## 快速遍历

- [待处理 Bug](https://github.com/CleverC2200/oa-workspace-minimal/issues?q=is%3Aissue+is%3Aopen+label%3Abug+-label%3Anon-actionable)
- [证据不足的 Bug](https://github.com/CleverC2200/oa-workspace-minimal/issues?q=is%3Aissue+is%3Aopen+label%3Abug+label%3Aneeds-info)
- [已关闭 Bug](https://github.com/CleverC2200/oa-workspace-minimal/issues?q=is%3Aissue+is%3Aclosed+label%3Abug+-label%3Anon-actionable)（关闭结论仍以票内验收范围为准）
- [已确认无需行动](https://github.com/CleverC2200/oa-workspace-minimal/issues?q=is%3Aissue+label%3Anon-actionable)（原因与证据必须保留）
- 其他有价值事项：[需求/改进](https://github.com/CleverC2200/oa-workspace-minimal/issues?q=is%3Aissue+label%3Aenhancement)、[文档](https://github.com/CleverC2200/oa-workspace-minimal/issues?q=is%3Aissue+label%3Adocumentation)、[咨询](https://github.com/CleverC2200/oa-workspace-minimal/issues?q=is%3Aissue+label%3Aquestion)

没有 `bug` 标签不等于无价值；`non-actionable` 的具体条件见 [标签规范](../agents/triage-labels.md#已确认无需行动)。

## 故障关键词

| 领域 | 症状 / 搜索词 | 主记录或历史组件 | 回归 / 经验入口 |
|---|---|---|---|
| 停止与发布 | 定时任务生成的无原因普通停止、待办/已办暂时冲突、模板模型或并发漂移、同步门禁 | [#393](https://github.com/CleverC2200/oa-workspace-minimal/issues/393)；工程子票 [#394](https://github.com/CleverC2200/oa-workspace-minimal/issues/394)、[#395](https://github.com/CleverC2200/oa-workspace-minimal/issues/395)、[#396](https://github.com/CleverC2200/oa-workspace-minimal/issues/396) | [阻断处置](../runbooks/blocking-recovery.md)、[发布约定](../../CONTRIBUTING.md#开发与生产隔离及发布)；采样时差是待确认解释，受控恢复与根因修复分开验收 |
| 主聊天冷却归属 | run_id=null、源码维护、人工单轮豁免、maintenance_original_execution_untrusted | [#461](https://github.com/CleverC2200/oa-workspace-minimal/issues/461) | [单轮豁免](../runbooks/cooldown-override.md)、[源码维护回归](../../tests/finance_preaudit/test_official_stop_maintenance.py)；主聊天事件与上轮审批闭合分别留证 |
| 包装补充校对 | 四项之外异常遗漏、储存/警示文字、整体结论、Excel 尾行 | [#383](https://github.com/CleverC2200/oa-workspace-minimal/issues/383) | [决定/消费回归](../../tests/finance_preaudit/test_packaging_supplementary_checks.py)、[补充合同](../../.agents/skills/oa-finance-preaudit/references/flow-engine-contract.md)；工程检查与生产语义验收分开 |
| 调度与交接 | 终态丢失、孤立 running、Unknown process id、提前判卡死、旧窗口派单 | [#325 重复交接症状记录](https://github.com/CleverC2200/oa-workspace-minimal/issues/325)；留证工程 [#326](https://github.com/CleverC2200/oa-workspace-minimal/issues/326)；相关组件 [#150](https://github.com/CleverC2200/oa-workspace-minimal/issues/150)、[#202](https://github.com/CleverC2200/oa-workspace-minimal/issues/202) | [执行与原会话回归](../../tests/finance_preaudit/test_controller_execution.py)、[交接回归](../../tests/finance_preaudit/test_controller_handoff.py)、[隔离排障与恢复](../agents/quarantine-repair.md)；组件实现不代表所有退出原因已解决 |
| App 轮次交接 | host_review_exchange_failed、旧请求文件冲突、首次心跳 stale_source、未授权自动告警 | [#472](https://github.com/CleverC2200/oa-workspace-minimal/issues/472) | [跨轮与首次心跳回归](../../tests/finance_preaudit/test_host_chat_review.py)、[告警授权回归](../../tests/finance_preaudit/test_concurrency_probe.py)、[原生执行协议](../agents/controller-execution.md)；本地验证与生产验收分开 |
| App 退出来源解析 | 真实 wait 已退出、无引号 JS 属性名、JSONDecodeError、shutdown pending | [#491](https://github.com/CleverC2200/oa-workspace-minimal/issues/491) | [公开收尾回归](../../tests/finance_preaudit/test_app_review_shutdown.py)、[统一收尾](../agents/controller-execution.md#app-统一收尾)；只解析固定字面量，真实原生来源和退出码保留 |
| 子监督者登记 | host_review_source_untrusted、直属 Agent、绝对 depth=2、同聊天新轮次 | [#482](https://github.com/CleverC2200/oa-workspace-minimal/issues/482) | [固定槽原生祖先回归](../../tests/finance_preaudit/test_app_reviewer_slots.py)、[原生执行协议](../agents/controller-execution.md#固定-app-审核槽的原生派单)；以拥有者祖先深度核验直属关系，旧轮不重绑 |
| App 审核期限与首错 | 900 秒、多页 Luna/max、host_review_exchange_failed、具体异常丢失 | [#486](https://github.com/CleverC2200/oa-workspace-minimal/issues/486) | [新请求预算与首错回归](../../tests/finance_preaudit/test_app_review_budget.py)、[原生执行协议](../agents/controller-execution.md#固定-app-审核槽的原生派单)；原期限不改写，历史缺失细码保持未知 |
| 观测与输出 | 状态 JSON 截断、完整文件、摘要门禁 | [#311](https://github.com/CleverC2200/oa-workspace-minimal/issues/311) | [完整状态输出回归](../../tests/finance_preaudit/test_status_output_file.py) |
| App 发布并发 | submission 已出现、registry 未提交、host_review_submission_untrusted、包装 evidence_directory 缺失 | [#475](https://github.com/CleverC2200/oa-workspace-minimal/issues/475) | [发布事务回归](../../tests/finance_preaudit/test_host_chat_review.py)、[包装证据回归](../../tests/finance_preaudit/test_host_packaging_review.py)；CI 表现与生产同因证据分开 |
| 已提交后撤回 | 原成功单重新待办、receiveTime 未变化、pending_write_state_unresolved、撤回恢复 | [#477](https://github.com/CleverC2200/oa-workspace-minimal/issues/477) | [撤回恢复回归](../../tests/finance_preaudit/test_withdrawn_submission.py)、[受控源码维护](../runbooks/production-source-release.md#已确认撤回后的源码维护)；旧成功记录保留，新 attempt 重新取材 |
| 商旅准备 | browser.close、Playwright 生命周期、真实准备 watchdog | [#313](https://github.com/CleverC2200/oa-workspace-minimal/issues/313)；诊断能力 [#304](https://github.com/CleverC2200/oa-workspace-minimal/issues/304) | [浏览器生命周期回归](../../tests/finance_preaudit/test_travel_browser_lifecycle.py)、[阶段采样回归](../../tests/finance_preaudit/test_prepare_trace.py)、[采集契约](../../tests/finance_preaudit/test_collect_case_contract.py)；关闭警告单独不能证明卡死 |
| 包装材料 | PPTX 不支持、隐藏页、页数不足、视觉候选漏页、PDF 页归属 | [#319](https://github.com/CleverC2200/oa-workspace-minimal/issues/319)、[#292](https://github.com/CleverC2200/oa-workspace-minimal/issues/292) | [PPTX 准备回归](../../tests/finance_preaudit/test_pptx_preparation.py)、[全链采集/页覆盖](../../tests/finance_preaudit/test_collect_case_contract.py) |
| 审核失败记账 | NameError、cli 未导入、record_production_failure_round | [#329](https://github.com/CleverC2200/oa-workspace-minimal/issues/329) | [真实入口参数/返回回归](../../tests/finance_preaudit/test_codex_review_worker.py)；不与历史异常退出原因自动归并 |
| 授权续期与失败回执 | action_token 轮换、旧 review-failure、codex_review_failure_evidence_invalid | [#438](https://github.com/CleverC2200/oa-workspace-minimal/issues/438) | [续期至 worker.review 的离线回归](../../tests/finance_preaudit/test_production_write_host.py)；仅归档与旧授权绑定一致的失败，不删除或改写历史证据 |
| 原生错误留证缺口 | unknown_error、未识别错误码、HTTP 状态、脱敏技术正文、旧首错读取 | [#440](https://github.com/CleverC2200/oa-workspace-minimal/issues/440)；原分级工程 [#435](https://github.com/CleverC2200/oa-workspace-minimal/issues/435) | [worker.review 公开入口回归](../../tests/finance_preaudit/test_review_fail_closed.py)、[留证合同](../runbooks/hourly-production.md)；分类未知不等于没有详情，历史未留存正文不可补造 |
| 原生传输切换误阻断 | WebSockets、HTTPS、fallback、Broken pipe、unknown_error | [#442](https://github.com/CleverC2200/oa-workspace-minimal/issues/442) | [worker.review 公开入口回归](../../tests/finance_preaudit/test_review_fail_closed.py)、[异常分级合同](../runbooks/hourly-production.md)；分类修复不证明底层连接故障已消除 |
| 审核协议 | 空属性对象 Schema、唯一 JSON 围栏、多候选、包装草稿 | [#327](https://github.com/CleverC2200/oa-workspace-minimal/issues/327)（正式入口改进） | [包装入口/消费回归](../../tests/finance_preaudit/test_packaging_review.py)、[只读审核边界](../agents/packaging-review.md)；草稿与真实看图验收分开 |
| 关联发票材料 | 铁路电子客票、已关联却报未见发票、发票云字段丢失、文件记录与正文覆盖 | [#415](https://github.com/CleverC2200/oa-workspace-minimal/issues/415)；采集工程 [#416](https://github.com/CleverC2200/oa-workspace-minimal/issues/416)、诊断工程 [#417](https://github.com/CleverC2200/oa-workspace-minimal/issues/417) | [采集公开入口回归](../../tests/finance_preaudit/test_collect_case_contract.py)、[只读诊断 CLI 回归](../../tests/finance_preaudit/test_invoice_material_diagnostics.py)、[覆盖诊断](../agents/offline-review-diagnosis.md#关联发票材料覆盖)；工程材料覆盖与真实审核质量分开验收 |
| 发票对应关系 | invoice_buyer_read_failed、读取成功但票号误报、ID 与显示号码不同序、位置配对 | [#484](https://github.com/CleverC2200/oa-workspace-minimal/issues/484) | [记录身份与字段组回归](../../tests/finance_preaudit/test_invoice_reference_binding.py)；按明确引用和返回记录身份核验同字段号码及次数，不按位置猜测关联 |
| 报销公司取数 | 原表单有公司、bxgs 原值为全文、browser.19001、冻结却 unresolved | [#493](https://github.com/CleverC2200/oa-workspace-minimal/issues/493) | [公开采集字段回归](../../tests/finance_preaudit/test_collect_case_contract.py)；只读取已确认字段的原始业务文本，不用开票公司或公司代码补猜 |
| 读取失败提交空意见 | invoice_buyer_read_failed、空骨架、degraded、全部未完成核对 | [#275](https://github.com/CleverC2200/oa-workspace-minimal/issues/275) | [新采集/旧冻结/完成回归](../../tests/finance_preaudit/test_flow_engine_material_gaps.py)、[当前提交门](../../.agents/skills/oa-finance-preaudit/references/flow-engine-contract.md)；历史已提交意见保留，不重放 |
| 一般费用未解析证据 | 有表单金额、附件及票据引用全部 unresolved、无可读证据却进入审核；更新后冻结门阻断 | [#488](https://github.com/CleverC2200/oa-workspace-minimal/issues/488) | [采集、隔离及后续队列回归](../../tests/finance_preaudit/test_flow_engine_material_gaps.py)、[源码发布门](../runbooks/production-source-release.md)；保留部分可读证据的业务不确定意见，原始资料是否齐备须另行核实 |
| 业务规则 | 里程、住宿、发票、支付、包装文字误报或漏报 | 从 [人工确认反馈](../../.agents/skills/oa-finance-preaudit/references/confirmed-feedback.md) 的编号找规则和原记录 | [脱敏业务回归](../../.agents/skills/oa-finance-preaudit/references/regression-cases.json)；不作为生产审核答案提示 |

## 其他相关工作

- [#413 已闭合提交未决的停止恢复](https://github.com/CleverC2200/oa-workspace-minimal/issues/413)：[版本 2 恢复证据合同](../runbooks/blocking-recovery.md#已闭合提交未决与首次维护身份观测)、[公开维护回归](../../tests/finance_preaudit/test_manual_stop_recovery.py)；严格区分原身份记录与首次维护观测的出生证据，不把恢复当作对账根因修复。

- [#328 机械质量检查](https://github.com/CleverC2200/oa-workspace-minimal/issues/328)：[YAML、生成 Schema 和 lint](../agents/quality-checks.md)。
- [#330 历史原会话定位](https://github.com/CleverC2200/oa-workspace-minimal/issues/330)：[UUID、压缩归档与有限工具投影](../agents/session-evidence.md)。
- [#331 小时调度指令分层](https://github.com/CleverC2200/oa-workspace-minimal/issues/331)：[配置候选及启用条件](../runbooks/hourly-production.md)；按授权发布后才接入。

- [#190 生产恢复与告警端到端验收](https://github.com/CleverC2200/oa-workspace-minimal/issues/190)：验收工作，证据范围看原票。
- [#321 审核质量门与执行回执](https://github.com/CleverC2200/oa-workspace-minimal/issues/321)：质量改造及验证；局部实验不等于默认生产已启用。
- [#324 本次清单、标签与索引建设](https://github.com/CleverC2200/oa-workspace-minimal/issues/324)：工程规范交付，不是生产故障本身。

更新时新增检索词、主票或必要回归入口即可。相同问题追加原 Issue 发生记录；关闭与复发遵守记录规范，不在索引中另行维护“已修复”状态。

- [#463 已封存宿主重启的停止恢复](https://github.com/CleverC2200/oa-workspace-minimal/issues/463)：[受控恢复合同](../runbooks/owner-restart-recovery.md)、[公开入口回归](../../tests/finance_preaudit/test_owner_restart_recovery.py)；区别于监督者丢失、退出码未知，不清其他原因的停止门。
