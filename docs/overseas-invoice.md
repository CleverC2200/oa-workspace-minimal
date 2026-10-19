# 海外采购发票回填

本入口与六类财务预审、包装校对分开。Agent 读取原件提取字段，脚本负责材料冻结、限定字段、独立授权及提交后核验。相关需求：[Issue #239](https://github.com/CleverC2200/oa-workspace-minimal/issues/239)。

小时调度在财务、包装及原审核槽统一收尾通过后，串行调用本入口。六类财务仍只给预审意见；本流程处理发票字段及当前 RPA 节点，不执行付款。无发票分支及调度接线见 [Issue #630](https://github.com/CleverC2200/oa-workspace-minimal/issues/630)。

## 授权与运行

固定工作目录为生产克隆 `oa-workspace-minimal`，使用其 `.venv/bin/python`。开发只做离线测试和只读材料分析；真实提交要求生产干净 `main`。发布及生产同步仍按 [CONTRIBUTING](../CONTRIBUTING.md#开发与生产隔离及发布) 单独执行。

独立授权保存在被 Git 忽略的 `runtime/overseas_invoice/write-policy.json`，只允许当前 OA 用户写海外采购的三个发票字段；不修改现有财务预审 `write-policy.json`，不加入滚动队列或创建定时任务。独立授权不绕过现有停止门。只有当前明确授权才可开启开关；配置开关不代表代码已部署或 OA 已提交。

下列开启和提交命令只在用户已明确授权范围内使用，日常状态查询不需要重新授权：

```bash
.venv/bin/python -m scripts.overseas_invoice authorize --confirm-write YES
.venv/bin/python -m scripts.overseas_invoice status
.venv/bin/python -m scripts.overseas_invoice pending
.venv/bin/python -m scripts.overseas_invoice collect --request-id REQUEST_ID
```

`collect` 默认只读 OA；本地返回一个随机 attempt 和材料文件，下载限于当前明细 `scfp` 已可靠绑定的 PDF/PNG/JPEG。每次读取绕过附件缓存，提交前重新下载核验 SHA256。材料和提交计划均使用本地 OA 密钥签名，但不将密钥写入文件。材料不提交 Git。

`pending` 仅返回当前执行身份的海外采购流程，按接收时间、请求 ID 排序，同时显示该请求是否已有提交回执。已有 `unknown` 或已提交回执但仍在待办时，保留原回执并转人工核对，不重新发送。

### 无发票与混合明细

用户已确认：此流程未上传发票时保留原字段，直接提交当前 RPA 节点。部署后，具有本范围授权的生产任务使用下面的独立授权选项启用该分支；旧授权默认不包含无发票提交：

```bash
.venv/bin/python -m scripts.overseas_invoice authorize --confirm-write YES --allow-no-invoice YES
```

`collect` 通过精确请求、主表 `formtable_main_1295` 及明细 `recordOrder` 关联只读源值。只有 API 无非空附件引用或 HTML 冲突，且唯一当前明细的 `scfp` 源值确为 NULL 时，才将 `invoice_empty_source` 随该明细冻结。显示占位或数据库读取失败保持未确认。混合明细分别绑定原件或空来源，只回填有发票行；全部无发票时材料的 `attachments=[]`。

```bash
.venv/bin/python -m scripts.overseas_invoice freeze --attempt ATTEMPT --no-invoice
.venv/bin/python -m scripts.overseas_invoice submit --attempt ATTEMPT --confirm-write YES
```

无发票计划仅适用于 RPA 预审节点。提交前重新核对空来源和当前身份、节点、全部字段；只提交节点，不发送 `mainData` 或 `detailData`，并启用原生必填检查。节点操作组缺失等 OA 配置错误仍按实际提交失败/未知结果保留，不改配节点、不自动重试。无发票与混合明细均要求 `no_invoice_enabled=true`。

使用 [独立技能](../.agents/skills/oa-overseas-invoice/SKILL.md) 阅读原件后，将识别结果写入本地 JSON。下例只说明结构，资源 ID、摘要、页码、原文和值必须从本次材料及原件取得：

```json
{
  "material_digest": "本次 material.json 的 payload 摘要",
  "invoices": [
    {
      "resource_id": "材料中的附件ID",
      "sha256": "材料中的附件SHA256",
      "currency": "USD",
      "fpje": {"status": "recognized", "value": "55.00", "page": 1, "quote": "TOTAL USD 55.00"},
      "fphm": {"status": "recognized", "value": "INV-EXAMPLE", "page": 1, "quote": "INVOICE NO INV-EXAMPLE"},
      "fpdqr": {"status": "missing", "value": null, "page": 1, "quote": "PREPAYMENT"}
    }
  ]
}
```

不得把识别缺失写成空字符串去清除原值；不得把不确定、冲突或不可读当作缺失。多行分别绑定各行附件，不按文件顺序、名称或金额猜归属。同一行有多份附件时当前版本停下等待明确映射，不擅自选第一份或求和。

```bash
.venv/bin/python -m scripts.overseas_invoice freeze --attempt ATTEMPT --recognition-file /absolute/path/recognition.json
.venv/bin/python -m scripts.overseas_invoice submit --attempt ATTEMPT --confirm-write YES
.venv/bin/python -m scripts.overseas_invoice status --attempt ATTEMPT
```

计划有效期一小时，冻结后不可改写。过期且从未开始提交可重新采集；提交前重新检查当前待办身份、接收时间、节点、所有字段、附件和权限。资源下载链接可以刷新，资源 ID、归属及其他元数据仍须一致，并用新鲜下载的 SHA256 确认原件未变。提交只发送官方 `detailData` 的指定字段，不发送删除明细参数或主表；启用必填校验。官方合同见 [OA API](agents/oa-api.md)。

所有 OA 写入共用原有全局串行锁。发送前先保存 `unknown` 回执；同一请求重复调用不再次发送，即使换了 attempt 也不重提。提交响应成功仍须字段回读一致、已办命中且待办消失才记为 `submitted`；其他情况保留未知回执，必须人工核对。状态查询返回脱敏结果，不输出原件正文或账号。

提交后只允许 OA 将原先为空的主表 `lcbh` 补成流程编号；其他主表字段仍须一致。已办行缺少流程 ID 时，必须由同一精确请求的当前流程上下文确认身份；明确冲突不可忽略。后查扫描完整已办分页，重新下载核对原件，并同时核验三字段、其他支付字段、待办消失及已办命中。

已有 `unknown` 回执时，可使用只读核对入口：

```bash
.venv/bin/python -m scripts.overseas_invoice postcheck --attempt ATTEMPT
```

此入口读取当前 OA 结果，不调用提交接口。结果为 `confirmed_submitted` 或 `unresolved`，另存绑定原回执、冻结计划及当前证据的签名记录；原未知回执保留原字节，`status` 仍展示它。即使核对成功，同一请求的再次 `submit` 仍返回原回执，不重新写入。证据缺失或冲突时继续人工核对，不换 attempt 重提。

撤销本流程授权使用：

```bash
.venv/bin/python -m scripts.overseas_invoice disable
```

撤销不改其他流程、不删除材料或提交回执，也不回滚已经发生的 OA 流转。

## 验证边界

聚焦测试覆盖字段/附件归属、独立授权、目标变化、签名和文件变化、过期、并发及重复提交、未知结果、字段回读和官方请求结构。真实原件样本识别和真实 OA 提交分别核验；mock 通过不代表真实回写已验收。
