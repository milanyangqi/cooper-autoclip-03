"""
YouTube相关API路由
处理YouTube视频解析和下载功能
"""

import copy
import logging
from typing import Optional, Any, Callable, Dict
from fastapi import APIRouter, HTTPException, Form, UploadFile, File
from pydantic import BaseModel, Field
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent.parent))
from ...core.config import get_data_directory
from ...utils.clip_selection_config import build_clip_selection_config
import uuid
import asyncio
from datetime import datetime
from contextlib import contextmanager
import os
import yt_dlp

logger = logging.getLogger(__name__)
router = APIRouter()

# 存储下载任务的状态
download_tasks = {}


@contextmanager
def sanitized_yt_env():
    """临时清理与 yt-dlp 相关的环境变量，避免外部配置影响行为"""
    original_env = os.environ.copy()
    try:
        for key in list(os.environ.keys()):
            upper_key = key.upper()
            if upper_key.startswith("YT_DLP") or upper_key.startswith("YTDL") or upper_key.startswith("YOUTUBE_DL") or upper_key.startswith("YOUTUBEDL"):
                os.environ.pop(key, None)
        yield
    finally:
        os.environ.clear()
        os.environ.update(original_env)


def _is_browser_cookie_error(error: Exception) -> bool:
    """判断 yt-dlp 是否因为服务器缺少浏览器 Cookie 数据库而失败。"""
    message = str(error).lower()
    cookie_markers = (
        "cookies database",
        "cookie database",
        "could not find chrome",
        "could not find firefox",
        "could not find safari",
        "could not find edge",
        "could not copy chrome cookie database",
    )
    return "cookie" in message and any(marker in message for marker in cookie_markers)


def _without_browser_cookies(ydl_opts: Dict[str, Any]) -> Dict[str, Any]:
    """复制 yt-dlp 配置并移除浏览器 Cookie 读取配置。"""
    fallback_opts = copy.deepcopy(ydl_opts)
    fallback_opts.pop("cookiesfrombrowser", None)
    return fallback_opts


def _is_subtitle_download_error(error: Exception) -> bool:
    """判断 yt-dlp 是否因为平台字幕下载失败而中断。"""
    message = str(error).lower()
    return (
        "subtitle" in message
        and (
            "too many requests" in message
            or "http error 429" in message
            or "unable to download video subtitles" in message
        )
    )


def _without_platform_subtitles(ydl_opts: Dict[str, Any]) -> Dict[str, Any]:
    """复制 yt-dlp 配置并关闭平台字幕下载，后续交给 Whisper 生成字幕。"""
    fallback_opts = copy.deepcopy(ydl_opts)
    for key in ("writesubtitles", "writeautomaticsub", "subtitleslangs", "subtitlesformat"):
        fallback_opts.pop(key, None)
    return fallback_opts


async def _run_yt_dlp_with_cookie_fallback(
    sync_func: Callable[[str, Dict[str, Any]], Any],
    url: str,
    ydl_opts: Dict[str, Any],
    browser: Optional[str],
    action_name: str,
) -> Any:
    """运行 yt-dlp；服务器没有浏览器 Cookie 时自动降级为无 Cookie 模式。"""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, sync_func, url, ydl_opts)
    except Exception as e:
        if browser and "cookiesfrombrowser" in ydl_opts and _is_browser_cookie_error(e):
            logger.warning(
                "%s时未找到服务器端%s Cookie 数据库，自动改用无 Cookie 模式重试",
                action_name,
                browser,
            )
            return await loop.run_in_executor(None, sync_func, url, _without_browser_cookies(ydl_opts))
        raise


async def _run_download_with_subtitle_fallback(
    sync_func: Callable[[str, Dict[str, Any]], Any],
    url: str,
    ydl_opts: Dict[str, Any],
    browser: Optional[str],
) -> Any:
    """下载视频；平台字幕被限流时改为只下载视频。"""
    try:
        return await _run_yt_dlp_with_cookie_fallback(
            sync_func,
            url,
            ydl_opts,
            browser,
            "下载YouTube视频",
        )
    except Exception as e:
        if _is_subtitle_download_error(e):
            logger.warning("YouTube平台字幕下载失败，自动改为只下载视频并稍后使用Whisper生成字幕: %s", e)
            return await _run_yt_dlp_with_cookie_fallback(
                sync_func,
                url,
                _without_platform_subtitles(ydl_opts),
                browser,
                "下载YouTube视频（无平台字幕）",
            )
        raise

