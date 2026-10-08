# 生产源码发布门

开发改动合并远端后，生产仍需要独立授权的源码同步。统一入口是 `apps.oa_finance_preaudit.production_release`；默认只读预检，`--confirm-sync YES` 才请求更新源码。两种模式都不提交审批、不改停止标记、不启停自动化、不安装依赖、不自动 fetch。

先现场核实目标为生产克隆的唯一干净 `main`，明确预期远端提交、生产项目 ID，以及本轮应保留的实际调度频率。授权范围内 fetch 后，从已审查的开发候选执行下列入口。`--automation-file` 使用本机原自动化 TOML；不得把它、凭据或现场业务证据提交到仓库。

```bash
python -m apps.oa_finance_preaudit.production_release \
  --target-root /Users/synear/HermesWorkspace/profiles/oa/projects/oa-workspace-minimal \
  --expected-revision "$release_revision" \
  --automation-file "$approval_automation_file" \
  --production-project-id "$production_project_id" \
  --expected-schedule "$preserved_schedule"
```

上面的变量必须来自已核实的当前发布和调度配置；不能把待验证值直接复制为期望值。预检通过后，在同一已授权入口追加 `--confirm-sync YES`。程序将重新预检，不依赖上一次缓存回执。

门禁要求生产独立克隆、唯一工作树、干净 `main`，实际 origin 与预期私有仓库相符，实时远端、`origin/main` 和预期发布提交一致，现有 HEAD 可快进。配置检查读取预期提交的 Git 文件内容，避免旧模板漂移阻止修复升级；同步后再检查实际生产树。实际自动化必须符合原生 GPT 6.1 Sol、medium、default、初始 3、最高 6、既有六财务流程和已确认项目及调度合同。

整个实时状态检查、源码更新和末尾检查持有目标 runtime 的非阻塞 controller/global Writer 租约。请求 Writer 或旧 Writer 活动时拒绝。状态子进程使用目标自己的 `.venv` 和源码，等原进程终态成功后按完整证据文件的路径、字节数及 SHA-256 复核；不使用缓存 JSON 或候选源码注入生产解释器。租约内只允许本维护持有的 controller/global Writer；上一控制器必须已终态、子进程已回收、支持新进程重启。历史 manifest 保持原样，即使其代码版本早于当前生产源码。

所有停止标记与环境停止请求均拒绝。当前身份、凭据、OA/web 认证及完整对账 continuation 必须可信；当前未知写入或 pending/handled 冲突拒绝。目标自身解释器另用既有只读校验器核验当前 write-policy 对六类财务和包装的授权，以及当前待办本地 attempt 的恢复合同：review_required 核验完整冻结资料，collecting 核验四个采集落盘阶段的原身份、已有材料、恢复 provenance 和未写入边界，可信 item 隔离回执核验失败产物绑定，仅材料收集失败免于重验已损坏材料；生成绑定本次 status SHA 的 current-input proof，不凭 continuation 推断这些条件。可信单据隔离允许保留，不因发布恢复、重试或清理单据。停止标记存在时，先按[停止恢复](blocking-recovery.md)的受控维护合同完成恢复；禁止直接删除标记或带停止同步源码。新恢复入口尚未在生产时，仅允许已授权、已审查候选的窄小停止维护，以显式生产目标运行生产现有只读状态子进程，修改范围限停止标记及私有审计回执；此维护不执行 OA 写入。业务资格未满足时保留停止门。

成功回执区分 `ready` 和 `synced`，记录更新前后 SHA、完整状态证据引用及 continuation，明确 `write_state=not_requested`、`production_acceptance=not_verified`。若快进尝试后的检查失败，API 和命令均返回 blocked、原细码、before_revision、after_revision、requested_revision，以及 source_sync_state=updated/not_updated/unknown；读取 HEAD 失败时明确 unknown。next_process_ready=false、production_acceptance=not_verified 保持，不能把失败当作未更新，也不能自动回滚、重试或提交。只有源码同步和末尾检查通过，下一轮新进程才具备加载条件；实际生产恢复仍须该轮版本、运行及提交后查证据。
