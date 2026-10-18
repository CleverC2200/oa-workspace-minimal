# ADR 0002：绑定授权、单据锁与确定性回执

- 状态：已接受
- 日期：2026-08-22
- 关联：GitHub Issue #39、#41
- 修订：动作存储与当前动作投影由 [ADR 0012](0012-canonical-attempt-state.md) 替代，下文保留历史决定

## 背景

差旅费 preview 已经把 Agent 的职责缩到“读取审核包、返回结构化业务决定”。受控提交如果继续使用流程级 Writer 或流程级锁，不同单据会无谓串行；如果授权只绑定流程名，持久文件被改动后又可能把 preview 提升成写入。

## 决定

写入资格由 FlowEngine 之外的运行环境提供，并且必须同时具备三个端口：授权源、外部 verifier、冻结提交适配器。授权严格绑定 `requestId + received_at + case_key + attempt`、材料指纹和证据指纹。FlowEngine 只保存完整授权及其指纹，生成当前 attempt 的一次性提交 token，并在提交前再次调用 verifier；审核决定不含写入模式或授权字段。

同一 attempt 的 `complete-review` 使用 attempt 目录内的文件锁单飞。不同 case 没有共享 Writer 锁，可以同时进入提交适配器。流程级锁只保护候选选择和 `collecting` claim 的原子落盘；材料收集、冻结、授权和提交都在 attempt 锁下进行。`current-action.json` 是便于人和 CLI 查看最近动作的投影，旧单据完成时不能覆盖已指向其他单据的投影。

脚本确定性生成意见、提交封套、提交 job、结果校验和脱敏执行回执。业务 `pass` 与 `needs_human` 都是可闭合结果，不改变写入资格。只有提交结果同时证明写入成功、已办命中和提交后复查命中，才记录 `write_state=submitted`；结果不可信时转为 `unknown` 且不自动重放。

最小观测事件按 attempt 保存。提交前无法保存必要事件时停止当前单据；确认提交后的观测失败不触发重写，在回执中明确记录 `observability_state=unknown`。

## 范围

本决定已闭合差旅费和业务招待费 FlowEngine 的 preview 与注入式受控写入替身。公开 JSON CLI 仍只配置 preview，不接入真实 OA 授权、凭据或提交适配器，不启动自动化。其他流程迁移和生产启用由后续 Issue 单独验收。

## 结果

- Agent 只产生业务决定，无法通过决定或文件字段自行获得写入资格。
- 写入安全边界与单据版本一致，不再使用 workflow 级通行证。
- 不同单据可并发，同一 attempt 保持幂等单飞。
- 执行回执同时表达业务、写入和观测状态，监控缺口不再被静默隐藏。