class YouTubeParseRequest(BaseModel):
    url: str
    browser: Optional[str] = None

class YouTubeDownloadRequest(BaseModel):
    url: str
    project_name: str
    video_category: Optional[str] = "default"
    browser: Optional[str] = None
    target_clip_count: Optional[int] = Field(default=None, ge=1)
    min_clip_duration_sec: Optional[int] = Field(default=None, ge=1)
    max_clip_duration_sec: Optional[int] = Field(default=None, ge=1)
    min_clip_sentence_count: Optional[int] = Field(default=None, ge=1)
    max_clip_sentence_count: Optional[int] = Field(default=None, ge=1)

class YouTubeVideoInfo(BaseModel):
    title: str
    description: str
    duration: int
    uploader: str
    upload_date: str
    view_count: int
    like_count: int
    thumbnail: str

class YouTubeDownloadTask(BaseModel):
    id: str
    url: str
    project_name: str
    video_category: str
    status: str  # pending, processing, completed, failed
    progress: float
    error_message: Optional[str] = None
    project_id: Optional[str] = None
    created_at: str
    updated_at: str

@router.post("/parse")
async def parse_youtube_video(
    url: str = Form(...),
    browser: Optional[str] = Form(None),
    client: Optional[str] = Form(None)
):
    """解析YouTube视频信息"""
    try:
        logger.info(f"开始解析YouTube视频: {url}")
        
        # 简单的URL验证
        if "youtube.com" not in url and "youtu.be" not in url:
            raise HTTPException(status_code=400, detail="无效的YouTube视频链接")
        
        # 记录版本信息，便于排查
        try:
            logger.info(f"yt-dlp={yt_dlp.version.__version__}, py={sys.executable}")
        except Exception:
            pass

        # 使用当前虚拟环境里的 Python 执行 yt-dlp，避免依赖作者本机路径。
        import subprocess
        import json
        import asyncio
        
        def extract_info_sync(url, browser):
            # 构建 yt-dlp 命令
            base_cmd = [
                sys.executable,
                '-m',
                'yt_dlp',
                '--ignore-config',
                '--no-warnings',
                '--no-playlist',
                '--dump-json',
                '--skip-download',  # 修正参数名
                '--no-cache-dir'
            ]

            # 可选兜底客户端，规避 SABR
            yt_client = (client or os.getenv('AUTOCLIP_YT_CLIENT', '')).strip().lower()

            def build_cmd(browser_name: Optional[str]):
                cmd = list(base_cmd)
                if browser_name:
                    cmd.extend(['--cookies-from-browser', browser_name.lower()])
                if yt_client in {"android", "ios", "tv"}:
                    cmd.extend(['--extractor-args', f"youtube:player_client={yt_client}"])
                cmd.append(url)
                return cmd

            def run_cmd(cmd):
                # 执行命令（清理环境变量，避免 YT_* 影响）
                env = os.environ.copy()
                for k in list(env.keys()):
                    uk = k.upper()
                    if uk.startswith('YT_DLP') or uk.startswith('YTDL') or uk.startswith('YOUTUBE_DL') or uk.startswith('YOUTUBEDL'):
                        env.pop(k, None)

                logger.info(f"执行命令: {' '.join(cmd)}")
                return subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    cwd=str(Path(__file__).resolve().parents[3]),
                    env=env
                )

            try:
                result = run_cmd(build_cmd(browser))

                logger.info(f"命令返回码: {result.returncode}")
                logger.info(f"命令输出(前200字): {result.stdout[:200]}...")
                if result.stderr:
                    logger.info(f"命令错误: {result.stderr}")

                if result.returncode != 0:
                    browser_error = Exception(result.stderr or result.stdout)
                    if browser and _is_browser_cookie_error(browser_error):
                        logger.warning("解析YouTube信息时未找到服务器端%s Cookie 数据库，自动改用无 Cookie 模式重试", browser)
                        result = run_cmd(build_cmd(None))
                        logger.info(f"无 Cookie 重试返回码: {result.returncode}")
                        if result.stderr:
                            logger.info(f"无 Cookie 重试错误: {result.stderr}")

                if result.returncode != 0:
                    raise Exception(f"yt-dlp failed: {result.stderr or result.stdout}")

                # 解析 JSON 输出
                info_dict = json.loads(result.stdout)
                return info_dict
                
            except subprocess.TimeoutExpired:
                raise Exception("yt-dlp timeout")
            except json.JSONDecodeError as e:
                raise Exception(f"Failed to parse yt-dlp output: {e}")
            except Exception as e:
                raise Exception(f"yt-dlp execution failed: {e}")
        
        loop = asyncio.get_event_loop()
        info_dict = await loop.run_in_executor(None, extract_info_sync, url, browser)
        
        logger.info(f"YouTube视频信息解析成功: {info_dict.get('title', 'Unknown')}")
        
        return {
            "success": True,
            "video_info": {
                "title": info_dict.get('title', 'Unknown'),
                "description": info_dict.get('description', ''),
                "duration": info_dict.get('duration', 0) or 0,
                "uploader": info_dict.get('uploader', 'Unknown'),
                "upload_date": info_dict.get('upload_date', ''),
                "view_count": info_dict.get('view_count', 0),
                "like_count": info_dict.get('like_count', 0),
                "thumbnail": info_dict.get('thumbnail', '')
            }
        }
        
    except Exception as e:
        logger.error(f"解析YouTube视频失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"解析失败: {str(e)}")

@router.post("/download")
async def create_youtube_download_task(request: YouTubeDownloadRequest):
    """创建YouTube视频下载任务 - 立即创建项目"""
    try:
        logger.info(f"创建YouTube下载任务: {request.url}")
        clip_selection_config = build_clip_selection_config(
            target_clip_count=request.target_clip_count,
            min_clip_duration_sec=request.min_clip_duration_sec,
            max_clip_duration_sec=request.max_clip_duration_sec,
            min_clip_sentence_count=request.min_clip_sentence_count,
            max_clip_sentence_count=request.max_clip_sentence_count,
        )
        
        # 先获取视频信息以获取缩略图
        import yt_dlp
        import asyncio
        
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'ignoreconfig': True,
            'noplaylist': True,
            'config_locations': [],
            'cachedir': False,
        }
        
        if request.browser:
            ydl_opts['cookiesfrombrowser'] = (request.browser.lower(),)

        # 可选兜底客户端
        yt_client_env = os.getenv('AUTOCLIP_YT_CLIENT', '').strip().lower()
        if yt_client_env in {"android", "ios", "tv"}:
            ydl_opts.setdefault('extractor_args', {}).setdefault('youtube', {}).setdefault('player_client', []).append(yt_client_env)
        
        def extract_info_sync(url, ydl_opts):
            with sanitized_yt_env():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    return ydl.extract_info(url, download=False)
        
        video_info = await _run_yt_dlp_with_cookie_fallback(
            extract_info_sync,
            request.url,
            ydl_opts,
            request.browser,
            "获取YouTube视频信息",
        )
        
        # 立即创建项目记录
        from ...core.database import SessionLocal
        from ...services.project_service import ProjectService
        from ...schemas.project import ProjectCreate, ProjectType, ProjectStatus
        
        db = SessionLocal()
        try:
            project_service = ProjectService(db)
            
            # 处理缩略图 - 直接使用解析出来的封面图
            thumbnail_data = None
            thumbnail_url = video_info.get('thumbnail', '')
            if thumbnail_url:
                try:
                    import requests
                    import base64
                    
                    # 下载缩略图
                    response = requests.get(thumbnail_url, timeout=10)
                    if response.status_code == 200:
                        # 转换为base64
                        thumbnail_base64 = base64.b64encode(response.content).decode('utf-8')
                        thumbnail_data = f"data:image/jpeg;base64,{thumbnail_base64}"
                        logger.info(f"YouTube缩略图获取成功: {video_info.get('title', 'Unknown')}")
                    else:
                        logger.warning(f"下载YouTube缩略图失败: {response.status_code}")
                except Exception as e:
                    logger.error(f"处理YouTube缩略图失败: {e}")
                    # 缩略图处理失败不影响主流程
            
            settings = {
                "download_status": "downloading",
                "download_progress": 0.0,
                "youtube_info": {
                    "url": request.url,
                    "browser": request.browser,
                    "title": video_info.get('title', 'Unknown'),
                    "uploader": video_info.get('uploader', 'Unknown'),
                    "duration": video_info.get('duration', 0),
                    "view_count": video_info.get('view_count', 0),
                    "thumbnail_url": thumbnail_url
                }
            }
            if clip_selection_config:
                settings["clip_selection"] = clip_selection_config

            # 创建项目数据
            project_data = ProjectCreate(
                name=request.project_name,
                description=f"从YouTube下载: {video_info.get('title', 'Unknown')}",
                project_type=ProjectType(request.video_category),
                status=ProjectStatus.PENDING,  # 初始状态为等待中
                source_url=request.url,
                source_file=None,  # 暂时为空，下载完成后更新
                settings=settings
            )
            
            project = project_service.create_project(project_data)
            project_id = str(project.id)
            
            # 设置缩略图
            if thumbnail_data:
                project.thumbnail = thumbnail_data
                db.commit()
                logger.info(f"项目 {project_id} 缩略图已设置")
            
            # 创建项目目录
            from ...core.path_utils import get_project_directory
            project_dir = get_project_directory(project_id)
            raw_dir = project_dir / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            
            logger.info(f"项目已创建: {project_id}")
            
            # 生成下载任务ID
            task_id = str(uuid.uuid4())
            
            # 创建任务记录
            task = YouTubeDownloadTask(
                id=task_id,
                url=request.url,
                project_name=request.project_name,
                video_category=request.video_category,
                status="pending",
                progress=0.0,
                project_id=project_id,  # 关联项目ID
                created_at=str(uuid.uuid1().time),
                updated_at=str(uuid.uuid1().time)
            )
            
            # 存储任务
            download_tasks[task_id] = task
            
            # 异步启动下载任务 - 使用安全的任务管理器
            from .async_task_manager import task_manager
            await task_manager.create_safe_task(
                f"youtube_download_{task_id}", 
                process_youtube_download_task, 
                task_id, 
                request, 
                project_id
            )
            
            # 返回项目信息而不是任务信息
            return {
                "project_id": project_id,
                "task_id": task_id,
                "status": "created",
                "message": "项目已创建，正在下载中..."
            }
            
        finally:
            db.close()
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"创建YouTube下载任务失败: {str(e)}")
        raise HTTPException(status_code=500, detail=f"创建任务失败: {str(e)}")

