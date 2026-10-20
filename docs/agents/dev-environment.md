# 开发环境预检与验证

开发入口使用仓库的 [.codex/environments/environment.toml](../../.codex/environments/environment.toml)，
沿用 [Codex Local environments / Actions](https://developers.openai.com/codex/app/local-environments)。
配置格式已与本机原生生成文件核对。当前命令面向 macOS/Linux；桌面按钮的现场执行与跨平台验收仍须单独核验。

setup 和“只读预检”只运行：

```bash
python3 -B -m scripts.dev_environment preflight
```

预检不安装、不联网、不切分支、不 stash、不改索引或其他工作树。报告 Git common directory 所属克隆的角色、
实际项目路径、分支、HEAD、`origin/main` 修订和差异、脏状态，以及 main 占用情况。
`--baseline <已有分支或固定 SHA>` 用于明确比较版本；`--switch-to <分支>` 只报告占用，不执行切换。
缺少本地基线时报告缺口，不能自动 fetch 或把当前 HEAD 当基线。脏开发工作树允许检查，但验证记录不标为可复用。

开发与生产角色沿用 [固定克隆约定](../../CONTRIBUTING.md#开发与生产隔离及发布)：通过 Git common directory
识别 `oa-workspace-dev` 与 `oa-workspace-minimal`，因此托管开发工作树继承开发克隆角色；未知克隆明确报告未知，
不开放依赖准备或验证。依赖准备及验证还要求 `codex/` 开发分支。目录角色是操作检查，不能代替业务权限门禁。

预检检查 git、uv、RTK、共享环境的实际 Python 3.11、完整 `requirements-dev.lock` 版本、依赖目录和模块来源。
`requirements-dev.txt` 维护直接版本，锁文件固定全部间接版本；现有 `scripts.quality environment` 同时检查直接版本与锁文件一致。
预检的 `environment.virtual_environment` 与 `environment.executable` 是当前工作树应使用的依赖环境。
简要输出直接显示实际 `module_origin` 和该项目 CodeGraph 新鲜度；环境缺失时来源保持 unknown。
需要完整路径和修订时执行 `python3 -B -m scripts.dev_environment --json preflight`。
源码检索和审核工具须针对报告中的实际工作树。

开发环境统一保存在 `~/.cache/oa-workspace/dependencies/`，不在每个工作树安装 `.venv`。
同一个锁文件、Python 实际路径及完整版本、平台、架构和固定 uv 版本共用一个环境。
直接版本变化必须更新锁文件；锁文件变化创建新环境，已准备环境保持只读，不原地同步或升级。
`OA_DEVELOPMENT_ENV_ROOT` 可指定工作树外的私有环境根目录，所有参与工作树应使用同一位置。
环境准备持有跨进程锁，只准备一次；准备失败仅清理由本次创建的不完整目录，保留命令失败证据。
`uv` 使用本机原生下载缓存。安装采用 copy，冻结环境时不会修改缓存文件权限。
生产虚拟环境与开发共享环境继续独立；历史工作树的 `.venv` 不自动迁移或删除。

模块来源检查不导入应用、不加载认证，不把相同文件名当同一源码。
探针用 `-S` 禁用 site 初始化，也不传入业务环境变量。拒绝系统 site-packages、依赖目录逃逸、
共享 runtime、可写依赖及不匹配的准备清单。未知启动钩子须检查；uv 当前原生引导文件只按已核验字节和指纹识别，
探针始终不执行它。版本变化后需要检查新的引导文件。

“准备开发依赖”是独立显式 Action：

```bash
python3 -B -m scripts.dev_environment prepare
```

仅在开发克隆的开发分支执行。使用已安装的 Python 3.11 和当前固定 uv 版本创建共享环境，
随后执行 `uv pip sync --python <共享解释器> --link-mode copy requirements-dev.lock`。
缺 Python、uv 版本不符、未知既有目录或命令失败均保留非零状态。
环境准备不改变直接声明或锁文件。升级依赖时在开发分支更新直接声明，并重新生成完整锁：

```bash
uv pip compile requirements-dev.txt --python-version 3.11 --universal --no-header --no-annotate --output-file requirements-dev.lock
```

“静态检查”、“聚焦环境回归”、“聚焦执行回执回归”和“完整离线验收”直接复用 `scripts.quality` 和 pytest：

```bash
python3 -B -m scripts.dev_environment verify --check lint --check schema
python3 -B -m scripts.dev_environment verify --tests tests/finance_preaudit/test_dev_environment.py
python3 -B -m scripts.dev_environment verify --tests tests/finance_preaudit/test_execution_readback.py
python3 -B -m scripts.dev_environment verify --full
```

可重复 `--check` 或 `--tests`；测试路径必须在本项目 tests 下。完整验收沿用现有质量集合、语法检查及全树测试，
事先检查磁盘与活动控制器；控制器活动时推迟完整验证，不停止业务进程。完整检查不会因前一项失败而丢失后续结果。
每项记录退出码和 passed/failed/not_run；任何必需项失败或未执行，整体非零。退出码保留首个可用失败码，
被信号终止或无法启动时整体为1，并在对应项保留原码或未执行原因。

验证子进程只收到工具所需环境，不继承 OA/模型密钥或 Python 注入路径，使用临时 HOME 和工作树外的空私有认证文件。
工作树存在 `.env` 或 `.env.oa-auth` 时不读取内容并拒绝验证；使用无凭据的隔离开发工作树运行。
这不改变业务认证实现，也不授权 OA、模型、自动化或生产操作。现有测试仍须用合成数据和禁止真实业务端口的替身。

默认输出简要状态；失败保留完整原始 stdout/stderr，RTK 使用 proxy 保留精确故障。
全局 `--json` 输出源修订、脏状态、解释器、锁文件指纹、检查集合和每项终态。
原始失败输出可能包含项目路径或测试数据，分享前按仓库隐私约定检查。

CodeGraph 记录明确项目、分支、HEAD，索引新鲜度固定为未核验；预检不创建或更新图谱。
跨文件查询前用图谱工具核对该项目和修订；缺索引或无法核验时使用源码检索，不能借用其他分支的图谱证明发布源码。

读取审核参考时先通过[财务预审 Skill](../../.agents/skills/oa-finance-preaudit/SKILL.md)确认审核包 `workflow_key`，
再从[结果接口与流程路由](../../.agents/skills/oa-finance-preaudit/references/result-contract.md)只加载匹配流程规则，
完整读取[证据阅读规则](../../.agents/skills/oa-finance-preaudit/references/observation-contract.md)。
包装流程同时加载[中文图片文字校对](../../.agents/skills/audit-chinese-image-text/SKILL.md)。
实际单据的 next-action、complete-review 和 OA 写入仍需独立授权，预检和离线测试不调用它们。
原执行的只读进度及终态命令见[控制器执行回执](controller-execution.md#只读进度等待与终态取回)。

## 定位文档与核对命令

在预检报告的实际工作树根目录定位已有文档，操作 GitHub 前读取本机对应子命令帮助：

```bash
rg --files docs/agents
gh issue comment --help
gh issue close --help
```

已授权补充验收并关闭 Issue 时，先把完整多行评论保存到 `.scratch/issue-comment.md`，
使用 `--body-file` 保留换行。确认评论成功后，再单独执行关闭命令：

```bash
gh issue comment <编号> --repo CleverC2200/oa-workspace-minimal --body-file .scratch/issue-comment.md
gh issue close <编号> --repo CleverC2200/oa-workspace-minimal --reason completed
```

`gh issue close` 的 `--comment` 接受字符串，不提供 `--comment-file`。
关闭条件及尚缺生产验收时的处理沿用[Issue 追踪](issue-tracker.md)，不因本地测试通过直接关闭。

`python -m scripts.quality environment` 在现有质量 CI 中校验配置；环境命令行为回归随现有全树测试执行。
配置/命令测试通过不代表原生桌面按钮已现场验收，更不代表真实业务或生产恢复。
