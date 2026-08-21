# OA 财务预审项目

## 项目事实

- 当前发布基线为 V1.1；发布本身不启动自动化，现有定时任务保持暂停。
- Codex Skill 负责读取证据和形成业务意见；脚本只负责取数、冻结、确定性校验和受控提交，不生成审批判断。
- 范围是四类财务预审与包装图片文字校对。财务预审只给意见，不批准、不退回；包装不扩展为法规审核。
- 优先复用 `apps/oa_approval/` 的现有连接能力，不建设通用审批平台、策略引擎或大规模状态模型。

## 工作约束

- 编程前明确目标、假设和成功标准；简单优先，只做必要的小范围改动，保留已有未提交内容。
- 手工调用默认只读 preview。任何 OA 写入、自动化启停或外部发布都必须有当前明确授权。
- 不提交密钥、token、Cookie、账号、内部地址、数据库、附件正文、运行目录或未脱敏日志；真实配置只放 `.env`、Keychain 或本地运行环境。
- 附件只有在归属当前流程且能关联具体审核项时才是证据；不得用文件名、顺序、相似金额或日期补猜事实。
- 业务 `❌/⚠️` 不是技术失败；流程身份、附件归属、读取完整性或授权不可信才停止当前单据。

## 按需读取

- 修改财务预审、测试或规则：先读 `.agents/skills/oa-finance-preaudit/SKILL.md`，再按其路由读取对应 reference。
- 修改 GitHub Issue、PR 或交付规则：读 `CONTRIBUTING.md`、`docs/agents/issue-tracker.md` 和 `docs/agents/triage-labels.md`。
- 统一领域词汇或记录架构决定：读 `CONTEXT.md` 和 `docs/agents/domain.md`。
- `README.md` 只维护当前可验证事实、入口和验证命令；未实现设计放 GitHub Issues，不写成现状。

## Issue、提交与 PR

- 非琐碎改动先关联 Issue；一个 PR 只解决一个可独立验收的问题，使用 `Closes #<issue>`。
- 分支默认使用 `codex/<slug>`；提交使用英文 Conventional Commits，正文说明原因和验证，不添加 AI 签名。
- 默认推送个人 `origin`；任何其他组织或上游仓库都需要当前明确授权。
- PR 未完成时保持 Draft；聚焦测试、必要的完整测试和文档检查通过后再转 Ready。合并后核对关联 Issue 已关闭。

## 验证

- 改行为或修 bug：先补聚焦测试，再运行受影响测试；跨模块契约变化运行完整测试。
- 只改文档：运行 Skill 校验、AGENTS 指令审计和 `git diff --check`。
- 无法验证时明确说明未验证项，不把 mock、preview 或计划描述为生产事实。
