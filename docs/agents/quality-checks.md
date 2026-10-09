# 质量检查

沿用 `scripts.quality` 和现有 GitHub quality workflow，不增加另一套检查平台。

- `yaml`：安全解析受版本控制的 YAML，拒绝重复键，采用 YAML 1.2 布尔值约定。Issue form 检查基本结构、非空标题、字段类型、唯一 ID、下拉选项及 required 布尔值；这是本项目固定合同检查，不代表 GitHub 平台的全部限制。
- `schema`：通过离线 FlowEngine 冻结六类财务包，检查真实生成器的 JSON Schema；同时检查包装 V2 生成器。拒绝供应商已实际拒绝过的空属性对象。包装页、非包装页、不确定页和 Excel 映射进入原消费者的回归保留在 pytest 中。静态有效不证明供应商已经实际接受全部分支。
- `lint`：固定 Ruff 版本，检查全部 apps/scripts/tests 的严重语法、不可成立的判断和未定义名称。规则以 `.ruff.toml` 为准；原 `format` 继续只处理换行/空白，不能宣称已经执行 Python 格式化或全面类型检查。
- `production-config`：以 `apps.oa_finance_preaudit.production_config` 的窄合同核对生产审核默认、六类财务范围、模板命令和小时手册。当前合同为原生 GPT 6.1 Sol、medium、普通服务档，起始 3、最高 6。检查只解析目标源码和文本，不导入目标代码、不读取真实认证，也不改写历史模型回执。实际自动化通过同模块的只读 `automation_findings` 另行核对原生产项目和原频率；CI 不读取本机自动化配置。
  原生命令检查支持直接列表返回，以及当前局部 `command` 列表加两种可选读取设置的有限组装。只允许工具输出预算、Worker角色文件及其项目文档设置；未知列表修改、返回路径或模型常量遮蔽均拒绝，不能用无用的正确字面量掩盖实际配置漂移。
- `native-contract --record <私有记录路径>`：使用已安装的固定原生 CLI 和本地模拟服务，沿 Worker 与只读 preview 核对实际下一请求、完整工具输出、图片交付及原对象拒绝。结果明确为 `verified` 或 `not_verified`；`--reuse` 重新核对版本、源码、参数和证据指纹，不启动模型。缺少 CLI 或证明失效时，依赖该证明的派发必须停止。

OpenAI 原生协议检查保留 Worker 的模型、推理档位、内置 provider 和请求服务档位，只用 `openai_base_url` 指向本机模拟端点。隔离目录内的认证文件只含公开测试占位值，不读取或复制用户认证；回执绑定 Worker 保留的 `tool-receipts`。模拟端点拒绝 WebSocket 后由原生 CLI 使用 Responses SSE，因此该检查不证明真实供应商、认证或财务质量已验收。旧 DeepSeek 基线仍按其原配置观察；基线协议证明始终不授予 `paid_dispatch_ready`、`input_complete` 或 `sample_eligible`。

当前 Ubuntu quality workflow 不安装固定原生 CLI。普通 pytest 会明确跳过无法运行的原生场景，缺少 CLI 的拒绝回归仍运行；CI 通过不能当作原生合同已验证。在具备固定 CLI 的环境中运行上述选择，再保留其私有证明。具体入口及证据范围见[受控只读对照](review-experiments.md)。

全部入口不用业务凭据，不调用真实 OA。完整测试、Skill、hygiene、links、PR 合同及原生产门禁继续保留。对标准判断和真实消费者的人工审查仍需按 CONTRIBUTING 执行。

效果实验启动前的实际差异检查、只读 preview 和失败成本回归见[受控只读对照](review-experiments.md)。
