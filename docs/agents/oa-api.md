# OA 官方接口入口

查询和修改 OA 请求时，使用[泛微 E9 官方后端接口文档](https://e-cloudstore.com/ec/api/applist/index.html#/)；在左侧搜索接口名称，展开参数示例。文档是技术依据，不授予真实业务写入权限。

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
