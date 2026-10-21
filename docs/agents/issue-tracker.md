# GitHub Issue 追踪

本项目以私人仓库 `CleverC2200/oa-workspace-minimal` 的 GitHub Issues 作为需求、缺陷、依赖和验收状态的唯一工程追踪入口。

## 规则

- 非琐碎工作先建 Issue，再开始实现。
- 大目标建立父 Issue；可独立交付的工作拆成 Sub-issues，并标明依赖关系。
- Issue 必须包含背景、范围、非目标、验收标准和验证方式；不得包含真实 requestId、附件、账号、内部地址或日志正文。
- 只有规格完整、没有待定产品决策且依赖已满足的票，才加 `ready-for-agent`。
- 开始实现时在 Issue 中记录分支或 PR；完成该票的 PR 使用 `Closes #<issue>`。Bug 的生产验收未齐时用 `Refs #<issue>`，保持打开；代码交付与验收完成分别记录。
- 合并后核对 `Closes` 票的自动关闭状态；若未关闭，补充验收证据后手动关闭为 completed。`Refs` 票按剩余验收收口。

## Bug 记录与复发

排错先查 [Bug 快速索引](../bugs/README.md)，按 [Bug 记录规范](bug-ledger.md) 关联发生记录、根因、回归和生产证据。索引只导航，不维护第二份状态表；当前状态从 Issue 读取。

已核实无需行动的项使用 `non-actionable` 并保留依据；其他需求、文档和咨询用原类型标签。未复现、根因未明或验收未齐仍是待跟进，不属于无价值问题。

## 状态

规范角色标签见 [triage-labels.md](triage-labels.md)。依赖未满足的 Issue 保持打开并在正文列出 `Blocked by: #...`，不加 `ready-for-agent`。

## 临时材料

本地分析、截图和执行草稿通过[项目空间管理](project-space.md)的受管实验入口保存，不得作为长期状态源。历史 `.scratch/` 中的源码、在用输入和恢复证据保持保护。稳定结论写入当前文档或 GitHub Issue。
