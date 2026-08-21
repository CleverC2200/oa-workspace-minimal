# 主分支保护状态

## 当前事实

- GitHub Actions `quality` 已在无业务凭据的全新 checkout 中通过。
- 成功、失败单测、伪造密钥和被跟踪数据库文件均已通过独立 PR 验证。
- 截至 2026-08-22，当前 GitHub 方案不支持私有仓库的 branch protection 或 rulesets；两个官方 API 均返回 HTTP 403，要求升级 GitHub Pro 或将仓库公开。
- 仓库必须继续保持私有，因此服务端尚未强制 main 只能经 PR 更新。当前只能执行贡献规范中的人工约束，不能声称分支保护已经生效。

## 升级后的目标规则

方案升级后由仓库所有者启用以下规则，并在 #8 留下设置回执：

- main 更新必须来自 PR；
- 必须通过 `quality`，并要求分支与 main 保持最新；
- 要求解决所有 review conversation；
- 使用线性历史，禁止强推、删除和重新创建 main；
- 规则同样适用于管理员，不保留静默绕过；紧急调整必须在 Issue 记录原因、时间和恢复结果。

设置完成后，读取 GitHub branch protection 或 ruleset API 做一次只读复核；只有 API 返回值与上述规则一致，才关闭 #8。
