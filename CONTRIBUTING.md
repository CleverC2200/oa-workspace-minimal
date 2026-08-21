# 贡献指南

## 开始之前

非琐碎改动先建立或领取 GitHub Issue。Issue 应明确背景、范围、非目标、验收标准和依赖；本地 `.scratch/` 只用于临时材料，不是状态源。

## 分支与提交

- 从最新 `main` 创建 `codex/<slug>` 分支。
- 一个分支和一个 PR 只完成一个可独立验收的问题。
- 提交使用英文 Conventional Commits，例如 `fix(queue): preserve request ownership`。
- 提交正文说明改动原因和验证结果，不添加 AI 工具签名。
- 默认推送个人 `origin`；推送到其他组织或上游必须获得当前明确授权。

## Pull Request

- 未完成或仍在探索的 PR 保持 Draft；验收标准和必要检查通过后再转 Ready。
- 标题使用 Conventional Commit 风格。
- 正文必须且只能包含一个独立 Issue 关联行，并保留 `## 验证`、`## 风险与回退`、改动摘要和实际结果。完成票据使用 `Closes #<issue>`；不应关闭尚未完成票据的跟进修正使用 `Refs #<issue>`。
- 不在 PR、Issue、截图或日志中暴露 requestId、附件正文、账号、内部地址、密钥或 Cookie。
- 合并后确认关联 Issue 已关闭；若自动关闭失败，补充验收证据后手动关闭。

## 验证层级

1. 行为修改或 bug 修复先增加聚焦测试。
2. 运行受影响模块测试；跨模块契约变化运行完整测试。
3. 修改 Skill 时运行 Skill 校验；修改文档或指令时运行链接/指令审计。
4. 所有改动运行 `git diff --check`，并复核待提交文件和敏感信息。

常用完整验证：

```bash
python3 -m scripts.quality skill
python3 -m scripts.quality format
python3 -m scripts.quality hygiene
python3 -m scripts.quality links
python3 -m unittest discover -s tests/finance_preaudit -p 'test_*.py' -v
python3 -m compileall -q apps scripts tests
```

GitHub `main` 的目标规则是通过名为 `quality` 的检查并经 PR 合并，禁止强推和删除，且同样约束管理员。当前私有仓库方案尚不支持服务端分支保护，实际状态和升级后配置见[主分支保护状态](docs/agents/branch-protection.md)；在规则生效前不得把人工规范表述为平台强制。

真实 OA 写入、自动化启停和发布均属于独立授权动作，测试通过不等于获得执行授权。
