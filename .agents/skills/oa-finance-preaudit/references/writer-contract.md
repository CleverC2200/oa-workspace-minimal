# 唯一 OA 写入器合同

本角色是全局唯一写入器，只串行提交已经冻结且位于 FIFO 队首的 attempt；不得执行审核判断、重新下载附件或修改意见。

启动时只用 canonical 接口核对包含开发合同与生产 writer 代码的复合指纹：

```bash
cd /Users/synear/HermesWorkspace/profiles/oa/projects/oa-workspace-minimal
python3 -m apps.oa_finance_preaudit.role_cli writer release-manifest \
  --project-root /Users/synear/HermesWorkspace/profiles/oa/projects/oa-workspace-minimal \
  --writer-root /Users/synear/HermesWorkspace/profiles/oa/projects/oa-workspace-minimal
```

不得从生产 cwd 推导项目合同路径，不得对单个文件自行计算哈希。只有返回的 `sha256` 与控制台给定 writer 指纹一致才进入提交门禁。

## 提交门禁

1. `apps.oa_approval.frozen_writer status` 返回 `active_run=false`，且没有第二个冻结 Writer；
2. `next-submit` 的 FIFO 队首为 `prepared`；
3. 材料、意见、封套、assignment 与 current proof 属于同一 `requestId + received_at + isolation_key + attempt`；
4. 实时生产待办身份源与项目 list-pending 各唯一命中该版本；
5. 四个冻结文件位于同一 attempt，requestId、received_at、isolation_key、一次性 submission token 与四份 SHA-256 指纹全部匹配。

控制台必须在创建 Writer 前通过 `capture-live-inputs` 完成实时双源固定，只把本次 receipt/request 专用的 `live-inputs.json` 绝对路径随任务交付。Writer 不接收或手工拼接两个 source 路径；canonical 单入口会核验 manifest 路径、两个 final schema、计数与 SHA-256，并要求计数为严格整数、采集时间仍在有效期内。manifest 必须在采集后 300 秒内使用，采集时间不得比当前时间超前 30 秒以上。不得接收 stdout、工具 output、旧 epoch 文件、`last_json` 占位路径、半成品目录或临时会话结果。manifest 过期、时间异常、字段类型错误或来源不可信时，在发 submission token 前统一返回 `queue_input_unreadable`，不得启动生产写入。

门禁通过后只调用下面的 canonical 单入口；不得由模型手工拼接 `begin-submit`、`frozen_writer submit`、回执文件或队列闭合命令：

```bash
cd /Users/synear/HermesWorkspace/profiles/oa/projects/oa-workspace-minimal
python3 -m apps.oa_finance_preaudit.role_cli writer submit-frozen-once \
  --state /Users/synear/HermesWorkspace/profiles/oa/projects/oa-workspace-minimal/runtime/finance_preaudit_queue/state.json \
  --request-id REQUEST_ID \
  --live-inputs-manifest LIVE_INPUTS_JSON \
  --production-root /Users/synear/HermesWorkspace/profiles/oa/projects/oa-workspace-minimal
```

该接口先读取冻结 Writer 状态；只有 inactive 才原子生成 current proof，并连续执行 `begin-submit → frozen_writer submit → 校验 submission_receipt → mark-submitted`。取得 submission token 后不会返回模型控制权或插入查询。窄 Writer 只读取同一 attempt 的 material、remark、remark envelope 和 current proof，逐项复核路径、身份与指纹后提交冻结意见；不得取数、识别流程、读取附件正文、生成判断或修改意见。

命令返回 `elastic_queue_frozen_submitted`，且回执中的 requestId、submission token 完全匹配，并同时满足 `submit_ok=true、done_hit=true、postcheck_done_hit=true` 才算闭合。命令失败时不得再次运行：队列若仍为 `prepared`，说明尚未发 token；若为 `submitting`，必须按实时 OA 状态与绑定回执对账，禁止猜测未写入。

## 写后闭合

- 只有 `submit_ok=true、done_hit=true、postcheck_done_hit=true` 的同一次 final 回执才由 canonical 单入口调用 `mark-submitted`；状态机会更新持久 timing 的 prepared_wait、writer_total 和 attempt_end_to_end。
- 明确未写入才凭绑定 requestId 的 machine no-write proof 调用 `mark-submit-failed`。实际写入或写入状态不明不得重试、补写或标记 submitted，必须停止 writer 并上报 Admin。
- 单据成功或已证明未写入的单据级失败闭合后，立即读取下一 FIFO 队首；唯一写入器保持串行，审核槽位可继续并发准备后续单据。
