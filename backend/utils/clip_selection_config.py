"""片段筛选配置工具。"""

from typing import Any, Dict, Mapping, Optional


def _first_present(source: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in source and source[key] is not None:
            return source[key]
    return None


def _positive_int(value: Any, field_name: str) -> Optional[int]:
    if value is None or value == "":
        return None

    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是正整数") from exc

    if parsed <= 0:
        raise ValueError(f"{field_name} 必须大于 0")

    return parsed


def build_clip_selection_config(
    target_clip_count: Any = None,
    min_clip_duration_sec: Any = None,
    max_clip_duration_sec: Any = None,
    min_clip_sentence_count: Any = None,
    max_clip_sentence_count: Any = None,
) -> Dict[str, int]:
    """构建并校验片段筛选配置。空值表示沿用自动模式。"""
    config: Dict[str, int] = {}

    target_count = _positive_int(target_clip_count, "生成片段数量")
    min_duration = _positive_int(min_clip_duration_sec, "最短时长")
    max_duration = _positive_int(max_clip_duration_sec, "最长时长")
    min_sentence_count = _positive_int(min_clip_sentence_count, "最少句数")
    max_sentence_count = _positive_int(max_clip_sentence_count, "最多句数")

    if min_duration is not None and max_duration is not None and min_duration > max_duration:
        raise ValueError("最短时长不能大于最长时长")
    if min_sentence_count is not None and max_sentence_count is not None and min_sentence_count > max_sentence_count:
        raise ValueError("最少句数不能大于最多句数")

    if target_count is not None:
        config["target_clip_count"] = target_count
    if min_duration is not None:
        config["min_clip_duration_sec"] = min_duration
    if max_duration is not None:
        config["max_clip_duration_sec"] = max_duration
    if min_sentence_count is not None:
        config["min_clip_sentence_count"] = min_sentence_count
    if max_sentence_count is not None:
        config["max_clip_sentence_count"] = max_sentence_count

    return config


def normalize_clip_selection_config(raw_config: Optional[Mapping[str, Any]]) -> Dict[str, int]:
    """从项目配置或请求配置中读取片段筛选参数。"""
    if not raw_config:
        return {}

    source: Mapping[str, Any] = raw_config
    nested = raw_config.get("clip_selection")
    if isinstance(nested, Mapping):
        source = nested

    return build_clip_selection_config(
        target_clip_count=_first_present(source, "target_clip_count", "clip_count", "max_clips"),
        min_clip_duration_sec=_first_present(source, "min_clip_duration_sec", "min_clip_duration"),
        max_clip_duration_sec=_first_present(source, "max_clip_duration_sec", "max_clip_duration"),
        min_clip_sentence_count=_first_present(source, "min_clip_sentence_count", "min_sentence_count"),
        max_clip_sentence_count=_first_present(source, "max_clip_sentence_count", "max_sentence_count"),
    )
