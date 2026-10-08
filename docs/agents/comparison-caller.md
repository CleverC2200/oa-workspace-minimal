# 私有二十单真实版本对照调用

仅用于现有业务招待费实验。公开 `freeze_pair` / `run_pair` 的效果准入保持原样。对照使用原配对计划、原父额度链及只读 preview，不创建单臂协议。

```text
comparison-caller.private.py
  固定仓库、协调模块、原引用适配器、身份清单
  _ComparisonCaller.validate_or_run
    原父锁
    范围、授权、源质量、完整财务资格、引用选择、预算、原生证明
    原阶段 claim
    selection.pending_arms
      逐侧重核授权时间、预算、源码、准备及取消
      原 source host execute
      实际 Worker → FlowEngine preview → build_report
    独立核对通过后，下一单才能继续
```

私有后继由[薄模板](templates/comparison-caller.private.py)复制。配置显式固定仓库绝对路径、协调模块与[原件复用模块](../../apps/oa_finance_preaudit/review_references.py)的文件及 SHA256、[引用薄适配器](templates/reference-pair-adapter.private.py)的文件及 SHA256、身份清单的文件及 SHA256。`validate` 和 `run` 还要求原授权的 `runner_sha256` 绑定调用者，以及 `caller_configuration_sha256` 绑定整份配置。移动脚本不能自动改变资料根或执行范围。

配置字段为 `repository`、`module`、`reference_module`、`identity_bank`、`adapter`；除仓库路径外，各字段都是 `{file, sha256}` 原件引用。请求字段为 `scope`、`authority`、`alias`、`prerequisites`；其中 prerequisites 绑定 `source_scope`、`source_authority`、`source_closed`、`source_phase`、`financial_qualification`。来源阶段必须属于最终调用的同一父额度链，来源例数从原 scope 读取。

最终 scope 固定二十个银行身份、候选与生产提交、两个原生证明、900 秒和单侧最多一次。每行包含 `plan`、`checklist`、`expectations`、`baseline_checklist`、`baseline_expectations`；两侧原资料和原规则各自匹配，四项期待均在运行前冻结。复用侧继续引用原 checklist/freeze，不能用新准备替换原期待。

新准备还在 `conditions.call_identity` 冻结原 Worker 命令实际声明的模型、供应商、推理强度和请求服务档位，两侧必须一致。供应商取自 `model_provider`，不由模型名推定。原命令未声明服务档位时记录 `not_configured`，不补写默认值。宿主在启动时将同一身份写入新 manifest；报告核对其与绑定的原准备一致，再展示 `source.requested_service_tier`。旧准备、manifest 和报告结构保持兼容。

每个 `reuse` 侧需完整原件包：`plan`、`arm`、`manifest`、`report`、`quality`、`assessment`、`checklist`、`freeze`，以及必要时的 `report_field`。新目标 plan 与来源计划分开，不复制旧输出。`snapshot_audit` 仅沿用既有生产跨日期审计，候选源码快照不能借它放行差异。同源同单已有启动只能引用合格原件，不能重复启动。来源九例含原二十中的八例及额外混合例时，最终新增侧数是二十个生产侧加十二个未启动候选侧，合计三十二。

调用形式为 `python <caller> <repository> <configuration> <readiness|validate|run> <request>`。调用者不加载密钥文件。只有全部准入通过后的真实执行，才检查调用环境是否提供已有 reviewer 凭据，并由原宿主处理。

OpenAI 原生认证沿原 Worker 使用当前环境的 `HOME`、`PATH` 和显式 `CODEX_HOME`；宿主保留原 `CODEX_HOME`，不读取或复制认证文件，不创建另一个账户入口。旧 DeepSeek 环境变量仍可用于旧冻结源码，但不代表新 OpenAI 调用使用该密钥。

`readiness` 只展示八项条件及各自缺口。它不 claim、不授予财务资格、不授权运行。`validate` 在原父锁下重新核验。`run` 使用相同核验，再 claim 并仅遍历 `pending_arms`。旧侧使用引用，不复制旧输出，不再次消费。已启动但无法确认归属的侧计为 unknown，不能自动补跑。原父链继续计入复用侧的原始成本；最终窗口中新启动的候选侧不会转算为来源诊断消耗。

新侧的实际结果与原始成本保存在 `comparison-execution.private.json`。格式拒绝、取消、未知启动或回收不明停止另一侧，保留已产生的结果。成功消费仍是 `awaiting_independent_assessment`。候选沿现有独立 audit binding；新基线需自身原规则、checklist、expectations 与独立 audit binding，不能借用候选核对。基线 audit binding 的 checklist/expectations 必须与 scope 相同。前单待核时不开始下一单；候选事实失败或未验证停止扩样。可信、闭合且完成独立核对的生产事实失败保留为对照缺陷。生产业务口径未知只有在原基线 checklist 的 `business_scope_is_unique=false` 于运行前冻结、独立四项审计完整、原身份/消费/计量及图片观测可信时才能继续；保留原 `unverified/eligible`，不计入质量有效配对或改善。缺少这些证据仍停止。调用者另返回 `automated_preflight_seconds`，Worker 输入准备至 preview 的原业务耗时保持原计量边界。

候选原生证明必须由候选归档源码验证。当前生产源码缺少新候选检查器时，由独立基线验证器导入原生产 Worker/FlowEngine 核对外置证明，绑定完整原源码、宿主、CLI 和原生观察。通过仅表示 `baseline_comparison_ready=true`；完整输入、付费派发就绪及样本资格标志仍为 false，生产原提示、Schema 和默认历史预算保持可观测。

旧外置原件适配器和所有历史资料封存不改。当前原件校验迁入上述单一模块，旧 schema 1/2 资格条件保持原样。schema 3 必须按原 freeze、解释记录和独立 assessment 重建质量报告，确认新窗口资格；旧 conflict、旧合取标志及 legacy eligible 保留。

原生没有提供推理 Token 时保留 `unknown`。复用仍要求输入、缓存、输出及总量精确并对应原终态记录；推理维度只在原终态及对应 rollout 都未提供时保持未知。已提供的值必须为非布尔整数、处于输出量内，并与报告一致。

回执读取兼容旧 `reviewer/codex-home` 和当前 Worker 保留的 `reviewer/tool-receipts`。后者逐文件核验原宿主结果中的 SHA-256；存在保留文件清单时还核对完整文件集合。计量仍保持 input 包含 cache、output 包含 reasoning，不重复相加，也不因回执迁移授予来源或财务资格。

本地 CLI fixture 回归仅证明进程、只读消费、计数、拒绝及停止行为。它不证明真实模型输入，也不提供财务资格、真实二十单结果或生产验收。
