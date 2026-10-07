# 业务招待费最终生成合同：机制诊断

关联 [生成合同缺陷 #376](https://github.com/CleverC2200/oa-workspace-minimal/issues/376) 与[后续修复规格 #365](https://github.com/CleverC2200/oa-workspace-minimal/issues/365)。

**格式缺陷尚未解决。** 已确认现有 Worker 向 CLI 交付生成 Schema，CLI 在受控单轮及工具结果续轮请求中保留 Responses 的结构化输出参数；没有发现可据此修补的宿主交付或落盘缺陷。原失败的最后一轮请求及供应商约束执行情况仍未确定。本轮只交付机制证据与离线回归，不把诊断称为生成修复，也不解除回退性能票 #377 的质量前件。

## 原失败与适用来源

- 原生产回放 I03 使用 `280fc829`，原候选回放 I24 使用 `6296bb8`。两份最终消息含英文前言；现有生产消费解析器确定性返回 `invalid_json`。
- 两份原生 `agent_message` 与其 `decision.json` 原文一致；事前保存的生成 Schema 与实际 CLI 使用的 Schema 在 JSON 语义上相同。文件排版差异不表示生成约束变化。
- 两份原计划绑定的 reviewer 二进制当前仍匹配原 SHA，版本均为 `codex-cli 0.160.0`，与现有原生 CLI 解析器选择的二进制一致。
- 本轮从文档提交 `1d8f3a9` 开始诊断，当时应用字节为已测候选 `6296bb8`。交付前接入费用规则修订 `0c68460`；该修订未改变本文定位的生成命令、Schema 或落盘链。原失败字节、冻结材料、评分、用量及失败状态均未改动。两次失败退出码为零且用量精确，仍不能视为消费成功。

## 已确认的生成与消费链

| 边界 | 证据与行为 |
| --- | --- |
| Worker → CLI | `_execute_codex_raw` 保存现有 `_decision_schema`，`_codex_command` 传入 `--output-schema` 与 `--output-last-message`。没有替换业务 Schema。 |
| CLI → Responses 请求 | 本机同一版本 CLI 的受控单轮请求包含 `text.format.type=json_schema`、`strict=true`、`name=codex_output_schema`，Schema 与该诊断传入对象相同。模型仍为 `deepseek-v4-flash`，推理档位仍为 `high`。 |
| 工具结果 → CLI 续轮请求 | 另一次限定双请求观察使用现有业务招待生成 Schema 与合成动作，首轮返回 `exec_command true`，第二轮已含对应工具结果，仍保留相同 Schema 与 `strict=true`。终态合法原对象、事件与落盘对应。 |
| 原生最终消息 → 文件 | 合法受控原生对象按原文落盘；原失败的最终消息也与原文件对应。没有证据表明宿主拼接了前言。 |
| 文件 → preview | 现有严格解析拒绝前言、多对象、重复键、非有限数及错误对象；合法原决定沿既有入口消费，绑定错误仍由 FlowEngine 拒绝。 |

单轮观察使用小型合成 Schema 和受控 SSE 返回，仅验证 CLI 编码及原文落盘。临时 HOME、CODEX_HOME、占位密钥、随机 localhost 端口与操作系统网络隔离保证没有真实模型或 OA 调用。两次启动前失败分别是临时目录未创建与网络隔离表达式不合法，均捕获零个模型请求；修正启动前条件后取得一次受控请求与返回。

后续针对工具续轮是否丢约束这一具体问题，另作最多两个请求的受控观察，未作压力循环或重试真实失败。工具调用参数合法，但外层网络隔离阻止 CLI 创建嵌套 sandbox，子工具返回退出码 `71`，`true` 未真正执行。CLI 将该工具结果带入第二次请求，结构化约束仍保持，并将合法最终对象原文落盘；这证明该工具结果续轮的交付，不是成功工具执行或所有真实工具路径的证明。日志、请求原文及原失败均只保存在工作树外私有资料中。

这不是原两次运行的真实 HTTP 请求抓包。旧记录保留 CLI 终态消息及落盘文件，没有原供应商原始 SSE；消息与文件一致只能排除宿主落盘拼接，不能排除 CLI 在形成终态消息之前的流组装问题。二进制来源对应和当前受控请求可说明现有命令的编码机制，不能证明所有真实供应商路径，也不能证明原供应商接收、转换或严格执行了该约束，更不证明合成返回有财务正确性。

## 官方能力说明与剩余机制缺口

[OpenAI 官方非交互文档](https://learn.chatgpt.com/docs/non-interactive-mode) 将 `--output-schema` 用于请求符合 Schema 的最终响应，并通过 `--output-last-message` 保存最终消息。这与当前 CLI 的离线编码观察一致。

[DeepSeek Responses 接口](https://api-docs.deepseek.com/api/create-response/) 说明 `text.format` 支持 `json_schema`；[兼容性说明](https://api-docs.deepseek.com/zh-cn/guides/responses_api/) 声明完整支持 `format`。这些文档没有提供原两次请求在服务端的实际约束应用记录，不能据此断言供应商忽略了 Schema，也不能把本地交付通过升级为真实生成保证。

仍缺少以下证据：原有工具、多轮任务最后一次请求是否仍携带相同约束；供应商侧参数接收、Schema 关键字应用与约束执行结果；固定模型在同类任务中的最终消息约束行为；原服务端实际模型版本及是否发生兼容转换。当前受控观察不填补原末轮请求和原始 SSE 的缺口，不得仅凭前言或模型元数据回退提示认定其中任何一项是根因。

在供应商、模型、推理档位、原业务 Schema 和既有链路保持的约束内，目前没有已证实的格式修补依据。改成 JSON 截取、清洗前言、放宽解析、重复强调同一提示、自动重试或更换模型都不能填补该机制缺口；本轮未实施这些改动。

## 复用回归与验收边界

本轮复用了现有覆盖，没有为已经正确的消费行为添加重复测试。初始诊断以及接入 `0c68460` 后的同组聚焦回归均为 **80 passed**：

- `test_codex_review_worker` 的原文拒绝与唯一 JSON 围栏用例，覆盖前言、多对象、重复键、非有限数、非对象及原失败字节留存。
- `test_entertainment_input_delivery` 的 Worker → FlowEngine preview 用例，覆盖初始直供、原完整读取回退、原决定消费及错误动作绑定拒绝。
- `test_review_diagnostics` 的完整模块，覆盖无状态写入的失败复现、原 Schema/冻结绑定、损坏证据拒绝及业务端口禁用。
- `test_review_experiment` 的合法消费和失败成本用例，覆盖实际受控 reviewer 进程收到 Schema、精确用量、失败原文保留及不冒称 preview 成功。

这些测试证明离线交付、严格拒绝与消费接线仍正确，不证明真实模型格式或金融判断已修复。原 I03/I24 解析失败复现保留；当前完整资格仍为 No-Go，#376 不标完成，#377 不进入性能验收。新的真实生成诊断须另行冻结一致源码、原资料与调用上限并取得授权，旧四十次额度为零。本轮没有 OA 写入、远端发布、生产同步或新付费调用。

## 下一次诊断的现有 SSE 日志接线

现有原生 CLI 支持[按目标限定的 `RUST_LOG`](https://learn.chatgpt.com/docs/config-file/environment-variables)。离线 canary 确认 `codex_api::sse` 的 trace 输出包含 SSE 事件数据，供应商模拟返回的前言可在终态 `agent_message` 之前定位，并与原落盘消息对应；模拟鉴权头、密钥和请求正文 canary 未出现在日志中。[原生 OTel 事件](https://learn.chatgpt.com/docs/config-file/config-advanced)主要提供事件种类、状态、时长与用量，不能替代这份原内容证据。

本次只补现有审核进程的诊断接线：显式设置 `OA_REVIEW_SSE_TRACE=1` 时，现有 source-host 的 execute 调用传入该限定开关，Worker 只生成固定 `RUST_LOG=error,codex_api::sse=trace`。不透传调用环境中的任意 `RUST_LOG`；prepare 不传开关，未设置或设为 `0` 时不启用。没有新客户端、代理、日志平台或消费协议。

启用必须同时满足原 `read_only=True` 和既有 `audit_directory`：目录为规范绝对路径、权限对其他用户关闭，且位于当前源树及审核 workspace 之外。条件不满足时在 CLI 启动前拒绝任务。普通生产默认不启用；不能把该开关用于没有外部私有归档的正常生产审核。

原 `stderr_summary` 继续只保留字节数和固定信号；原 `audit_directory/stderr.log` 留存完整诊断数据，不新增原文到汇报 JSON。SSE 数据可能含模型返回的业务材料，只可留在工作树外私有归档，不能进入 Git、Issue、PR 或终端摘要。原最终消息、Schema、供应商、模型、`high` 档位及严格消费解析均保持；记录前言不意味着允许消费前言。

这份日志是原生 CLI 所见 SSE 事件数据，不是完整 HTTP 线字节或请求头抓包。canary 未观察到请求的 `text.format` 参数，因而下一轮仍须区分“供应商返回的前言已经可见”和“末轮实际格式约束已验证”；后者不能仅凭该日志宣称通过。

已查看[官方 CLI 0.160.0 发布记录](https://learn.chatgpt.com/docs/changelog)：其中没有建立与本次英文前言、Schema 约束或响应流组装相同根因的证据。此有限检索不能证明不存在其他问题，也不构成升级、换模型或换供应商的依据。本次未执行升级。

新增离线接线回归覆盖只读私有归档启用、非只读与非私有拒绝、默认关闭、两层 execute-only 传递、原前言拒绝与消息留存；实际 source-bound 执行验证使用受控外部 CLI，不调用真实供应商。接线随当前新修订冻结，不能把已停止的旧 `2023069` 批次或其未用额度迁移为新修订的执行授权。格式缺陷和真实金融验收仍待独立授权后的证据。

## 2026-10-07 角色与终态协议修订

最新 I26 的实际服务端 SSE 最后一个 message 标为 final_answer，包含英文覆盖说明和 JSON；其文本与落盘逐字一致。该响应回显 json_schema、strict=true 和与保存对象一致的 Schema。原消费者拒绝是正确行为，不能用提取内嵌对象改变结果。

同一原生进程 session_meta 保留了约20751字符的通用编程代理基础指令，其中要求工具前前言和长任务进度。这与单据工作进程终态仅交付 JSON 的职责存在冲突，但尚不能认定它是供应商失约的唯一原因。

本修订只对 business_entertainment 的 Worker 使用 [worker-role.md](../../.agents/skills/oa-finance-preaudit/references/worker-role.md)。通过 [OpenAI Docs model_instructions_file](https://learn.chatgpt.com/docs/config-file/config-reference) 替换通用基础指令，并在这次子进程中将 project_doc_max_bytes 设为0，隔离开发仓库的编程指令。原冻结任务仍携带完整 Skill、证据和业务规则；不修改全局配置、AGENTS、业务 Schema、模型、high、材料及工具预算。

来源宿主把角色文件加入实际调用和输入指纹。角色文件随 calling-source 保留，Git 字节及原生证明继续绑定源码。新原生检查核对实际首请求的短角色已交付且通用前言已消失，原非法输出继续拒绝，不做输出清洗。这些零付费结果只证明替换和接线，真实供应商在长工具链上是否遵守终态合同仍须独立验证，不能提前关闭#376。
