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

## 运行与核对保护

计划固定当前本地 apps Python 实现、宿主、driver、解释器和依赖清单；新进程加载前检查实际字节。calling-source 的项目指令和当前流程五份必需规则文档也必须在 Git 固定清单中，漏列不能用来绕过漂移检查。

授权可选声明带时区的 not_before / expires_at，声明后按实际宿主时间逐次核验。取消保护先阻塞信号，登记宿主 PID 和 handler 后恢复信号；保护不可用的非 main 调用拒绝启动。准备完成后的取消也停止后续模型派发。所有 execute 宿主失败，包括 communicate 超时，均回查真实启动回执；无法确认保持 unknown，不报伪零或把已启动基线列为 not_started。

原生成合同直接校验原 JSON，不借消费端的既有协议补全冒充合格。原决定确实可消费但生成合同不合格时，事实核对仍单独报告，完整质量资格保持失败。财务核对绑定当前流程完整所选规则及原消费指标窗口；同历史 attempt 的旧轮次不能仅靠相同 key 套到当前窗口，未来的核对时间不能算已完成。

原图统计中 `expected` 是原页数，`unique_images` 是独立图片字节数，`view_returned` 是实际返回的看图回执数，`covered` 是逻辑覆盖页数。只有原决定绑定的精确重复页记录、原页字节及已观察代表页均一致时，才将观察覆盖投影回重复页。一次看图仍只计一次；覆盖成立不证明模型理解正确，也不替代付款事实的业务去重判断。初始递交与实际看图回执仍分别展示。
