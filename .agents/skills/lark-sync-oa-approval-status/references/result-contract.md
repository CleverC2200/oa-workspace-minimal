# 结果与回执契约

## 固定写入契约

- `approval_code=8C813133-3F77-4A1A-94A1-84F356CFD0CC`。
- `instance_id` 原样使用唯一 OA requestId。
- `status=APPROVED`。
- `start_time`、`end_time`、`update_time` 使用同一当前 Unix 毫秒时间戳。
- 使用 `--profile default` 和 bot 身份；应用必须为“`Hermes小念`”，bot 必须 `ready` 且 `verified=true`。
- 请求体只包含项目 CLI 定义的必填字段，不临时增加标题、表单、人员、任务或抄送数据。

## 执行与后置校验

写入路径为 `/open-apis/approval/v4/external_instances`。真实写入成功后，以同一 `instance_id` 和 `update_time` 调用 `/open-apis/approval/v4/external_instances/check`：

```json
{
  "instances": [
    {
      "instance_id": "REQUEST_ID",
      "update_time": "UNIX_MS"
    }
  ]
}
```

只有响应中的 `data.diff_instances` 是空数组，才能认定飞书端已有当前版本。任何差异或无法完成检查都属于未闭环。

## 稳定回执

CLI 只输出最小稳定回执，不输出完整认证或 API envelope：

```json
{
  "schema_version": 1,
  "kind": "external_instance_sync_receipt",
  "ok": true,
  "dry_run": false,
  "instance_id": "REQUEST_ID",
  "status": "APPROVED",
  "update_time": "UNIX_MS",
  "postcheck": {
    "done": true,
    "current": true
  }
}
```

dry-run 必须使用 `postcheck.done=false`、`postcheck.current=null`，不得据此报告已同步。

## 报告格式

- 闭环成功：`REQUEST_ID：已同步并校验，状态 APPROVED`。
- 执行或校验失败：`REQUEST_ID：同步未闭环，简短原因`。
- 因公共故障未执行：`REQUEST_ID：未执行`。

同时说明使用默认 profile 的“Hermes小念”bot 身份；不输出 token、完整 scope、认证响应或大段 API envelope。
