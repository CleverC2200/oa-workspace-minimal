# 阻断处置与有限恢复

## 安全门和当前单据

生产只读查询使用生产克隆与其 Python：

```bash
.venv/bin/python -m apps.oa_finance_preaudit.cli status --check-live --reconcile --summary
```

解析完整终态 JSON。工具返回 session_id 时持续等待原会话；30 秒是工具等待窗口，不是业务命令超时。可信活动控制器只跳过重复启动。

`continuation` 是完整对账结果；领取与对账复用相同历史未知证据校验。历史 Writer 记录须绑定冻结单据、有效签名和可信阶段；旧的零 Writer preview 记录只保留既有兼容分支。未知结果保持未知，不重放。当前未决写入、身份冲突或损坏边界阻断；不能只凭错误码或边界文件存在放行。

`pending_dispositions` 按当前 `received_at` 计龄，输出单据哈希、流程、执行/写入状态、处理人、下一步、恢复次数上限、接管与解决期限。`attempt_created_at` 是首次本地尝试时间，不是 OA 流程创建时间；流程创建时间以 OA 当前权威数据为准。

普通待办超过 2 小时标为超期。隔离默认交给已配置的 OA 告警接收人，接管期限为当前 RPA 接收时间后 4 小时、解决期限为 24 小时；这些是处置目标，未自动改变 OA 状态。

## 一次跨轮恢复

用户已明确授权该范围后，唯一小时调度使用：

```bash
.venv/bin/python -m scripts.finance_preaudit_concurrency_probe \
  --rolling --adaptive --capacity 6 --confirm-write YES \
  --recover-confirmed-no-write-once YES
```

选择时同时验证本地授权清单、完整对账、实时唯一待办、未命中已办、单据级可信未写入回执及现有恢复契约。允许审核进程失败、无效决定、允许的材料局部失败和经过可信边界确认未写入的 Writer 失败。

每个单据轮次只对首个 attempt 恢复一次，新 attempt 重新采集、审核、授权、提交和后查，原证据保留。已有后续 attempt 的失败转人工。即使恢复字段被移除，attempt 大于 1 也不再次选择。不能与精确 `--recover-action-file` 参数混用，不能在只读探针使用。普通未授权调用仍跳过历史隔离。

同一单据的收集或审核在新 attempt 打开后中断，下轮依据现行冻结和安全门继续该动作，不创建第三个恢复 attempt。登录、冻结、隔离或 Writer 证据不可信时停止。

## 人工接管

处理人实际接管后，记录到对应失败 attempt 的旁路回执：

```bash
.venv/bin/python -m apps.oa_finance_preaudit.case_recovery \
  --action-file /absolute/path/to/action.json --confirm-takeover YES
```

该命令只确认最新失败 attempt 的责任归属，默认处理人为当前告警接收人；不访问或提交 OA，不恢复单据，不修改 action/state。回执绑定 state 指纹，重复确认不延长期限；实际接管后解决期限为确认时间后 24 小时。原状态改变后不复用旧接管回执。逾期必须报告处理人、原因和下一步，不能只报告隔离数量。

无效决定不得自动生成行政交接意见。AI 超时仅按现行特定交接合同处理。未知写入先由处理人核对原提交与 OA 已办、待办和签字记录；没有可信终态不得重提。

## 协议装配

业务决定完成后，可在 attempt 外的新临时目录装配冻结协议字段，再走原完成入口：

```bash
.venv/bin/python -m apps.oa_finance_preaudit.review_decision \
  --action-file /absolute/path/to/action.json \
  --input /absolute/path/to/draft.json --output /absolute/path/to/new-decision.json
```

宿主只补绑定字段，保留模型已有冲突；业务项、证据、包装页面分类和版本关系仍由审核者观察，Writer 前完整校验。包装视觉合同拒绝保存独立细码。

## 修复任务占位

生产任务使用项目修复占位入口；入口本身不创建任务、不发消息、不执行 OA。

1. `repair_dispatch status` 查询活动归属。`reserved` 检查已发起创建是否有结果；`running` 用 Codex 任务工具查询已绑定任务进度。2 小时无更新须检查与升级，不以时间删除占位。
2. `repair_dispatch reserve --window-key ... --source-thread-id ...` 先持久化窗口和活动归属，返回 reservation_id。已有窗口不重复创建任务；已有活动占位则明确报告归属和下一步。
3. 创建工具确认返回真实 threadId 后，用 `repair_dispatch bind --reservation-id ... --thread-id ...` 绑定。创建结果不明、只有临时 clientThreadId 或旧任务归属未知时保持等待，不再调用创建工具。
4. 修复任务每阶段通过 `repair_dispatch progress --reservation-id ... --thread-id ...` 记录进度。只更新自身归属。
5. 已经从任务工具确认终态后，使用 `repair_dispatch finish --reservation-id ... --thread-id ... --result completed|failed|waiting_for_user --confirm-terminal YES`。它保存窗口终态并归档该活动目录，历史窗口记录保留。同一窗口不自动重新创建任务；未完成处置给出处理人与期限。

现存无归属的空 active 目录，只有操作人已核实没有活动修复任务时，才使用 `repair_dispatch quarantine-orphan --confirm-no-active-task YES` 原子归档。非空、符号链接或未确认归属拒绝，不自动删除任何记录。

## 每轮收尾

分别报告指定恢复项、实际提交回执与后查、人工接管、隔离、未领取、当前未决写入以及进程/Writer 回收。连续两轮相同技术故障须检查同一修复任务的进度，不重复发起失败处理。告警只使用已有授权渠道和稳定错误码，原细码落盘，unknown/started 不表示送达、不自动重发。

发布同步及新进程门禁、未知写入和原证据留存见[隔离排障与恢复](../agents/quarantine-repair.md)。
