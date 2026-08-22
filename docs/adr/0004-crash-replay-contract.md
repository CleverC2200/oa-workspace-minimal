# ADR 0004：崩溃恢复与确定性重放

## 决定

差旅深模块的每次调用都从 attempt 目录中的原子 JSON 重新判断下一步，不依赖进程内存或对话历史。单据版本固定为 `request_id + received_at` 的哈希；同一版本只有在上一次 preview 回执包含 `confirmed_no_write` 时才能创建新 attempt。

| 最后可信边界 | 新进程动作 | 是否可再次提交 |
| --- | --- | --- |
| 尚未形成 attempt | 重新发现并创建 attempt 1 | 不涉及 |
| `collecting` 已落盘 | 在同一 attempt 重新准备 | 不涉及 |
| 审核决定已落盘 | 只接受相同决定继续，冲突拒绝 | 尚未提交时可以继续 |
| `submitting` 已落盘、无可信回执 | `needs_admin + write_state=unknown` | 否 |
| 提交返回或后置校验完成、无可信回执 | `needs_admin + write_state=unknown` | 否 |
| 完整执行回执已原子落盘 | 校验冻结指纹后直接闭合 | 否 |
| closed action/state 部分落盘 | 由完整回执重建 closed 状态 | 否 |

共享授权与提交适配器故障停止当前流程；单据材料等技术故障只停止当前单据，后续单据仍可继续。Python 深模块和本地 JSON CLI 复用同一状态机，不另建恢复逻辑。

## 结果

- `write_state=unknown` 不自动恢复为可写状态，也不自动增加 attempt。
- 回执缺失与回执不可信采用相同的保守边界；只有外部核验可以解除未知写入。
- replay 测试覆盖发现、准备、决定、提交开始、提交返回、后置校验、回执和闭合边界。
