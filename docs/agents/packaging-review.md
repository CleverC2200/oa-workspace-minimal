# 包装只读审核入口

新草稿通过 `supplementary_checks` 承载四项之外的逐位置关键文字异常；空数组表示没有补充异常。完整字段、页引用和整体结论规则见[FlowEngine 补充校对合同](../../.agents/skills/oa-finance-preaudit/references/flow-engine-contract.md)。该通道只承载审核者形成的观察，宿主不生成疑点。

已有同轮 `action=review_required` 的冻结包装 V2 材料时，调用 `python -m apps.oa_finance_preaudit.packaging_review --action-file <绝对 action.json 路径>`。缺材料的恢复/领取仍使用原受控入口和独立授权。本入口不领取、恢复、提交或告警，不修改冻结 attempt；开发测试使用脱敏离线材料。

包装审核沿用本轮 `OA_REVIEW_PROVIDER` 与 `OA_REVIEW_TRANSPORT`。OpenAI 默认使用 App 当前对话和本轮固定 3 个已登记子 Agent，不创建第 4 个 Agent，不启动审核 CLI，不因失败切换供应商。未观测的模型、推理级别和 Token 保持 unknown。财务原控制器可信正常终态、所有只读进程已回收且 Writer 空闲后，主对话才进入包装阶段；原控制器不能重启。原生派单、来源登记和心跳见[控制器执行回执](controller-execution.md)。

显式 `deepseek` 使用原 Responses API 和 `deepseek-flash / high`，凭据仅通过 `OA_REVIEW_API_KEY` 注入，不写入配置、命令参数或证据；`requires_openai_auth=false`，每次调用使用独立、空的 `CODEX_HOME`，不读取或复制 OpenAI 认证。`--ignore-user-config` 不加载全局用户配置，记忆注入、记忆生成和全局 history 保存关闭。仓库内的 `config/deepseek-models.json` 声明图片输入和推理能力，仅在本次 CLI 加载，不改全局配置或自动化。`service_tier="default"` 保留为请求设置，DeepSeek 不提供实际档位遥测。

每次 DeepSeek 包装调用由 Worker 在自身独占证据目录创建 `visual-aux/`，权限为 0700，并在进程结束前复核目录身份、归属和私有权限。仅该子目录允许模型创建当前冻结原图的无损辅助裁图，其他目录、原图、action/state、冻结资料、源码和其他审计文件继续只读。模型使用 PIL 等工具保存 PNG 裁图，再用既有 `view_image` 读取文件路径；禁止 OCR 及 Shell Base64 分块传图。裁图沿用原版本和 `page_ref`，仅辅助读取，不成为新业务原始证据。调用者不传任意可写路径；普通 Worker 和无审计调用保持原权限。

默认 900 秒，只可在 60～3600 秒内调整。App 通过原请求截止时间和父任务心跳拒绝超期、取消或父任务丢失后的结果；DeepSeek 超时和取消由原进程组回收处理。以下原 stdout、stderr 和数据库留证仅适用于 DeepSeek CLI。本次 stdout、stderr、原决定和 Schema 保存在独占的 `runtime/control/packaging-reviews/<id>/`。原生会话保留开启，状态数据库和日志目录隔离到该证据目录。宿主只用本次 `thread.started` 的 UUID 查询本次数据库，验证原生会话的 ID 和项目路径，再复制这一份工具记录到 `tool-receipts/`；不扫描或读取其他聊天。宿主固定父目录，以不跟随链接、非阻塞方式打开，只接受普通文件；元数据首行上限 1 MiB，完整记录上限 32 MiB，并核对打开前后的文件身份、大小和时间戳。缺失、FIFO、损坏、超限、读取中被替换或不能关联当前调用的记录统一返回 `codex_tool_receipt_unavailable`，不进入 `draft_ready`；原输出仍保留，不把空工具记录当成图片阅读证据。

新回执记录供应商和执行方式，已观测的模型、effort、Token 和 `requested_service_tier` 如实保留，App 未观测时保持 unknown。DeepSeek 回执记录模型、effort 和请求档位。该档位字段是命令请求设置，不表示已经获得供应商实际服务档位遥测。历史证据保持原模型与推理级别，包括 `gpt-6.1-sol / medium` 和旧 DeepSeek 的 `high`；新审核按本轮供应商与执行方式执行，OpenAI 使用当前 App 子 Agent，未观测身份保持 unknown。模型与 effort 不匹配时拒绝。

宿主只生成引用/类型合同，不做图片分类或业务判断。Schema 覆盖包装页、非包装页、不确定页、多版本及有无 Excel；空属性对象在调用前拒绝。结果只接受一个纯 JSON 或唯一 `json` 围栏，拆封只改格式，不更改字段。非 JSON、多围栏和不匹配 Schema 保留原输出并失败。包装版本 ID 由共享合同约束为 1～80 个 ASCII 字母、数字、点、下划线或短横线；非包装和不确定页继续使用 null。原生 Schema 使用供应商支持的普通锚定正则，不含 lookaround；本地草稿解析和提交消费者继续用同一正则的 fullmatch，拒绝中文、空串、超长值和尾部换行。宿主仅通过现有 `review_decision.bind_frozen_fields` 补齐协议绑定字段，并在前后核对冻结资料。绑定后调用 `FlowEngine.validate_review_decision`，复用提交消费者的项目、整体结论、逐页分类、版本覆盖、Excel 和补充项完整合同；该入口不续期授权，不改写 attempt，也不生成提交产物。校验失败返回 `packaging_review_contract_invalid`，原模型输出保留，不修复无效业务字段。

