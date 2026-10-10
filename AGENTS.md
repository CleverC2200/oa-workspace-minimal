# OA 财务预审项目

## 项目事实

- 当前发布基线为 V1.1；发布本身不创建或启动自动化。生产定时任务只能由独立授权的唯一 Codex 审批 cron 在生产克隆的唯一 `main` 工作树创建独立任务，并调用现行滚动控制器；频率以当前自动化配置为准。
- Codex Skill 负责读取证据和形成业务意见；脚本只负责取数、冻结、确定性校验和受控提交，不生成审批判断。
- 范围是六类财务预审、包装图片文字校对及海外采购发票识别回填。财务预审只给意见，不批准、不退回；包装不扩展为法规审核；发票回填只处理已授权字段及流程提交。
- 优先复用 `apps/oa_approval/` 的现有连接能力，不建设通用审批平台、策略引擎或大规模状态模型。

## 工作约束

- 开发固定在已保存的独立克隆 `oa-workspace-dev` 内使用 `codex/<slug>` 分支；生产项目 `oa-workspace-prd` 对应 `oa-workspace-minimal`，只保留干净 `main` 供生产审批，不在其中开发、创建开发工作树或切换功能分支。
- 建立开发环境、发布、合并或生产同步时，先读 [开发与生产隔离及发布](CONTRIBUTING.md#开发与生产隔离及发布)。远端合并不等于生产生效；生产同步与核验完成后，下一轮新进程才加载新版本。
- 手工调用默认只读 preview。任何 OA 写入、自动化启停或外部发布都必须有当前明确授权。
- 不提交密钥、token、Cookie、账号、内部地址、数据库、附件正文、运行目录或未脱敏日志；真实配置只放 `.env`、Keychain 或本地运行环境。
- 附件只有在归属当前流程且能关联具体审核项时才是证据；不得用文件名、顺序、相似金额或日期补猜事实。
- 业务 `❌/⚠️` 不是技术失败；流程身份、附件归属、读取完整性或授权不可信才停止当前单据。

## 按需读取

- 查询或修改 OA 接口调用时，优先查阅[泛微 E9 官方后端接口文档](https://e-cloudstore.com/ec/api/applist/index.html#/)，以对应接口的参数说明和展开示例为依据；提交及发票回填的已核实合同见 [OA 官方接口入口](docs/agents/oa-api.md)。

- 用户要求排查、修复或清理隔离/历史失败时，安全止损与原会话收尾优先；排错先查 [Bug 快速索引](docs/bugs/README.md) 和匹配的 Issue，再读[隔离排障与恢复](docs/agents/quarantine-repair.md)。同轮追加发生记录，根因未明只标相关；记录与关闭标准见 [Bug 记录规范](docs/agents/bug-ledger.md)。生产隔离是止损，开发排障须继续诊断、回测并在已有同范围授权内显式恢复；隔离成功或普通队列为空不代表修复完成。
- 海外采购付款的发票识别及字段回填使用 `.agents/skills/oa-overseas-invoice/SKILL.md`，独立授权与单据级提交，不进入财务预审滚动队列。
- 实际单据预审使用 `.agents/skills/oa-finance-preaudit/SKILL.md`。开发结果结构读 [.agents/skills/oa-finance-preaudit/references/flow-engine-contract.md](.agents/skills/oa-finance-preaudit/references/flow-engine-contract.md)；证据规则读 [.agents/skills/oa-finance-preaudit/references/observation-contract.md](.agents/skills/oa-finance-preaudit/references/observation-contract.md)；流程规则经 [result-contract.md](.agents/skills/oa-finance-preaudit/references/result-contract.md) 只选择匹配流程。开发和测试不执行真实单据 next-action 或 complete-review。
- 创建、更新 GitHub Issue 或拆分需求、依赖与验收：读 [Issue 追踪](docs/agents/issue-tracker.md)；设置工作状态或分类标签时再读 [标签规范](docs/agents/triage-labels.md)。
- 准备提交、PR 或调整交付规则：读 `CONTRIBUTING.md` 中对应的“分支与提交”“Pull Request”或“验证层级”；涉及发布或生产同步时按上方发布入口读取。
- 统一领域词汇或记录架构决定：读 `CONTEXT.md` 和 `docs/agents/domain.md`。
- `README.md` 只维护当前可验证事实、入口和验证命令；未实现设计放 GitHub Issues，不写成现状。

## Issue、提交与 PR

- 非琐碎改动先关联 Issue；一个 PR 只解决一个可独立验收的问题。完成该票使用 `Closes #<issue>`；Bug 的生产验收未齐时使用 `Refs #<issue>`，保留打开。
- 分支默认使用 `codex/<slug>`；提交使用英文 Conventional Commits，正文说明原因和验证，不添加 AI 签名。
- 默认交付停在验证通过的本地提交；用户只说“提交”时仅创建本地 commit。只有用户明确授权对应动作和范围时，才执行 `git push`、创建或更新 PR、转 Ready、合并或清理远端分支；否则汇报待发布分支和提交，等待用户统一推送。
- PR 未完成时保持 Draft；聚焦测试、必要的完整测试和文档检查通过后再转 Ready。合并后核对 `Closes` 票已关闭；`Refs` 票继续跟进验收，证据齐后再关闭。

## 验证

- 局部代码改动采用聚焦测试、受影响静态检查和一次聚焦审查。写入、授权、幂等、进程回收、跨模块契约或发布改动保留 Standards/Spec 双路审查及必要完整验收；细则读 `CONTRIBUTING.md` 的“验证层级”。
- 文档改动检查受影响链接和 `git diff --check`；修改 Skill 时校验对应 Skill，修改 AGENTS 或指令路由时执行指令审计。
- 无法验证时明确说明未验证项，不把 mock、preview 或计划描述为生产事实。

## 只读对照的约束与执行机制

| 约束 | 执行机制与回归入口 |
| --- | --- |
| 新档案只绑定本次调用证据，运行缓存不成为永久证据；旧档案保留原合同。 | `review_archive_outputs.evidence_paths` 统一选取；`review_batch_report` 核对集合与哈希；[归档范围回归](tests/finance_preaudit/test_review_archive_outputs.py)拒绝旧的递归绑定写法。 |
| 新启动前，四项事实的预期须独立核对原材料并提前冻结。 | `freeze_financial_expectations` 与 `_ComparisonCaller._side_expectations` 核对来源、覆盖和时序；[原材料核对回归](tests/finance_preaudit/test_original_fact_review.py)。脚本不替代业务判断。 |
| 失败停止、已启动侧不重试、额度沿原回执累计；验收关闭不授予下一轮运行。 | 原停止记录、`_same_source_attempt`、`_counted_sides` 和 `_parent_budget`；[派发回归](tests/finance_preaudit/test_comparison_dispatch.py)、[停止资格回归](tests/finance_preaudit/test_readonly_comparison_start.py)。新修订或额度仍须明确授权。 |

归档合同与运行边界见[受控只读对照](docs/agents/review-experiments.md)。以上机制不授予 OA 写入、生产修改或发布权限。
