# 失败审核输出离线回放

用于读取已留存的失败原输出，不重新取材或调用模型。先确定原 attempt 和与拒绝回执关联的
`review-output-<SHA-256>.txt`，使用正式只读入口：

```bash
.venv/bin/python -m apps.oa_finance_preaudit.review_diagnostics \
  --raw-output-file /absolute/path/to/review-output-SHA.txt \
  --action-file /absolute/path/to/action.json
```

结果分为来源、解析、冻结绑定和 Schema 合同及完整决定合同五个阶段。来源必须匹配原字节 SHA、文件名和拒绝摘要；
解析与生产消费使用同一个实现。输出只含指纹、计数、固定错误及必要的解析位置，不含原文或动作令牌。
`failure_reproduced=true` 只表示当前错误阶段匹配已留存的拒绝分类，不证明底层历史根因或业务判断正确。

新留存的无效最终输出同时保存 `review-context-<原输出 SHA>.json`：其中是进程实际使用的 Schema 与
动作、状态、审核包、材料的原始指纹。拒绝回执引用该上下文摘要；文件权限为0600。
诊断只采用这个被关联的 Schema，不生成或替换历史 Schema，不加载凭据，不访问 OA，也不执行完成审核、恢复、Writer、告警或停止操作。
`schema_validated` 仅表示留存 Schema 与冻结绑定通过；完整决定合同明确为 `not_verified`，
包括审核项数量、证据归属及规则一致性在内的完成审核校验仍由 FlowEngine 负责。本入口不调用带状态写入的完成审核，
Schema 通过不代表完整决定合同通过，也不代表真实视觉/财务质量通过。

旧记录没有上下文时，只保留可确认的来源和解析阶段；原动作、材料被后续操作修改或已不存在时，
冻结绑定标为未验证，不能拿当前材料或 token 填补历史事实。上下文损坏、来源不匹配、符号链接或权限不可信时明确阻断。
原 attempt 与证据字节不改写，失败回归用原字节建立；没有原输出时记录缺口，不启动模型制造“历史失败”。

可通过 `--output-file` 指定新私有报告文件，父目录须已存在。输出必须位于证据树之外，且文件不得存在，
权限0600；stdout 给出报告字节数与 SHA。保存失败返回非零，不能视为成功报告。报告不进入 Git、Issue、PR 或未脱敏终端。

本入口不改变供应商、自动重试或审批调度。诊断、工程回归、模型业务验收及生产提交验收分别记录。

## 关联发票材料覆盖

排查“已关联却报缺票”时，用同一只读模块查看原 attempt 的 `material.json`：

```bash
.venv/bin/python -m apps.oa_finance_preaudit.review_diagnostics \
  --material-file /absolute/path/to/material.json \
  --output-file /absolute/path/outside-evidence/report.json
```

材料源须为普通私有文件，权限0600；拒绝符号链接、非普通文件与超大输入。报告沿用上方外置、文件不已存在及0600约定。材料模式与原输出模式互斥，不接受 `--action-file`，不连接 OA、不调用模型、不创建 attempt、不修改原材料。

摘要只输出资料指纹、字节数、固定状态与覆盖计数。不会输出票号、金额、公司、姓名、账号、令牌、正文、URL或原失败信息。逐票来源一致计数仅说明冻结材料中的来源路径指向当前关联字段，且显示票号及已读发票号码相符；`source_verification=not_performed` 表示没有再次核验 OA、发票云或冻结指纹。此处没有业务通过结论。

`legacy_not_recorded` 表示旧材料没有扩展记录，`not_recorded` 表示采集器未记录扩展内容。两者都不代表申请人没提供发票。`read_failed`、`unknown`、逐票来源不一致与字段 `not_provided` 分别保留。材料整体降级的 `collection_gap_count` 只统计缺口，不打印原错误文字。

文件记录计数不证明文件下载、正文读取或归属之外的审核适用性。当前采集合同的正文状态是 `not_read`；其他未定义的正文状态进入 unknown，不推断已读。普通附件存在也不自动计入关联发票正文覆盖。先看同一关联条目的权威字段覆盖，再定位具体正文缺口；有购买方不代表金额、票种或其他内容均已传给审核者。

当前生产版本用生产项目的 `git rev-parse HEAD` 及实时 `status` 核对，不用旧截图或本地测试版本代替。原输出工具事实的活动/归档定位见[原会话证据导航](session-evidence.md)。工程回归与真实模型审核质量分别验收。