`draft_ready` 和 `contract_validation=passed` 表示草稿绑定后已通过完整只读合同校验，**不表示已提交、业务通过或图片阅读已验证**。`tool_receipt_files` 只是原生记录的路径；必须按真实 tool call/output、页面路径与版本核对实际查看及必要复核，记录缺口，不能凭文件存在、event 计数或模型自述证明已阅读。`semantic_acceptance=not_checked` 保持到独立业务验收。Schema/正常路径离线测试不证明供应商实际接受全部形状。

## App 草稿、独立复核与完成

OpenAI 包装调用加上 `--owner-task-id <本轮主会话UUID>`。返回原工具 session 后，主对话继续心跳和 pending 派单；不等该 session 结束才处理请求。主审使用已登记的空闲槽，读取当前冻结规则和原图，输出 App envelope：`draft` 保持原包装草稿，另有 `recheck_required`、`recheck_page_refs`、`recheck_reasons`。复核条件和原因由审核者按现行图片校对 Skill 判断，宿主不生成业务疑点。

App 逐页看图需要当前派单之后、草稿发布之前的真实工具调用和配对图片输出。一个独立原生 `exec`/`functions.exec` 只执行以下一种完整形状，原页路径必须是字符串字面量，不附加注释、分支、变量替换、其他工具或其他图片输出；也接受直接 view_image 工具的真实调用和配对输出。一个调用对应一个原页。

```javascript
const r=await tools.view_image({path:"原页绝对路径"});image(r.image_url);
```

或使用：

```javascript
image((await tools.view_image({"path":"原页绝对路径"})).image_url);
```

只提到路径、旧轮次看图、无图片输出或工具失败均不能成为主审覆盖。回执只保存原生调用/输出行哈希、页引用和材料摘要，不复制 Base64。缺失任何原页观察不进入 draft_ready。

主审和独立复核 payload 都按[规范决定发布](controller-execution.md#固定-app-审核槽的原生派单)使用 draft_command / write-draft，经单次 `text(await tools.exec_command({"cmd":"完整规范命令字符串"}));` 发布。原生输出必须是已完成命令的专用发布回执，其摘要绑定实际原 JSON 字节；通用写文件或自行声明完成均拒绝。App `draft_ready` 保留主审 envelope、决定、真实来源和逐页观察，`semantic_acceptance=not_checked`。主对话核对原像素、意见及 Skill 的复核触发条件，不能只凭主审 false 布尔值判定无需复核。需要独立复核时执行：

```bash
python -B -m apps.oa_finance_preaudit.packaging_review --operation recheck --receipt-file <主审review-receipt.json> --owner-task-id <本轮主会话UUID>
```

返回请求经原 pending/followup/assign 派给另一个已登记槽；不创建新 Agent。复核提示只传当前区域和规则，避免传主审结论。复核 payload 绑定原决定 SHA-256、page_refs、outcome 和逐位置 findings，真实看图来源仍需验证。复核结果为不同或不确定时，主对话保留分歧。只有原主审决定已经将所有受影响页记录为 uncertain、overall=needs_human，才能保留该警示意见完成；宿主不会把 pass 改成 uncertain。独立读取 unavailable 必须有真实失败工具记录，受影响页仍须由主审原决定明确标为 uncertain。原主审 pass 不能靠复核失败放行，保持人工处置。

主对话形成私有语义确认 JSON 后，用原主审 request_file 和新的确认文件路径调用 draft_command / write-draft，并使用上述严格单次 exec 发布形状，字段为 `receipt_sha256`、`decision_sha256`、`accepted`、`requirements_checked`、`recheck_required`、`recheck_request_file`、`conclusion`。accepted 和 requirements_checked 必须为 true，且由主对话实际评估原页、业务结论和复核条件。无复核时 recheck_request_file 为 null；需要时指向可信的不同槽复核请求。等待该次专用发布命令实际结束并核对 request/path/字节摘要回执，保留主会话真实来源行后执行：

```bash
python -B -m apps.oa_finance_preaudit.packaging_review --operation confirm --receipt-file <主审review-receipt.json> --confirmation-file <父语义确认.json> --owner-task-id <本轮主会话UUID> --source-session-file <父原生会话jsonl> --source-line <已完成确认写入行>
```

此入口校验原决定、逐页观察、父会话身份及必要的不同槽复核，独占保存 semantic-confirmation.json，仍不写 OA。只有已有 OA 写入授权且生产门禁可信时才执行 App 包装完成入口：

```bash
python -B -m apps.oa_finance_preaudit.packaging_review --operation complete --receipt-file <主审review-receipt.json> --confirm-write YES
```

完成入口重新核验原草稿、来源、冻结材料和语义确认，再调用原 FlowEngine complete_review。原消费者确认 closed 后，入口保存独占 app-completion.json，并在同轮槽位登记其摘要，绑定语义确认、原决定、闭合 action/state 和执行回执。正常 close 会重新核对这个完成回执；主审只提交草稿、未走完成入口或文件漂移时不能结束为 completed。关闭/取消、来源漂移、主审或复核决定变更、超期、停止门或缺语义确认均不进入 Writer。不改通用手工 FlowEngine 语义。DeepSeek 继续使用下方原调用者确认门及 complete-review，不采用 App 确认回执。

确认工具证据、页面合同及业务语义后，只有已有 OA 写入授权且生产门禁完整可信时，才可把 `decision_file` 交给原 `complete-review`。该消费者继续执行完整确定性校验和受控提交；本入口不替代它。全部非包装仍由模型逐页确认，原消费者生成对应跳过意见；包装材料不得扩展为法规审核。原输出包含业务材料，仅本地受控留存，不提交 Git/Issue。
