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
- 正文包含 `Closes #<issue>`、改动摘要、验证命令、风险和回退方式。
- 不在 PR、Issue、截图或日志中暴露 requestId、附件正文、账号、内部地址、密钥或 Cookie。
- 合并后确认关联 Issue 已关闭；若自动关闭失败，补充验收证据后手动关闭。

## 验证层级

1. 行为修改或 bug 修复先增加聚焦测试。
2. 运行受影响模块测试；跨模块契约变化运行完整测试。
3. 修改 Skill 时运行 Skill 校验；修改文档或指令时运行链接/指令审计。
4. 所有改动运行 `git diff --check`，并复核待提交文件和敏感信息。

常用完整验证：

```bash
python3 /Users/synear/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .agents/skills/oa-finance-preaudit
python3 -m unittest discover -s tests/finance_preaudit -p 'test_*.py' -v
git diff --check
```

真实 OA 写入、自动化启停、仓库设置和发布均属于独立授权动作，测试通过不等于获得执行授权。
