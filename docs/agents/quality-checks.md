# 质量检查

沿用 `scripts.quality` 和现有 GitHub quality workflow，不增加另一套检查平台。

- `storage`：核对项目空间规范的 AGENTS 入口、原生 Actions、开发预检与 CI 接线，检查已接入的材料入口并拒绝新增写死的 `.scratch` 路径。具体容量由受管写入和运行时库存执行，静态入口检查不能证明任意 shell 命令都受到文件系统配额限制。实验产物须通过受管导入保存。

附件内容复用由 `test_attachment_content_reuse.py` 经真实下载、准备和 `collect_case` 入口检查。相同名称但内容不同仍分别保存，每个附件 ID 独立读取并保留明细归属；相同正文复用下载文件，Excel 复用解析产物，不删除旧 attempt 的缓存文件。历史 67 份工作簿重复保存及重复提图的形状在旧实现上分别触发路径数量、提取次数与采集文件数量断言失败；DOCX、压缩包内图片与视频的反例保护原有子附件身份。该文件在本地按改动范围运行。

- `payment-rules`：用 AST 读取 Worker，不导入业务代码。阻止 Worker 字符串再次写入人员关系、委托代付款或委托收款指令，报错指向共享付款规则。此检查只约束已发生的重复提示词，不判断实际材料，也不保证模型语义正确。历史 `ec9c4a1` 的原指令会触发检查；当前 Worker 通过，脱敏原句回归由 `test_quality_gates.py` 在本地 pytest 入口运行。

- `yaml`：安全解析受版本控制的 YAML，拒绝重复键，采用 YAML 1.2 布尔值约定。Issue form 检查基本结构、非空标题、字段类型、唯一 ID、下拉选项及 required 布尔值；这是本项目固定合同检查，不代表 GitHub 平台的全部限制。
- `schema`：通过离线 FlowEngine 冻结六类财务包，检查真实生成器的 JSON Schema；同时检查包装 V2 生成器。拒绝供应商已实际拒绝过的空属性对象。包装页、非包装页、不确定页和 Excel 映射进入原消费者的回归保留在 pytest 中。静态有效不证明供应商已经实际接受全部分支。
- `lint`：固定 Ruff 版本，检查全部 apps/scripts/tests 的严重语法、不可成立的判断和未定义名称。规则以 `.ruff.toml` 为准；原 `format` 继续只处理换行/空白，不能宣称已经执行 Python 格式化或全面类型检查。
- `test-names`：AST 检查同一模块或类中的重复测试函数名，防止 Python 静默覆盖旧用例；不同类中的同名方法和 fixture 参数合法。移除重复定义前核对原来实际收集的用例，保留有差异的有效断言。
- `production-config`：以 `apps.oa_finance_preaudit.production_config` 的窄合同核对生产审核默认、六类财务范围、模板命令和小时手册。当前默认 OpenAI 使用 App 固定 3 个子 Agent 对话，模型及推理身份未观测时保持 unknown；每轮可显式选择 DeepSeek Flash/high 和独立 API 密钥，沿用 CLI 工具承载，固定 3 槽。OpenAI CLI 明确拒绝，历史 CLI 回执仍只读接受。外层调度保留 GPT 6.1 Sol、medium。检查只解析目标源码和文本，不导入目标代码、不读取真实认证，也不改写历史模型回执。实际自动化通过同模块的只读 `automation_findings` 另行核对原生产项目和原频率；CI 不读取本机自动化配置。

全部入口不用业务凭据，不调用真实 OA。完整测试、Skill、hygiene、links、PR 合同及原生产门禁继续保留。对标准判断和真实消费者的人工审查仍需按 CONTRIBUTING 执行。

## 离线传输与证据回归

`tests/conftest.py` 在每个 pytest 用例启动时拒绝未替换的 Python DNS、IPv4/IPv6 连接及 UDP 发送，包括同进程的线程。本地 Unix IPC 保持可用。触发 `offline_network_forbidden` 时，为该用例注入录制客户端或显式替换真实传输；拒绝使用 pytest 失败信号，业务适配器的 `except Exception` 无法把它吞成预期的读取失败。

