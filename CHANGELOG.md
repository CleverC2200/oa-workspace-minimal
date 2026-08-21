# Changelog

本项目使用语义化版本号。未发布变更记录在 `Unreleased`。

## Unreleased

### Added

- 增加 attempt 级脱敏阶段事件、精确 Token usage 补录和按流程/小时/日期聚合。
- 增加五个相互隔离的单流程定时入口配置、dry-run 计划器和运行合同。
- 增加 30 天留存计划器、受限目录清理和脱敏归档/删除回执。
- 增加五类流程 20 个脱敏 golden replay 样本及离线质量、时效和 Token 比较器。
- 增加无凭据 CI、Skill/格式/链接/敏感信息检查、PR 模板和主分支保护规范。

### Changed

- 将五类流程拆成独立规则包；Reviewer 在确认 `workflow_key` 后只加载当前流程规则和样本。
- 将 OA 提交从全局 Writer 锁收紧为 requestId 级单飞和有界并发提交池。
- 新自动化热路径不再使用跨流程 FIFO、永久控制台或共享审核对话。

## 1.1.0 - 2026-08-21

### Added

- 建立 OA 财务预审与包装文字校对的首次 GitHub 私有发布基线。
- 增加 Issue、Sub-issue、原子 PR、Conventional Commit 和验收闭环规范。
- 增加安全配置示例、敏感文件忽略规则和安全报告说明。

### Changed

- 数据库地址、账号和 Keychain 服务改为只从本地环境读取，不再提供部署默认值。
- README 与 AGENTS 只描述当前可验证事实，并把后续架构改造交给 GitHub Issues 追踪。

### Security

- 远程 `main` 从当前已脱敏代码生成新的根提交，不上传包含历史运行材料的本地 Git 历史。
