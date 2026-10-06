# 私有候选诊断阶段

本入口收敛当前 OA 候选诊断的私有调用逻辑。它继续消费 `freeze_pair` 生成的原配对计划，候选执行仍由原来源宿主和 Worker 完成。公开 `scripts.review_experiment freeze/run` 协议及效果准入拒绝保持。这里没有新增公开单臂计划，不生成模型、OA 或发布授权。

## 新阶段资料

新建私有阶段目录，将[薄调用者模板](templates/candidate-phase.private.py)逐字复制到该目录。模板只传资料指针，协调逻辑归 `review_experiment` 的私有入口。原阶段脚本、停止记录、原图、输出和评分只读保存。复制模板不恢复旧阶段；原 claim、STOP 或阶段关闭记录都阻止继续调用。

阶段 scope 使用 `kind=preaudit_private_candidate_phase`，绑定候选修订、原 paired plans、单据顺序及原 checklist/expectations 引用。每条 case 保留 alias、原 plan 的 `file + sha256`、稳定身份及原预期引用。`condition_differences` 逐条声明实际条件差异，只接受已有诊断的 rules/call_profile 差异。effect 计划不能进入该私有入口。

scope 的 `native_contract` 引用已保存的 #379 证明。来源快照还须保留 `scripts/quality.py` 与 `scripts/review_experiment_host.py` 的 Git 字节。原生检查在实际候选来源宿主中调用 `readiness(record)`，并匹配真实 CLI 身份与实际 host 字节。仅 dispatcher checkout 的证明不能用于另一候选来源。源码或 profile 变化后，旧证明失效；稳定新源码须另做零付费原生检查。

同修订已执行单以 `prerequisites` 声明，包含原 plan、checklist、expectations 及新的只读 `audit_binding` 引用。绑定指向原报告和原 assessment，不改旧文件。当前阶段前单的审核者独占写入 `candidate-audit-binding.private.json`，内容为 quality_report 与 assessment 的 `file + sha256`。金融报告继续由现有独立人工核对及质量生成器形成，阶段入口不生成财务判断。

## 授权与计数

当前授权绑定 scope SHA、薄调用者 SHA、明确授权来源、时限及原父累计授权引用。阶段、累计、诊断上限均取冻结数据，子授权不能扩大父授权的累计或诊断上限。baseline_execution_authorized 和 oa_write_authorized 均必须为 false，first_quality_or_format_failure_stops 必须为 true。凭据由原执行环境提供，模板不读取凭据文件。

`prior_plans` 引用全部已发生调用的原计划，并声明 source_revision 及 diagnostic 分组。父授权的 includes_previous_starts 和 first_phase_authority 锚定原始前置调用。后续阶段还绑定 prior_phase_authority 与 prior_phase_scope，从前一授权及该 scope 的实际启动回执推导应有计划集合。不能省略旧失败或将换阶段视作计数归零。

每次计数核验原 started、cli-start 和计划指纹。未知启动保持 unknown 并阻止派发。已启动的同修订同单不能换阶段再跑。不存在当前有效授权、来源漂移、取消或回收未知时，入口拒绝或停止，不补跑。

## 逐单推进

`_run_candidate_phase(scope, authority, alias, caller_file=...)` 一次最多消费一个未启动候选。先核原预算、同修订质量前件、原资料、实际来源及原生证明，再重读实际准备结果并认领原计划。只有前单实际消费、原格式、完整核对和四个显式 check_outcomes 均为 pass 才进入下一单。单个 verdict 或 overall 不能替代四项结果。未知或失败停止扩样。

独立报告读取复用 #380，旧 schema 1 的时序显示 unknown，原历史、评分及 reported_eligible 保持。单例四项通过可以支持继续诊断，不能替代全部场景、二十样本资格或正式上线门。

## 状态与一次收尾

`_candidate_phase_status` 从原回执、实际批次报告和独立核对生成状态，不启动模型或原生检查。`phase-status.private.json` 是同一派生状态的当前记录，不能作为执行授权。它分别列出当前源码、实际 PID/进程状态、已返回、已核对、未测、质量前件、预算及阻断。

业务处理时间使用原 input_preparation_to_preview_consumption 计时。已认领调用的自动派发前检查时间由单独计时记录。完整工程阶段耗时仍为 unknown，不从业务时间或人工等待反推。

`_finish_candidate_phase` 只在完成或停止时生成一次 `phase-report.private.json` 和 `phase-report.html`。现有窗口报告纳入全部 prior 与当前实际尝试，保留失败、慢单及未知费用，未测单另列状态。缓存属于输入，推理属于输出，不重复计量。关闭回执绑定报告与证据；再次读取复用同一报告，不重新渲染页面。页面核对状态初始为 pending，由维护者在阶段收尾时做一次必要核对。关闭阶段不再接受执行。

## 验证边界

`tests/finance_preaudit/test_candidate_phase.py` 经同一薄私有调用者、冻结来源宿主、受控外部进程及实际 FlowEngine preview 验证推进、拒绝、停测和计量。原生证明与可执行文件在该调度回归中是受控依赖，只证明接线；#379 的真实原生 loopback 检查单独证明模型上下文。两者均不代表真实财务判断、供应商稳定性或生产验收。

关联[受控对照](review-experiments.md)、[质量核对](financial-quality-assessment.md)和[验证层级](../../CONTRIBUTING.md#验证层级)。本票工程交付不关闭 #369 的业务失败、#366 的二十样本要求或原 No-Go。
