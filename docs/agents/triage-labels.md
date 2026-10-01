# GitHub 标签

## 工作状态

| 标签 | 含义 |
| --- | --- |
| `needs-triage` | 维护者需要评估范围、优先级或依赖 |
| `needs-info` | 等待补充必要信息 |
| `ready-for-agent` | 规格和验收完整，可开始实现 |
| `ready-for-human` | 需要人工权限、判断或现场操作 |
| `wontfix` | 已决定不处理 |

## 分类

- 优先级：`priority:p0`、`priority:p1`、`priority:p2`
- 领域：`area:rules`、`area:automation`、`area:submission`、`area:observability`、`area:retention`、`area:quality`
- 类型优先使用 GitHub 默认 `bug`、`enhancement`、`documentation`。
- 咨询用现有 `question`；其他有价值事项按实际类型与领域分类。

## 已确认无需行动

`non-actionable` 是快速排除无需缺陷修复项的筛选标签，不代替问题类型或具体处置原因：

- 重复记录：加 `duplicate`，关联主票；主票不因此关闭。
- 已证实为预期行为或报告不成立：加 `invalid`，说明预期行为和证据。
- 收益不足、超出当前范围或明确不做：由维护者确认后加 `wontfix`，写明决定；低优先级本身不等于无需行动。
- 只在上述结论明确时加 `non-actionable`；关闭无需行动的票选择“不计划处理”（API `not_planned`，`gh issue close --reason 'not planned'`），保留记录。

缺证据、尚未复现、根因未明或生产验收未齐时使用 `needs-info` / `needs-triage`，不得标为 `non-actionable`。历史未知写入的人工核对、原始业务单据及其处理范围不受工程标签影响。重新确认存在可行动问题时移除 `non-actionable`，按 [Bug 记录规范](bug-ledger.md) 重开或关联正确主票。

同一 Issue 只使用一个优先级；可使用多个领域标签。依赖未满足时不得添加 `ready-for-agent`。