@router.get("/tasks/{task_id}")
async def get_youtube_task_status(task_id: str):
    """获取YouTube下载任务状态"""
    if task_id not in download_tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    return download_tasks[task_id]

@router.get("/tasks")
async def get_all_youtube_tasks():
    """获取所有YouTube下载任务"""
    return list(download_tasks.values())

async def update_project_download_progress(project_id: str, progress: float, message: str):
    """更新项目下载进度"""
    try:
        from ...core.database import SessionLocal
        from ...services.project_service import ProjectService
        
        db = SessionLocal()
        try:
            project_service = ProjectService(db)
            project = project_service.get(project_id)
            
            if project:
                # 更新项目设置中的下载进度
                processing_config = dict(project.processing_config or {})
                processing_config.update({
                    "download_progress": progress,
                    "download_message": message
                })
                project.processing_config = processing_config
                
                # 如果进度达到100%，更新状态为等待处理
                if progress >= 100.0:
                    from ...models.project import ProjectStatus
                    project.status = ProjectStatus.PENDING
                
                db.commit()
                logger.info(f"项目 {project_id} 下载进度更新: {progress}% - {message}")
                
        finally:
            db.close()
            
    except Exception as e:
        logger.error(f"更新项目下载进度失败: {e}")


async def update_project_download_failed(project_id: str, error_message: str):
    """将下载失败状态写回项目，避免前端一直显示导入中。"""
    try:
        from ...core.database import SessionLocal
        from ...services.project_service import ProjectService
        from ...models.project import ProjectStatus

        db = SessionLocal()
        try:
            project_service = ProjectService(db)
            project = project_service.get(project_id)

            if project:
                processing_config = dict(project.processing_config or {})
                processing_config.update({
                    "download_status": "failed",
                    "download_progress": 0.0,
                    "download_message": "下载失败",
                    "error_message": error_message,
                })
                project.processing_config = processing_config
                project.status = ProjectStatus.FAILED
                db.commit()
                logger.info(f"项目 {project_id} 下载失败状态已写回: {error_message}")
        finally:
            db.close()

    except Exception as e:
        logger.error(f"写回项目下载失败状态失败: {e}")

