# OA 官方接口入口

查询和修改 OA 请求时，使用[泛微 E9 官方后端接口文档](https://e-cloudstore.com/ec/api/applist/index.html#/)；在左侧搜索接口名称，展开参数示例。文档是技术依据，不授予真实业务写入权限。

## 流程附件的原生文档读取

2026-10-17 通过原生页面请求和同一会话前后对照核实：部分附件直接下载返回权限拒绝，先读取 `GET /api/doc/detail/basicInfo`，携带 `docid`、`isrequest=1` 和 `requestid` 后，OA 会补齐当前用户的文档授权，原下载 URL 即可读取。该入口来自实际原生页面观测，不作为对外接口文档合同。

下载器仅在直接访问被拒绝时使用此入口。当前流程与文件须经只读 `WORKFLOW_DOCSHAREINFO`、`DOCIMAGEFILE` 关联确定唯一文档，RPA Cookie 的服务器回显用户须与配置身份一致。先在不带流程上下文的文档读取中核对主文件，再初始化并再次核对返回主文件身份。随后仅使用原 Cookie 和 URL 重试一次，真实拒绝继续保留。无明确流程关联、文档歧义或读取失败不初始化，不从目录或附件私有字段推断流程。

原生读取可能由 OA 自动新增当前用户的文档共享记录，不能称为远端零副作用；它不提交审批、不修改表单，也不授予启动或恢复审批的权限。

资源数字 ID 用于文档归属与主文件核验。API 下载 URL 的 `fileid` 也可能是不透明令牌，不能与资源数字 ID 直接比较或猜解码；保留原 API 返回的 ID/URL 配对和原 Cookie。URL 只允许 OA 同源下载路径及唯一非空 `fileid`；数值标识仍须与资源 ID 一致。

## 一般费用空材料的来源确认

一般费用表单 `formtable_main_569` 的三个附件/关联发票字段可能在源值为 NULL 时仍返回不支持显示占位。没有取得附件、发票记录或权威事实时，采集器按当前流程和原生 `recordOrder` 对应的精确明细 ID，通过现有只读 SQL 核对 `formtable_main_569_dt1`。仅剥离已核实的 span/div 样式包装和不支持显示占位。只有当前全部目标明细关联唯一、API 无非空引用或 HTML 冲突，且三个源字段均确为 NULL 时，才一次性将这些字段记为 `read_status=empty`，并把 `empty_source` 的查询来源、流程、明细、表和字段身份随材料冻结。

该确认只说明材料缺失，不生成财务结论；审核者仍按原流程规则核验必需材料、发票关联及其他项目。未覆盖表单、非空引用、数据库读取失败、记录缺失或歧义继续保留 unresolved。已读取的附件或发票不走此补查，旧冻结包和隔离不改写；源码发布不启动审批或恢复旧 attempt。

## 流程实例：提交（对外）

2026-09-17 核实官方“工作流程 → 流程实例：提交(对外)”：

- 方法：`POST /api/workflow/paService/submitRequest`，沿用本项目 `application/x-www-form-urlencoded` 传输及 token 身份。
- `requestId`：必填请求 ID；`remark`：签字意见。
- `detailData`：JSON 明细表数据；`mainData`：主表字段数据，结构参照官方新建流程接口。
- `otherParams.src`：`save` 为保存、`submit` 为提交，默认提交。
- `otherParams.judgeFormMustInput`：`1` 验证必填，默认不验证；新发票回填入口显式使用 `1`。
- 主表和明细参数均不传时，只做流程提交，不保存表单数据。

官方展开示例的明细结构如下。例值是脱敏示意；已有行的 `recordOrder` 应来自当前读取结果，不能拿数组下标或页面显示序号替代。真实更新后的行数、行标识和字段值还必须回读核验。

```json
[
  {
    "tableDBName": "formtable_main_100_dt1",
    "workflowRequestTableRecords": [
      {
        "recordOrder": 4,
        "workflowRequestTableFields": [
          {"fieldName": "fphm", "fieldValue": "INV-EXAMPLE"}
        ]
      }
    ]
  }
]
```

官方示例同时展示 `deleteAll`、`deleteKeys`；这些是删除明细的参数，发票回填不得复制它们。也不发送未获授权的主表或明细字段。

现有财务预审的 `writer_api.py` 只提交 `requestId`、`remark`，继续保留这一边界。海外采购发票回填使用独立授权、独立材料与回执，向同一接口增加限定字段的 `detailData`。

## 关联发票原件读取

当前已核实的发票云资料响应在同一记录中提供有效 PDF 文件、`modify_info.pdf/furl` 与 `oa.doc_id/img_id`。采集器仅支持唯一有效 PDF、两个原件引用均指向该云文件、明确返回记录身份及票号的映射。云文件 ID 与 OA 主文件 ID 分属两个来源，不能互换。其他格式、多个 PDF 或缺少映射时保留未读，不推测下载接口。

下载前重新读取当前流程及关联票资料，核对请求身份、六类流程配置、关联票 ID/票号和完整原件映射，再以当前 Cookie 核验账号及原生文档的主文件。缓存命中也执行该核验。普通流程附件共享表没有这类原件时，被拒绝后的初始化沿同一可信关联重新解析文档；仍保留 `basicInfo` 初始化前后主文件核对、`isrequest=1`/当前 `requestid`、同 Cookie 同 URL 单次重试。原生访问可能触发 OA 正常共享初始化，不修改权限配置或发起审批。

原件沿普通下载、PDF 全页准备及冻结资源链进入材料。正文覆盖状态以[观察合同](../../.agents/skills/oa-finance-preaudit/references/observation-contract.md)为准；明确绑定原件的下载或准备失败继续停止当前取材。历史冻结材料和隔离记录不改写。
