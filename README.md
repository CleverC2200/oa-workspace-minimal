# OA 财务预审与包装文字校对

当前版本：`1.1.0`

本项目以 Codex Skill 为审核入口：Codex 读取表单、附件和权威查询结果，形成四类财务流程的预审意见，以及包装版面的图片文字校对结果；Python 代码只负责稳定取数、材料冻结、确定性校验和受控提交，不生成业务判断。

V1.1 是首次 GitHub 私有发布基线。发布不会启动定时任务，现有自动化继续保持暂停，直至后续改造票据完成并单独验收。

## 范围

- 差旅费报销
- 业务招待费
- 业务支持费
- 小额市场费
- 包装新制（更改）及包装版面审批

财务流程只给出预审意见，不代替批准或退回。包装流程只校对图片文字，并在存在 Excel 时逐字符对照，不扩展为法规合规审核。

## 设计边界

- Skill 维护流程规则、证据关系和输出格式。
- `apps/oa_finance_preaudit/` 维护单据材料、任务状态和冻结结果。
- `apps/oa_approval/` 复用 OA、附件、商旅、发票、只读 SQL 和最小提交能力。
- 手工调用默认只读 preview；写入必须有当前明确授权，并使用已冻结、可追溯的同一单据材料。
- 密钥、账号、内部地址、数据库和运行材料只保存在本地环境，不进入 Git。

## 目录

```text
.agents/skills/oa-finance-preaudit/  # Skill 入口、流程规则与证据契约
.agents/skills/oa-finance-preaudit/references/flows/  # 五类隔离规则包
apps/oa_finance_preaudit/           # 单一深模块、JSON CLI、状态与观测
apps/oa_approval/                   # OA 和外部数据的复用连接能力
tests/finance_preaudit/             # 聚焦合同测试与回放测试
docs/agents/                        # Issue、标签和领域文档约定
```

## 本地配置

```bash
mkdir -p ~/.config/oa-workspace-minimal
chmod 700 ~/.config/oa-workspace-minimal
cp -n .env.example ~/.config/oa-workspace-minimal/oa-auth.env
chmod 600 ~/.config/oa-workspace-minimal/oa-auth.env
```

只在工作树外的 `~/.config/oa-workspace-minimal/oa-auth.env` 填写真实 OA 配置；可用进程环境变量 `OA_AUTH_ENV_FILE` 指向另一个绝对位置。运行时优先使用进程环境和稳定配置文件，仓库内被忽略的 `.env`、`.env.oa-auth` 仅作兼容回退。所有配置文件必须是普通文件且禁止组或其他用户访问，否则安全失败。OA 执行人标识只允许通过本地 `OA_UID` 提供，流程 JSON 明确拒绝内嵌账号。数据库连接没有仓库内默认账号或地址；如需从 macOS Keychain 读取密码，需同时配置 `OA_DB_USER` 和 `OA_DB_KEYCHAIN_SERVICE`。完整的数据与凭据边界见[安全说明](SECURITY.md)。

## 只读 preview

四类财务流程和包装版面校对已经提供同一个本地深模块两动作接口。先取得指定流程的下一个动作：

```bash
python3 -m apps.oa_finance_preaudit.cli next-action \
  --workflow WORKFLOW_KEY
```

返回 `review_required` 时，只读取 `review_packet_file`，按其固定审核项生成结构化决定，再完成本轮：

```bash
python3 -m apps.oa_finance_preaudit.cli complete-review \
  --action-file /absolute/path/to/action.json \
  --decision-file /absolute/path/to/review-decision.json
```

两条命令默认都是 preview，动作固定 `write_authorized=false`，写入状态固定为 `not_requested`；不会调用 OA 提交。模块和 CLI 共用同一业务实现，大对象通过本地文件传递，stdout 只输出一个版本化 JSON。冻结材料、流程身份或持久状态不一致时返回正常的 `needs_admin` 动作并停止当前单据，不生成意见。五类流程的目录、锁和当前动作投影相互独立；积压中已有单据等待审核时，后续调用可取得另一单据，每个 attempt 使用自己的完成锁，互不等待。

正常运行只使用 `next-action`、`complete-review`、`status` 和 `prune` 四个命令。候选发现、材料收集、冻结校验、单据锁、提交安全门和回执均隐藏在深模块内；没有角色 CLI、底层队列命令或跨任务消息协议。

## 脱敏运行指标

每个 attempt 的 `observability.json` 只保存 attempt 哈希、流程、阶段、耗时、结果和 Token 状态。阶段拆为机械取数、审核脚本、可选 exact 等待、提交和协调；协调秒数与 Token 必须为 0。FlowEngine 不猜测 Agent 活动或等待，宿主没有提供绑定当前 item 的 exact timing receipt 时，两者保持 unknown 并停止等待比例调优。当前运行固定记录 `token_usage=unknown`，不做估算；`status` 按单据哈希、流程和 Asia/Shanghai 日期给出 P50/P95 与 Token 完整性。重复事件不会重复计数，冲突值会被拒绝；提交前的必要事件无法保存时停止当前单据，提交后的指标异常不会触发 OA 重放。

