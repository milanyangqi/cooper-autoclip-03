# Docker 部署

Cooper AutoClip 03 可使用仓库内 Docker 配置进行部署。部署前请确认已经准备好大模型 API Key，并避免把任何密钥写入 Git。

## 快速启动

```bash
git clone https://github.com/milanyangqi/cooper-autoclip-03.git
cd cooper-autoclip-03
docker compose up -d
```

查看日志：

```bash
docker compose logs -f
```

停止服务：

```bash
docker compose down
```

## 数据目录

默认数据会挂载到 `data/` 或 Docker volume 中，包含数据库、上传素材、生成切片和缓存。请将这些数据视为私有数据，不要提交到 GitHub。

## 配置建议

- 使用环境变量或服务器本地配置保存 API Key。
- 使用反向代理暴露前端端口。
- 定期备份数据库和项目数据目录。