async def process_youtube_download_task(task_id: str, request: YouTubeDownloadRequest, project_id: str):
    """处理YouTube下载任务"""
    try:
        # 更新任务状态为处理中
        download_tasks[task_id].status = "processing"
        download_tasks[task_id].progress = 10.0
        
        # 更新项目状态和进度
        await update_project_download_progress(project_id, 10.0, "正在获取视频信息...")
        
        # 使用yt-dlp下载视频
        import yt_dlp
        import asyncio
        from ...core.config import get_data_directory
        
        data_dir = get_data_directory()
        download_dir = data_dir / "temp" / f"youtube_{project_id}"
        download_dir.mkdir(parents=True, exist_ok=True)
        for leftover in download_dir.glob("*"):
            if leftover.is_file() or leftover.is_symlink():
                leftover.unlink(missing_ok=True)
            elif leftover.is_dir():
                import shutil
                shutil.rmtree(leftover)
        
        # 更新项目进度
        await update_project_download_progress(project_id, 30.0, "正在下载视频...")
        
        # 设置下载选项
        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'writesubtitles': True,
            'writeautomaticsub': True,  # 下载自动生成的字幕
            # 主下载阶段优先只拉英文字幕。YouTube自动翻译字幕容易触发429，
            # 如果没有可用平台字幕，后续会用Whisper生成字幕。
            'subtitleslangs': ['en', 'en-US'],
            'subtitlesformat': 'srt',
            'outtmpl': str(download_dir / '%(title)s.%(ext)s'),
            'noplaylist': True,
            'quiet': True,
            'no_warnings': False,  # 显示警告信息以便调试
            'ignoreconfig': True,
            'config_locations': [],
            'cachedir': False,
        }
        
        if request.browser:
            ydl_opts['cookiesfrombrowser'] = (request.browser.lower(),)

        # 可选兜底客户端
        yt_client_env = os.getenv('AUTOCLIP_YT_CLIENT', '').strip().lower()
        if yt_client_env in {"android", "ios", "tv"}:
            ydl_opts.setdefault('extractor_args', {}).setdefault('youtube', {}).setdefault('player_client', []).append(yt_client_env)
        
        def download_sync(url, ydl_opts):
            with sanitized_yt_env():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    return ydl.download([url])
        
        await _run_download_with_subtitle_fallback(
            download_sync,
            request.url,
            ydl_opts,
            request.browser,
        )
        
        # 查找下载的文件
        video_files = list(download_dir.glob("*.mp4"))
        subtitle_files = list(download_dir.glob("*.srt"))
        
        if not video_files:
            raise Exception("未找到下载的视频文件")
        
        video_path = str(video_files[0])
        subtitle_path = str(subtitle_files[0]) if subtitle_files else ""
        
        download_tasks[task_id].progress = 80.0
        
        # 更新项目进度
        await update_project_download_progress(project_id, 60.0, "视频下载完成，正在处理字幕...")
        
        # 如果没有字幕文件，优先使用Whisper生成字幕
        if not subtitle_path:
            logger.info("优先使用Whisper生成高质量字幕")
            # 更新项目进度
            await update_project_download_progress(project_id, 70.0, "正在使用Whisper生成字幕...")
            
            try:
                from ...utils.speech_recognizer import generate_subtitle_for_video, SpeechRecognitionError
                video_file_path = Path(video_path)
                
                # 根据视频信息选择合适的模型
                model = "base"  # 默认使用平衡模型
                language = "auto"  # 默认自动检测语言
                
                # 可以根据视频标题判断内容类型
                # 这里可以添加更智能的内容类型判断逻辑
                
                logger.info(f"使用Whisper生成字幕 - 语言: {language}, 模型: {model}")
                
                generated_subtitle = generate_subtitle_for_video(
                    video_file_path,
                    language=language,
                    model=model
                )
                subtitle_path = str(generated_subtitle)
                logger.info(f"Whisper字幕生成成功: {subtitle_path}")
                
                # 更新项目进度
                await update_project_download_progress(project_id, 90.0, "字幕生成完成，正在准备处理...")
                
            except SpeechRecognitionError as e:
                logger.error(f"Whisper字幕生成失败: {e}")
                # Whisper失败时，尝试多种策略获取平台字幕作为备用
                logger.info("尝试下载平台字幕作为备用方案")
                try:
                    subtitle_path = await _try_youtube_subtitle_strategies(request.url, download_dir, request.browser)
                    if subtitle_path:
                        logger.info(f"备用字幕获取成功: {subtitle_path}")
                    else:
                        logger.warning("所有字幕获取策略都失败了")
                        subtitle_path = None  # 确保字幕路径为空，后续会标记项目失败
                except Exception as backup_error:
                    logger.error(f"备用字幕获取也失败: {backup_error}")
                    subtitle_path = None  # 确保字幕路径为空，后续会标记项目失败
            except Exception as e:
                logger.error(f"生成字幕过程中发生未知错误: {e}")
                subtitle_path = None  # 确保字幕路径为空，后续会标记项目失败
        
        logger.info(f"下载完成 - 视频文件: {video_path}, 字幕文件: {subtitle_path}")
        
        # 更新项目信息（项目已在开始时创建）
        from ...services.project_service import ProjectService
        from ...core.database import SessionLocal
        
        db = SessionLocal()
        try:
            project_service = ProjectService(db)
            
            # 获取已创建的项目
            project = project_service.get(project_id)
            if not project:
                raise Exception(f"项目 {project_id} 不存在")
            
            # 更新项目信息
            project.description = f"从YouTube下载: {request.project_name}"
            # 注意：不要在这里设置video_path，等文件移动完成后再设置
            
            # 更新项目设置。JSON字段需要整体赋值，避免SQLAlchemy漏记原地update。
            processing_config = dict(project.processing_config or {})
            processing_config.update({
                "youtube_info": {
                    "title": request.project_name,
                    "uploader": "YouTube",
                    "duration": 0,
                    "view_count": 0,
                    "like_count": 0
                },
                "subtitle_path": subtitle_path,
                "download_status": "completed",
                "download_progress": 100.0
            })
            project.processing_config = processing_config
            
            # 移动文件到项目目录
            from ...core.path_utils import get_project_directory
            project_dir = get_project_directory(project_id)
            raw_dir = project_dir / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            
            # 移动视频文件到项目目录
            import shutil
            from pathlib import Path
            
            if video_path:
                video_file_path = Path(video_path)
                if video_file_path.exists():
                    # 重命名视频文件为input.mp4
                    new_video_path = raw_dir / "input.mp4"
                    shutil.move(str(video_file_path), str(new_video_path))
                    logger.info(f"视频文件已移动到: {new_video_path}")
                    
                    # 更新项目中的视频路径
                    project.video_path = str(new_video_path)
            
            # 移动字幕文件到项目目录
            if subtitle_path:
                subtitle_file_path = Path(subtitle_path)
                if subtitle_file_path.exists():
                    # 重命名字幕文件为input.srt
                    new_subtitle_path = raw_dir / "input.srt"
                    shutil.move(str(subtitle_file_path), str(new_subtitle_path))
                    logger.info(f"字幕文件已移动到: {new_subtitle_path}")
                    
                    # 更新项目处理配置中的字幕路径
                    processing_config = dict(project.processing_config or {})
                    processing_config["subtitle_path"] = str(new_subtitle_path)
                    project.processing_config = processing_config
            
            # 保存项目更新
            db.commit()
            
            # 检查字幕文件是否存在，如果不存在则标记项目为失败
            srt_file_path = raw_dir / "input.srt"
            if not srt_file_path.exists():
                logger.error(f"字幕文件不存在: {srt_file_path}，项目将标记为失败状态")
                from ...models.project import ProjectStatus
                project.status = ProjectStatus.FAILED
                processing_config = dict(project.processing_config or {})
                processing_config.update({
                    "download_status": "failed",
                    "download_progress": 0.0,
                    "download_message": "下载失败：字幕文件不存在",
                    "error_message": "字幕文件不存在且Whisper生成失败",
                })
                project.processing_config = processing_config
                db.commit()
                
                # 更新任务状态为失败
                download_tasks[task_id].status = "failed"
                download_tasks[task_id].error_message = "字幕文件不存在且Whisper生成失败"
                download_tasks[task_id].progress = 0.0
                download_tasks[task_id].project_id = str(project.id)
                download_tasks[task_id].updated_at = datetime.now().isoformat()
                
                # 更新项目下载进度为失败
                await update_project_download_progress(project_id, 0.0, "下载失败：字幕文件不存在")
                
                logger.info(f"YouTube下载任务失败: {task_id}, 项目ID: {project.id}, 原因: 字幕文件不存在")
                return
            
            # 更新项目下载进度为完成
            await update_project_download_progress(project_id, 100.0, "下载完成，准备开始处理")
            
            # 更新任务状态
            download_tasks[task_id].status = "completed"
            download_tasks[task_id].progress = 100.0
            download_tasks[task_id].project_id = str(project.id)
            download_tasks[task_id].updated_at = datetime.now().isoformat()
            
            logger.info(f"YouTube下载任务完成: {task_id}, 项目ID: {project.id}")
            
            # 自动启动处理流程
            try:
                # 更新项目状态为等待处理
                from ...schemas.project import ProjectStatus
                project.status = ProjectStatus.PENDING  # 改为PENDING，让自动化服务启动
                db.commit()
                
                logger.info(f"YouTube项目 {project.id} 下载完成，等待自动化流水线启动")
                
                # 异步启动自动化流水线
                import asyncio
                from ...services.auto_pipeline_service import auto_pipeline_service
                
                # 使用create_task在已运行的事件循环中执行
                try:
                    loop = asyncio.get_running_loop()
                    # 在已运行的事件循环中创建任务
                    task = loop.create_task(
                        auto_pipeline_service.auto_start_pipeline(str(project.id))
                    )
                    # 等待任务完成
                    pipeline_result = await task
                except RuntimeError:
                    # 如果没有运行的事件循环，创建新的
                    pipeline_result = await auto_pipeline_service.auto_start_pipeline(str(project.id))
                
                if pipeline_result['status'] == 'started':
                    logger.info(f"YouTube项目 {project.id} 自动化流水线已启动: {pipeline_result}")
                else:
                    logger.warning(f"YouTube项目 {project.id} 自动化流水线启动结果: {pipeline_result}")
                
            except Exception as e:
                logger.error(f"启动YouTube项目 {project.id} 自动化流水线失败: {str(e)}")
                # 即使处理启动失败，也要返回下载成功
                # 用户可以通过重试按钮重新启动处理
            
        except Exception as e:
            logger.error(f"创建项目失败: {str(e)}")
            # 即使处理启动失败，也要返回下载成功
            # 用户可以通过重试按钮重新启动处理
            
        finally:
            db.close()
            
    except Exception as e:
        logger.error(f"处理下载任务失败: {str(e)}")
        await update_project_download_failed(project_id, str(e))
        download_tasks[task_id].status = "failed"
        download_tasks[task_id].error_message = str(e)
        download_tasks[task_id].progress = 0.0
        download_tasks[task_id].updated_at = datetime.now().isoformat()