内部离线验收报告固定检查审核项/资源/页面/格式/golden replay 100%、重复提交/跨单据混用/unknown 自动重试/Agent 消息 0，以及 P95 小于 900 秒、等待比例小于 20%、技术错误率不高于 2%。每流程不足 20 个闭合脱敏样本时不建议调参；缺少平台精确 usage 时只停止 Token 调优。真实 `python -m` 入口会把四命令的成功、参数、JSON 合同和文件传递结果写入本地 30 天脱敏 CLI 日志；进程尚未启动时的失败和人工复现由本地 launcher/harness 补充。日志不含 argv、stderr 或路径；status/prune 每次只记一条 global 调用，按流程展示时投影影响范围，但 overall 不重复计数。未知值不能推断为 0。只有 CLI 故障严格多于其他技术故障且证据完整时，报告才允许提出独立 stdio MCP 后续 Issue；本项目当前不实现或安装 MCP。

OA 提交只通过冻结写入入口执行。授权证明、锁、token、冻结指纹和终态回执均绑定单个 `requestId + received_at + attempt` 及其材料版本：同一 attempt 单飞并幂等重放，不同单据可以并发。受控写入由同一个 CLI 向 FlowEngine 注入授权源、外部 verifier 和提交适配器；必须同时满足本地流程授权清单和两步命令各自的显式 `--confirm-write YES`，缺一不可：

```bash
python3 -m apps.oa_finance_preaudit.cli next-action \
  --workflow WORKFLOW_KEY \
  --mode controlled-write \
  --confirm-write YES

python3 -m apps.oa_finance_preaudit.cli complete-review \
  --action-file /absolute/path/to/action.json \
  --decision-file /absolute/path/to/review-decision.json \
  --confirm-write YES
```

流程授权清单只保存在被 Git 忽略的 `runtime/control/write-policy.json`。授权源用本地 OA 应用密钥对单据版本、attempt 和材料指纹生成一小时有效的绑定证明；提交前 verifier 再验签、复核流程授权清单、冻结文件和当前 OA 待办版本。测试只使用模拟接口，不会写真实 OA。

## 单流程运行

每次 `next-action` 只处理参数指定的一类流程，规则包、状态目录和当前单据相互隔离。空队列立即闭合；积压并发由调用方对不同单据重复使用同一双动作接口实现，不在审核上下文建立角色或消息协调。仓库合并不会创建或启用生产 cron；每类流程仍需单独完成真实环境验收和当前授权后，才能建立并启用自己的定时入口。

## 留存与清理

结构化日志、单据级脱敏指标、冻结材料和临时附件保留 30 天；不含单据哈希的日/流程聚合保留 180 天。每类流程只保留最新一轮可见任务，旧的已闭合任务先归档，严格超过 30 天后才删除。未闭合任务或写入结果 `unknown` 的恢复证据始终保留，但超过 30 天不进入原始分析窗口。清理以创建时间和最后事件时间中较晚者计算年龄，避免老任务刚闭合就被删除。

人工、Codex 和后续定时任务使用同一个只读状态入口；输出只有脱敏哈希、计数、年龄、三维状态和失败码：

```bash
python3 -m apps.oa_finance_preaudit.cli status \
  --root /absolute/path/to/finance_preaudit_flows
```

需要同时核验稳定凭据、OA 认证、执行身份绑定和实时待办映射时，在同一命令增加 `--check-live`。它只返回配置来源、健康状态和按流程聚合的数量，不输出配置值、账号、单号或附件内容，也不领取、冻结或提交单据：

```bash
python3 -m apps.oa_finance_preaudit.cli status --check-live
```

受控清理使用同一 JSON CLI。它在同一维护锁和事件锁内先计算脱敏日聚合，完整校验计划与所有目标，再以不跟随符号链接的原子写入落盘，最后才归档或删除原始 attempt：

```bash
python3 -m apps.oa_finance_preaudit.cli prune \
  --root /absolute/path/to/finance_preaudit_flows
```

`prune` 要求运行根已存在专用 `.oa-retention-root` 标记，不会自动标记目录。未标记根、符号链接、绝对目标或越界目标会在任何变更前整体失败。仓库不会自动清理现有生产目录，也不连接真实任务系统；生产启用需要另行验收。

## 离线质量回放

五类流程各有正常、业务异常、证据不足和技术失败四个脱敏样本。回放比较固定审核项、状态/停止码、格式、耗时和精确 Token，不需要 OA 登录：

```bash
python3 -m pytest tests/finance_preaudit/test_flow_evals.py -v
```

离线样本不包含原始附件或业务身份，性能改善不得以准确性和格式回归为代价。回放实现属于开发质量模块，不是正常运行入口。

## 代码质量门

GitHub PR 的 `quality` 检查在无业务凭据环境运行 Skill 校验、文本格式、敏感文件/密钥扫描、Markdown 链接、PR 单 Issue 合同、Python 语法和完整离线测试。贡献者可在本地运行：

```bash
python3 -m scripts.quality skill
python3 -m scripts.quality format
python3 -m scripts.quality hygiene
python3 -m scripts.quality links
python3 -m pytest -v
```

仓库继续保持私有，不配置 GitHub 服务端分支保护。`main` 的普通变更仍按本地维护规则通过质量门和 PR 合并，但这不是平台强制；具体见[main 本地管理规则](docs/agents/branch-protection.md)。CI 不读取真实 OA 凭据，也不启用生产自动化。

## 验证

```bash
python3 -m scripts.quality skill
python3 -m pytest -v
git diff --check
```

贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。V1.2 的流程拆分、并发提交、监控、留存与代码质量工程在 GitHub Issues 中分票推进；已合并能力只代表代码和测试完成，不代表生产已经启用。
