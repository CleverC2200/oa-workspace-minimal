# ADR 0001：本地 FlowEngine 与 JSON CLI

- 状态：已接受
- 日期：2026-08-22
- 关联：GitHub Issue #38
- 修订：多单据活动、提交授权与锁粒度由 ADR 0002 替代

## 背景

项目只在本机运行。旧热路径把 Runner、Reviewer、Writer、Observer、队列动作和任务交接暴露给 Codex，导致审核者承担了身份识别、路径组织、状态协调和写入准备等确定性工作。流程之间的规则不同且没有业务依赖，因此不需要跨流程编排，也不需要 HTTP、MCP 或常驻服务。

## 决定

使用一个进程内 Python 深模块 `FlowEngine` 隐藏候选发现、材料准备、冻结校验、状态持久化和意见渲染。审核热路径只暴露两个动作：

- `next_action(workflow_key, mode)`：返回当前唯一动作；需要业务判断时写出审核包并返回其本地路径。
- `complete_review(action_file, decision_file)`：校验结构化审核决定，确定性渲染意见并返回下一动作或闭合状态。

Codex、人工 preview 和测试统一通过本地 JSON CLI 的 `next-action` 与 `complete-review` 调用这两个动作。大对象放在本地文件中，stdout 每次只输出一个版本化 JSON。执行状态、业务状态和写入状态独立表达；动作持久化 `mode=preview`、`write_authorized=false` 和 `write_state=not_requested`，审核决定不能切换运行模式。

处理尝试按递增编号保存，每类流程另保存唯一 `current-action.json`。`next_action` 先恢复现有未闭合 attempt，再读取新候选，因此待办列表变化不会制造第二个活动动作。未闭合尝试重复调用时返回同一动作和 token，不重复取数；正常闭合同时更新动作、状态和当前动作文件，后续 preview 创建新尝试而不覆盖历史。完成审核前重新验证同一 attempt 的动作、状态、审核包、材料路径和冻结指纹；不一致以持久化的 `needs_admin` 正常动作返回，不生成预审意见。动作转换使用每流程文件锁，重复或并发决定不能覆盖已闭合结果。

每类流程使用独立状态根和独立规则。一个顶层任务中的一个审核 Agent 只读取当前审核包并返回审核决定，不创建 Reviewer、Writer、Observer 或 Agent-to-Agent 交接。多单据并发由以后加入的进程池处理，而不是通过对话协调。

## 当前范围

本票只打通差旅费 preview。旧命令和旧角色路径暂时保留作为回退能力；其他流程迁移、受控写入、恢复、留存和旧热路径删除分别由后续 Issue 完成。

## 结果

- 审核上下文只包含一类流程的一份审核包，协调时间和协调 Token 为零。
- 确定性工作可由普通 Python 测试覆盖，业务判断仍由 Codex 或人工完成。
- 本地 JSON CLI 同时适合人和 Codex，暂不承担 MCP 的协议和维护成本。
- 运行目录含单据材料和动作 token，只能保存在本地忽略目录，不进入 Git。

## 后续评估 MCP 的条件

只有 JSON CLI 已稳定、出现至少两个真实调用方，并且 MCP 能显著减少解析错误或权限风险而不是增加一层重复封装时，才重新评估 MCP。HTTP API 不在本地单机项目的计划内。
