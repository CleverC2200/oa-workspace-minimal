# 按请求隔离的 OA 提交合同

Writer 一次只提交一个已经冻结的 `requestId + received_at + attempt`；不得执行审核判断、重新下载附件或修改意见。不同 requestId 没有业务依赖，可以由有界提交池并发；同一 requestId 始终单飞。

启动时使用 canonical 接口核对包含开发合同与生产提交代码的复合指纹：

```bash
export PROJECT_ROOT=/absolute/path/to/oa-workspace-minimal
cd "$PROJECT_ROOT"
python3 -m apps.oa_finance_preaudit.role_cli writer release-manifest \
  --project-root "$PROJECT_ROOT" \
  --writer-root "$PROJECT_ROOT"
```

不得从生产 cwd 推导项目合同路径，不得对单个文件自行计算哈希。只有返回的 `sha256` 与调度入口给定 writer 指纹一致才进入提交门禁。

## 单据级提交门禁

1. 材料、意见、封套、assignment 与 current proof 属于同一 `requestId + received_at + isolation_key + attempt`；
2. 实时生产待办身份源与项目 list-pending 各唯一命中该版本；
3. 四个冻结文件位于同一 attempt，身份、一次性 submission token 与四份 SHA-256 指纹全部匹配；
4. requestId 级锁内不存在同 requestId 的活动提交、冲突回执或未知写入结果。

Writer 只接收 canonical `live-inputs.json`，并复核 manifest 路径、两个 final schema、计数与 SHA-256；manifest 超时、来源变化或任一来源不是唯一命中时不得生成 submission token。

门禁通过后只调用 canonical 单入口：

```bash
cd "$PROJECT_ROOT"
python3 -m apps.oa_finance_preaudit.role_cli writer submit-frozen-once \
  --state "$PROJECT_ROOT/runtime/finance_preaudit_queue/state.json" \
  --request-id REQUEST_ID \
  --live-inputs-manifest LIVE_INPUTS_JSON \
  --production-root "$PROJECT_ROOT"
```

该入口连续执行 current proof、`begin-submit`、冻结提交、回执校验和 `mark-submitted`；取得 submission token 后不会返回模型控制权。调用方不得手工拼接阶段，也不得绕过冻结路径直接提交。

## 并发与幂等

- 调度入口使用配置上限与平台实际槽位共同约束的有界提交池；不同 requestId 可并发，不建立跨流程 FIFO。
- 锁只绑定 requestId。相同 requestId 的并发、重放和超时重试必须等待同一锁，并优先读取 attempt 内的持久成功回执；同一 token 只能产生一次有效 OA 写入。
- requestId、冻结目录、current proof、submission token 和回执逐单绑定。任何跨单据路径或指纹混用都只阻断受影响单据，并停止该请求的写入。
- 提交临界区只包含单据级冻结复核、接口调用、写后确认和持久终态；不保留全局 Writer 锁。

## 写后闭合与恢复

- 只有 `submit_ok=true、done_hit=true、postcheck_done_hit=true` 的同一次回执才可 `mark-submitted`。
- 明确未写入的认证或传输前失败可以在同 requestId 门禁下重试；必须保持原冻结指纹并重新验证当前待办身份。
- 接口已返回成功但已办命中、待办消失或回执状态不能闭合时，原子写入脱敏 `writer_result_unknown` 标记。同 requestId 后续调用只返回 unknown，不得再次提交；其他 requestId 可以继续。
- 不同 requestId 的失败互不阻断。只有发现共享认证整体失效、跨单据材料串线或多个有效回执冲突时，才暂停提交池并上报 Admin。