async def _try_youtube_subtitle_strategies(url: str, download_dir: Path, browser: Optional[str] = None) -> str:
    """尝试多种YouTube字幕获取策略"""
    strategies = [
        lambda: _try_download_with_different_formats(url, download_dir, browser),
        lambda: _try_download_with_different_langs(url, download_dir, browser),
        lambda: _try_extract_from_metadata(url, download_dir, browser)
    ]
    
    for strategy in strategies:
        try:
            subtitle_path = await strategy()
            if subtitle_path:
                logger.info(f"YouTube备用字幕策略成功")
                return subtitle_path
        except Exception as e:
            logger.warning(f"YouTube备用字幕策略失败: {e}")
            continue
    
    logger.warning("所有YouTube字幕获取策略都失败了")
    return ""


async def _try_download_with_different_formats(url: str, download_dir: Path, browser: Optional[str] = None) -> str:
    """尝试下载不同格式的字幕"""
    import asyncio
    logger.info("尝试下载不同格式的YouTube字幕...")
    
    formats = ['srt', 'vtt', 'json3']
    
    for fmt in formats:
        try:
            ydl_opts = {
                'format': 'best[ext=mp4]/best',
                'writesubtitles': True,
                'writeautomaticsub': True,
                'subtitleslangs': ['en', 'zh-Hans', 'zh'],
                'subtitlesformat': fmt,
                'outtmpl': str(download_dir / f'subtitle_%(title)s.%(ext)s'),
                'noplaylist': True,
                'quiet': True,
                'ignoreconfig': True,
                'config_locations': [],
            }
            
            if browser:
                ydl_opts['cookiesfrombrowser'] = (browser.lower(),)
            
            def download_sync(url, ydl_opts):
                with sanitized_yt_env():
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        return ydl.download([url])
            
            await _run_yt_dlp_with_cookie_fallback(
                download_sync,
                url,
                ydl_opts,
                browser,
                f"下载YouTube {fmt}字幕",
            )
            
            # 查找下载的字幕文件
            subtitle_files = list(download_dir.glob(f"*.{fmt}"))
            if subtitle_files:
                subtitle_path = str(subtitle_files[0])
                
                # 如果是VTT格式，转换为SRT
                if fmt == 'vtt':
                    srt_path = subtitle_path.replace('.vtt', '.srt')
                    await _convert_vtt_to_srt(subtitle_path, srt_path)
                    return srt_path
                
                return subtitle_path
                
        except Exception as e:
            logger.debug(f"尝试格式 {fmt} 失败: {e}")
            continue
    
    return ""


