# Changelog

本项目使用语义化版本号。未发布变更记录在 `Unreleased`。

## Unreleased

### Added

- 增加 attempt 级脱敏阶段事件，并由 `status` 按流程/小时/日期聚合；缺少精确 Token 时保持 `unknown`。
- 增加 30 天留存计划器、受限目录清理和脱敏归档/删除回执。
- 增加五类流程 20 个脱敏 golden replay 样本及离线质量、时效和 Token 比较器。
- 增加无凭据 CI、Skill/格式/链接/敏感信息检查、PR 模板和主分支保护规范。

### Changed

- 将五类流程拆成独立规则包；审核调用确认 `workflow_key` 后只加载当前流程规则和样本。
- 将 OA 提交从全局锁收紧为 requestId 级单飞；不同单据可并发提交。
- 正常入口收缩为 FlowEngine 与 `next-action`、`complete-review`、`status`、`prune` 四个 JSON CLI 命令。
- 删除旧角色、租约、队列命令、发布清单、定时 dry-run 计划器及其镜像测试；保留材料、提交安全、恢复、观测和留存能力。

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
