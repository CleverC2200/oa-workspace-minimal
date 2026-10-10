# 通用费用实际模型只读回归

需要验证真实 Worker 判断时，使用 `scripts.finance_preaudit_native_replay`。该入口从已保存材料生成新的 `preview`，调用现有 `codex_review_worker.review`，再通过 `FlowEngine.complete_review` 验证并闭合。它不配置 OA 连接、授权源或提交回调。

## 准备私有输入

在仓库外保存清单，每个案例仅包含本地别名、原始材料文件和原始状态文件路径：

```json
{
  "cases": [
    {
      "case": "employee-payment",
      "material_file": "/private/local-evidence/attempt-1/material.json",
      "state_file": "/private/local-evidence/attempt-1/state.json"
    }
  ]
}
```

原始 `attachments/`、`prepared/` 与材料文件必须在同一 attempt 下。入口核对历史材料和附件指纹，完整复制证据并只重绑定路径，保留表单、权威事实、发票关系、图片及读取缺口。原始材料、状态和历史意见均不修改。

预期结论放在另一份私有文件，运行后单独比对，不传给本入口。模型只读独立源码及材料快照；该快照排除测试、清单、已确认反馈、回归答案和历史决定。每个 label/case 独立，不复用已启动的快照。

## 执行一例

在已授权的开发工作树使用 Python 3.11 或更新版本，先准备，不调用模型：

```bash
python3 -m scripts.finance_preaudit_native_replay \
  --prepare --case employee-payment --label candidate-a \
  --project /absolute/development-project \
  --inventory /private/local-evidence/cases.json \
  --audit /private/local-evidence/replay
```

确认返回 `status=prepared`、`mode=preview`、`write_authorized=false`。`prepare.json` 保存源码哈希、原始指纹、复制文件映射、新冻结绑定，以及当前 FlowEngine 补入的合同字段差异。需要完整材料对照时检查 `current_engine_changed_top_level_fields`，不要把合同更新写成原材料完全未变。

获得实际模型调用授权后，使用同一组参数显式调用：

```bash
python3 -m scripts.finance_preaudit_native_replay \
  --run --case employee-payment --label candidate-a \
  --project /absolute/development-project \
  --inventory /private/local-evidence/cases.json \
  --audit /private/local-evidence/replay
```

默认使用现有 Worker 的 GPT 6.1 Sol、medium、普通服务档，保留其最多两次技术进程尝试，不添加业务重试。每个案例原生尝试、事件、工具回执、Token 用量、决定和闭合结果保存在私有审计目录。一次运行启动后，等待原进程结束；已有运行记录时入口拒绝重跑。

## 核对结果

成功闭合需要 `preview_closed_no_write=true`、`write_state=not_requested`、原材料哈希未变。`business_state=needs_human` 可以是有效结果；逐项对照私有预期，保留真实缺材料、付款归属不明、未支付、退款和金额差额等异常。

`execution_transport=native` 表示调用原生执行器。离线测试注入记录执行器时标为 `recorded`，不作为实际模型验收。源码改变后用新 label 重新准备，不修改正在运行或已冻结的快照。回归结果不授权重写 OA 意见、上线或恢复生产。

### 独立业务验收检查

`--run` 的返回码只说明工程闭合，其 `business_acceptance` 固定为 `not_verified`。使用已有 `--verify` 分别检查实际原生执行和独立业务验收记录，不重新运行模型或修改记录。

模型调用前独立核对并冻结预期文件和 SHA-256。文件中每个 case 的 `allowed_statuses` 必须覆盖冻结包的全部审核项，值为允许的 `pass`、`fail`、`uncertain` 数组。预期文件保存在模型项目快照外：

```json
{
  "schema_version": 1,
  "cases": [{
    "case": "employee-payment",
    "allowed_statuses": {
      "attachment_payment_evidence": ["pass"],
      "invoice_linkage": ["pass"],
      "budget": ["pass"],
      "reimbursable_minimum": ["pass"],
      "payment_company_consistency": ["pass"]
    }
  }]
}
```

上述仅为正常案例的格式示例。实际预期来自原始证据；真实异常继续允许 `fail` 或 `uncertain`，不复制全通过作为其他案例的答案。

模型运行后独立核对全部意见及理由。复核回执保存 `schema_version=1`、本次 `case`、`label`、`prepare.json` 的 `source_head`、冻结的 `expectations_sha256` 和实际 `native-preview-decision.json` 的 `decision_sha256`。只有意见符合证据且无待修问题，才记录 `verdict=accepted` 和 `findings=[]`；存在问题使用 `rejected` 并保留具体 findings。复核回执也保存在模型项目外，完成后单独固定其 SHA-256。

```bash
python3 -m scripts.finance_preaudit_native_replay \
  --verify --case employee-payment --label candidate-a \
  --project /absolute/development-project \
  --inventory /private/local-evidence/cases.json \
  --audit /private/local-evidence/replay \
  --expectations /private/local-evidence/expectations.json \
  --expectations-sha256 "$FROZEN_EXPECTATIONS_SHA256" \
  --independent-review /private/local-evidence/business-review.json \
  --review-sha256 "$INDEPENDENT_REVIEW_SHA256"
```

返回码 0 表示指定的全部工程记录、逐项预期和独立复核绑定通过；否则返回 1，给出错误及对应处理入口。输出报告包含 `business_acceptance`，不写回原始记录。模型输入与闭合决定允许 JSON 键顺序不同，但内容必须一致且各自符合原指纹。

该检查核对既定指纹和复核结果，不证明预期本身正确、实际看图完成或复核身份独立。状态完全相同但理由错误时，仍由独立复核记录 `rejected`，不能仅依靠状态数组签收。程序不解释意见文字或生成财务判断。CI 离线夹具覆盖检查行为，不能作为新一次实际模型调用。

默认 pytest 不调用实际模型；离线公开入口检查为：

```bash
python3 -m pytest -q tests/finance_preaudit/test_native_replay.py
```
