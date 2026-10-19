# 财务预审实际模型只读回归

验证当前 App 或保留的 DeepSeek CLI 判断时，使用 `scripts.finance_preaudit_native_replay`。该入口从已保存材料生成新的 `preview`，沿冻结的审核传输取得决定，再通过离线 `FlowEngine.complete_review` 闭合。它不配置 OA 连接、授权源或提交回调。

准备时使用历史状态中的 `workflow_key`，保留对应流程的固定审核项；差旅材料不会转成通用费用审核。

每次回放在准备阶段冻结 `OA_REVIEW_PROVIDER` 和 `OA_REVIEW_TRANSPORT` 选择的审核配置。项目默认是 OpenAI 原生 App（`host_chat`）。App 的 `--run` 必须提供当前父会话 UUID，并独立登记恰好三个 direct reviewer。每个 label/case 只运行一次，不能借用生产轮次、注入 executor 或改用已禁止的 OpenAI CLI。旧快照未冻结回放入口源码时，使用新 label 重新准备。

需要运行保留的 DeepSeek CLI 时，准备和运行均显式设置 `OA_REVIEW_PROVIDER=deepseek OA_REVIEW_TRANSPORT=cli`，使用保留的隔离 DeepSeek CLI 配置与独立凭据。入口不会自动切换供应商。默认 App 配置已经准备的快照保持不变；选择 DeepSeek 后必须用新标签重新准备。模型目录纳入准备源码快照。历史 OpenAI 或 App 配置的记录仍可只读 `--verify`；未记录配置的历史回放继续按原 OpenAI/medium 合同验证，这不授予重新执行旧 CLI 的许可。不能以回放闭合代替独立业务验收。

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

先提交将被复制的源码、规则和配置；准备入口要求这些文件与声明的提交一致，快照范围外的本地文件不阻断准备。

```bash
OA_REVIEW_PROVIDER=deepseek OA_REVIEW_TRANSPORT=cli \
python3 -m scripts.finance_preaudit_native_replay \
  --prepare --case employee-payment --label candidate-a \
  --project /absolute/development-project \
  --inventory /private/local-evidence/cases.json \
  --audit /private/local-evidence/replay
```

确认返回 `status=prepared`、`mode=preview`、`write_authorized=false`。`prepare.json` 保存源码哈希、原始指纹、复制文件映射、新冻结绑定，以及当前 FlowEngine 补入的合同字段差异。需要完整材料对照时检查 `current_engine_changed_top_level_fields`，不要把合同更新写成原材料完全未变。

获得实际模型调用授权后，使用同一组参数显式调用：

```bash
OA_REVIEW_PROVIDER=deepseek OA_REVIEW_TRANSPORT=cli \
python3 -m scripts.finance_preaudit_native_replay \
  --run --case employee-payment --label candidate-a \
  --project /absolute/development-project \
  --inventory /private/local-evidence/cases.json \
  --audit /private/local-evidence/replay
```

沿用准备时冻结的模型、推理和供应商配置，请求普通服务档。当前 Worker 每次只执行一次审核进程，不添加业务重试；历史记录保留原实际尝试数。每个案例原生尝试、事件、工具回执、Token 用量、决定和闭合结果保存在私有审计目录。一次运行启动后，等待原进程结束；已有运行记录时入口拒绝重跑。

运行前核对原材料、明确绑定的原状态和原证据指纹。依赖缺失、不可读或变化时返回非零，在私有 `result.json` 留下原因、`review_status=not_started` 和空 attempts，执行器调用为 0；修复输入后使用新 label 重新准备。执行中发生 I/O 失败时，结果保留实际尝试及已返回回执，未知终态继续未知；保存的失败结果同样不能重跑或覆盖。

## 当前 App 只读运行

真实审核需另有明确模型调用授权。准备及离线测试不提供调用额度。先以新的 label 冻结同一候选和完整材料，再由当前父会话启动：

```bash
OA_REVIEW_PROVIDER=openai OA_REVIEW_TRANSPORT=host_chat \
python3 -m scripts.finance_preaudit_native_replay \
  --run --case employee-payment --label candidate-a \
  --project /absolute/development-project \
  --inventory /private/local-evidence/cases.json \
  --audit /private/local-evidence/replay \
  --owner-task-id CURRENT_PARENT_UUID
```

