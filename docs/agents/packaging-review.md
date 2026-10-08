# 包装只读审核入口

新草稿通过 `supplementary_checks` 承载四项之外的逐位置关键文字异常；空数组表示没有补充异常。完整字段、页引用和整体结论规则见[FlowEngine 补充校对合同](../../.agents/skills/oa-finance-preaudit/references/flow-engine-contract.md)。该通道只承载审核者形成的观察，宿主不生成疑点。

已有同轮 `action=review_required` 的冻结包装 V2 材料时，调用 `python -m apps.oa_finance_preaudit.packaging_review --action-file <绝对 action.json 路径>`。缺材料的恢复/领取仍使用原受控入口和独立授权。本入口不领取、恢复、提交或告警，不修改冻结 attempt；开发测试使用脱敏离线材料。

审核进程默认通过原生 OpenAI 认证使用 `gpt-6.1-sol / medium`，命令显式请求 `service_tier="default"`，不开 Fast/priority。`--ignore-user-config` 不加载全局用户配置，记忆注入、记忆生成和全局 history 保存关闭；不修改用户配置或自动化。认证保留在原 `CODEX_HOME` 的凭据存储，审核工具沿只读权限配置拒绝读取该目录，凭据不复制进运行目录，也不作为命令参数或证据输出。

默认 900 秒，只可在 60～3600 秒内调整；超时和取消由原进程组回收处理。本次 stdout、stderr、原决定和 Schema 保存在独占的 `runtime/control/packaging-reviews/<id>/`。原生会话保留开启，状态数据库和日志目录隔离到该证据目录。宿主只用本次 `thread.started` 的 UUID 查询本次数据库，验证原生会话的 ID 和项目路径，再复制这一份工具记录到 `tool-receipts/`；不扫描或读取其他聊天。原生记录不能关联当前调用时失败，不把空工具记录当成图片阅读证据。

新回执记录模型、effort、供应商和 `requested_service_tier`。该档位字段是命令请求设置，不表示已经获得供应商实际服务档位遥测。历史 DeepSeek 和旧 OpenAI 证据保持原模型与 `high`，新模型只接受 `medium`；模型与 effort 不匹配时拒绝。

宿主只生成引用/类型合同，不做图片分类或业务判断。Schema 覆盖包装页、非包装页、不确定页、多版本及有无 Excel；空属性对象在调用前拒绝。结果只接受一个纯 JSON 或唯一 `json` 围栏，拆封只改格式，不更改字段。非 JSON、多围栏和不匹配 Schema 保留原输出并失败。包装版本 ID 由共享合同约束为 1～80 个 ASCII 字母、数字、点、下划线或短横线；非包装和不确定页继续使用 null。中文、空串、超长值和尾部换行在草稿 Schema 校验阶段拒绝。宿主仅通过现有 `review_decision.bind_frozen_fields` 补齐协议绑定字段，并在前后核对冻结资料。绑定后调用 `FlowEngine.validate_review_decision`，复用提交消费者的项目、整体结论、逐页分类、版本覆盖、Excel 和补充项完整合同；该入口不续期授权，不改写 attempt，也不生成提交产物。校验失败返回 `packaging_review_contract_invalid`，原模型输出保留，不修复无效业务字段。

`draft_ready` 和 `contract_validation=passed` 表示草稿绑定后已通过完整只读合同校验，**不表示已提交、业务通过或图片阅读已验证**。`tool_receipt_files` 只是原生记录的路径；必须按真实 tool call/output、页面路径与版本核对实际查看及必要复核，记录缺口，不能凭文件存在、event 计数或模型自述证明已阅读。`semantic_acceptance=not_checked` 保持到独立业务验收。Schema/正常路径离线测试不证明供应商实际接受全部形状。

确认工具证据、页面合同及业务语义后，只有已有 OA 写入授权且生产门禁完整可信时，才可把 `decision_file` 交给原 `complete-review`。该消费者继续执行完整确定性校验和受控提交；本入口不替代它。全部非包装仍由模型逐页确认，原消费者生成对应跳过意见；包装材料不得扩展为法规审核。原输出包含业务材料，仅本地受控留存，不提交 Git/Issue。
