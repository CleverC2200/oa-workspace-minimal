# 不同供应商之间的账号相等要求

不采用 [#437](https://github.com/CleverC2200/oa-workspace-minimal/issues/437) 提议的 Desktop 与 CLI 账号必须相等门禁。OpenAI 使用当前 App 审核，保留的 DeepSeek CLI 使用独立供应商凭据，两者账号相等不构成有效的授权判据。

沿用[审核配置合同](../apps/oa_finance_preaudit/production_config.py)选择传输，按[控制器执行回执](../docs/agents/controller-execution.md)核验原生来源、当前 owner 与子任务归属。历史 CLI 回执保持只读可核对；不通过全局凭据探测证明当前 App 身份，也不因此放宽已有来源验证。

原生生命周期与来源证据的未解决问题继续由 [#325](https://github.com/CleverC2200/oa-workspace-minimal/issues/325) 跟踪。拒绝原账号相等方案不代表这些问题已修复。
