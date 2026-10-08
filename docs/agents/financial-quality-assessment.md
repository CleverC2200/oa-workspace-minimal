# 已办样例的质量核对

执行闭合、输出格式正确和业务事实正确分别验收。沿用 `flow_evals.build_acceptance_report` 的可选 `financial_reviews`，或直接调用 `build_financial_quality_report` 核对存档输出。两者不调用模型、OA、提交或恢复。

## 回放前：原资料与历史审批交叉核对

### 先明确判定标准与问题类别

业务决定的 `pass/fail/uncertain` 与独立核对的 `pass/fail/unverified` 分别记录。证据不足时，模型如实保留已确认事实及必要未知，可以获得独立核对 `pass`；它不表示单据可全额核销。

修复前先由核对者用原资料、原规则和实际消费结果说明差异：

| 情况 | 处理 |
| --- | --- |
| 原事实和规则能唯一证明输出错误 | 记录错误事实、原引用及受影响项目，再修复和回归。 |
| 原资料不足，输出已正确保留必要未知 | 按真实业务结果核对，不将正常 `uncertain` 当程序失败。 |
| 证据充分性或规则边界存在两种合理解释 | 独立核对保留 `unverified`，先明确口径，不用修改提示或反复模型调用选择答案。 |
| 身份、归属、文件指纹或消费绑定不可信 | 按技术边界拒绝该记录，不将其包装成财务差错。 |

作为准确性回归案例前，各项预期必须能由同一规则唯一判断。边界未定的案例保留为待澄清资料，不计入准确性通过数。明确口径后，在新私有位置冻结对应版本的预期；旧清单、原评分、失败成本和冻结时间不变。事后明确的预期不能追认旧调用为事前合格试验。

Spec 审查核对业务规则、审核项职责及预期是否一致。金融规则以对应流程原文为依据；Worker 提示中的职责摘要不能替代规则，也不能成为第二套证据充分性标准。工程测试只证明它实际覆盖的输入、结构和消费合同，金融正确性仍按已冻结的原事实独立核对。

按用户当前要求，使用已办原材料、当时规则和原历史审批记录，不要求新增财务签字。清单记录真实存档或合成来源、场景、全部原资料 `source_refs`、对应规则 `rule_refs`、固定检查的 `id` 与原证据预期。所有文件引用均为绝对路径及 SHA-256，正文及身份留在私有目录。

`history_refs` 引用原 `original-context.json`。核对其原工作流与已办行的 requestId 是否均与原 material 的 request_id 一致，并保留真实 workflowRequestLogs。不能仅贴一个“已办”标签。每个检查同时记录 `historical_checks`：`consistent`、`conflict` 或 `uninformative`，依据必须是 `original_material_and_rules`，说明历史记录与原事实如何对应。历史泛泛“同意”、空意见或旧 AI 预审不能单独成为真值；宿主只验证这些显式记录，不按文字关键词判业务正确。

调用 `freeze_financial_expectations(checklist_reference, destination)` 独占创建私有冻结记录，使用实际宿主时间，绑定清单指纹。保留冲突及缺失状态；它们不能取得准入资格。冻结记录和预期不得进入受试模型工作目录。已有回放后才冻结的预期不能追认该次为事前合格试验。

## 回放后：原决定及实际消费输出核对

逐项评估用 `method=independent_manual`、`reviewer_role=historical_crosscheck`、真实核对记录及带时区 reviewed_at 记录。每项明确填写 pass/fail/unverified 与理由，不只比较状态。工程草稿可使用 engineering_draft；旧 finance 格式兼容保留，但签字不能替代当前要求的原历史记录交叉核对。

评估同时绑定清单、最终 archive manifest、result、canonical item_key，以及模型原决定、宿主实际消费决定、最终意见和消费回执指纹。存档报告负责运行归属和消费对应关系验证，财务核对不复制另一套校验器。不能用另一回放改名为当前样例，或把未消费输出当成实际 preview。预期清单可仅绑定回放前不变的原资料和规则；最终存档及输出绑定在事后评估中完成。

实际开始及闭合时间只读存档报告验证的宿主回执，外层时间不能覆盖。老档缺可信启动或闭合时间，仍可记录工程事实核对，但相关前置或消费资格保持未验证。预期、历史记录或输出有变化则旧评估失效；改口径必须另存版本并说明原因，不能覆盖旧评分或原字节。

## 报告资格

新生成的财务质量报告使用 `schema_version=2`。业务决定、预期冻结和人工评估仍使用原格式。每个 case 的 `expectations_timing` 单独说明冻结时间是否早于可信启动回执，`historical_crosscheck` 保留原历史核对状态。提前冻结且历史冲突时，前者为 `verified`，后者为 `conflict`，资格仍为 false。