async def _try_download_with_different_langs(url: str, download_dir: Path, browser: Optional[str] = None) -> str:
    """尝试下载不同语言的字幕"""
    import asyncio
    logger.info("尝试下载不同语言的YouTube字幕...")
    
    lang_combinations = [
        ['en', 'en-US'],      # 英文
        ['zh-Hans', 'zh'],    # 中文
        ['ja', 'ja-JP'],      # 日文
        ['ko', 'ko-KR'],      # 韩文
        ['auto']              # 自动检测
    ]
    
    for langs in lang_combinations:
        try:
            ydl_opts = {
                'format': 'best[ext=mp4]/best',
                'writesubtitles': True,
                'writeautomaticsub': True,
                'subtitleslangs': langs,
                'subtitlesformat': 'srt',
                'outtmpl': str(download_dir / f'lang_%(title)s.%(ext)s'),
                'noplaylist': True,
                'quiet': True,
                'ignoreconfig': True,
                'config_locations': [],
            }
            
            if browser:
                ydl_opts['cookiesfrombrowser'] = (browser.lower(),)
            
            def download_sync(url, ydl_opts):
                with sanitized_yt_env():
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        return ydl.download([url])
            
            await _run_yt_dlp_with_cookie_fallback(
                download_sync,
                url,
                ydl_opts,
                browser,
                f"下载YouTube {langs}字幕",
            )
            
            # 查找下载的字幕文件
            subtitle_files = list(download_dir.glob("*.srt"))
            if subtitle_files:
                return str(subtitle_files[0])
                
        except Exception as e:
            logger.debug(f"尝试语言 {langs} 失败: {e}")
            continue
    
    return ""


