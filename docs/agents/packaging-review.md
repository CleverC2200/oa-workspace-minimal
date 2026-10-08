# 包装只读审核入口

新草稿通过 `supplementary_checks` 承载四项之外的逐位置关键文字异常；空数组表示没有补充异常。完整字段、页引用和整体结论规则见[FlowEngine 补充校对合同](../../.agents/skills/oa-finance-preaudit/references/flow-engine-contract.md)。该通道只承载审核者形成的观察，宿主不生成疑点。

已有同轮 `action=review_required` 的冻结包装 V2 材料时，调用 `python -m apps.oa_finance_preaudit.packaging_review --action-file <绝对 action.json 路径>`。缺材料的恢复/领取仍使用原受控入口和独立授权。本入口不领取、恢复、提交或告警，不修改冻结 attempt；开发测试使用脱敏离线材料。

审核进程沿用独立 `OA_REVIEW_API_KEY`、DeepSeek 模型及 read-only sandbox，隔离 OpenAI/Codex 宿主凭据。默认 900 秒、只可在 60～3600 秒内调整；超时和取消由原进程组回收实现处理。此次审核的空 `CODEX_HOME` 与 stdout、stderr、原决定、Schema 保存在独占的 `runtime/control/packaging-reviews/<id>/`。此入口保留原生会话记录，所以不使用会丢弃原生工具记录的 ephemeral 选项；独立 home 不读取其他聊天。

宿主只生成引用/类型合同，不做图片分类或业务判断。Schema 覆盖包装页、非包装页、不确定页、多版本及有无 Excel；空属性对象在调用前拒绝。结果只接受一个纯 JSON 或唯一 `json` 围栏，拆封只改格式，不更改字段。非 JSON、多围栏和不匹配 Schema 保留原输出并失败。宿主仅通过现有 `review_decision.bind_frozen_fields` 补齐协议绑定字段，并在前后核对冻结资料。

`draft_ready` 表示已取得可继续检查的业务草稿，**不表示已提交、业务通过或图片阅读已验证**。`tool_receipt_files` 只是原生记录的路径；必须按真实 tool call/output、页面路径与版本核对实际查看及必要复核，记录缺口，不能凭文件存在、event 计数或模型自述证明已阅读。`semantic_acceptance=not_checked` 保持到独立业务验收。Schema/正常路径离线测试不证明供应商实际接受全部形状。

确认工具证据、页面合同及业务语义后，只有已有 OA 写入授权且生产门禁完整可信时，才可把 `decision_file` 交给原 `complete-review`。该消费者继续执行完整确定性校验和受控提交；本入口不替代它。全部非包装仍由模型逐页确认，原消费者生成对应跳过意见；包装材料不得扩展为法规审核。原输出包含业务材料，仅本地受控留存，不提交 Git/Issue。