[quality workflow](../../.github/workflows/quality.yml) 只保留一个 Ubuntu 静态 job，执行 lint、语法、敏感信息、链接以及必要 schema、生产配置和文档合同检查，保留普通 job 超时。行为测试在本机 Mac 按受影响范围运行；显式完整入口见[开发环境](dev-environment.md)。`test_offline_network_guard.py` 通过独立 pytest 调用验证漏注入的默认发票客户端、线程内 IPv4/IPv6、显式 HTTP fake 和本地 IPC。子 pytest 加载同一 conftest；普通 Python 子进程、原生外部程序、独立临时脚本及模块导入期间的操作不在此夹具保护范围内，不能由 pytest 成功推断这些操作无网络访问。新增离线回放应放到既有测试入口，独立回放仍需显式录制客户端及单独的传输保护。

本地默认 pytest 通过 `tests/conftest.py` 加载 `scripts.pytest_evidence`，失败身份保存在 `.pytest_cache/d/failure-records/` 的独立运行文件。setup、call、teardown 失败报告到达时立即同步落盘，记录用例身份、阶段、worker、收集指纹及可用的执行绑定，不保存异常正文。后续进程被终止仍保留已经发生的失败；未完成运行不能作为通过依据。`test_ci_evidence.py` 用真实 pytest 终止回归核验默认与显式入口及旧记录保护。

需要完整执行证据时，显式运行独立收集，再把该文件交给执行入口：

```bash
python -m pytest -q --collect-only --ci-evidence-file="<新收集文件>"
python -m pytest -q --tb=short --ci-expected-file="<新收集文件>" --ci-evidence-file="<新执行文件>"
```

两个调用使用相同测试选择和源码。执行入口核对可信收集、每项恰好执行一次、worker 完整性和失败状态；跳过项不能形成完整通过证明。显式失败记录写入 `<新执行文件>.failures.jsonl`，已有输出或同名失败记录拒绝覆盖。插件记录本次 pytest 耗时，不施加宿主 CI 时间预算。远端不运行全量业务 pytest、macOS 专项、跨 job 产物汇总或作业元数据查询；历史 CI 回执保持原判定。

录制 CLI 的分类与回执用例通过共享 fixture 缩短观察间隔，仍启动真实子进程；超时用例在既有 executor 接口缩短测试期限，仍走真实超时和进程回收。`real_observation_interval` 用例保留生产 5 秒周期，核对首次错误在重试前停止。生产默认、停止门与历史证据不受这些测试参数影响。

准备超时回收夹具通过原真实执行器等待子进程 PID 和匹配阶段快照，再向调度器返回 Future，保持原 watchdog 计时与回收路径。`test_dispatcher_prepare_timeout_reclaims_owned_child_process_group` 的 1.2 秒启动延迟会使原 1 秒测试预算在对象就绪前耗尽；修复后仍使用 1 秒预算，核验真实子进程退出和超时诊断，不靠增加预算消除竞态。

证据覆盖沿用已接线的公开入口检查，不增加第二套结论规则。`test_invoice_linkage_preview.py` 核验上传附件不能代替可信 OA 关联；`test_invoice_evidence_coverage.py` 核验关联票面资料进入 collect_case、真实 Worker 冻结输入及 preview；`test_invoice_material_diagnostics.py` 核验文件记录与正文读取分别计数。这些检查不证明模型业务判断总是正确。

重复错误与旧版本验证记录见 [#419](https://github.com/CleverC2200/oa-workspace-minimal/issues/419)。旧实现未保留票面资料时，既有 collect_case 回归因缺少 `invoice_evidence` 失败；未确认 OA 关联仍判通过的旧消费端，使 preview 回归出现 `pass != needs_human`。历史代码只在私有离线检查进程加载，未替换当前工作树或历史单据材料。

## 实际模型只读回归

需要验证 Worker 的业务判断时，按[通用费用实际模型回归](../runbooks/finance-native-replay.md)准备私有冻结材料，再显式调用原生 Worker 并闭合 preview。该入口的离线行为测试在本地按范围执行，不调用模型，不把工程通过替代实际模型验收。

同一 CLI 的 `--verify` 只读取既有原生回放和独立复核记录。逐项状态不符、独立业务复核拒绝或遗漏、绑定指纹不符及非原生记录均返回非零；原始模型结果保持业务未验收，不因生成校验报告改写。检查无法代替对原始证据和意见理由的独立判断。`test_native_replay.py` 在既有本地 pytest 入口核验这些行为。[#425](https://github.com/CleverC2200/oa-workspace-minimal/issues/425) 留存真实历史错误及最终正确结果的只读拒绝/通过证明。
