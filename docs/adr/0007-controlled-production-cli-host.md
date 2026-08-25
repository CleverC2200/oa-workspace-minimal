# ADR 0007：在同一 JSON CLI 接入最小受控生产宿主

- 状态：已接受
- 日期：2026-08-23
- 关联：GitHub Issue #47

## 背景

FlowEngine 已具备绑定授权、外部复核和冻结提交三个端口，但此前真实 CLI 只注入 preview 依赖。仅有用户授权无法自动补齐这些端口，也不能用旧 runner、旁路脚本或直接调用 OA adapter 绕过门禁。

## 决定

继续只保留 `next-action`、`complete-review`、`status`、`prune` 和 `clean` 五个 JSON 命令。`clean --all-cache` 只维护可重建附件缓存，并要求控制器和所有 Writer 均为空闲；默认业务调用保持 preview，受控生产任务在前两个命令上分别显式传入 `--confirm-write YES`，并由同一 CLI 为 FlowEngine 注入三个生产端口。

生产授权同时要求：

- 被 Git 忽略的本地流程授权清单明确包含当前流程；
- 授权证明绑定单据版本、case、attempt、材料和证据指纹，且一小时后失效；
- `complete-review` 前再次验签并复核授权清单；
- 冻结提交适配器验证同一 attempt 下的材料、决定、意见和封套指纹；
- 真正提交前重新读取 OA 待办，确认 `requestId + received_at` 仍精确匹配；
- 提交结果必须同时证明提交成功、已办命中和待办消失，否则进入 unknown 且不自动重放。

流程授权清单只表达当前允许写入的流程，不包含单据身份或凭据。授权证明使用本地 OA 应用密钥进行域分离 HMAC；密钥仍只从被忽略的本地环境读取，不新增仓库凭据。

## 结果

- 人、Codex 和定时任务仍只理解一个 CLI 和一个 FlowEngine；
- preview 不能被审核决定或缺少确认参数的第二步提升为写入；
- 各流程可以独立授权、启用、暂停和回退；
- 不恢复旧多角色 runner，不增加 MCP、HTTP、daemon 或流程专属脚本。
