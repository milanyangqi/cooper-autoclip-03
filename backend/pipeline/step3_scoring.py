"""
Step 3: 内容评分 - 对每个话题进行质量评分，筛选出高质量内容
"""
import json
import logging
import re
from typing import List, Dict, Any, Optional
from pathlib import Path
from collections import defaultdict

# 导入依赖
from ..utils.llm_client import LLMClient
from ..utils.text_processor import TextProcessor
from ..utils.clip_selection_config import normalize_clip_selection_config
from ..utils.subtitle_sync import (
    DEFAULT_CLIP_TAIL_PADDING_SECONDS,
    build_sentence_groups,
    collect_overlapping_sentence_groups,
    find_project_input_srt,
    load_srt_entries,
)
from ..core.shared_config import PROMPT_FILES, METADATA_DIR, MIN_SCORE_THRESHOLD

logger = logging.getLogger(__name__)

class ClipScorer:
    """内容评分器"""
    
    def __init__(self, prompt_files: Dict = None):
        self.llm_client = LLMClient()
        self.text_processor = TextProcessor()
        
        # 加载提示词
        prompt_files_to_use = prompt_files if prompt_files is not None else PROMPT_FILES
        with open(prompt_files_to_use['recommendation'], 'r', encoding='utf-8') as f:
            self.recommendation_prompt = f.read()
    
    def score_clips(self, timeline_data: List[Dict]) -> List[Dict]:
        """
        为切片评分 (新版：按块批量处理，并使用LLM进行综合评估)
        """
        if not timeline_data:
            logger.warning("时间线数据为空，无法评分")
            return []
            
        logger.info(f"开始为 {len(timeline_data)} 个切片进行批量评分...")
        
        # 1. 按 chunk_index 对所有 timeline 数据进行分组
        timeline_by_chunk = defaultdict(list)
        for item in timeline_data:
            chunk_index = item.get('chunk_index')
            if chunk_index is not None:
                timeline_by_chunk[chunk_index].append(item)
            else:
                logger.warning(f"  > 话题 '{item.get('outline', '未知')}' 缺少 chunk_index，将被跳过。")
        
        all_scored_clips = []
        # 2. 遍历每个块，批量处理其中的所有话题
        for chunk_index, chunk_items in timeline_by_chunk.items():
            logger.info(f"处理块 {chunk_index}，其中包含 {len(chunk_items)} 个话题...")
            try:
                # 3. 使用LLM进行批量评估
                scored_chunk_items = self._get_llm_evaluation(chunk_items)
                
                if scored_chunk_items:
                    all_scored_clips.extend(scored_chunk_items)
                else:
                    logger.warning(f"块 {chunk_index} 的LLM评估返回为空，跳过。")

            except Exception as e:
                logger.error(f"  > 处理块 {chunk_index} 进行评分时出错: {str(e)}")
                continue

        # 4. 按最终得分对所有结果进行排序
        if all_scored_clips:
            all_scored_clips.sort(key=lambda x: x.get('final_score', 0), reverse=True)
            # 保持Step 2分配的固定ID，不再重新分配
            logger.info("按评分排序完成，保持原有固定ID不变")
            
            # 最终按ID排序，确保时间顺序的一致性
            all_scored_clips.sort(key=lambda x: int(x.get('id', 0)))
            logger.info("按ID排序完成，保持时间顺序")
                
        logger.info("所有切片评分完成")
        return all_scored_clips
    
    def _get_llm_evaluation(self, clips: List[Dict]) -> List[Dict]:
        """
        使用LLM进行批量评估，为每个clip添加 final_score 和 recommend_reason
        """
        try:
            # 输入给LLM的数据不需要包含所有字段，只给必要的
            input_for_llm = [
                {
                    "outline": clip.get('outline'), 
                    "content": clip.get('content'),
                    "start_time": clip.get('start_time'),
                    "end_time": clip.get('end_time'),
                } for clip in clips
            ]
            
            response = self.llm_client.call_with_retry(self.recommendation_prompt, input_for_llm)
            parsed_list = self.llm_client.parse_json_response(response)
            
            if not isinstance(parsed_list, list) or len(parsed_list) != len(clips):
                logger.error(f"LLM返回的评分结果数量与输入不匹配。输入: {len(clips)}, 输出: {len(parsed_list)}")
                return []
                
            # 将评分结果合并回原始的clips数据
            for original_clip, llm_result in zip(clips, parsed_list):
                score = llm_result.get('final_score')
                reason = llm_result.get('recommend_reason')
                
                if score is None or reason is None:
                    logger.warning(f"LLM返回的某个结果缺少score或reason: {llm_result}")
                    original_clip['final_score'] = 0.0
                    original_clip['recommend_reason'] = "评估失败"
                else:
                    original_clip['final_score'] = round(float(score), 2)
                    original_clip['recommend_reason'] = reason
                    # 安全地获取outline标题用于日志显示
                    outline = original_clip.get('outline', {})
                    if isinstance(outline, dict):
                        title = outline.get('title', '未知标题')
                    else:
                        title = str(outline)
                    logger.info(f"  > 评分成功: {title[:20]}... [分数: {score}]")

            return clips

        except Exception as e:
            logger.error(f"LLM批量评估失败: {e}")
            # 如果批量失败，为所有clips标记为失败
            for clip in clips:
                clip['final_score'] = 0.0
                clip['recommend_reason'] = "批量评估失败"
            return clips

    def save_scores(self, scored_clips: List[Dict], output_path: Path):
        """保存评分结果"""
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(scored_clips, f, ensure_ascii=False, indent=2)
        logger.info(f"评分结果已保存到: {output_path}")

    def _duration_seconds(self, clip: Dict) -> Optional[float]:
        """计算片段时长，无法解析时返回 None。"""
        try:
            bounds = self._time_bounds_seconds(clip)
            if bounds is None:
                return None

            start_seconds, end_seconds = bounds
            duration = end_seconds - start_seconds
            return duration if duration > 0 else None
        except Exception as e:
            logger.warning(f"片段 {clip.get('id', '未知')} 时长解析失败: {e}")
            return None

    def _time_bounds_seconds(self, clip: Dict) -> Optional[tuple[float, float]]:
        """返回片段起止秒数。"""
        start_time = clip.get('start_time')
        end_time = clip.get('end_time')
        if not start_time or not end_time:
            return None

        start_seconds = self.text_processor.time_to_seconds(str(start_time))
        end_seconds = self.text_processor.time_to_seconds(str(end_time))
        if end_seconds <= start_seconds:
            return None

        return start_seconds, end_seconds

    @staticmethod
    def _seconds_to_srt_time(seconds: float) -> str:
        """将秒数转为 SRT 时间格式。"""
        total_milliseconds = max(0, int(round(seconds * 1000)))
        hours, remainder = divmod(total_milliseconds, 3600 * 1000)
        minutes, remainder = divmod(remainder, 60 * 1000)
        secs, milliseconds = divmod(remainder, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"

    def select_high_score_clips(
        self,
        scored_clips: List[Dict],
        selection_config: Optional[Dict[str, Any]] = None,
        subtitle_entries: Optional[List[Dict[str, Any]]] = None,
        subtitle_source_path: Optional[Path] = None,
        min_score_threshold: float = MIN_SCORE_THRESHOLD,
    ) -> List[Dict]:
        """按评分、时长和数量筛选最终进入后续步骤的片段。"""
        try:
            normalized_config = normalize_clip_selection_config(selection_config)
        except ValueError as e:
            logger.warning(f"片段筛选配置无效，已忽略人工控制参数: {e}")
            normalized_config = {}

        target_count = normalized_config.get("target_clip_count")
        min_duration = normalized_config.get("min_clip_duration_sec")
        max_duration = normalized_config.get("max_clip_duration_sec")
        min_sentence_count = normalized_config.get("min_clip_sentence_count")
        max_sentence_count = normalized_config.get("max_clip_sentence_count")
        duration_filter_enabled = min_duration is not None or max_duration is not None
        sentence_control_enabled = min_sentence_count is not None or max_sentence_count is not None
        subtitle_entries = subtitle_entries or []
        sentence_groups = build_sentence_groups(subtitle_entries) if sentence_control_enabled else []

        selected_clips = []
        for clip in scored_clips:
            duration = self._duration_seconds(clip)
            bounds = self._time_bounds_seconds(clip)
            if duration is not None:
                clip["duration_seconds"] = round(duration, 2)

            if clip.get('final_score', 0) < min_score_threshold:
                continue

            if duration_filter_enabled and duration is None:
                continue
            if min_duration is not None and duration < min_duration:
                continue

            selected_clip = dict(clip)
            final_start_seconds = None
            final_end_seconds = None
            if bounds is not None:
                final_start_seconds, final_end_seconds = bounds

            if max_duration is not None and duration > max_duration:
                if final_start_seconds is None or final_end_seconds is None:
                    continue

                final_end_seconds = min(final_end_seconds, final_start_seconds + max_duration)
                selected_clip["original_start_time"] = clip.get("start_time")
                selected_clip["original_end_time"] = clip.get("end_time")
                selected_clip["original_duration_seconds"] = round(duration, 2)
                selected_clip["end_time"] = self._seconds_to_srt_time(final_end_seconds)
                selected_clip["duration_seconds"] = round(final_end_seconds - final_start_seconds, 2)
                selected_clip["was_truncated_by_clip_selection"] = True

            if sentence_control_enabled:
                selected_clip["sentence_range_applied"] = False
                selected_clip["sentence_group_applied"] = False
                selected_clip["sentence_count"] = 0
                selected_clip["tail_padding_seconds"] = DEFAULT_CLIP_TAIL_PADDING_SECONDS
                if subtitle_source_path:
                    selected_clip["subtitle_source_path"] = str(subtitle_source_path)
                if min_sentence_count is not None:
                    selected_clip["min_clip_sentence_count"] = min_sentence_count
                if max_sentence_count is not None:
                    selected_clip["max_clip_sentence_count"] = max_sentence_count

                if (
                    sentence_groups
                    and final_start_seconds is not None
                    and final_end_seconds is not None
                    and final_end_seconds > final_start_seconds
                ):
                    matched_groups = collect_overlapping_sentence_groups(
                        sentence_groups,
                        final_start_seconds,
                        final_end_seconds,
                        max_groups=max_sentence_count,
                    )
                    selected_clip["sentence_count"] = len(matched_groups)
                    selected_clip["sentence_range_applied"] = True
                    selected_clip["sentence_group_applied"] = True
                    selected_clip["sentence_cue_count"] = sum(int(group.get("cue_count", 0)) for group in matched_groups)
                    selected_clip["sentence_count_below_min"] = (
                        min_sentence_count is not None and len(matched_groups) < min_sentence_count
                    )

                    if matched_groups:
                        original_start_time = selected_clip.get("start_time")
                        original_end_time = selected_clip.get("end_time")
                        sentence_start_seconds = float(matched_groups[0]["start_seconds"])
                        sentence_end_seconds = float(matched_groups[-1]["end_seconds"])
                        max_end_by_duration = (
                            sentence_start_seconds + max_duration
                            if max_duration is not None else None
                        )
                        hard_end_limit = final_end_seconds
                        if max_end_by_duration is not None:
                            hard_end_limit = min(hard_end_limit, max_end_by_duration)

                        sentence_limited_end_seconds = min(sentence_end_seconds, hard_end_limit)
                        if sentence_end_seconds > hard_end_limit:
                            selected_clip["sentence_boundary_truncated_by_limit"] = True

                        padded_end_seconds = min(
                            sentence_limited_end_seconds + DEFAULT_CLIP_TAIL_PADDING_SECONDS,
                            hard_end_limit,
                        )

                        if padded_end_seconds > sentence_start_seconds:
                            if (
                                abs(sentence_start_seconds - final_start_seconds) > 0.001
                                or abs(padded_end_seconds - final_end_seconds) > 0.001
                            ):
                                selected_clip.setdefault("original_start_time", original_start_time)
                                selected_clip.setdefault("original_end_time", original_end_time)
                                selected_clip.setdefault("original_duration_seconds", round(duration, 2) if duration else None)

                            final_start_seconds = sentence_start_seconds
                            final_end_seconds = padded_end_seconds
                            selected_clip["start_time"] = self._seconds_to_srt_time(final_start_seconds)
                            selected_clip["end_time"] = self._seconds_to_srt_time(final_end_seconds)
                            selected_clip["sentence_limited_end_time"] = self._seconds_to_srt_time(sentence_limited_end_seconds)
                            selected_clip["tail_padding_applied_seconds"] = round(
                                max(0.0, final_end_seconds - sentence_limited_end_seconds),
                                3,
                            )
                            selected_clip["duration_seconds"] = round(final_end_seconds - final_start_seconds, 2)
                            selected_clip["was_adjusted_to_sentence_boundary"] = True

            selected_clips.append(selected_clip)

        if target_count is not None:
            selected_clips = sorted(
                selected_clips,
                key=lambda x: x.get('final_score', 0),
                reverse=True
            )[:target_count]

        selected_clips.sort(key=lambda x: self.text_processor.time_to_seconds(str(x.get('start_time', '00:00:00,000'))))

        logger.info(
            "片段筛选完成: 原始=%s, 入选=%s, 评分阈值=%.2f, 数量上限=%s, 时长范围=%s-%s秒, 句数范围=%s-%s",
            len(scored_clips),
            len(selected_clips),
            min_score_threshold,
            target_count or "自动",
            min_duration or "不限",
            max_duration or "不限",
            min_sentence_count or "不限",
            max_sentence_count or "不限",
        )

        return selected_clips

def run_step3_scoring(
    timeline_path: Path,
    metadata_dir: Path = None,
    output_path: Optional[Path] = None,
    prompt_files: Dict = None,
    selection_config: Optional[Dict[str, Any]] = None,
    subtitle_path: Optional[Path] = None,
    min_score_threshold: float = MIN_SCORE_THRESHOLD,
) -> List[Dict]:
    """
    运行Step 3: 内容评分与筛选
    
    Args:
        timeline_path: 时间线文件路径
        output_path: 输出文件路径
        prompt_files: 自定义提示词文件
        
    Returns:
        高分切片列表
    """
    # 加载时间线数据
    with open(timeline_path, 'r', encoding='utf-8') as f:
        timeline_data = json.load(f)

    if metadata_dir is None:
        metadata_dir = METADATA_DIR
    subtitle_source_path = subtitle_path or find_project_input_srt(metadata_dir)
    subtitle_entries = load_srt_entries(subtitle_source_path)
    
    # 创建评分器
    scorer = ClipScorer(prompt_files)
    
    # 评分
    scored_clips = scorer.score_clips(timeline_data)
    
    # 筛选最终进入后续步骤的切片
    high_score_clips = scorer.select_high_score_clips(
        scored_clips,
        selection_config=selection_config,
        subtitle_entries=subtitle_entries,
        subtitle_source_path=subtitle_source_path,
        min_score_threshold=min_score_threshold,
    )
    
    # 保存结果
    # 保存所有评分后的片段（用于调试和分析）
    all_scored_path = metadata_dir / "step3_all_scored.json"
    scorer.save_scores(scored_clips, all_scored_path)
    
    # 保存筛选后的高分片段（用于后续步骤）
    if output_path is None:
        output_path = metadata_dir / "step3_high_score_clips.json"
        
    scorer.save_scores(high_score_clips, output_path)

    if timeline_data and scored_clips and not high_score_clips:
        raise ValueError("没有符合条件的片段，请放宽时长或数量参数")
    
    return high_score_clips
