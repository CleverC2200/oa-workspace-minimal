# 质量检查

沿用 `scripts.quality` 和现有 GitHub quality workflow，不增加另一套检查平台。

- `payment-rules`：用 AST 读取 Worker，不导入业务代码。阻止 Worker 字符串再次写入人员关系、委托代付款或委托收款指令，报错指向共享付款规则。此检查只约束已发生的重复提示词，不判断实际材料，也不保证模型语义正确。历史 `ec9c4a1` 的原指令会触发检查；当前 Worker 通过，脱敏原句回归由 `test_quality_gates.py` 在本地及 CI 同一 pytest 入口运行。

- `yaml`：安全解析受版本控制的 YAML，拒绝重复键，采用 YAML 1.2 布尔值约定。Issue form 检查基本结构、非空标题、字段类型、唯一 ID、下拉选项及 required 布尔值；这是本项目固定合同检查，不代表 GitHub 平台的全部限制。
- `schema`：通过离线 FlowEngine 冻结六类财务包，检查真实生成器的 JSON Schema；同时检查包装 V2 生成器。拒绝供应商已实际拒绝过的空属性对象。包装页、非包装页、不确定页和 Excel 映射进入原消费者的回归保留在 pytest 中。静态有效不证明供应商已经实际接受全部分支。
- `lint`：固定 Ruff 版本，检查全部 apps/scripts/tests 的严重语法、不可成立的判断和未定义名称。规则以 `.ruff.toml` 为准；原 `format` 继续只处理换行/空白，不能宣称已经执行 Python 格式化或全面类型检查。
- `test-names`：AST 检查同一模块或类中的重复测试函数名，防止 Python 静默覆盖旧用例；不同类中的同名方法和 fixture 参数合法。移除重复定义前核对原来实际收集的用例，保留有差异的有效断言。
- `production-config`：以 `apps.oa_finance_preaudit.production_config` 的窄合同核对生产审核默认、六类财务范围、模板命令和小时手册。当前默认 OpenAI 使用 App 固定 3 个子 Agent 对话，模型及推理身份未观测时保持 unknown；每轮可显式选择 DeepSeek Flash/high 和独立 API 密钥，沿用 CLI 工具承载，固定 3 槽。OpenAI CLI 明确拒绝，历史 CLI 回执仍只读接受。外层调度保留 GPT 6.1 Sol、medium。检查只解析目标源码和文本，不导入目标代码、不读取真实认证，也不改写历史模型回执。实际自动化通过同模块的只读 `automation_findings` 另行核对原生产项目和原频率；CI 不读取本机自动化配置。

全部入口不用业务凭据，不调用真实 OA。完整测试、Skill、hygiene、links、PR 合同及原生产门禁继续保留。对标准判断和真实消费者的人工审查仍需按 CONTRIBUTING 执行。

## 离线传输与证据回归

`tests/conftest.py` 在每个 pytest 用例启动时拒绝未替换的 Python DNS、IPv4/IPv6 连接及 UDP 发送，包括同进程的线程。本地 Unix IPC 保持可用。触发 `offline_network_forbidden` 时，为该用例注入录制客户端或显式替换真实传输；拒绝使用 pytest 失败信号，业务适配器的 `except Exception` 无法把它吞成预期的读取失败。

本地和 CI 使用同一 pytest 测试集合。完整 CI 的并行配置见 [quality workflow](../../.github/workflows/quality.yml)，使用 xdist 动态分配任务。`test_offline_network_guard.py` 通过独立 pytest 调用验证漏注入的默认发票客户端、线程内 IPv4/IPv6、显式 HTTP fake 和本地 IPC。子 pytest 加载同一 conftest；普通 Python 子进程、原生外部程序、独立临时脚本及模块导入期间的操作不在此夹具保护范围内，不能由 pytest 成功推断这些操作无网络访问。新增离线回放应放到既有测试入口，独立回放仍需显式录制客户端及单独的传输保护。

完整 CI 在三个标准 Ubuntu runner 上各自独立串行收集全套节点，再按节点 SHA256 的固定三分规则分配测试，每个 runner 保持四个 worker 并动态分配测试。静态门在分片 0 执行。`scripts.pytest_evidence` 在筛选前核对完整收集，再核对本分片各 worker 的集合和每项执行次数，记录分配数量、CPU 和耗时；worker 丢失、失败、漏跑、重复或新增 skip 均失败。Linux 只允许既有文件出生时间相关的 12 项 skip，macOS 停止恢复任务继续要求全部执行、无 skip。

本地默认 pytest 通过 `tests/conftest.py` 加载同一插件，失败身份保存在 `.pytest_cache/d/failure-records/` 的独立运行文件；CI 显式证据入口则写入 `execution.json.failures.jsonl`。Linux 和 macOS 的既有 artifact 步骤均上传各自的即时失败记录。setup、call、teardown 失败报告到达时立即同步落盘，记录用例身份、阶段、worker、完整收集指纹及可用的 CI 轮次绑定，不保存异常正文。它不替代完整执行回执或作为通过依据；CI 已有同名失败记录时拒绝复用输出。`test_ci_evidence.py` 的实际 pytest 终止回归核验默认与显式入口，证明旧版在失败后中断会丢失记录，新版保留失败身份且不生成完整成功回执。

