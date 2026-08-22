# ADR 0006：以运行事实建立调优与 MCP 准入门

- 状态：已接受
- 日期：2026-08-22
- 关联：GitHub Issue #46

## 背景

FlowEngine 已成为五类流程唯一热路径，但优化仍需要可比较的时效、质量和 Token 事实。CLI、Agent 消息和平台 usage 不都发生在 FlowEngine 内；把未观测事实推断为 0 会产生错误结论。日趋势又不需要长期保存单据哈希和原始运行证据。

## 决定

- `observability` 继续使用严格事件合同，新增单据/流程/Asia/Shanghai 日三级 P50/P95；活动/等待只接受绑定当前 item 的宿主 exact timing receipt，缺失时保持 unknown；协调开销固定为 0。
- FlowEngine 闭合回执只增加审核项、资源和页面覆盖计数及格式结果。平台没有返回精确 usage 时继续写 `unknown`，跨 attempt 自动重试与 Agent 消息也默认 unknown；只有与样本窗口完全一致的权威 runtime evidence 才可覆盖，禁止按架构意图推断为 0。
- `flow_evals` 以纯函数生成五流程验收报告和单变量前后对比，不读取 OA、不应用配置。真实 `python -m` 四命令记录 30 天脱敏调用事实；进程启动失败和人工复现由本地 launcher/harness 补充。
- 每流程 20 个闭合样本、固定质量门、性能目标和回滚条件写死在合同中，不建设策略引擎。
- MCP 默认不引入。只有样本和质量门完整、人工复现已知且 CLI 故障严格多于其他技术故障时，报告才允许提出独立 stdio MCP 后续 Issue；本次不实现 MCP。
- 原始 attempt、事件和单据指标保留 30 天；`prune` 在同一维护锁、FlowEngine 状态锁和事件锁内先完整校验，再生成不含 item_key 的日/流程观测与业务结果聚合，最后清理原始数据；日聚合保留 180 天。年龄以创建时间和最后事件时间中较晚者为准。

## 结果

正常公开入口仍是四个 JSON CLI 命令，没有新增 MCP、HTTP、数据库、daemon 或自动调参。运行数据可以回答审批耗时、等待、质量和 Token 完整性，同时无法观测的外部事实保持 unknown，生产自动化仍需逐流程单独授权。
