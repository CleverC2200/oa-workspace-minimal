# 已办样例的质量核对

执行闭合、输出格式正确和业务事实正确分别验收。沿用 `flow_evals.build_acceptance_report` 的可选 `financial_reviews`，或直接调用 `build_financial_quality_report` 核对存档输出。两者不调用模型、OA、提交或恢复。

## 回放前：原资料与历史审批交叉核对

按用户当前要求，使用已办原材料、当时规则和原历史审批记录，不要求新增财务签字。清单记录真实存档或合成来源、场景、全部原资料 `source_refs`、对应规则 `rule_refs`、固定检查的 `id` 与原证据预期。所有文件引用均为绝对路径及 SHA-256，正文及身份留在私有目录。

`history_refs` 引用原 `original-context.json`。核对其原工作流与已办行的 requestId 是否均与原 material 的 request_id 一致，并保留真实 workflowRequestLogs。不能仅贴一个“已办”标签。每个检查同时记录 `historical_checks`：`consistent`、`conflict` 或 `uninformative`，依据必须是 `original_material_and_rules`，说明历史记录与原事实如何对应。历史泛泛“同意”、空意见或旧 AI 预审不能单独成为真值；宿主只验证这些显式记录，不按文字关键词判业务正确。

调用 `freeze_financial_expectations(checklist_reference, destination)` 独占创建私有冻结记录，使用实际宿主时间，绑定清单指纹。保留冲突及缺失状态；它们不能取得准入资格。冻结记录和预期不得进入受试模型工作目录。已有回放后才冻结的预期不能追认该次为事前合格试验。

## 回放后：原决定及实际消费输出核对

逐项评估用 `method=independent_manual`、`reviewer_role=historical_crosscheck`、真实核对记录及带时区 reviewed_at 记录。每项明确填写 pass/fail/unverified 与理由，不只比较状态。工程草稿可使用 engineering_draft；旧 finance 格式兼容保留，但签字不能替代当前要求的原历史记录交叉核对。

评估同时绑定清单、最终 archive manifest、result、canonical item_key，以及模型原决定、宿主实际消费决定、最终意见和消费回执指纹。存档报告负责运行归属和消费对应关系验证，财务核对不复制另一套校验器。不能用另一回放改名为当前样例，或把未消费输出当成实际 preview。预期清单可仅绑定回放前不变的原资料和规则；最终存档及输出绑定在事后评估中完成。

实际开始及闭合时间只读存档报告验证的宿主回执，外层时间不能覆盖。老档缺可信启动或闭合时间，仍可记录工程事实核对，但相关前置或消费资格保持未验证。预期、历史记录或输出有变化则旧评估失效；改口径必须另存版本并说明原因，不能覆盖旧评分或原字节。

## 报告资格

返回每条事实结论、历史交叉核对、事前冻结、实际消费、来源种类和事后核对状态。五类业务招待场景为普通支付、多支付/退款/确认超报、关系不明、合法共享支付、已确认异常与其他未知并存。全部准入结果须来自真实存档、事前完成无未解冲突的交叉核对、真实闭合且逐项正确；混入合成结果不能通过。合成记录只证明工程行为。

原验收入口还要求每个闭合样例都经过同一核对窗口。旧资源覆盖、格式、完整用量及提交安全门继续保留，无 financial_reviews 的旧报告不代表已经新增业务语义验收。

正常简洁全通过意见无需逐笔展开退款；不能用旧“未展开退款即失败”理由单独判错。复杂目标保持在清单中：原图“使用零钱通支付”支持所显示的支出金额，主要未知是与本次费用的对应关系；不能仅因缺“支付成功”或流水号判支付失败。

链接：[业务招待规则](../../.agents/skills/oa-finance-preaudit/references/flows/business-entertainment.md)、[验证层级](../../CONTRIBUTING.md#验证层级)、[质量检查](quality-checks.md)。
