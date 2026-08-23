# 离线 Golden Replay 合同

质量基线固定为 `tests/finance_preaudit/fixtures/golden_replay.json`。五类流程各包含正常、业务异常、证据不足和技术失败四种场景，四类财务流程再增加材料采集降级场景，共 24 个纯脱敏合成样本；不保存 requestId、人员、公司、附件正文或原始业务附件。

每个样本必须固定：

- 唯一 case id、workflow_key 和场景；
- 来源类型、可回查 reference 和版本；标为 `confirmed_feedback` 时 reference 必须存在于 `confirmed-feedback.md`；
- 期望 outcome、固定审核项状态或技术停止码；
- 用于比较的耗时与精确 Token 基线。

`apps.oa_finance_preaudit.flow_evals` 离线比较结果与基线，输出 case 数、准确率、格式合规率、按流程耗时/Token 均值与基线差、Token unknown 数，以及可定位到流程和审核项的差异。耗时和 Token 只用于观测，不得掩盖准确性或格式回归。

Golden replay 不调用模型、OA、数据库或附件连接器，不以模型自评代替业务验收，也不因运行故障自动改变规则。新增确认反馈时，先按 Skill 的反馈沉淀流程建立脱敏来源和预期结果，再更新样本。

## 调优验收报告

`build_acceptance_report` 只接收脱敏闭合样本、golden replay 结果和本地 CLI 验收回执，输出单据、流程、Asia/Shanghai 日三级指标。每流程至少 20 个闭合样本才具备调优资格；质量门固定为审核项/资源/页面/格式/replay 100%，重复提交、跨单据混用、unknown 自动重试、Agent-to-Agent 消息和协调开销为 0。FlowEngine 无法观察的跨 attempt 自动重试与 Agent 消息默认是 `unknown`，只有起止时间与本批样本完全一致的权威 runtime evidence 才能覆盖，禁止按架构意图推断为 0。初始目标固定为 P95 端到端小于 900 秒、等待比例小于 20%、技术错误率不高于 2%。

`evaluate_experiment` 只允许频率、审核并发、提交并发或审核包指纹中的一个变量变化，不应用配置。replay 或格式回归、安全计数非零、技术错误率增加至少 2 个百分点、P95 恶化超过 20%或 unknown 增加时固定回滚。任一 Token usage 不是平台 exact 时只停止 Token 调优，禁止估算。

真实 `python -m` 入口把四命令的成功、参数、JSON 合同和文件传递结果写入本地 30 天脱敏日志；status/prune 每次只写一条 `global` 调用，按流程展示时投影影响范围，overall 调用数和故障数不得重复。进程尚未启动时的失败和人工复现由本地 launcher/harness 补充。所有回执不含 argv、stderr、路径或正文。样本不足、人工复现未知或 CLI 故障不严格多于其他技术故障时，MCP 结论固定为 `do_not_introduce_mcp`；准入也只生成独立 stdio MCP 后续提案，不创建或安装 MCP。
