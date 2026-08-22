---
name: lark-sync-oa-approval-status
description: 使用项目 owned CLI，把一个或多个 Weaver OA requestId 作为飞书三方审批 instance_id，以默认 lark-cli profile 的 Hermes小念应用将实例状态同步为 APPROVED，并执行写后校验。用户要求“同步 OA 状态到飞书”“更新飞书三方审批实例”或连续提供 OA requestId 进行状态补同步时使用；不用于读取财务材料、审批 OA、创建 automation，或同步 PENDING、REJECTED、CANCELED、DELETED 状态。
---

# 同步 OA 审批状态到飞书

复用 `apps.oa_approval.feishu_external_instance`，不要重新拼接 HTTP、读取 token 或复制飞书连接层。该 CLI 只写飞书三方审批中心，不写 OA。

## 0. 目标门禁

- 执行效果固定为 `external-write`，状态固定为 `APPROVED`；不自动查询、推断或映射其他状态。
- 真实同步必须有仍然有效的明确写入意图。只有孤立编号时先 dry-run，再询问是否真实同步。
- `instance_id` 必须是唯一 OA requestId；模糊编号或业务单号不得猜成 requestId。
- 不改变 approval code、默认 profile、bot 身份、跳转链接或请求体契约；完整固定契约见[结果与回执契约](references/result-contract.md)。
- 不保存或输出 appSecret、accessToken、Cookie、用户凭据或完整认证响应。

## 1. 输入与证据

提取用户提供的全部 requestId，去重并保持原顺序。每个 requestId 必须是非空数字字符串，不得自行追加。

先读取可用的 `$lark-shared` Skill并遵守认证规则，然后在仓库根目录执行：

```bash
LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1 \
LARKSUITE_CLI_NO_SKILLS_NOTIFIER=1 \
lark-cli auth status --profile default --json --verify
```

只确认结果契约规定的 app、bot 状态和验证结果。profile 不存在、应用不匹配或 bot 不可用时停止；不切换 user 身份，不重新登录，不从环境变量读取 tenant token。

## 2. 计划或判断

对每个唯一 requestId 顺序执行 dry-run：

```bash
python3 -m apps.oa_approval.feishu_external_instance \
  --request-id REQUEST_ID \
  --profile default \
  --dry-run
```

dry-run 只证明请求构造与本地调用契约通过，不代表飞书端已经更新。逐项核对 `instance_id`、`status=APPROVED` 和时间戳；公共 profile、认证、路径或请求体契约错误时停止整个批次，单个 requestId 格式错误只标记该项。

## 3. 执行效果

只有写入意图明确且对应 dry-run 通过后，才顺序执行：

```bash
python3 -m apps.oa_approval.feishu_external_instance \
  --request-id REQUEST_ID \
  --profile default \
  --confirm-write YES
```

不得并发，不得绕过 `--confirm-write YES`，不得改用 `curl`、`urllib` 或手工 token。接口写入返回成功只表示执行效果已发生，尚不能单独作为最终成功证据。

## 4. 后置验证与回执

真实写入后，owned CLI 必须使用同一 `instance_id` 和 `update_time` 调用 `/open-apis/approval/v4/external_instances/check`。只有 `diff_instances` 为空，才生成 `external_instance_sync_receipt` 且令 `postcheck.done=true`、`postcheck.current=true`。

dry-run 回执必须令 `postcheck.done=false`、`postcheck.current=null`。检查接口返回差异、缺少 `diff_instances` 或无法读取时，均不得声称同步完成。稳定回执字段与逐项报告格式见[结果与回执契约](references/result-contract.md)。

## 5. 反馈沉淀

用户明确指出同步结果、身份、状态、实例归属或回执判断错误时，已经构成反馈，不要求重复确认。完整读取[已确认反馈清单](references/confirmed-feedback.md)，将反馈脱敏后映射到固定契约、失败规则或测试；不能稳定复现时标为待样本，不直接改动执行口径。

没有明确错误反馈时，不为运行成功自动生成新规则，也不把 API envelope 当作业务反馈。

## 6. 错误回归

[错误回归案例](references/regression-cases.json)只保存真实、脱敏且可重放的错误案例，至少包含最小输入事实、旧错误结果、正确结果、失败模式和规则落点。正常同步不读取案例集；只有复盘、修复或维护测试时才读取，避免把历史答案混入本次判断。

需要改变固定契约或修复错误时，先增加聚焦案例/测试，再最小修改 `apps/oa_approval/feishu_external_instance.py`，运行：

```bash
python3 -m pytest tests/test_feishu_external_instance.py -v
python3 -m pytest -v
git diff --check
```

## 7. 技术失败

按[停止与失败规则](references/error-handling.md)区分公共故障、单项故障和后置校验失败。公共认证、应用、权限或契约失败时停止后续实例；单个实例特有错误记录后可继续其他独立实例。不要无条件重试，不把非零退出码、dry-run 或仅写入成功当作闭环成功。

不要把本 Skill 接入生产 automation、OA runner 或财务预审流程，除非用户另行明确授权。