入口复用已有监督进程和 lifecycle，在本 case 的私有 `runs/<label>/<case>/control` 下持有租约并记录真实 PID、原执行及退出事实。不启动生产调度，不加载 OA 认证。子进程仅继承必要运行环境及新的只读绑定，清除旧认证、授权和控制环境。准备包、原件和实际执行源码不一致时拒绝启动。

父会话等待原工具返回可续读的 session_id，沿现有 execution 入口绑定该原 session。之后所有登记、读取进度、心跳、派发、草稿提交和收尾调用，都必须显式使用本 case 的私有控制文件：

```bash
export OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE=/private/local-evidence/replay/runs/candidate-a/employee-payment/control/controller.lock
export OA_FINANCE_PREAUDIT_STOP_FILE=/private/local-evidence/replay/runs/candidate-a/employee-payment/control/stop.flag
export OA_FROZEN_WRITER_LOCK_FILE=/private/local-evidence/replay/runs/candidate-a/employee-payment/control/writer.lock
```

复用[当前 App 登记与原始执行回执](../agents/controller-execution.md)的三个真实子会话、原生父心跳及 assign/submit 来源校验。父会话创建和派发 reviewer，Python 不创建 agent。审核请求只接受本次冻结的唯一 preview action，不能改为受控写入或包装阶段。登记和审核共用本次 `--timeout` 预算，剩余不足一分钟时不开始审核。

相对 Skill 和文档路径以请求声明的冻结项目为根；Python 模块命令使用请求指定的执行源码目录，不能依赖聊天默认 checkout。专用 write-draft 的原生 exec 必须显式传入该 workdir，并等待真实配对工具输出。完成宿主验证同一 request/assignment/source/decision，再由离线 FlowEngine 闭合，保持 `write_state=not_requested`。不要另调生产 Worker 的 complete 入口。

等待原执行真实退出后，按同一收尾入口取得三个 interrupt、list_agents 和原 session 的 terminal wait，再封存该只读轮次。进程退出与 reviewer idle 分别核验。只读失败保留在本 case 的私有目录，不清除生产停止门，也不自动重跑。App 实际模型名和 Token 用量无法观测时保持 `unknown`。

## 核对结果

成功闭合需要 `preview_closed_no_write=true`、`write_state=not_requested`、原材料哈希未变。`business_state=needs_human` 可以是有效结果；逐项对照私有预期，保留真实缺材料、付款归属不明、未支付、退款和金额差额等异常。

`execution_transport=native` 必须同时核对冻结配置和对应原生来源。App 验收要求已封存的三槽登记、派发、专用草稿、原工具退出及无写闭合回执；单独这个字段不能证明原生执行。离线测试注入记录执行器时标为 `recorded`，不作为实际模型验收。源码改变后用新 label 重新准备，不修改正在运行或已冻结的快照。回归结果不授权重写 OA 意见、上线或恢复生产。

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
  --review-sha256 "$INDEPENDENT_REVIEW_SHA256" \
  --output-file /private/local-evidence/candidate-a-verification.json
```

返回码 0 表示指定的全部工程记录、逐项预期和独立复核绑定通过；否则返回 1，给出错误及对应处理入口。输出报告包含 `business_acceptance`，不写回原始记录。模型输入与闭合决定允许 JSON 键顺序不同，但内容必须一致且各自符合原指纹。

`--output-file` 可选，只用于 `--verify`。目标须是模型项目外已有目录中的新文件；命令独占创建权限为 `0600` 的报告，不覆盖材料、复核记录或旧报告，不创建目录或改变目录权限。stdout 保留验收摘要及报告路径、字节数和 SHA-256，不输出业务复核原文。只有复核、预期与实际闭合决定的指纹绑定可信时，私有报告的 `independent_review` 才保留原复核的 verdict 和 findings；每次最多显示 20 条、每条 1000 字符，`total` 与 `display_truncated` 标明截断，完整发现仍以原复核文件为准。报告写入或读回核对失败时返回非零且不给成功文件回执；可能留下的部分文件保留排查，不能当作成功验收记录。真实复核内容仅本地查看，不上传 Issue 或 CI。

该检查核对既定指纹和复核结果，不证明预期本身正确、实际看图完成或复核身份独立。状态完全相同但理由错误时，仍由独立复核记录 `rejected`，不能仅依靠状态数组签收。程序不解释意见文字或生成财务判断。CI 离线夹具覆盖检查行为，不能作为新一次实际模型调用。

默认 pytest 不调用实际模型；离线公开入口检查为：

```bash
python3 -m pytest -q tests/finance_preaudit/test_native_replay.py
```
