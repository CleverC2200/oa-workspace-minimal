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
apps/oa_finance_preaudit/           # 材料收集、冻结结果与角色窄入口
apps/oa_approval/                   # OA 和外部数据的复用连接能力
tests/finance_preaudit/             # 聚焦合同测试与回放测试
docs/agents/                        # Issue、标签和领域文档约定
```

## 本地配置

```bash
cp .env.example .env
```

只在本地填写真实配置。数据库连接没有仓库内默认账号或地址；如需从 macOS Keychain 读取密码，需同时配置 `OA_DB_USER` 和 `OA_DB_KEYCHAIN_SERVICE`。

## 只读 preview

先阅读 [Skill 说明](.agents/skills/oa-finance-preaudit/SKILL.md)，再按 requestId 收集单据：

```bash
python3 -m apps.oa_finance_preaudit.cli collect-case \
  --request-id REQUEST_ID \
  --allow-browser-download
```

需要从已办流程选择测试样本时，可先读取候选索引：

```bash
python3 -m apps.oa_finance_preaudit.cli list-handled \
  --page-size 200 \
  --max-pages 5
```

候选标题不能证明流程身份，必须以 `collect-case` 返回的 `workflow_key` 为准。

## 脱敏运行指标

每个 attempt 的 `observability.json` 只保存 attempt 哈希、流程、阶段、耗时、结果和平台精确 usage；没有精确 usage 时固定记为 `unknown`，不做估算。汇总按审核、沟通协调、等待和提交四类拆分：

```bash
python3 -m apps.oa_finance_preaudit.observability summary \
  --log /absolute/attempt/observability.json
```

平台在 Agent 闭合后返回精确 usage 时，使用同一 `item_key + event_id` 的 `usage` 命令补充；重复回放不会重复计数，冲突值会被拒绝。指标缺失只影响监控，不阻断审核或提交。

OA 提交只通过冻结写入入口执行。锁、token、冻结指纹和终态回执均绑定单个 `requestId + received_at + attempt`：同一 requestId 单飞并幂等重放，不同 requestId 可由 `submission_pool.run_bounded` 有界并发。生产写入仍需单独授权；本仓库测试只使用模拟接口。

## 单流程调度 dry-run

五个定时入口的唯一配置位于 `config/flow-runners.json`。每个入口只加载一类流程规则，并使用独立的状态目录和指标目录；旧小时 dispatcher、当天控制台和跨流程 FIFO 不进入新热路径。

候选文件只接受当前流程的 queued、prepared 或 submitting 单据。以下命令只计算本轮容量和闭合状态，不读取附件、不形成意见、不调用 OA：

```bash
python3 -m apps.oa_finance_preaudit.flow_runner \
  --workflow-key travel_expense \
  --candidates-file /absolute/path/to/candidates.json \
  --available-reviewer-slots 3 \
  --available-submit-slots 3
```

空队列立即闭合；积压按配置和实际槽位的较小值并发。配置不会创建或启用生产 cron，生产入口仍保持暂停，需另行完成人工验收后逐个启用。

## 验证

```bash
python3 /Users/synear/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .agents/skills/oa-finance-preaudit
python3 -m unittest discover -s tests/finance_preaudit -p 'test_*.py' -v
git diff --check
```

贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。V1.2 的流程拆分、并发提交、监控、留存与代码质量工程在 GitHub Issues 中分票推进；已合并能力只代表代码和测试完成，不代表生产已经启用。
