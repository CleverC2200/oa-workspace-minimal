# OA 官方接口入口

查询和修改 OA 请求时，使用[泛微 E9 官方后端接口文档](https://e-cloudstore.com/ec/api/applist/index.html#/)；在左侧搜索接口名称，展开参数示例。文档是技术依据，不授予真实业务写入权限。

## 流程附件的原生文档读取

2026-10-17 通过原生页面请求和同一会话前后对照核实：部分附件直接下载返回权限拒绝，先读取 `GET /api/doc/detail/basicInfo`，携带 `docid`、`isrequest=1` 和 `requestid` 后，OA 会补齐当前用户的文档授权，原下载 URL 即可读取。该入口来自实际原生页面观测，不作为对外接口文档合同。

下载器仅在直接访问被拒绝时使用此入口。当前流程与文件须经只读 `WORKFLOW_DOCSHAREINFO`、`DOCIMAGEFILE` 关联确定唯一文档，RPA Cookie 的服务器回显用户须与配置身份一致。先在不带流程上下文的文档读取中核对主文件，再初始化并再次核对返回主文件身份。随后仅使用原 Cookie 和 URL 重试一次，真实拒绝继续保留。无明确流程关联、文档歧义或读取失败不初始化，不从目录或附件私有字段推断流程。

原生读取可能由 OA 自动新增当前用户的文档共享记录，不能称为远端零副作用；它不提交审批、不修改表单，也不授予启动或恢复审批的权限。

资源数字 ID 用于文档归属与主文件核验。API 下载 URL 的 `fileid` 也可能是不透明令牌，不能与资源数字 ID 直接比较或猜解码；保留原 API 返回的 ID/URL 配对和原 Cookie。URL 只允许 OA 同源下载路径及唯一非空 `fileid`；数值标识仍须与资源 ID 一致。

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