| 时序状态 | reason | 含义 |
| --- | --- | --- |
| verified | frozen_before_replay | 清单指纹匹配，两个时间均有时区，冻结不晚于可信启动 |
| unverified | frozen_after_replay_start | 冻结晚于可信启动 |
| unverified | freeze_binding_mismatch | 冻结记录未绑定当前清单或类型不符 |
| unknown | expectations_not_bound | 没有绑定冻结记录 |
| unknown | replay_start_not_bound | 没有可信宿主启动回执 |
| unknown | freeze_or_start_time_missing_or_invalid | 时间缺失或无效 |
| unknown | timezone_missing | 冻结或启动时间没有时区 |

文件指纹漂移仍直接拒绝评估，不生成时间通过结果。`expectations_frozen_before_replay` 保留旧的时间、历史及配置合取条件，报告以 `legacy_expectations_flag_semantics=time_history_and_configuration_conjunction` 明示该含义。配置漂移或历史冲突可使它为 false，即使独立时间状态通过。`eligible` 继续要求原完整场景、真实存档、完整核对、原生成合同、实际消费及全部原资格条件。

`read_financial_quality_report(reference)` 只读取已绑定报告，不重算评分或资格。它接受 schema 1 和 2。schema 1 未独立记录时序证明，展示 `unknown` 与 `legacy_conjunction_no_separate_timing`，保留原合取标志及原 eligible。读取不会写回旧文件。

返回每条事实结论、历史交叉核对、事前冻结、实际消费、来源种类和事后核对状态。五类业务招待场景为普通支付、多支付/退款/确认超报、关系不明、合法共享支付、已确认异常与其他未知并存。全部准入结果须来自真实存档、事前完成无未解冲突的交叉核对、真实闭合且逐项正确；混入合成结果不能通过。合成记录只证明工程行为。

原验收入口还要求每个闭合样例都经过同一核对窗口。旧资源覆盖、格式、完整用量及提交安全门继续保留，无 financial_reviews 的旧报告不代表已经新增业务语义验收。

正常简洁全通过意见无需逐笔展开退款；不能用旧“未展开退款即失败”理由单独判错。复杂目标保持在清单中：原图“使用零钱通支付”支持所显示的支出金额，主要未知是与本次费用的对应关系；不能仅因缺“支付成功”或流水号判支付失败。

链接：[业务招待规则](../../.agents/skills/oa-finance-preaudit/references/flows/business-entertainment.md)、[验证层级](../../CONTRIBUTING.md#验证层级)、[质量检查](quality-checks.md)。

原历史 context 还须按已有 flow 选择入口核验真实流程：原 workflow 和 handled 实际定义一致，material 的规范名或已保存别名也唯一映射到同一 flow key；不能仅用 caller workflow_key 或费用内容分类。沿用配置的精确名称/别名 fallback，配置历史 ID 与实际 ID 不同并不单独拒绝，也不修改配置。冻结记录保留实际匹配配置文件指纹；配置漂移不能追认原冻结资格。相关配置 JSON 同时进入宿主 runtime pin 和 calling-source Git pin。

完整财务资格须显式声明 `review_scope=complete_financial_review`，并将每个检查以 `item_key` 绑定原冻结 packet 的固定审核项。业务招待的四项均须完整覆盖原规则要求的断言；缺预算策略、发票主体/关联、付款/费用对应或净额异常/未知核对时，人工记录保持 unverified。宿主只验证完整范围声明、固定项覆盖与显式逐项结论，不按文字猜实际核对是否正确。有限付款/金额事实仍可报告 pass，但不能升级为完整财务100%资格。

`flow_configs` 使用配置目录内相对文件名及内容 SHA-256，允许同字节配置随源码 checkout 交付，不把工作树路径变化当作配置变化。实际配置字节变化仍失去原冻结资格；旧绝对路径格式记录保留为未验证，不回填或改旧冻结时间。该稳定命名仅适用于配置内容 pin，不归一化原资料、历史记录、来源角色或回放输出指纹。

新准备清单先调用 `prepare_financial_checklist(checklist_reference, destination)`：在新的私有位置保存原规则的逐字快照，返回现有 `file + sha256` 引用。后续冻结预期和评估均绑定这个新清单；原清单、评分、时间和回执不变。快照只保留原引用已校验的字节，不替换规则版本，不证明该规则已经实际回放。修改工作树后仍可回查准备时规则；实际消费报告继续核对 calling-source 与完整规则哈希是否一致。

报告的 `check_outcomes` 原样列出各显式检查的 pass/fail/unverified。汇总 fail 表示有检查未满足，不表示所有核销金额仍错误；核心金额、内部事实一致性和实际意见披露按原逐项评估分别说明，不按自然语言关键词重新评分。`missing_evidence` 是内部缺口字段，意见仅渲染 `conclusion`，回查时分别使用原决定、消费决定和 `remark` 指纹，不能将未展示文字描述成财务已收到的意见。

版本展示分别注明最新本地候选、最后真实回放修订、远端候选和正式版本。存档的 `source.revision` 只说明该次实际调用源码；本地后续修改或工程回归不更新历史回放修订。当前候选没有同修订模型证据时，应明确列为未验证，保留真实验收及父票未完成状态。
