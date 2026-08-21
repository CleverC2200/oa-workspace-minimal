# GitHub Issue 追踪

本项目以私人仓库 `CleverC2200/oa-workspace-minimal` 的 GitHub Issues 作为需求、缺陷、依赖和验收状态的唯一工程追踪入口。

## 规则

- 非琐碎工作先建 Issue，再开始实现。
- 大目标建立父 Issue；可独立交付的工作拆成 Sub-issues，并标明依赖关系。
- Issue 必须包含背景、范围、非目标、验收标准和验证方式；不得包含真实 requestId、附件、账号、内部地址或日志正文。
- 只有规格完整、没有待定产品决策且依赖已满足的票，才加 `ready-for-agent`。
- 开始实现时在 Issue 中记录分支或 PR；PR 正文使用 `Closes #<issue>`。
- 合并后核对自动关闭状态；若未关闭，补充验收证据后手动关闭为 completed。

## 状态

规范角色标签见 [triage-labels.md](triage-labels.md)。依赖未满足的 Issue 保持打开并在正文列出 `Blocked by: #...`，不加 `ready-for-agent`。

## 临时材料

本地分析、截图和执行草稿放在被忽略的 `.scratch/<topic>/`，不得作为长期状态源。稳定结论写入当前文档或 GitHub Issue。
