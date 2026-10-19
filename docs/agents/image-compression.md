# Agent 图片压缩

需要将本地静态图片缩小到阅读或传输预算时，调用以下入口。支持 JPEG、PNG、WebP 和单帧 GIF，复用仓库固定的 Pillow 依赖。工具只处理本地文件，返回 JSON，不访问 OA 或模型。

```bash
python -B -m apps.oa_approval.image_compression \
  --file /absolute/path/source.jpg \
  --output-dir /absolute/path/new-private-directory \
  --max-dimension 8192 \
  --max-pixels 16000000 \
  --max-bytes 8388608
```

输出目录必须尚不存在，且在冻结 attempt 之外。省略目录时使用当前工作树的 `runtime/image-compressions/<新UUID>/`，这些副本由调用者保管，不参与审批材料自动清理。原文件保持原样，处理目录与文件为私有权限。

Agent 按以下顺序使用结果：

1. 核对退出码为 0、`status=complete`，并读取 `report_file` 指向的 `compression.json`。失败时退出码为 1，读取 `error_code`，保留失败记录。
2. 核对 `source.path/sha256` 与当前资源一致，采用 `output.path` 的副本，并保留原资源归属和处理回执。回执包含原图及副本尺寸、字节数、指纹、解码尺寸、方向修正、缩放及 JPEG 有损编码标记。
3. 在实际阅读时核对小字、金额、日期、印章及颜色。`readability=not_verified` 表示工具没有验证文字清晰度或财务事实；缩放及 JPEG 重编码可能丢失细节，EXIF/ICC 等元数据不保留。

财务原图的完整识别继续使用[附件识别入口](attachment-recognition.md)。本工具生成辅助阅读副本，不把副本当作原图完整读取，不自动替换冻结页、解除识别失败或触发提交。无法从副本确认的原值应保留未知；需要读取原图细节时回到原资源的受控读取路径。包装原图观察和独立复核合同保持。

目标参数及范围以 `--help` 为准。PNG 或透明图片输出 PNG，其余输出 JPEG，按比例缩小且不放大；字节仍超限时继续减小尺寸，最多 24 次编码，无法满足时明确失败。`--quality` 只控制 JPEG 编码质量，不保证内容清晰，也不保证每张原本很小的图片字节数进一步减少。

处理在独立子进程中进行。源文件上限 32 MiB、源像素上限 3 亿、实际解码上限 6400 万像素；JPEG 在加载前使用解码缩小，超大渐进式 JPEG、非 JPEG 超解码预算及动画直接拒绝。子进程 CPU 上限 30 秒，外层等待默认 60 秒，超时终止并等待子进程退出。像素阈值只在该子进程内调整，调用者及现有附件识别器的保护不变。

Linux 子进程附加 1 GiB 数据段限制。macOS 不支持本工具使用的有限数据段限制，回执明确 `worker_data_limit_enforced=false`，仍执行像素、CPU 和等待限制；这不是 macOS 的 RSS 硬上限承诺。运行预算和实际平台限制分别记录在回执中。

Pillow 的惰性打开、JPEG 解码缩小与像素保护依据见 [Pillow Image 文档](https://pillow.readthedocs.io/en/stable/reference/Image.html)。压缩完成只证明文件处理与目标限制通过。
