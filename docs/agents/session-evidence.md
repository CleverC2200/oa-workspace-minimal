# 按任务定位原始工具证据

排查需要原工具调用或退出事实时，先用 `python -m scripts.session_evidence --task-id <完整任务 UUID> --limit 30`。默认检索当前用户的 sessions 和 archived_sessions，透明读取 `.jsonl` 与 `.jsonl.zst`；压缩文件使用现有 `zstd`，不可用时明确失败，不自行安装。只输出最新有限条工具事件、来源行号、call_id、tool 和白名单会话/退出字段，不打印业务 stdout、提示词或 reasoning。

文件名仅提供候选；`session_meta_matches_task=false` 或 unknown 不表示归属可信。继续核对原任务元数据、工具 call_id、生产 run_id/controller_instance_id 及采样时间。只有从原调用取得的实际 exit_code 才是退出证据；工具函数的 Script completed、输出 hash、文件存在或事件计数均不能代替它。工具输出未结构化时 facts 为空，应定位原文件对应行，而不是用文字猜退出。

指定 `--call-id` 可投影同一调用；`--root` 可换显式只读检索根，`--limit` 范围1～500。display_truncated 仅表示历史事件未全部显示，不把早期失败删除为成功。读取有坏 JSON 时报告 invalid_lines；解压或读取失败给稳定错误。该工具不是脱敏日志上传器，不将原始资料粘贴到 Issue，也不维护第二份问题状态。

OA 实时对账继续使用已发布 `status --summary --output-file`，等待原 session 退出并核对同一文件的 bytes/SHA-256，再读取必需字段；历史会话工具不替代实时门禁或恢复授权。
