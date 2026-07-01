# cooper-autoclip-03

Cooper AutoClip 03 是一个面向个人视频工作流的 AI 视频切片工具。它支持从 YouTube、B站链接或本地视频导入素材，自动分析字幕内容、筛选高价值片段、生成标题，并输出可复用的视频切片。

## 主要功能

- 链接导入：支持 YouTube 与 B站视频链接。
- 文件导入：支持本地视频与可选 SRT 字幕文件。
- 多模型配置：支持通过设置页配置大模型 API。
- 片段控制：导入时可设置生成片段数量、最短时长和最长时长。
- 自动截短：当候选片段超过最长时长时，会按起点自动截短，而不是生成空项目。
- 项目管理：完成、失败、处理中项目可在首页查看，完成项目支持删除。

## 快速启动

```bash
git clone https://github.com/milanyangqi/cooper-autoclip-03.git
cd cooper-autoclip-03
./start_autoclip.sh
```

启动后访问：

```text
http://localhost:3000
```

停止服务：

```bash
./stop_autoclip.sh
```

查看状态：

```bash
./status_autoclip.sh
```

## 配置

首次使用前，请在设置页配置可用的大模型 API。服务器部署时也可以使用 `data/settings.json` 或环境变量保存配置。

常用环境变量示例：

```bash
DATABASE_URL=sqlite:///./data/autoclip.db
REDIS_URL=redis://localhost:6379/0
```

敏感信息不要提交到仓库。

## 部署说明

本项目默认使用：

- 前端：Vite + React
- 后端：FastAPI
- 队列：Celery + Redis
- 数据库：SQLite

生产部署可以继续使用仓库内脚本，也可以根据服务器环境改为 systemd、Docker 或反向代理部署。

## 数据与隐私

用户上传视频、下载素材、数据库、生成切片和缓存默认存放在 `data/` 下。该目录已被 `.gitignore` 排除，不应提交到 GitHub。

## 许可证

本项目基于 MIT License 发布。保留仓库中的 `LICENSE` 文件以满足原许可要求。
