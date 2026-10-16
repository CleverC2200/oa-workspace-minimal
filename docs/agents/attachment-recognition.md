# DeepSeek 附件识别

需要读取财务附件或大图片时，使用本地 Python 工具直接调用 DeepSeek HTTP API。子 Agent 只接收文本结果，不通过看图工具把原图传入自己的会话。工具不启动 Codex CLI 或其他模型进程，不访问 OA，也不形成或提交审批决定。

在当前 checkout 的 Python 环境执行。凭据从进程环境或现有 OA 私有配置的 `OA_REVIEW_API_KEY` 读取，不放在参数或聊天中。

```bash
python -m apps.oa_approval.attachment_recognition \
  --file /absolute/path/image.png \
  --file /absolute/path/details.pdf \
  --prompt '逐页读取付款金额、最终状态、实际收款对象和日期；看不清或没有显示的内容分别说明。'
```

也可直接读取当前冻结动作的 `prepared.images` 和 `prepared.structured_contents`，保留原资源、明细、表格位置和页引用：

```bash
python -m apps.oa_approval.attachment_recognition \
  --action-file /absolute/path/attempt/action.json \
  --prompt '完整识别每页文字、表格、数字、单位、日期、印章及照片可见内容，分别说明无法确认的内容。'
```

`--prompt` 是本次问题，可以自由修改；不传时使用完整识别的默认问题。定向复读可对单页 `--file` 再提问，例如“只读取仪表累计里程与单位，区分小计里程”。定向回答仅覆盖所问事实，不能据此声称已经完成整页其他事项或整单审核。

标准输出只返回状态、结果文件、分段数量和错误码。读取返回的 `report_file` 获得 `parts[*].answer`，其中保留问题、模型、来源、页码或文本分段及 Token 用量。结果文件和展开页保存在独立私有目录，不覆盖冻结材料。可用 `--output-dir` 指定尚不存在的结果目录；冻结动作模式拒绝写入该 attempt 内部。

图片每次 HTTP 请求只有一页；PDF、Office、XML、OFD 复用现有本地展开，UTF-8 文本直接分段。相同类型和内容的重复页在本次问题内复用回答，各自保留来源映射。每次调用从空上下文开始，不累计前页图片。不支持的格式和展开失败明确返回失败，不标为已识别。只有文件记录、没有本地正文的附件仍未读取，工具不下载或猜测正文。

`status=complete` 只证明当前选定输入已获得未截断的 API 回答，并通过来源完整性检查。它不证明答案正确、问题覆盖充分或业务通过。`status=failed` 时保留已完成分段、失败位置和错误码，不自动重试；审核者不得把失败或空答案改写成全项空警告提交。API 默认每请求 90 秒、整次 1800 秒，均可通过 `--timeout-seconds`、`--budget-seconds` 调整到工具允许范围。

识别回答是模型读取结果，不是官方主数据，也不证明附件归属、OA 发票关联、付款费用关系或未显示事实。审核仍按[证据阅读规则](../../.agents/skills/oa-finance-preaudit/references/observation-contract.md)绑定原资源和审核项。金额、主体、日期、里程等高影响原值需定向复读并按规则验算；不稳定内容保留具体缺口。包装生产的原图观察及独立复核回执合同继续单独验收，本工具的文本回答不能替代其 App 完成回执。

API 图片输入合同见 [DeepSeek 官方文档](https://api-docs.deepseek.com/guides/vision)。