async def _try_extract_from_metadata(url: str, download_dir: Path, browser: Optional[str] = None) -> str:
    """尝试从视频元数据中提取字幕信息"""
    import asyncio
    logger.info("尝试从YouTube视频元数据提取字幕信息...")
    
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'ignoreconfig': True,
            'config_locations': [],
        }
        
        if browser:
            ydl_opts['cookiesfrombrowser'] = (browser.lower(),)
        
        def extract_info_sync(url, ydl_opts):
            with sanitized_yt_env():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    return ydl.extract_info(url, download=False)
        
        info_dict = await _run_yt_dlp_with_cookie_fallback(
            extract_info_sync,
            url,
            ydl_opts,
            browser,
            "提取YouTube字幕元数据",
        )
        
        # 检查是否有字幕信息
        subtitles = info_dict.get('subtitles', {})
        auto_subtitles = info_dict.get('automatic_captions', {})
        
        if subtitles or auto_subtitles:
            logger.info(f"发现YouTube字幕信息: {list(subtitles.keys()) + list(auto_subtitles.keys())}")
            # 这里可以进一步处理字幕信息，但目前返回空字符串
            return ""
        
        return ""
        
    except Exception as e:
        logger.debug(f"提取YouTube视频元数据失败: {e}")
        return ""


