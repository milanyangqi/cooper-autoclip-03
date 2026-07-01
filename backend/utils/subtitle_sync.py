"""同步字幕裁剪与拼接工具。"""

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .text_processor import TextProcessor


def seconds_to_srt_time(seconds: float) -> str:
    """将秒数转为 SRT 时间格式。"""
    total_milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_milliseconds, 3600 * 1000)
    minutes, remainder = divmod(remainder, 60 * 1000)
    secs, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def load_srt_entries(srt_path: Optional[Path]) -> List[Dict[str, Any]]:
    """读取 SRT，并补齐秒数字段。"""
    if not srt_path or not srt_path.exists():
        return []

    processor = TextProcessor()
    entries = []
    for entry in processor.parse_srt(srt_path):
        try:
            item = dict(entry)
            item["start_seconds"] = processor.time_to_seconds(str(entry["start_time"]))
            item["end_seconds"] = processor.time_to_seconds(str(entry["end_time"]))
            if item["end_seconds"] > item["start_seconds"]:
                entries.append(item)
        except Exception:
            continue

    return entries


def find_project_input_srt(metadata_dir: Optional[Path]) -> Optional[Path]:
    """从项目 metadata 目录推断原始 input.srt 路径。"""
    if not metadata_dir:
        return None

    project_dir = Path(metadata_dir).parent
    candidates = [
        project_dir / "raw" / "input.srt",
        project_dir / "input.srt",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def collect_overlapping_entries(
    entries: Iterable[Dict[str, Any]],
    start_seconds: float,
    end_seconds: float,
    max_entries: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """收集与时间窗重叠的字幕条。"""
    selected = []
    for entry in entries:
        entry_start = float(entry["start_seconds"])
        entry_end = float(entry["end_seconds"])
        if entry_end <= start_seconds or entry_start >= end_seconds:
            continue

        selected.append(entry)
        if max_entries is not None and len(selected) >= max_entries:
            break

    return selected


def write_clipped_srt(
    entries: Iterable[Dict[str, Any]],
    output_path: Path,
    window_start: float,
    window_end: float,
) -> int:
    """写出相对片段 0 秒起算的 SRT。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    count = 0

    for entry in entries:
        entry_start = max(float(entry["start_seconds"]), window_start)
        entry_end = min(float(entry["end_seconds"]), window_end)
        if entry_end <= entry_start:
            continue

        count += 1
        relative_start = max(0.0, entry_start - window_start)
        relative_end = max(relative_start, entry_end - window_start)
        lines.extend([
            str(count),
            f"{seconds_to_srt_time(relative_start)} --> {seconds_to_srt_time(relative_end)}",
            str(entry.get("text", "")).strip(),
            "",
        ])

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return count


def read_relative_srt_entries(srt_path: Path) -> List[Dict[str, Any]]:
    """读取已经相对 0 秒的 SRT。"""
    return load_srt_entries(srt_path)


def concatenate_srt_files(
    clip_subtitles: Iterable[Dict[str, Any]],
    output_path: Path,
) -> int:
    """按切片顺序拼接字幕，并按累计时长平移时间。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    subtitle_index = 1
    offset_seconds = 0.0

    for item in clip_subtitles:
        subtitle_path = Path(item["subtitle_path"])
        duration = float(item.get("duration_seconds") or 0)
        entries = read_relative_srt_entries(subtitle_path) if subtitle_path.exists() else []

        max_entry_end = 0.0
        for entry in entries:
            start = float(entry["start_seconds"]) + offset_seconds
            end = float(entry["end_seconds"]) + offset_seconds
            max_entry_end = max(max_entry_end, float(entry["end_seconds"]))
            if end <= start:
                continue

            lines.extend([
                str(subtitle_index),
                f"{seconds_to_srt_time(start)} --> {seconds_to_srt_time(end)}",
                str(entry.get("text", "")).strip(),
                "",
            ])
            subtitle_index += 1

        offset_seconds += duration if duration > 0 else max_entry_end

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return subtitle_index - 1
