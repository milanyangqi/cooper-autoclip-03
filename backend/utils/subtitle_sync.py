"""同步字幕裁剪与拼接工具。"""

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .text_processor import TextProcessor

DEFAULT_SENTENCE_PAUSE_SECONDS = 1.0
DEFAULT_CLIP_TAIL_PADDING_SECONDS = 0.5
DEFAULT_SUBTITLE_TAIL_HOLD_SECONDS = 0.1
SENTENCE_END_PUNCTUATION = ".?!。！？"
TRAILING_SENTENCE_CLOSERS = "\"'”’)]）】》」』"


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


def _entry_text(entry: Dict[str, Any]) -> str:
    return str(entry.get("text", "")).strip()


def _has_sentence_terminal(text: str) -> bool:
    cleaned = text.strip().rstrip(TRAILING_SENTENCE_CLOSERS).strip()
    return bool(cleaned) and cleaned[-1] in SENTENCE_END_PUNCTUATION


def build_sentence_groups(
    entries: Iterable[Dict[str, Any]],
    pause_threshold_seconds: float = DEFAULT_SENTENCE_PAUSE_SECONDS,
) -> List[Dict[str, Any]]:
    """将连续 SRT cue 合并为完整句子段。"""
    ordered_entries = sorted(
        [entry for entry in entries if float(entry.get("end_seconds", 0)) > float(entry.get("start_seconds", 0))],
        key=lambda item: float(item["start_seconds"]),
    )
    groups: List[Dict[str, Any]] = []
    current_entries: List[Dict[str, Any]] = []
    current_texts: List[str] = []

    def flush_group() -> None:
        if not current_entries:
            return

        groups.append({
            "start_seconds": float(current_entries[0]["start_seconds"]),
            "end_seconds": float(current_entries[-1]["end_seconds"]),
            "text": " ".join(text for text in current_texts if text).strip(),
            "entries": list(current_entries),
            "cue_count": len(current_entries),
        })
        current_entries.clear()
        current_texts.clear()

    for index, entry in enumerate(ordered_entries):
        start_seconds = float(entry["start_seconds"])
        end_seconds = float(entry["end_seconds"])

        if current_entries:
            previous_end = float(current_entries[-1]["end_seconds"])
            if start_seconds - previous_end >= pause_threshold_seconds:
                flush_group()

        current_entries.append(entry)
        current_texts.append(_entry_text(entry))

        next_entry = ordered_entries[index + 1] if index + 1 < len(ordered_entries) else None
        next_gap = None
        if next_entry:
            next_gap = float(next_entry["start_seconds"]) - end_seconds

        if _has_sentence_terminal(" ".join(current_texts)):
            flush_group()
        elif next_gap is not None and next_gap >= pause_threshold_seconds:
            flush_group()

    flush_group()
    return groups


def collect_overlapping_sentence_groups(
    groups: Iterable[Dict[str, Any]],
    start_seconds: float,
    end_seconds: float,
    max_groups: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """收集与时间窗重叠的完整句子段。"""
    selected = []
    for group in groups:
        group_start = float(group["start_seconds"])
        group_end = float(group["end_seconds"])
        if group_end <= start_seconds or group_start >= end_seconds:
            continue

        selected.append(group)
        if max_groups is not None and len(selected) >= max_groups:
            break

    return selected


def write_clipped_srt(
    entries: Iterable[Dict[str, Any]],
    output_path: Path,
    window_start: float,
    window_end: float,
    extend_last_to_window_end: bool = False,
    last_subtitle_end_seconds: Optional[float] = None,
) -> int:
    """写出相对片段 0 秒起算的 SRT。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    clipped_entries: List[Dict[str, Any]] = []

    for entry in entries:
        entry_start = max(float(entry["start_seconds"]), window_start)
        entry_end = min(float(entry["end_seconds"]), window_end)
        if entry_end <= entry_start:
            continue

        clipped_entries.append({
            "start_seconds": entry_start,
            "end_seconds": entry_end,
            "text": str(entry.get("text", "")).strip(),
        })

    if extend_last_to_window_end and last_subtitle_end_seconds is None:
        last_subtitle_end_seconds = window_end

    if last_subtitle_end_seconds is not None and clipped_entries:
        clipped_entries[-1]["end_seconds"] = max(
            float(clipped_entries[-1]["end_seconds"]),
            float(last_subtitle_end_seconds),
        )

    count = 0
    for entry in clipped_entries:
        count += 1
        relative_start = max(0.0, float(entry["start_seconds"]) - window_start)
        relative_end = max(relative_start, float(entry["end_seconds"]) - window_start)
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
            relative_start = float(entry["start_seconds"])
            relative_end = float(entry["end_seconds"])
            max_entry_end = max(max_entry_end, relative_end)
            if duration > 0:
                relative_start = min(relative_start, duration)
                relative_end = min(relative_end, duration)

            start = relative_start + offset_seconds
            end = relative_end + offset_seconds
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
