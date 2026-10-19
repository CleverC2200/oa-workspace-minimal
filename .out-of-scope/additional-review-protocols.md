# 额外金额摘要与计算回执协议

不将旧实验的 `amount_summary`、日期副本和批量计算回执扩为审核决定的另一套必填协议。原始字段、证据引用及金额关系正确性的要求继续保留。

旧方案要求模型重复生成来源值和额外回执，增加事实副本。回执存在也不能证明费用属于本次报销。当前沿用冻结材料与结构化决定，固定协议绑定只补宿主已知字段，业务事实仍由审核者读取和判断。当前接口见[固定绑定](../apps/oa_finance_preaudit/review_decision.py)与[无损材料读取](../apps/oa_finance_preaudit/read_material.py)。

若具体业务规则确需确定性计算，按实际调用在现有模块就近实现并验收，不以恢复整套旧协议为前提。

相关原提案为 [#317](https://github.com/CleverC2200/oa-workspace-minimal/issues/317)、[#318](https://github.com/CleverC2200/oa-workspace-minimal/issues/318) 和 [#320](https://github.com/CleverC2200/oa-workspace-minimal/issues/320)。有效业务场景归入 [#369](https://github.com/CleverC2200/oa-workspace-minimal/issues/369)，独立质量验收归入 [#366](https://github.com/CleverC2200/oa-workspace-minimal/issues/366)。当前进度以对应 Issue 为准。
