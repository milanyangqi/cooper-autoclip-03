"""同步字幕句子边界测试。"""

from backend.pipeline.step3_scoring import ClipScorer
from backend.utils.subtitle_sync import (
    DEFAULT_CLIP_TAIL_PADDING_SECONDS,
    build_sentence_groups,
    collect_overlapping_sentence_groups,
    seconds_to_srt_time,
    write_clipped_srt,
)
from backend.utils.text_processor import TextProcessor


def _entry(start: float, end: float, text: str) -> dict:
    return {
        "start_seconds": start,
        "end_seconds": end,
        "start_time": seconds_to_srt_time(start),
        "end_time": seconds_to_srt_time(end),
        "text": text,
    }


def _scorer() -> ClipScorer:
    scorer = ClipScorer.__new__(ClipScorer)
    scorer.text_processor = TextProcessor()
    return scorer


def test_sentence_groups_merge_half_sentence_cues():
    entries = [
        _entry(1.0, 3.0, "David Muir, ABC's World News Tonight, America's"),
        _entry(3.1, 5.0, "most watched newscast."),
        _entry(5.1, 8.0, "Buildings collapsing in Venezuela, the desperate search"),
        _entry(8.1, 9.0, "at this hour for people trapped."),
    ]

    groups = build_sentence_groups(entries)

    assert len(groups) == 2
    assert groups[0]["cue_count"] == 2
    assert groups[0]["start_seconds"] == 1.0
    assert groups[0]["end_seconds"] == 5.0
    assert groups[1]["cue_count"] == 2
    assert groups[1]["end_seconds"] == 9.0


def test_sentence_group_collection_limits_by_sentences_not_cues():
    entries = [
        _entry(1.0, 3.0, "First half"),
        _entry(3.1, 5.0, "first sentence."),
        _entry(5.1, 8.0, "Second half"),
        _entry(8.1, 9.0, "second sentence."),
    ]
    groups = build_sentence_groups(entries)

    selected = collect_overlapping_sentence_groups(groups, 1.0, 20.0, max_groups=1)

    assert len(selected) == 1
    assert selected[0]["cue_count"] == 2
    assert selected[0]["end_seconds"] == 5.0


def test_clip_selection_keeps_complete_sentence_and_tail_padding():
    scorer = _scorer()
    subtitle_entries = [
        _entry(1.0, 3.0, "First sentence starts"),
        _entry(3.1, 5.0, "and ends here."),
        _entry(5.1, 8.0, "Second sentence starts"),
        _entry(8.1, 9.0, "and ends here."),
    ]
    clips = [{
        "id": "1",
        "start_time": "00:00:01,000",
        "end_time": "00:00:20,000",
        "final_score": 0.95,
    }]

    selected = scorer.select_high_score_clips(
        clips,
        selection_config={"max_clip_sentence_count": 2},
        subtitle_entries=subtitle_entries,
        min_score_threshold=0.1,
    )

    assert len(selected) == 1
    clip = selected[0]
    assert clip["sentence_count"] == 2
    assert clip["sentence_cue_count"] == 4
    assert clip["start_time"] == "00:00:01,000"
    assert clip["sentence_limited_end_time"] == "00:00:09,000"
    assert clip["end_time"] == "00:00:09,500"
    assert clip["tail_padding_applied_seconds"] == DEFAULT_CLIP_TAIL_PADDING_SECONDS


def test_tail_padding_respects_max_duration_limit():
    scorer = _scorer()
    subtitle_entries = [
        _entry(1.0, 3.0, "First sentence starts"),
        _entry(3.1, 5.0, "and ends here."),
        _entry(5.1, 8.0, "Second sentence starts"),
        _entry(8.1, 9.0, "and ends here."),
    ]
    clips = [{
        "id": "1",
        "start_time": "00:00:01,000",
        "end_time": "00:00:20,000",
        "final_score": 0.95,
    }]

    selected = scorer.select_high_score_clips(
        clips,
        selection_config={"max_clip_sentence_count": 2, "max_clip_duration_sec": 8},
        subtitle_entries=subtitle_entries,
        min_score_threshold=0.1,
    )

    assert len(selected) == 1
    clip = selected[0]
    assert clip["sentence_limited_end_time"] == "00:00:09,000"
    assert clip["end_time"] == "00:00:09,000"
    assert clip["tail_padding_applied_seconds"] == 0.0
    assert clip["duration_seconds"] == 8.0


def test_clipped_srt_never_exceeds_video_window(tmp_path):
    output_path = tmp_path / "clip.srt"
    entries = [
        _entry(1.0, 3.0, "First sentence."),
        _entry(3.1, 5.8, "Second sentence."),
    ]

    count = write_clipped_srt(entries, output_path, 1.0, 5.5)

    content = output_path.read_text(encoding="utf-8")
    assert count == 2
    assert "00:00:04,500" in content
    assert "00:00:04,800" not in content
