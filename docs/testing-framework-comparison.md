# Python 测试框架决策：只保留 pytest

> 决策与实施日期：2026-08-22

## 结论

本项目只保留 Python 3.11 + `pytest==9.1.1`，统一全量入口为：

```bash
python3 -m pytest -v
```

测试可以继续使用标准库 `unittest.mock`。它只提供 mock、patch 和 autospec，不是第二个测试运行框架。

不引入 `pytest-xdist`、`pytest-mock`、`pytest-subtests` 或测试 MCP。当前全量测试不足 2 秒，额外插件不会带来足够收益，反而扩大依赖和兼容面。

## 对比

| 维度 | unittest | pytest | 本项目结论 |
| --- | --- | --- | --- |
| 依赖 | Python 标准库内建 | 独立开发依赖 | 固定 pytest 版本并统一 Python 3.11 |
| 测试结构 | `TestCase`、`setUp`、`tearDown` | 普通函数/类、fixture、普通断言 | pytest 样板更少，结构更直观 |
| 参数化 | 循环和 `subTest` | `parametrize` 独立收集每个案例 | pytest 的失败定位和场景统计更清楚 |
| 临时资源 | 手工创建与清理 | `tmp_path`、`monkeypatch` 自动回收 | 减少目录和环境恢复样板 |
| 失败反馈 | 断言方法输出 | 断言改写、短摘要、node id | 人和 Agent 都更容易定位失败 |
| 测试选择 | 模块、类、方法 | 路径、node id、`-k`、marker、失败缓存 | pytest 聚焦验证体验更好 |
| Mock | `unittest.mock` | 可直接复用 `unittest.mock` | 不增加 pytest-mock |
| 并行 | 核心 CLI 不提供 | xdist 插件提供 | 当前套件太快，不并行 |
| 扩展性 | runner/suite 扩展 | hook 和插件生态 | 保持零插件，按证据再评估 |
| Agent 工具 | 本地 CLI 足够 | 本地 CLI 更易选择与报告 | 不安装专用 MCP 或 Skill |

## 性能判断

迁移前在同一隔离 Python 3.11 环境做过三次热运行：

| 指标 | unittest | pytest |
| --- | ---: | ---: |
| 热运行中位数 | 1.453 秒 | 1.769 秒 |
| 单次最大常驻内存样本 | 约 190 MiB | 约 201 MiB |

这个结果说明 pytest 不会天然加速业务测试。采用 pytest 的主要收益是测试表达、失败反馈、局部选择和长期维护性，而不是全量串行墙钟时间。

迁移完成后曾达到 307 项；Issue #45 收缩验收为 236 passed，耗时约 2.1 秒。此规模引入多进程并行的启动和调度成本仍大概率高于收益。

## 实施结果

- 全部测试文件均使用 pytest 收集，采用函数/类、fixture、普通断言和参数化。
- 活跃代码不再包含传统测试类、子测试上下文、内建主入口或旧 discovery 运行方式。
- CI、README、贡献指南、Security 文档和项目 Skill 均使用 pytest。
- 开发依赖固定 `pytest==9.1.1`，CI 与验证环境统一 Python 3.11。
- 只过滤 PyMuPDF/SWIG 已知的三类弃用噪声；项目自身告警仍会显示。
- CodeGraph 负责分析变更影响面，pytest CLI 负责执行和报告，二者职责分离。

## 当前测试规模

旧角色、租约、队列命令和转发层删除后，测试代码约 8,229 行。保留的主体是材料采集、OA adapter、五流程双动作回放、受控提交、崩溃恢复、观测和留存安全场景；公开 CLI 只保留少量四命令合同测试。测试主要穿过 FlowEngine 接口，内部实现重构不再要求同步改写旧角色测试。

## Agent 使用约束

- 先用 CodeGraph 或变更文件确定受影响测试，再运行聚焦 pytest。
- 行为修改或跨模块契约变化后运行统一全量入口。
- fixture 保持局部、显式；不把测试状态集中到大型 `conftest.py`。
- 新增参数场景优先使用 `parametrize`，但不为了少算案例而合并独立业务语义。
- 不把项目代码、日志、附件、密钥或测试输出发送给外部 MCP/LLM 服务。

## 主要依据

- [pytest 功能概览](https://docs.pytest.org/en/stable/)
- [pytest fixture](https://docs.pytest.org/en/stable/how-to/fixtures.html)
- [pytest 参数化](https://docs.pytest.org/en/stable/how-to/parametrize.html)
- [pytest 调用方式](https://docs.pytest.org/en/stable/how-to/usage.html)
- [pytest Python 版本与兼容政策](https://docs.pytest.org/en/stable/backwards-compatibility.html)
- [Python 标准库 mock](https://docs.python.org/3.11/library/unittest.mock.html)
