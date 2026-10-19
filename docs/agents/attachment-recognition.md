# DeepSeek 附件识别

需要先生成较小的辅助阅读副本时，使用[Agent 图片压缩工具](image-compression.md)，保留原资源和处理回执。副本的压缩成功及可识别状态不代替原图完整读取或清晰度验收。

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

默认目录为 `runtime/finance_preaudit_flows/.attachment-recognitions/<id>/`，也随 `OA_FINANCE_PREAUDIT_FLOW_ROOT` 使用指定的 flow 根。报告在读取原材料前记录 `created_at`、`retention` 和运行状态，结束时记录 `completed_at`；action 模式及直接读取该 flow 根内 attempt 文件时保留所属 attempt。独立的 `retention.json` 只保存这些生命周期字段，作为清理依据，避免大段转写正文影响原材料保护。每次先保存正文、再原子保存生命周期；中断时保留较早的运行状态。识别请求期间不持有维护锁。

现有 `python -m apps.oa_finance_preaudit.cli prune` 按 30 天保留期清理已结束的默认识别输出。绑定 attempt 的输出只有在原 attempt 也允许删除时才清理；活跃、未知写入、最新任务或无法核对的归属继续保留。新近或运行中的识别会阻止其原材料提前归档或删除。受管目录中缺失、损坏或超过 64 KiB 的生命周期记录无法确认归属时，暂停该 flow 根内 attempt 的归档和删除，待人工核对；不根据报告文件时间猜测归属。清理计划只包含路径身份及原因，不包含转写正文。

自定义 `--output-dir`、旧 `runtime/control/attachment-recognitions/` 和缺少可靠时间/归属的旧报告不自动迁移或删除，继续按原证据用途人工处理。自定义输出不得位于保留的 `.attachment-recognitions/` 命名空间，等价路径和符号链接也遵循该限制。需要长期独立保存的结果使用其他自定义目录；修改保留方式不授予生产清理权限。

附件转写请求显式使用 `thinking.type=disabled`，把每页 8192 输出 Token 留给转写回答。工具不形成财务判断；业务审核仍由当前 Reviewer 按规则执行。

源文件按流计算摘要，上限 512 MiB；单页图片、UTF-8 文本及冻结 JSON 的读取上限为 32 MiB，格式展开还遵循已有准备器的限制。大 PDF 或 Excel 按页或内嵌图发送，不把容器整体放进 API 请求。图片每次 HTTP 请求只有一页；PDF、Office、XML、OFD 复用现有本地展开，UTF-8 文本直接分段。相同类型和内容的重复页在本次问题内复用回答，各自保留来源映射。每次调用从空上下文开始，不累计前页图片。不支持的格式和展开失败明确返回失败，不标为已识别。只有文件记录、没有本地正文的附件仍未读取，工具不下载或猜测正文。

`status=complete` 只证明当前选定输入已获得未截断的 API 回答，并通过来源完整性检查。它不证明答案正确、问题覆盖充分或业务通过。`status=failed` 时保留已完成分段、失败位置和错误码，不自动重试；审核者不得把失败或空答案改写成全项空警告提交。API 默认每请求 90 秒、整次 1800 秒，均可通过 `--timeout-seconds`、`--budget-seconds` 调整到工具允许范围。

视频附件按扩展名跳过正文读取，包括 MP4、MOV、M4V、AVI、MKV、WebM 等，具体范围由准备器的 `VIDEO_EXTS` 维护。原文件仍保留归属和完整性摘要，不解码画面或音频，不生成帧图，也不发送视频给模型。准备清单记录 `video_skipped` 和 `body_read_status=not_read`，压缩包中的视频同样保留跳过记录。采集材料的 `video_attachment_skipped` 提醒审核者只采用已读取证据；需要视频才能确认的事项须说明具体缺口并交人工核对，不能由文件名或其他相似材料补猜。

识别报告通过 `skipped_sources` 列出未读取的视频，`coverage=non_video_only` 表示读取范围排除了视频。混合输入的 `status=complete` 只涵盖其余已准备内容；仅视频输入返回 `status=skipped`、零分段，不调用模型。历史冻结的视频帧保持原样，仍按历史准备页校验和读取，不追溯改写已有记录。其他格式不支持、文件缺失、展开或读取失败继续按原完整性合同拒绝。

响应不完整或格式无效时，`failed_response` 仅保存白名单终止原因、回答字符数、是否返回工具调用及数值 Token 用量。`length` 表示输出达到预算，`stop` 且字符数为零表示没有最终回答；未知终止原因记为 `unknown`。不保存残缺回答、推理正文、工具参数或任意响应字段；旧报告未保存这些字段时不能追溯补造终止原因。

识别回答是模型读取结果，不是官方主数据，也不证明附件归属、OA 发票关联、付款费用关系或未显示事实。审核仍按[证据阅读规则](../../.agents/skills/oa-finance-preaudit/references/observation-contract.md)绑定原资源和审核项。金额、主体、日期、里程等高影响原值需定向复读并按规则验算；不稳定内容保留具体缺口。包装生产的原图观察及独立复核回执合同继续单独验收，本工具的文本回答不能替代其 App 完成回执。

API 图片输入合同见 [DeepSeek 官方文档](https://api-docs.deepseek.com/guides/vision)。

图片任一边超过官方单图 8192 像素限制时，识别入口按原像素切分为有重叠的无损 PNG 分区，逐区请求，不缩放或覆盖原图。每块继续满足单图尺寸及字节上限；相邻分区重叠内容不能重复计入金额或数量。`parts[*].image_tiles` 保存原图坐标、各区回答及用量，页面回答按坐标汇总，原页引用和内容指纹仍对应原文件。全部分区成功才算该页读取完成；任一区失败时，`failed_part` 保留已完成分区和失败坐标，整次状态仍为 failed，不重试或生成意见。分区请求共用原整次预算，实际调用和成本可能增加；用量缺失保持未知。

尺寸读取与裁切使用固定依赖 Pillow。尺寸合规的图片仍发送原字节，包括 WebP 和 GIF；超限动画明确返回 `attachment_animated_image_too_large`，不能只读首帧就声称完整识别。

Pillow 默认像素保护保持启用。只有默认读取因像素保护拒绝时，入口才允许单帧 RGB8、三通道 baseline JPEG 使用同一 Pillow JPEG 解码器继续读取，并要求原图宽×高×4 不超过 1 GiB，即最多 268,435,456 像素。该预算仅估算底图缓冲，分区、PNG 编码及请求数据另占内存；这是完整底图解码，不是流式读取。原图不缩放，后续仍遵循已有分区、字节和整次时间预算。

此受限路径不接受 progressive、其他 JPEG 编码、CMYK、带 MPF 的 MPO 或非 JPEG 巨幅图，返回 `attachment_image_large_decode_unsupported`；超过底图预算返回 `attachment_image_decoded_memory_limit`。安全拒绝不表示原件损坏。无法解析或裁区解码发现损坏时返回 `attachment_image_invalid`。正常尺寸输入继续使用原格式支持和默认读取路径。
