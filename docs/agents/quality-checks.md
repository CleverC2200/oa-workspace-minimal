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

完整 CI 在两个标准 Ubuntu runner 上各自独立串行收集全套节点，再按节点 SHA256 的固定二分规则分配测试，每个 runner 使用动态 worker 分配。静态门在分片 0 执行。`scripts.pytest_evidence` 在筛选前核对完整收集，再核对本分片各 worker 的集合和每项执行次数，记录分配数量、CPU 和耗时；worker 丢失、失败、漏跑、重复或新增 skip 均失败。Linux 只允许既有文件出生时间相关的 12 项 skip，macOS 停止恢复任务继续要求全部执行、无 skip。

`quality-complete` 等待两个分片和 macOS 专项，下载同一轮 artifact，要求所有必需任务成功，再核对两份完整收集、源码与 run 身份、分片归属、逐项并集恰好一次，以及 macOS JUnit 无跳过、无失败。汇总从最早必需任务的首个 step 计时，包含准备、运行、上传及汇总启动，保留收尾预算。JSON 和 JUnit 随工作流留存，不自动重跑失败 worker；实际全工作流耗时仍按 GitHub 完成记录验收。

录制 CLI 的分类与回执用例通过共享 fixture 缩短观察间隔，仍启动真实子进程；超时用例在既有 executor 接口缩短测试期限，仍走真实超时和进程回收。`real_observation_interval` 用例保留生产 5 秒周期，核对首次错误在重试前停止。生产默认、停止门与历史证据不受这些测试参数影响。

耗时验收以真实 GitHub workflow 的全部必需任务完成时间为准，排队时间另列。工作流从首个 step 计时并留出收尾预算；本地测试耗时及 step 预算通过不能代替完整 CI 在 180 秒内完成的验收。尚未推送或实际运行未达标时，[#478](https://github.com/CleverC2200/oa-workspace-minimal/issues/478) 保持打开。

证据覆盖沿用已接线的公开入口检查，不增加第二套结论规则。`test_invoice_linkage_preview.py` 核验上传附件不能代替可信 OA 关联；`test_invoice_evidence_coverage.py` 核验关联票面资料进入 collect_case、真实 Worker 冻结输入及 preview；`test_invoice_material_diagnostics.py` 核验文件记录与正文读取分别计数。这些检查不证明模型业务判断总是正确。

重复错误与旧版本验证记录见 [#419](https://github.com/CleverC2200/oa-workspace-minimal/issues/419)。旧实现未保留票面资料时，既有 collect_case 回归因缺少 `invoice_evidence` 失败；未确认 OA 关联仍判通过的旧消费端，使 preview 回归出现 `pass != needs_human`。历史代码只在私有离线检查进程加载，未替换当前工作树或历史单据材料。

## 实际模型只读回归

需要验证 Worker 的业务判断时，按[通用费用实际模型回归](../runbooks/finance-native-replay.md)准备私有冻结材料，再显式调用原生 Worker 并闭合 preview。默认 CI 只运行该入口的离线行为测试，不调用模型，不把工程通过替代实际模型验收。

同一 CLI 的 `--verify` 只读取既有原生回放和独立复核记录。逐项状态不符、独立业务复核拒绝或遗漏、绑定指纹不符及非原生记录均返回非零；原始模型结果保持业务未验收，不因生成校验报告改写。检查无法代替对原始证据和意见理由的独立判断。`test_native_replay.py` 在既有 pytest 与 CI 入口核验这些行为。[#425](https://github.com/CleverC2200/oa-workspace-minimal/issues/425) 留存真实历史错误及最终正确结果的只读拒绝/通过证明。
