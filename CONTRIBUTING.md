# 贡献指南

## 开始之前

非琐碎改动先建立或领取 GitHub Issue。Issue 应明确背景、范围、非目标、验收标准和依赖；本地 `.scratch/` 只用于临时材料，不是状态源。

## 分支与提交

- 从最新 `main` 创建 `codex/<slug>` 分支。
- 一个分支和一个 PR 只完成一个可独立验收的纵向能力；不要按单个函数或过细技术动作拆分 Issue。
- 提交使用英文 Conventional Commits，例如 `fix(queue): preserve request ownership`。
- 提交正文说明改动原因和验证结果，不添加 AI 工具签名。
- 默认交付终点是验证通过的本地提交；用户只说“提交”时仅创建本地 commit，并汇报分支、提交和验证结果，等待仓库所有者统一发布。

## Pull Request

- `git push`、创建或更新 PR、转 Ready、合并和删除远端分支均需用户当轮明确指令；历史任务授权或“完成”“继续”不包含这些远端动作。
- 未完成或仍在探索的 PR 保持 Draft；验收标准和必要检查通过后再转 Ready。
- 标题使用 Conventional Commit 风格。
- 正文必须且只能包含一个独立 Issue 关联行，并保留 `## 验证`、`## 风险与回退`、改动摘要和实际结果。完成票据使用 `Closes #<issue>`；不应关闭尚未完成票据的跟进修正使用 `Refs #<issue>`。
- 不在 PR、Issue、截图或日志中暴露 requestId、附件正文、账号、内部地址、密钥或 Cookie。
- 合并后确认关联 Issue 已关闭；若自动关闭失败，补充验收证据后手动关闭。

## 验证层级

1. 行为修改或 bug 修复先增加并运行聚焦测试；开发阶段不反复运行完整测试。
2. 聚焦测试通过后，静态检查与 Standards/Spec 两路代码审查并行进行；等待两路结果齐备后一次性汇总修复。
3. 修复后先运行聚焦回归，复审只检查修改增量和原发现是否关闭。
4. 最终完整测试原则上只运行一次，优先使用无业务凭据的干净 GitHub CI。OA 写入、授权、幂等和进程回收等高风险边界，最多额外运行一次本地完整测试。
5. 修改 Skill 时运行 Skill 校验；修改文档或指令时运行链接和 AGENTS 指令审计。
6. 所有改动运行 `git diff --check`，并复核待提交文件、敏感信息和磁盘余量。

测试使用精简输出；失败时再读取详细日志。测试仍处于一分钟级时，不引入 pytest-xdist、复杂 CI 分片或额外工作树。

常用完整验证：

```bash
python3 -m scripts.quality skill
python3 -m scripts.quality format
python3 -m scripts.quality hygiene
python3 -m scripts.quality links
python3 -m pytest -q --tb=short
python3 -m compileall -q apps scripts tests
git diff --check
```

仓库继续保持私有，不配置 GitHub 服务端分支保护。`main` 的分支、验证、PR、合并和清理规则由仓库所有者本地维护，具体见[main 本地管理规则](docs/agents/branch-protection.md)；不得把人工规范表述为平台强制。

真实 OA 写入、自动化启停和发布均属于独立授权动作，测试通过不等于获得执行授权。
