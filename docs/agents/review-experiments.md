# 受控只读对照

本入口只消费新建的私有冻结档案，不查询 OA、不恢复单据、不进入 Writer。实际单据和模型运行仍须覆盖本批的明确授权；生成计划、报告或本地提交不授予运行授权。

## 新归档的证据范围

新宿主将 `output_files_scope=review_evidence_v1` 写入结果，由
`review_archive_outputs.evidence_paths` 统一选择实际存在的提示、生成 Schema、
原决定、进程事件、stderr，以及本次声明并保留的原生 rollout。
旧 Worker 在私有 `codex-home/session.jsonl` 或 `codex-home/sessions/` 中保留的
原生 rollout 也进入证据集合；消费端只从绑定集合读取这些 JSONL。
报告消费端要求路径集合恰好匹配，再核对原字节哈希。
原生数据库、日志目录、插件和技能缓存属于运行环境，不递归绑定为新档案证据。
调用源码、解释器、依赖、CLI 和规则指纹仍由原冻结合同核对。

未声明该范围的旧档案继续核对原 `output_files` 全集；旧路径、原哈希、
失败记录及资格保持，不删缓存、不回填范围、不追认通过。
这个合同仅确定证据归属，不授权清理或新的模型调用。

反例取自修订 `43a551e6` 的宿主：其 `output_files` 对 `reviewer` 做递归全集绑定。
[归档范围回归](../../tests/finance_preaudit/test_review_archive_outputs.py)
用中性文件重现同一写法，证明新消费端拒绝缓存混入，同时旧合同仍检查原字节。

## 启动前核验

沿用单批档案合同：每侧包含 `calling-source`、当前只读 preview 动作及冻结原材料，以及声明实际修订和文件指纹的 `manifest.json`。来源必须能和开发克隆中的 Git 对象逐文件核对；全部调用源码和依赖声明均须冻结。已有 started/result/reviewer 的档案不能重用。

`python -m scripts.review_experiment freeze` 显式接受两侧档案、计划输出、开发 repository、审核器、purpose、时限和最大启动数。两个来源宿主分别准备实际 prompt、Schema、初始图片和调用配置；准备过程没有审核模型调用。仅声明不同分支、action token、临时路径或“candidate”标签不能证明优化生效。当前效果入口只允许原读取路径到完整初始输入的变化；同路径或容量回退拒绝效果调用。diagnostic 是单独声明的目的，不能自动把被拒的 effect 转成 diagnostic。

`python -m scripts.review_experiment run PLAN --authorization AUTHORIZATION` 要求私有授权记录绑定计划 SHA-256、最大启动数、非空当前授权来源和 `oa_write_authorized=false`。该记录应由本次执行的明确授权形成，不由实验入口自动生成。每侧最多启动一次，不重试；整个计划只能消费一次。时限、规则、样本、模型/供应商/推理、Schema、原图字节和准备结果的漂移都停止派发。取消和进程回收未知也停止后续侧。

## 输出与计量

复用 `scripts.review_batch_report`、`scripts.review_cost_report` 和 `scripts.review_window_report`。所有派发、失败、未知和明确未启动保留在窗口；唯一完整原生终态用量才 exact，partial 只是已观察小计。input 已包含 cache，output 已包含 reasoning，不能再次相加。非 JSON 或非对象结果仍可有 exact 失败成本，但不得形成完整业务验收，也不能修补旧决定。

计时从本次输入准备开始，到相同 FlowEngine preview 消费完成结束；不包含 OA 取数/提交，也不推算供应商请求数或账单金额。初始图片实际递交、工具图片回执、结构化文本和精确重复复用分开呈现；递交不证明正确观察。财务质量由独立人工评估及现有质量门决定，准入和记账不代替业务判断。真实运行、可比用量、财务通过和生产加载仍是独立状态。

窗口 selection 可选传入 `financial_quality_reports`，值为已保存财务质量报告的 `file + sha256` 引用列表。例如保留原 archives 与 current_archive，另加：

```json
{"financial_quality_reports": [{"file": "/absolute/private/quality.json", "sha256": "64位文件SHA-256"}]}
```

窗口只展示与所选档案唯一匹配的 case。匹配同时核验原 item_key、流程及完整 `window_binding`；schema 2 还核验新增的 `archive_manifest_sha256`。没有报告时状态为 `not_established`，引用、版本或窗口不可信时为 `unverified`。不将缺失绑定的原资格声明带入当前窗口。

