# 受控只读对照

本入口只消费新建的私有冻结档案，不查询 OA、不恢复单据、不进入 Writer。实际单据和模型运行仍须覆盖本批的明确授权；生成计划、报告或本地提交不授予运行授权。

## 启动前核验

沿用单批档案合同：每侧包含 `calling-source`、当前只读 preview 动作及冻结原材料，以及声明实际修订和文件指纹的 `manifest.json`。来源必须能和开发克隆中的 Git 对象逐文件核对；全部调用源码和依赖声明均须冻结。已有 started/result/reviewer 的档案不能重用。

`python -m scripts.review_experiment freeze` 显式接受两侧档案、计划输出、开发 repository、审核器、purpose、时限和最大启动数。两个来源宿主分别准备实际 prompt、Schema、初始图片和调用配置；准备过程没有审核模型调用。仅声明不同分支、action token、临时路径或“candidate”标签不能证明优化生效。当前效果入口只允许原读取路径到完整初始输入的变化；同路径或容量回退拒绝效果调用。diagnostic 是单独声明的目的，不能自动把被拒的 effect 转成 diagnostic。

`python -m scripts.review_experiment run PLAN --authorization AUTHORIZATION` 要求私有授权记录绑定计划 SHA-256、最大启动数、非空当前授权来源和 `oa_write_authorized=false`。该记录应由本次执行的明确授权形成，不由实验入口自动生成。每侧最多启动一次，不重试；整个计划只能消费一次。时限、规则、样本、模型/供应商/推理、Schema、原图字节和准备结果的漂移都停止派发。取消和进程回收未知也停止后续侧。

## 输出与计量

复用 `scripts.review_batch_report`、`scripts.review_cost_report` 和 `scripts.review_window_report`。所有派发、失败、未知和明确未启动保留在窗口；唯一完整原生终态用量才 exact，partial 只是已观察小计。input 已包含 cache，output 已包含 reasoning，不能再次相加。非 JSON 或非对象结果仍可有 exact 失败成本，但不得形成完整业务验收，也不能修补旧决定。

计时从本次输入准备开始，到相同 FlowEngine preview 消费完成结束；不包含 OA 取数/提交，也不推算供应商请求数或账单金额。初始图片实际递交、工具图片回执、结构化文本和精确重复复用分开呈现；递交不证明正确观察。财务质量由独立人工评估及现有质量门决定，准入和记账不代替业务判断。真实运行、可比用量、财务通过和生产加载仍是独立状态。

## 无凭据回归

`python -m pytest tests/finance_preaudit/test_review_experiment.py tests/finance_preaudit/test_review_batch_report.py tests/finance_preaudit/test_review_cost_report.py tests/finance_preaudit/test_review_window_report.py -q` 使用受控外部进程，走实际 Worker → FlowEngine preview → 报告；不调用付费模型或 OA。现有 quality workflow 的 pytest/lint/hygiene/links 继续覆盖这些模块，未增加另一套检查平台。
