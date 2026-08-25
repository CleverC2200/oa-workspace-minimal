# ADR 0005：收缩旧多角色热路径

- 状态：已接受
- 日期：2026-08-22
- 关联：GitHub Issue #45

## 背景

五类流程均已迁移到 FlowEngine 后，仓库仍保留角色 CLI、底层队列 CLI、租约、assignment、发布清单和定时 runner。静态调用审计显示，这组模块的非测试调用者只存在于组内互相转发；FlowEngine 仅借用旧队列模块的审核项名称和材料校验函数。继续保留会形成第二套接口和第二套状态模型。

## 决定

把审核项名称和材料校验收回 FlowEngine，删除无外部运行调用者的旧编排闭包及其镜像测试。正常公开 CLI 固定为五个命令：

- 审核：`next-action`、`complete-review`；
- 运维：`status`、`prune`、`clean`。

候选发现、材料收集、冻结指纹、attempt 锁、一次性提交、后置校验和回执继续由 FlowEngine 及其内部实现承担。Skill 只保留证据读取、流程路由、固定审核项、决定结构和双动作步骤。

`observability` 与 `flow_evals` 是深模块测试和离线质量分析的内部模块，不是正常运行入口；生产启用仍需逐流程单独验收和明确授权。

## 删除证据

删除前的仓库级引用审计结果：

- `queue_cli` 只被 `role_cli` 和旧 runtime snapshot 引用；
- `role_cli`、`review_attempt`、`runtime_snapshot`、`live_inputs` 和 `submission_pool` 没有旧编排闭包之外的运行调用者；
- `flow_runner` 只被旧 tuning、测试和 README dry-run 引用；
- `elastic_queue` 的新路径调用仅为审核项名称与材料校验，二者已收回 FlowEngine；
- 旧编排测试主要穿过已删除接口；其中仍有效的材料安全、规则和回归数据断言已迁移到深模块与 Skill 合同测试。

因此删除的是无当前运行调用者的旧接口与转发实现，不删除 OA adapter、材料采集、提交适配、观测、恢复或留存能力。

## 结果

- 人和 Codex 只需理解一个深模块、一个 JSON CLI 和五个命令；
- 单据审核不再承担角色协调、队列动作或运行目录组织；
- 五类流程继续复用同一状态机和写入安全门；
- 测试以深模块接口为主，只保留少量 CLI 与 Skill 合同测试。
