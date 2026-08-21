# 单流程定时运行合同

本合同是自动化热路径。五个定时入口彼此独立，每个入口只处理一个 `workflow_key`；旧的小时 dispatcher、当天控制台、跨流程 lane 和全局 FIFO 不参与新运行。

## 五个入口

唯一配置源是 `config/flow-runners.json`：

- `oa-travel-expense` → `travel_expense`；
- `oa-business-entertainment` → `business_entertainment`；
- `oa-business-support-fee` → `business_support_fee`；
- `oa-small-marketing-fee` → `small_marketing_fee`；
- `oa-packaging-layout-approval` → `packaging_layout_approval`。

每个入口拥有独立 runner id、状态根、指标根和规则包。运行上下文只包含主 Skill、共享结果接口、Reviewer 合同和当前流程的一个规则包；不得加载其他四个流程规则包、样本或对话。

## 一轮最短路径

1. 只读拉取实时待办，并精确过滤当前 `workflow_key`；混入其他流程立即以 `flow_candidate_mismatch` 停止当前入口。
2. 0 条待办直接返回 `closure=closed`，不创建控制台、Reviewer、Writer 或等待循环。
3. 1 条待办创建一个全新 Reviewer；同类多条待办按配置上限和平台实际可用槽位建立有界审核池。一个 Reviewer 只处理一个 `requestId + received_at + attempt`，完成后结束，不复用对话。
4. prepared 单据进入有界提交池。不同 requestId 可并发，同一 requestId 按 requestId 级锁、冻结指纹、一次性 token 和持久回执单飞。
5. 当前流程没有 queued、prepared 或 submitting 后立即闭合本轮；不保留永久控制台，不空转等待下一次 cron。

调度入口只做发现、过滤、分派、容量计算、状态闭合和脱敏指标；不得读取附件、观察页面、形成意见或直接调用 OA 提交接口。

## 隔离与失败

- 状态、attempt 目录、日志和指标均以 workflow_key 隔离。不同流程没有依赖，不共享审核对话，也不建立跨流程 FIFO。
- 单据级采集、证据、格式或写入门禁失败只闭合当前 requestId；同一流程内其他安全单据继续。
- 一个流程入口配置错误、候选混入或运行失败，不得暂停、重启或改写其他四个入口。
- 只有共享 OA 认证整体失效、跨 requestId 材料串线或多个有效写入回执冲突，才暂停提交能力并上报 Admin。

## 监控与回执

每个 attempt 使用脱敏阶段事件记录 `collect、review、coordination、wait、submit、postcheck`。Token 只接收平台精确 usage，缺失记 `unknown`；指标写入失败不阻断审批。

每轮回执只包含 runner id、workflow_key、候选/成功/失败/unknown 数、实际 Reviewer/Submitter 峰值、四类耗时与 Token 汇总、closure 和 stop reason，不包含 requestId、人员、公司、附件名、附件正文或意见正文。

## 发布门

本配置只定义五个入口和 dry-run 行为，不自动创建或启用生产 cron。生产启用必须另行人工验收：逐入口验证 0/1/多待办、真实平台槽位、OA 登录、模拟提交回执和回退开关后，才可单独启用该入口。
