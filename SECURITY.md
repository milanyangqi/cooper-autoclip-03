# 安全说明

## 敏感信息

请不要把以下内容提交到 GitHub：

- API Key、Cookie、Token、账号密码
- `.env`、`data/settings.json`
- `data/` 下的数据库、视频、字幕、切片和缓存

仓库已通过 `.gitignore` 排除常见敏感文件和生成产物。发布前仍建议运行一次敏感信息检查。

## 报告问题

如果你在 Cooper AutoClip 03 中发现安全问题，请通过 GitHub Issues 或私有渠道联系仓库维护者。请不要在公开 issue 中粘贴密钥、Cookie 或用户数据。

## 部署建议

- 生产环境建议放在反向代理之后，并限制管理端口访问。
- 大模型 API Key 建议只保存在服务器配置或环境变量中。
- 定期备份 `data/autoclip.db`，但备份文件不要提交到仓库。
