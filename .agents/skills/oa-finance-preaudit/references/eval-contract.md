# 离线 Golden Replay 合同

质量基线固定为 `tests/finance_preaudit/fixtures/golden_replay.json`。五类流程各包含正常、业务异常、证据不足和技术失败四种场景，共 20 个纯脱敏合成样本；不保存 requestId、人员、公司、附件正文或原始业务附件。

每个样本必须固定：

- 唯一 case id、workflow_key 和场景；
- 来源类型、可回查 reference 和版本；标为 `confirmed_feedback` 时 reference 必须存在于 `confirmed-feedback.md`；
- 期望 outcome、固定审核项状态或技术停止码；
- 用于比较的耗时与精确 Token 基线。

`apps.oa_finance_preaudit.flow_evals` 离线比较结果与基线，输出 case 数、准确率、格式合规率、按流程耗时/Token 均值与基线差、Token unknown 数，以及可定位到流程和审核项的差异。耗时和 Token 只用于观测，不得掩盖准确性或格式回归。

Golden replay 不调用模型、OA、数据库或附件连接器，不以模型自评代替业务验收，也不因运行故障自动改变规则。新增确认反馈时，先按 Skill 的反馈沉淀流程建立脱敏来源和预期结果，再更新样本。
