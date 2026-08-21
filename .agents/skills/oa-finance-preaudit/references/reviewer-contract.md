# 单据审核 Agent 合同

本角色只处理一个 assignment，不执行队列调度、任务创建/归档或 OA 写入。

## 必须读取

1. 主 `SKILL.md`；
2. [证据阅读规则](observation-contract.md)；
3. 控制台传入的原样 `assignment.json` 与本角色 release 指纹。

采集成功并精确确认材料中的 `workflow_key` 后，完整读取[共享结果接口与流程路由](result-contract.md)，只读取该 `workflow_key` 对应的一个流程规则包；不得读取其他流程规则包。流程规则包要求附加 Skill 时再按其路由读取；因此只有 `workflow_key=packaging_layout_approval` 才能加载包装问题集或包装视觉提示。

启动时用唯一 canonical 接口计算本角色复合指纹：

```bash
python3 -m apps.oa_finance_preaudit.role_cli reviewer release-manifest \
  --project-root /Users/synear/HermesWorkspace/profiles/oa/projects/oa-workspace-minimal
```

只比较返回的 `sha256` 与 canonical `assignment.json` 顶层 `reviewer_release_sha256`。该字段由 `role_cli controller claim --reviewer-release-sha256` 在原子领取时冻结；canonical `role_cli reviewer` 会在采集或封存前机械拒绝缺失或格式异常的冻结指纹，Reviewer 仍须比较值是否与启动时加载的 release manifest 一致。缺失或不一致时不得采集或封存，直接上报 `reviewer_release_fingerprint_missing` 或 `reviewer_release_fingerprint_mismatch`。不得拿当前新 generation 的 manifest 否定已经按旧 generation 启动的 Reviewer；旧代际须按其冻结指纹自然闭合。不得对单个文件运行 `shasum`，也不得自行选择文件或重建哈希算法。

不读取自动化批次总契约、dispatcher/controller/writer 合同、旧意见、财务意见、回归案例或其他 requestId 材料。
禁止在项目根执行宽泛 `rg`、`find` 或全文搜索来发现运行契约；本合同已给出唯一闭合格式。实现核对只允许读取 reviewer release manifest 已列出的文件，不得让搜索结果带入其他角色文档。

## 执行

- 核对 assignment 的 `request_id、received_at、isolation_key、attempt、worker_id、lease_token` 和绝对路径；每个审核对话只处理一个 assignment。
- 控制台消息或 epoch 目录中的 claim/assignment 归档副本只用于审计记录。`collect-assignment`、`collection-status` 和 `seal-review` 的 `--assignment-file` 只能传入 payload 内嵌的绝对 `assignment_file`；不得传入 epoch 归档副本，也不得把收到消息或归档副本的本地路径当成 canonical assignment。执行前先确认该内嵌路径存在并与当前 `request_id + received_at + attempt` 一致。
- 从项目根只调用一次 canonical 采集命令；它从 assignment 读取 requestId、附件目录、准备目录和材料路径，不得自行拼接 `collect-case`、切换到 request_root 作为模块 cwd、增加整进程 180 秒终止器或复制 stdout：

```bash
cd /Users/synear/HermesWorkspace/profiles/oa/projects/oa-workspace-minimal && \
python3 -m apps.oa_finance_preaudit.role_cli reviewer collect-assignment \
  --assignment-file ABSOLUTE_ASSIGNMENT_JSON
```

- `preaudit_assignment_material_written` 表示可读取 assignment 指定的 `material_file`；`preaudit_assignment_collection_failed` 表示读取其脱敏 `failure_code`，以同一码执行 blocked 闭合。工具返回 `session_id` 表示原命令仍在运行，必须只对同一个 session 持续调用 `write_stdin`，直到返回明确 `exit_code`；在此之前不得调用 `collection-status`、结束 reviewer 或报告采集失败。canonical 命令未终止时只等待原调用，不得另设整进程超时、启动第二次采集或凭空使用 `collection_material_missing`。每个 attempt 的下载目录、准备目录与采集缓存均由该命令按 assignment 隔离。
- 若已确认原 canonical 采集进程结束，但任务传输层没有返回 stdout，只允许执行一次只读状态恢复命令；该命令只读取同一 attempt 的 `material_file` 或脱敏 `collection-failure.json`，绝不重新采集：

```bash
cd /Users/synear/HermesWorkspace/profiles/oa/projects/oa-workspace-minimal && \
python3 -m apps.oa_finance_preaudit.role_cli reviewer collection-status \
  --assignment-file ABSOLUTE_ASSIGNMENT_JSON
```

`collection-status` 返回的两种 kind 与原采集命令相同。若原 canonical 采集进程仍持有 attempt 级采集锁，它必须返回 `collection_process_active`；继续等待原进程，不得结束 Reviewer、上报材料不可用或再次调用状态恢复。若进程已退出后返回 `collection_result_unavailable` 或 `collection_result_ambiguous`，保持原租约并上报 Admin；不得补跑采集或自造失败码。
- 完整观察当前材料，由 Codex 按主 Skill、共享结果接口和唯一流程规则包判断，只写 assignment 指定的 `remark_file`。封套、指纹、身份字段和结果 JSON 均由 canonical 闭合命令生成，审核 Agent 不手工计算或填写。
- 附件下载、结构化解析与视觉观察可以使用项目辅助能力；不得让脚本生成业务判断。只有模糊且高影响的事实才增加一次隔离观察，具体升级条件以当前流程规则包为准。
- 记录 collection、observation、judgment、preparation_total 秒数，以及页数、视觉单元数和观察 Agent 峰值。时间来自本 attempt 的单调时钟，不从聊天估算。

## 唯一闭合接口

意见完成后只调用 `seal-review`。`prepared` 时该命令校验材料和固定意见格式，自动生成带 `sha256:` 前缀的封套与结果，并用原租约原子闭合；技术失败时将 `--outcome` 改为 `blocked`、`--result-code` 改为 canonical 采集返回的脱敏码。七个计时参数均为非负整数，`preparation-total-seconds` 不小于三个阶段中的任一值：

```bash
cd /Users/synear/HermesWorkspace/profiles/oa/projects/oa-workspace-minimal && \
python3 -m apps.oa_finance_preaudit.role_cli reviewer seal-review \
  --assignment-file ABSOLUTE_ASSIGNMENT_JSON \
  --outcome prepared --result-code prepared \
  --collection-seconds N --observation-seconds N --judgment-seconds N \
  --preparation-total-seconds N --pages-total N --visual-units N \
  --observer-agents-peak N
```

该命令统一校验租约、身份、路径、材料、意见、封套和计时，再原子闭合队列。失败时返回脱敏 failure code；不得退回旧的手工结果包闭合路径，不得为补错误码重复采集，也不得手工改队列。最终只回传 `ITEM_RESULT` 或 `OPS_EVENT`，不包含附件正文。
