# 包装只读审核入口

新草稿通过 `supplementary_checks` 承载四项之外的逐位置关键文字异常；空数组表示没有补充异常。完整字段、页引用和整体结论规则见[FlowEngine 补充校对合同](../../.agents/skills/oa-finance-preaudit/references/flow-engine-contract.md)。该通道只承载审核者形成的观察，宿主不生成疑点。

已有同轮 `action=review_required` 的冻结包装 V2 材料时，调用 `python -m apps.oa_finance_preaudit.packaging_review --action-file <绝对 action.json 路径>`。缺材料的恢复/领取仍使用原受控入口和独立授权。本入口不领取、恢复、提交或告警，不修改冻结 attempt；开发测试使用脱敏离线材料。

审核进程通过 DeepSeek Responses API 使用 `deepseek-flash / high`，凭据仅通过 `OA_REVIEW_API_KEY` 注入，不写入配置、命令参数或证据。供应商固定 `requires_openai_auth=false`，每次调用使用独立、空的 `CODEX_HOME`；不读取或复制 OpenAI 认证。`--ignore-user-config` 不加载全局用户配置，记忆注入、记忆生成和全局 history 保存关闭。仓库内的 `config/deepseek-models.json` 声明图片输入和推理能力，仅在本次 CLI 加载，不改全局配置或自动化。`service_tier="default"` 保留为请求设置，DeepSeek 不提供该字段的实际档位遥测。

每次包装调用由 Worker 在自身独占证据目录创建 `visual-aux/`，权限为 0700，并在进程结束前复核目录身份、归属和私有权限。仅该子目录允许模型创建当前冻结原图的无损辅助裁图，其他目录、原图、action/state、冻结资料、源码和其他审计文件继续只读。模型使用 PIL 等工具保存 PNG 裁图，再用既有 `view_image` 读取文件路径；禁止 OCR 及 Shell Base64 分块传图。裁图沿用原版本和 `page_ref`，仅辅助读取，不成为新业务原始证据。调用者不传任意可写路径；普通 Worker 和无审计调用保持原权限。

默认 900 秒，只可在 60～3600 秒内调整；超时和取消由原进程组回收处理。本次 stdout、stderr、原决定和 Schema 保存在独占的 `runtime/control/packaging-reviews/<id>/`。原生会话保留开启，状态数据库和日志目录隔离到该证据目录。宿主只用本次 `thread.started` 的 UUID 查询本次数据库，验证原生会话的 ID 和项目路径，再复制这一份工具记录到 `tool-receipts/`；不扫描或读取其他聊天。宿主固定父目录，以不跟随链接、非阻塞方式打开，只接受普通文件；元数据首行上限 1 MiB，完整记录上限 32 MiB，并核对打开前后的文件身份、大小和时间戳。缺失、FIFO、损坏、超限、读取中被替换或不能关联当前调用的记录统一返回 `codex_tool_receipt_unavailable`，不进入 `draft_ready`；原输出仍保留，不把空工具记录当成图片阅读证据。

新回执记录模型、effort、供应商和 `requested_service_tier`。该档位字段是命令请求设置，不表示已经获得供应商实际服务档位遥测。历史证据保持原模型与推理级别，包括 `gpt-6.1-sol / medium` 和旧 DeepSeek 的 `high`；新审核使用 `deepseek-flash / high`。模型与 effort 不匹配时拒绝。

宿主只生成引用/类型合同，不做图片分类或业务判断。Schema 覆盖包装页、非包装页、不确定页、多版本及有无 Excel；空属性对象在调用前拒绝。结果只接受一个纯 JSON 或唯一 `json` 围栏，拆封只改格式，不更改字段。非 JSON、多围栏和不匹配 Schema 保留原输出并失败。包装版本 ID 由共享合同约束为 1～80 个 ASCII 字母、数字、点、下划线或短横线；非包装和不确定页继续使用 null。原生 Schema 使用供应商支持的普通锚定正则，不含 lookaround；本地草稿解析和提交消费者继续用同一正则的 fullmatch，拒绝中文、空串、超长值和尾部换行。宿主仅通过现有 `review_decision.bind_frozen_fields` 补齐协议绑定字段，并在前后核对冻结资料。绑定后调用 `FlowEngine.validate_review_decision`，复用提交消费者的项目、整体结论、逐页分类、版本覆盖、Excel 和补充项完整合同；该入口不续期授权，不改写 attempt，也不生成提交产物。校验失败返回 `packaging_review_contract_invalid`，原模型输出保留，不修复无效业务字段。

`draft_ready` 和 `contract_validation=passed` 表示草稿绑定后已通过完整只读合同校验，**不表示已提交、业务通过或图片阅读已验证**。`tool_receipt_files` 只是原生记录的路径；必须按真实 tool call/output、页面路径与版本核对实际查看及必要复核，记录缺口，不能凭文件存在、event 计数或模型自述证明已阅读。`semantic_acceptance=not_checked` 保持到独立业务验收。Schema/正常路径离线测试不证明供应商实际接受全部形状。

确认工具证据、页面合同及业务语义后，只有已有 OA 写入授权且生产门禁完整可信时，才可把 `decision_file` 交给原 `complete-review`。该消费者继续执行完整确定性校验和受控提交；本入口不替代它。全部非包装仍由模型逐页确认，原消费者生成对应跳过意见；包装材料不得扩展为法规审核。原输出包含业务材料，仅本地受控留存，不提交 Git/Issue。
