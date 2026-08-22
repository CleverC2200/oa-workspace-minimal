# main 本地管理规则

## 当前事实

- GitHub Actions `quality` 已在无业务凭据的全新 checkout 中通过。
- 成功、失败单测、伪造密钥和被跟踪数据库文件均已通过独立 PR 验证。
- 截至 2026-08-22，当前 GitHub 方案不支持私有仓库的 branch protection 或 rulesets；两个官方 API 均返回 HTTP 403，要求升级 GitHub Pro 或将仓库公开。
- 仓库所有者决定继续保持私有，不为分支保护升级方案，也不再配置服务端 branch protection 或 rulesets。
- `main` 没有平台强制保护；以下规则由仓库所有者在本地和 PR 流程中维护，不得表述为 GitHub 已强制执行。

## 本地维护规则

- 普通改动从最新 `main` 创建 `codex/<slug>` 分支，不直接在 `main` 开发。
- 推送前运行与改动范围匹配的本地验证；PR 必须通过 GitHub Actions `quality`。
- 一个 PR 只关联一个可验收 Issue，并记录验证结果、风险和回退方式。
- 合并后同步本地 `main`，确认关联 Issue 已闭合，并删除已合并功能分支。
- 不强推或删除 `main`。确需例外时，由仓库所有者当前明确决定，并在对应 Issue 留下原因和结果。

## 决策状态

Issue #8 的 CI、失败测试和敏感内容阻断均已验证完成。服务端分支保护不再属于待交付项；本文件记录最终接受的本地管理方式。
