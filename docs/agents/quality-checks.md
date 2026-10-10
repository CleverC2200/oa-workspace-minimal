# 质量检查

沿用 `scripts.quality` 和现有 GitHub quality workflow，不增加另一套检查平台。

- `yaml`：安全解析受版本控制的 YAML，拒绝重复键，采用 YAML 1.2 布尔值约定。Issue form 检查基本结构、非空标题、字段类型、唯一 ID、下拉选项及 required 布尔值；这是本项目固定合同检查，不代表 GitHub 平台的全部限制。
- `schema`：通过离线 FlowEngine 冻结六类财务包，检查真实生成器的 JSON Schema；同时检查包装 V2 生成器。拒绝供应商已实际拒绝过的空属性对象。包装页、非包装页、不确定页和 Excel 映射进入原消费者的回归保留在 pytest 中。静态有效不证明供应商已经实际接受全部分支。
- `lint`：固定 Ruff 版本，检查全部 apps/scripts/tests 的严重语法、不可成立的判断和未定义名称。规则以 `.ruff.toml` 为准；原 `format` 继续只处理换行/空白，不能宣称已经执行 Python 格式化或全面类型检查。
- `production-config`：以 `apps.oa_finance_preaudit.production_config` 的窄合同核对生产审核默认、六类财务范围、模板命令和小时手册。当前合同为原生 GPT 6.1 Sol、medium、普通服务档，起始 3、最高 6。检查只解析目标源码和文本，不导入目标代码、不读取真实认证，也不改写历史模型回执。实际自动化通过同模块的只读 `automation_findings` 另行核对原生产项目和原频率；CI 不读取本机自动化配置。

全部入口不用业务凭据，不调用真实 OA。完整测试、Skill、hygiene、links、PR 合同及原生产门禁继续保留。对标准判断和真实消费者的人工审查仍需按 CONTRIBUTING 执行。

## 离线传输与证据回归

`tests/conftest.py` 在每个 pytest 用例启动时拒绝未替换的 Python DNS、IPv4/IPv6 连接及 UDP 发送，包括同进程的线程。本地 Unix IPC 保持可用。触发 `offline_network_forbidden` 时，为该用例注入录制客户端或显式替换真实传输；拒绝使用 pytest 失败信号，业务适配器的 `except Exception` 无法把它吞成预期的读取失败。

本地和现有 CI 使用同一 `python -m pytest -v` 入口。`test_offline_network_guard.py` 通过独立 pytest 调用验证漏注入的默认发票客户端、线程内 IPv4/IPv6、显式 HTTP fake 和本地 IPC。子 pytest 加载同一 conftest；普通 Python 子进程、原生外部程序、独立临时脚本及模块导入期间的操作不在此夹具保护范围内，不能由 pytest 成功推断这些操作无网络访问。新增离线回放应放到既有测试入口，独立回放仍需显式录制客户端及单独的传输保护。

证据覆盖沿用已接线的公开入口检查，不增加第二套结论规则。`test_invoice_linkage_preview.py` 核验上传附件不能代替可信 OA 关联；`test_invoice_evidence_coverage.py` 核验关联票面资料进入 collect_case、真实 Worker 冻结输入及 preview；`test_invoice_material_diagnostics.py` 核验文件记录与正文读取分别计数。这些检查不证明模型业务判断总是正确。

重复错误与旧版本验证记录见 [#419](https://github.com/CleverC2200/oa-workspace-minimal/issues/419)。旧实现未保留票面资料时，既有 collect_case 回归因缺少 `invoice_evidence` 失败；未确认 OA 关联仍判通过的旧消费端，使 preview 回归出现 `pass != needs_human`。历史代码只在私有离线检查进程加载，未替换当前工作树或历史单据材料。

## 实际模型只读回归

需要验证 Worker 的业务判断时，按[通用费用实际模型回归](../runbooks/finance-native-replay.md)准备私有冻结材料，再显式调用原生 Worker 并闭合 preview。默认 CI 只运行该入口的离线行为测试，不调用模型，不把工程通过替代实际模型验收。