async def _convert_vtt_to_srt(vtt_path: str, srt_path: str):
    """将VTT字幕文件转换为SRT格式"""
    try:
        with open(vtt_path, 'r', encoding='utf-8') as vtt_file:
            vtt_content = vtt_file.read()
        
        # 简单的VTT到SRT转换
        lines = vtt_content.split('\n')
        srt_lines = []
        subtitle_count = 1
        
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            
            # 跳过VTT头部信息
            if line.startswith('WEBVTT') or line.startswith('NOTE') or not line:
                i += 1
                continue
            
            # 查找时间戳行
            if '-->' in line:
                # 转换时间格式 (VTT使用点，SRT使用逗号)
                time_line = line.replace('.', ',')
                srt_lines.append(str(subtitle_count))
                srt_lines.append(time_line)
                
                # 获取字幕文本
                i += 1
                subtitle_text = []
                while i < len(lines) and lines[i].strip():
                    subtitle_text.append(lines[i].strip())
                    i += 1
                
                srt_lines.extend(subtitle_text)
                srt_lines.append('')  # 空行分隔
                subtitle_count += 1
            
            i += 1
        
        # 写入SRT文件
        with open(srt_path, 'w', encoding='utf-8') as srt_file:
            srt_file.write('\n'.join(srt_lines))
            
        logger.info(f"VTT转SRT转换成功: {vtt_path} -> {srt_path}")
        
    except Exception as e:
        logger.error(f"VTT转SRT转换失败: {e}")
        raise
