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
| 包装补充校对 | 四项之外异常遗漏、储存/警示文字、整体结论、Excel 尾行 | [#383](https://github.com/CleverC2200/oa-workspace-minimal/issues/383) | [决定/消费回归](../../tests/finance_preaudit/test_packaging_supplementary_checks.py)、[补充合同](../../.agents/skills/oa-finance-preaudit/references/flow-engine-contract.md)；工程检查与生产语义验收分开 |
| 调度与交接 | 终态丢失、孤立 running、Unknown process id、提前判卡死、旧窗口派单 | [#325 重复交接症状记录](https://github.com/CleverC2200/oa-workspace-minimal/issues/325)；留证工程 [#326](https://github.com/CleverC2200/oa-workspace-minimal/issues/326)；相关组件 [#150](https://github.com/CleverC2200/oa-workspace-minimal/issues/150)、[#202](https://github.com/CleverC2200/oa-workspace-minimal/issues/202) | [执行与原会话回归](../../tests/finance_preaudit/test_controller_execution.py)、[交接回归](../../tests/finance_preaudit/test_controller_handoff.py)、[隔离排障与恢复](../agents/quarantine-repair.md)；组件实现不代表所有退出原因已解决 |
| 观测与输出 | 状态 JSON 截断、完整文件、摘要门禁 | [#311](https://github.com/CleverC2200/oa-workspace-minimal/issues/311) | [完整状态输出回归](../../tests/finance_preaudit/test_status_output_file.py) |
| 原生工具上下文 | CLI stdout 完整但模型上下文截断、双层输出预算 | [#379](https://github.com/CleverC2200/oa-workspace-minimal/issues/379) | [原生合同检查](../agents/review-experiments.md#原生模型上下文检查)、[无付费原生回归](../../tests/finance_preaudit/test_native_review_contract.py)；机制证明不代表财务或供应商验收 |
| 商旅准备 | browser.close、Playwright 生命周期、真实准备 watchdog | [#313](https://github.com/CleverC2200/oa-workspace-minimal/issues/313)；诊断能力 [#304](https://github.com/CleverC2200/oa-workspace-minimal/issues/304) | [浏览器生命周期回归](../../tests/finance_preaudit/test_travel_browser_lifecycle.py)、[阶段采样回归](../../tests/finance_preaudit/test_prepare_trace.py)、[采集契约](../../tests/finance_preaudit/test_collect_case_contract.py)；关闭警告单独不能证明卡死 |
| 包装材料 | PPTX 不支持、隐藏页、页数不足、视觉候选漏页、PDF 页归属 | [#319](https://github.com/CleverC2200/oa-workspace-minimal/issues/319)、[#292](https://github.com/CleverC2200/oa-workspace-minimal/issues/292) | [PPTX 准备回归](../../tests/finance_preaudit/test_pptx_preparation.py)、[全链采集/页覆盖](../../tests/finance_preaudit/test_collect_case_contract.py) |
| 审核失败记账 | NameError、cli 未导入、record_production_failure_round | [#329](https://github.com/CleverC2200/oa-workspace-minimal/issues/329) | [真实入口参数/返回回归](../../tests/finance_preaudit/test_codex_review_worker.py)；不与历史异常退出原因自动归并 |
| 审核协议 | 空属性对象 Schema、唯一 JSON 围栏、多候选、包装草稿 | [#327](https://github.com/CleverC2200/oa-workspace-minimal/issues/327)（正式入口改进） | [包装入口/消费回归](../../tests/finance_preaudit/test_packaging_review.py)、[只读审核边界](../agents/packaging-review.md)；草稿与真实看图验收分开 |
| 业务规则 | 里程、住宿、发票、支付、包装文字误报或漏报 | 从 [人工确认反馈](../../.agents/skills/oa-finance-preaudit/references/confirmed-feedback.md) 的编号找规则和原记录 | [脱敏业务回归](../../.agents/skills/oa-finance-preaudit/references/regression-cases.json)；不作为生产审核答案提示 |

## 其他相关工作

- [#328 机械质量检查](https://github.com/CleverC2200/oa-workspace-minimal/issues/328)：[YAML、生成 Schema 和 lint](../agents/quality-checks.md)。
- [#330 历史原会话定位](https://github.com/CleverC2200/oa-workspace-minimal/issues/330)：[UUID、压缩归档与有限工具投影](../agents/session-evidence.md)。
- [#331 小时调度指令分层](https://github.com/CleverC2200/oa-workspace-minimal/issues/331)：[配置候选及启用条件](../runbooks/hourly-production.md)；按授权发布后才接入。

- [#190 生产恢复与告警端到端验收](https://github.com/CleverC2200/oa-workspace-minimal/issues/190)：验收工作，证据范围看原票。
- [#321 审核质量门与执行回执](https://github.com/CleverC2200/oa-workspace-minimal/issues/321)：质量改造及验证；局部实验不等于默认生产已启用。
- [#324 本次清单、标签与索引建设](https://github.com/CleverC2200/oa-workspace-minimal/issues/324)：工程规范交付，不是生产故障本身。

更新时新增检索词、主票或必要回归入口即可。相同问题追加原 Issue 发生记录；关闭与复发遵守记录规范，不在索引中另行维护“已修复”状态。