`quality-complete` 等待三个分片和 macOS 专项，按这些 job 在 `needs` 输出的 artifact ID 精确下载原始证据，不按同名列表或宽泛通配符选择，也不把上次汇总产物当作输入。原始 artifact 名含逻辑分片及 `run_attempt`；JSON 同时绑定源码、run、attempt 和 job。汇总要求所有必需任务成功，再核对三份完整收集、各 job 实际 attempt、分片归属、逐项并集恰好一次，以及 macOS JUnit 无跳过、无失败。输出缺失、产物身份不符或重跑失败时停止，不回退到旧成功。

170 秒运行预算按并行上游任务中最长的实际执行时间，加汇总任务从开始执行到完成验证的时间计算。上游起止来自[GitHub 当前 workflow attempt 的任务元数据](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt)，包含检出、依赖安装、静态门、测试、证据上传和收尾；汇总包含检出、安装、下载和元数据查询。任务集合、成功状态、源码分支 SHA、run 和 attempt 必须匹配；PR 测试源码的合并候选 SHA 仍由原 artifact 校验独立绑定。缺少、重复、错轮或无效时间证据均拒绝。汇总完成后最终回执的上传和收尾按 GitHub 完成记录另报。

完整回执分别记录运行关键路径、从最早上游开始到汇总验证的墙钟跨度，以及调度等待对关键路径的影响。分片启动错开和汇总等待执行器不计入运行预算；完整覆盖、必需任务状态和 170 秒阈值保持。真实主机的执行超限仍失败，不用本地通过或单个任务通过代替完整 hosted CI 验收。历史墙钟计时报告保持原判定，修复与验收见 [#554](https://github.com/CleverC2200/oa-workspace-minimal/issues/554)。

仅重跑失败任务时，未重跑的成功 job 可明确复用原 artifact ID 和 attempt；汇总记录 `evidence_attempts`、`artifact_ids` 与 `reused_jobs`。本 attempt 的关键路径只计本轮实际执行的最长上游任务和汇总开销；复用 job 的原始证据和 GitHub 元数据执行时长仍各自核验 170 秒预算，历史等待不计入本轮。只重跑汇总时，本轮关键路径只计汇总实际执行时间。JSON 和 JUnit 按 attempt 留存，不自动重跑失败 worker。

录制 CLI 的分类与回执用例通过共享 fixture 缩短观察间隔，仍启动真实子进程；超时用例在既有 executor 接口缩短测试期限，仍走真实超时和进程回收。`real_observation_interval` 用例保留生产 5 秒周期，核对首次错误在重试前停止。生产默认、停止门与历史证据不受这些测试参数影响。

准备超时回收夹具通过原真实执行器等待子进程 PID 和匹配阶段快照，再向调度器返回 Future，保持原 watchdog 计时与回收路径。`test_dispatcher_prepare_timeout_reclaims_owned_child_process_group` 的 1.2 秒启动延迟会使原 1 秒测试预算在对象就绪前耗尽；修复后仍使用 1 秒预算，核验真实子进程退出和超时诊断，不靠增加预算消除竞态。

证据覆盖沿用已接线的公开入口检查，不增加第二套结论规则。`test_invoice_linkage_preview.py` 核验上传附件不能代替可信 OA 关联；`test_invoice_evidence_coverage.py` 核验关联票面资料进入 collect_case、真实 Worker 冻结输入及 preview；`test_invoice_material_diagnostics.py` 核验文件记录与正文读取分别计数。这些检查不证明模型业务判断总是正确。

重复错误与旧版本验证记录见 [#419](https://github.com/CleverC2200/oa-workspace-minimal/issues/419)。旧实现未保留票面资料时，既有 collect_case 回归因缺少 `invoice_evidence` 失败；未确认 OA 关联仍判通过的旧消费端，使 preview 回归出现 `pass != needs_human`。历史代码只在私有离线检查进程加载，未替换当前工作树或历史单据材料。

## 实际模型只读回归

需要验证 Worker 的业务判断时，按[通用费用实际模型回归](../runbooks/finance-native-replay.md)准备私有冻结材料，再显式调用原生 Worker 并闭合 preview。默认 CI 只运行该入口的离线行为测试，不调用模型，不把工程通过替代实际模型验收。

同一 CLI 的 `--verify` 只读取既有原生回放和独立复核记录。逐项状态不符、独立业务复核拒绝或遗漏、绑定指纹不符及非原生记录均返回非零；原始模型结果保持业务未验收，不因生成校验报告改写。检查无法代替对原始证据和意见理由的独立判断。`test_native_replay.py` 在既有 pytest 与 CI 入口核验这些行为。[#425](https://github.com/CleverC2200/oa-workspace-minimal/issues/425) 留存真实历史错误及最终正确结果的只读拒绝/通过证明。