输出的 `financial_quality.reports` 分别提供原格式版本、冻结合取语义、`reported_eligible` 及每个 case 的 batch_index、冻结时序、历史状态、原消费绑定、原生成合同、原逐项 check_outcomes 和事实 verdict。旧 schema 1 的独立冻结时序显示未知。资格声明仅作原报告记录，`qualification=not_granted_by_window`，不改变现有验收、执行或写入授权。具体状态见[质量核对合同](financial-quality-assessment.md#报告资格)。

## 无凭据回归

`python -m pytest tests/finance_preaudit/test_review_experiment.py tests/finance_preaudit/test_review_batch_report.py tests/finance_preaudit/test_review_cost_report.py tests/finance_preaudit/test_review_window_report.py -q` 使用受控外部进程，走实际 Worker → FlowEngine preview → 报告；不调用付费模型或 OA。现有 quality workflow 的 pytest/lint/hygiene/links 继续覆盖这些模块，未增加另一套检查平台。

### 原生模型上下文检查

已安装的固定 Codex CLI 0.160.0 可通过 `python -m scripts.quality native-contract --record /外部私有目录/native-contract.json` 检查。输出目录须已存在且仅当前用户可读，记录不能位于源码目录中或覆盖旧记录。入口不会安装或升级 CLI。缺少 CLI、版本未验证、运行失败或合同不完整时，记录为 `not_verified`，退出码为 1。

检查用中性材料、空临时 HOME、合成密钥和固定 loopback 模拟服务，沿实际 Worker 只读调用、原生 CLI、FlowEngine preview 和已有资格报告运行。长 stdout 在 CLI 事件、下一模型请求和原生上下文中分别核对。默认历史预算截断必须被检测为不完整；招待费回退的双层60000预算须让全部798条合成字段及原文首尾完整到达。回退还实际用 `view_image` 逐张读取两张不同的中性准备页，每次返回后的下一请求与原生上下文都按实际编码字节和顺序核对，缺失图片不能通过。直接供给和其他流程保留默认预算，初始图片按实际编码字节核对。合法原对象按原消费者闭合，带英文前缀的原输出保持拒绝。每个原生进程最多60秒且沿 Worker 回收，不重试。

记录绑定 CLI 路径、版本与二进制指纹、审核调用源码字节、有效调用配置和私有证据文件指纹。`python -m scripts.quality native-contract --record /外部私有目录/native-contract.json --reuse` 只核验已有记录，不进行模型协议调用。当前业务诊断派发可调用 `apps.oa_finance_preaudit.native_review_contract.readiness(record)`；仅 `status=verified` 且 `paid_dispatch_ready=true` 表示这项机制证明仍有效。源码、二进制、参数或证据变化后须重新检查，缺失或不可信的记录阻挡依赖该证明的派发。它不授予付费、OA或发布权限，计划、资料、授权与累计额度门继续独立核对。

该检查没有真实业务材料或付费模型请求，不声称操作系统全外网隔离、真实供应商格式稳定、图片内容理解或财务正确。合成 preview 的业务样本资格保持不通过。对应 pytest 入口为 [原生合同回归](../../tests/finance_preaudit/test_native_review_contract.py)。原生 CLI 不在环境中时，显式 quality 检查仍记录未验证；pytest 的原生分支跳过不能代替这份证明。

### 旧生产配置的原生观察

旧生产源码可以没有当前检查器。开发侧 `baseline_native_contract.check(record, source=..., repository=..., source_revision=..., host_file=..., binary=..., source_files=...)` 单独检查冻结的 `calling-source`，不向旧源码复制模块。`source_files` 是档案 manifest 的完整相对路径与 SHA-256 映射；检查先与该 Git 修订逐文件核对。独立子进程只加载旧 Worker、FlowEngine、规则和原参数，通过假密钥和 loopback 服务观察默认长文本读取、两张中性图片、原 JSON 消费及非法原输出拒绝。子进程和原生 CLI 不重试，取消沿各自所有者回收。

记录绑定旧源码全量指纹、原调用配置、CLI、开发侧检查器、宿主及实际证据。`readiness(record, source=..., source_revision=..., host_file=..., binary=..., source_files=...)` 仅重验绑定。宿主仅在 baseline 的 `production_code_replay` 角色使用外置检查器，candidate 保留上节完整合同。

成功状态是 `verified_baseline_observation`，`baseline_comparison_ready=true` 只表示原配置行为已核实。记录必须保留默认历史预算和实际截断，`input_complete=false`、`sample_eligible=false`、`paid_dispatch_ready=false`。它不把旧配置缺陷改称完整输入，也不授予模型调用、OA、来源质量或发布资格。配对派发仍独立检查授权、范围、资料、累计额度和终态。缺少记录、源码、参数、宿主、CLI或证据漂移时该观察不可复用。无原生 CLI 的受控外部进程回归见[基线观察测试](../../tests/finance_preaudit/test_baseline_native_contract.py)，这类回归不替代安装 CLI 的实际证明。

## 运行与核对保护

计划固定当前本地 apps Python 实现、宿主、driver、解释器和依赖清单；新进程加载前检查实际字节。calling-source 的项目指令和当前流程五份必需规则文档也必须在 Git 固定清单中，漏列不能用来绕过漂移检查。

授权可选声明带时区的 not_before / expires_at，声明后按实际宿主时间逐次核验。取消保护先阻塞信号，登记宿主 PID 和 handler 后恢复信号；保护不可用的非 main 调用拒绝启动。准备完成后的取消也停止后续模型派发。所有 execute 宿主失败，包括 communicate 超时，均回查真实启动回执；无法确认保持 unknown，不报伪零或把已启动基线列为 not_started。

原生成合同直接校验原 JSON，不借消费端的既有协议补全冒充合格。原决定确实可消费但生成合同不合格时，事实核对仍单独报告，完整质量资格保持失败。财务核对绑定当前流程完整所选规则及原消费指标窗口；同历史 attempt 的旧轮次不能仅靠相同 key 套到当前窗口，未来的核对时间不能算已完成。

原图统计中 `expected` 是原页数，`unique_images` 是独立图片字节数，`view_returned` 是实际返回的看图回执数，`covered` 是逻辑覆盖页数。只有原决定绑定的精确重复页记录、原页字节及已观察代表页均一致时，才将观察覆盖投影回重复页。一次看图仍只计一次；覆盖成立不证明模型理解正确，也不替代付款事实的业务去重判断。初始递交与实际看图回执仍分别展示。

当前候选诊断的薄私有调用者、原预算与逐单核对状态见[私有候选阶段](candidate-phase.md)。其协调逻辑归既有 review_experiment，公开配对与 effect 准入合同保持。迁移不恢复旧停止阶段或授予新模型额度。
