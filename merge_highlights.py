# -*- coding: utf-8 -*-
"""Issue #4: 精华合并后处理（以人为中心 / 以话题为中心）。

扫描一个 exports 根目录下全部会话子目录的 highlights/candidates.json
（Issue #3 的产出），把话题段跨会话、跨日期收齐、去重、按时间升序
打包到 merged/ 子目录，并附给大模型的提示词文件。

分工与 Issue #3 的 B 方案一致：程序只负责收齐与本地关键词预筛，
相关性判断、话题归类与总结归大模型。程序不联网、不接大模型 API、
不做语义聚类；不修改 Issue #3 的候选产出格式。本模块是纯后处理，
不需要连接微信数据库。

去重键构成（与 Issue #4 口径对齐）：
- 会话标识 chat_key：优先取 exports 根目录 multi_index.json（Issue #1
  批量导出索引）里该会话目录对应的 username；取不到时退回会话目录名。
  因此同一会话经批量导出重复出现时能按 username 认出彼此；单次导出
  又换了目录名重导出的副本无法据此互认，属已知限制。
- 消息键：server_id 优先，其次 local_id，两者都缺失时用
  「时间|发送者账号或显示名|内容」的指纹兜底。
- 段去重键 = (chat_key, 该段全部消息键组成的元组)：同段不重复收。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from exporter_core import (
    APP_VERSION,
    HIGHLIGHTS_CANDIDATES_JSON_NAME,
    HIGHLIGHTS_DIR_NAME,
    MULTI_INDEX_JSON_NAME,
    TOPIC_REASON_LABELS,
    parse_sender_list,
    safe_folder_name,
)

# --- Issue #4: highlight merging (merged/) ---

# 精华合并的独立常量（不与 Issue #1/#2/#3 的常量共用）。
MERGED_DIR_NAME = "merged"
MERGE_PERSON_FILE_PREFIX = "person_"
MERGE_TOPIC_FILE_PREFIX = "topic_"
MERGE_PROMPT_FILE_SUFFIX = "_prompt.md"
# 以人为中心时，该人在段中的角色（机器可读口径，MD 里同文展示）。
MERGE_ROLE_STARTER = "发起"
MERGE_ROLE_PARTICIPANT = "参与讨论"
MERGE_ROLE_MONOLOGUE = "独白"
# 话题模式在 MD 里给命中消息加的行首标注。
MERGE_TOPIC_HIT_MARK = "【命中】"

MERGE_PERSON_LLM_PROMPT = """# 任务：把一个人散落在多个群、多个日期的话题段，按话题合并成精华

下面附的是程序从多个会话的精华候选里收齐的、与「__PERSON__」有关的话题段
（同目录下 person 开头的 .md 文件内容，请把它粘贴在这段提示词后面），
覆盖他发起的、他参与讨论的和他独白的段。程序只负责跨群跨日期收齐、
去重和按时间排序，话题归类与总结由你完成。

## 要求

1. **按话题归类**：把讲同一件事的段归为一组（哪怕来自不同群、不同日期）；
   只是都由他参与、但话题不同的段不要硬合并成一个话题。
2. **判相关性**：同一组内的段若只是人名相同、实际讲的不是同一件事，
   拆成独立条目并说明理由。
3. **逐话题输出**：每组输出一个标题「__PERSON__《话题》精华 <日期范围>」
   （日期范围取该组最早到最晚的日期），标题下写精华总结：保留他的关键
   观点、结论和有代表性的原话，不要编造段里没有的内容。
4. **标注来源**：每个话题下列出它来自哪些会话（群名）和日期。

## 输出格式

逐话题输出：标题、日期范围、来源会话、精华总结。
"""

MERGE_TOPIC_LLM_PROMPT = """# 任务：把围绕同一个话题的讨论段合并成一份精华总结

下面附的是程序用关键词「__KEYWORD__」在多个会话的精华候选里预筛出的话题段
（同目录下 topic 开头的 .md 文件内容，请把它粘贴在这段提示词后面）。
程序只做了字面命中预筛，相关性复核与合并总结由你完成。

## 要求

1. **复核相关性**：逐段判断是否真的在讨论这个话题；仅字面命中关键词、
   实际无关的段要剔除，并列出被剔除段的来源（会话名 + 日期）与理由。
2. **合并总结**：把相关段按时间线合并成一份精华总结，保留关键事实、
   不同人的主要观点与结论，不要编造段里没有的内容。
3. **标注来源**：总结中每个要点后标注来源（会话名 + 日期）。

## 输出格式

先给合并后的精华总结，标题为「《__KEYWORD__》精华 <日期范围>」
（日期范围取相关段最早到最晚的日期）；再列「已剔除段」清单
（来源 + 理由）。
"""


def parse_merge_list(raw) -> list[str]:
    """Split a person / keyword list.

    Same separators as the sender list (',' / '，' / '、' / ';' / '；');
    items are supplied by the user, nothing is hardcoded.
    """
    return parse_sender_list(raw)


def _load_folder_usernames(exports_root: Path) -> dict[str, str]:
    """folder name -> username, from the Issue #1 multi-export index.

    Used only to give a session a stable identity for dedupe; a missing
    or unreadable index simply means every session falls back to its
    folder name.
    """
    index_path = exports_root / MULTI_INDEX_JSON_NAME
    try:
        data = json.loads(index_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    chats = data.get("chats") if isinstance(data, dict) else None
    mapping: dict[str, str] = {}
    for entry in chats or []:
        if not isinstance(entry, dict):
            continue
        username = entry.get("username")
        if not username:
            continue
        folder = entry.get("folder")
        if folder:
            mapping[str(folder)] = str(username)
        output_dir = entry.get("output_dir")
        if output_dir:
            mapping[Path(str(output_dir)).name] = str(username)
    return mapping


def _message_key(message: dict) -> str:
    """Stable key of one message inside its conversation.

    server_id first, local_id second (Issue #4 dedupe口径); when both
    are absent (older/hand-built candidates), fall back to a
    time|sender|content fingerprint.
    """
    server_id = message.get("server_id")
    if server_id not in (None, ""):
        return f"s:{server_id}"
    local_id = message.get("local_id")
    if local_id not in (None, ""):
        return f"l:{local_id}"
    return (
        f"f:{message.get('time') or ''}"
        f"|{message.get('sender_username') or message.get('sender') or ''}"
        f"|{message.get('content') or ''}"
    )


def _segment_sort_key(record: dict):
    start = record.get("start_time") or ""
    return (
        not start,
        start,
        str(record.get("source", {}).get("chat_key") or ""),
        record.get("index") or 0,
    )


def scan_highlight_candidates(exports_root, progress=None) -> dict:
    """Collect every candidate segment under an exports root.

    Sessions without a readable highlights/candidates.json are skipped
    and listed in ``skipped_sessions`` (never fatal). Segments are
    deduped by (chat_key, message keys) and sorted by start time
    ascending across all sessions.
    """
    def log(msg):
        if progress:
            progress(msg)

    root = Path(exports_root)
    if not root.is_dir():
        raise ValueError(f"导出目录不存在或不是目录：{root}")

    usernames = _load_folder_usernames(root)
    sessions_scanned = 0
    sessions_with_candidates = 0
    skipped_sessions: list[dict] = []
    segments: list[dict] = []
    seen_keys: set = set()
    duplicates_removed = 0

    session_dirs = sorted(
        (p for p in root.iterdir() if p.is_dir()),
        key=lambda p: p.name,
    )
    for session_dir in session_dirs:
        if session_dir.name == MERGED_DIR_NAME:
            continue
        sessions_scanned += 1
        candidates_path = (
            session_dir / HIGHLIGHTS_DIR_NAME / HIGHLIGHTS_CANDIDATES_JSON_NAME
        )
        if not candidates_path.is_file():
            skipped_sessions.append({
                "folder": session_dir.name,
                "reason": "无 highlights/candidates.json（未生成精华候选）",
            })
            continue
        try:
            data = json.loads(candidates_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            skipped_sessions.append({
                "folder": session_dir.name,
                "reason": f"candidates.json 读取失败：{exc}",
            })
            continue
        candidates = data.get("candidates") if isinstance(data, dict) else None
        if not isinstance(candidates, list):
            skipped_sessions.append({
                "folder": session_dir.name,
                "reason": "candidates.json 结构异常（无 candidates 列表）",
            })
            continue
        sessions_with_candidates += 1
        chat_key = usernames.get(session_dir.name) or session_dir.name
        for segment in candidates:
            if not isinstance(segment, dict):
                continue
            messages = segment.get("messages") or []
            dedupe_key = (
                chat_key,
                tuple(_message_key(m) for m in messages if isinstance(m, dict)),
            )
            if dedupe_key in seen_keys:
                duplicates_removed += 1
                continue
            seen_keys.add(dedupe_key)
            record = dict(segment)
            record["source"] = {
                "chat_key": chat_key,
                "chat_name": data.get("chat_name") or session_dir.name,
                "chat_type": data.get("chat_type"),
                "folder": session_dir.name,
                "candidates_json": str(candidates_path.resolve()),
            }
            segments.append(record)

    segments.sort(key=_segment_sort_key)
    log(
        f"精华合并扫描：会话 {sessions_scanned} 个，"
        f"有候选 {sessions_with_candidates} 个，"
        f"跳过 {len(skipped_sessions)} 个；"
        f"话题段 {len(segments)} 段（去重 {duplicates_removed} 段）。"
    )
    return {
        "exports_root": str(root.resolve()),
        "sessions_scanned": sessions_scanned,
        "sessions_with_candidates": sessions_with_candidates,
        "skipped_sessions": skipped_sessions,
        "duplicates_removed": duplicates_removed,
        "segments": segments,
    }


def _segment_matches_person(segment: dict, person: str) -> bool:
    if person in (segment.get("participants") or []):
        return True
    if person in (segment.get("participant_usernames") or []):
        return True
    if person in (segment.get("starter"), segment.get("starter_username")):
        return True
    for message in segment.get("messages") or []:
        if person in (
            message.get("sender"),
            message.get("sender_username"),
        ):
            return True
    return False


def _person_role(segment: dict, person: str) -> str:
    if segment.get("is_monologue"):
        return MERGE_ROLE_MONOLOGUE
    if person in (segment.get("starter"), segment.get("starter_username")):
        return MERGE_ROLE_STARTER
    return MERGE_ROLE_PARTICIPANT


def _segment_keyword_hits(segment: dict, keyword: str) -> list[int]:
    """1-based positions of messages whose text contains the keyword.

    The searched text is the message content plus its voice transcript
    when present (both are the segment's 原文). Pure substring match —
    a local pre-filter only; relevance is judged by the LLM.
    """
    positions: list[int] = []
    for position, message in enumerate(segment.get("messages") or [], 1):
        texts = [str(message.get("content") or "")]
        transcript = message.get("transcript") or (
            message.get("media") or {}
        ).get("transcript")
        if transcript:
            texts.append(str(transcript))
        if any(keyword in text for text in texts):
            positions.append(position)
    return positions


def _date_range(segments: list[dict]) -> dict:
    starts = [s.get("start_time") for s in segments if s.get("start_time")]
    ends = [
        s.get("end_time") or s.get("start_time")
        for s in segments
        if s.get("end_time") or s.get("start_time")
    ]
    return {
        "from": min(starts) if starts else None,
        "to": max(ends) if ends else None,
    }


def _base_payload(mode: str, scan: dict, segments: list[dict]) -> dict:
    return {
        "exporter_version": APP_VERSION,
        "mode": mode,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "exports_root": scan["exports_root"],
        "sessions_scanned": scan["sessions_scanned"],
        "sessions_with_candidates": scan["sessions_with_candidates"],
        "skipped_sessions": scan["skipped_sessions"],
        "duplicates_removed": scan["duplicates_removed"],
        "segment_total": len(segments),
        "message_total": sum(s.get("message_count") or 0 for s in segments),
        "date_range": _date_range(segments),
        "segments": segments,
    }


def _segment_md_lines(segment: dict, extra_lines=(), hit_positions=()):
    source = segment.get("source") or {}
    lines = [f"## {segment.get('title_placeholder') or '（无标题占位）'}", ""]
    lines.append(
        f"- 来源会话：{source.get('chat_name')}"
        f"（目录 {source.get('folder')}，段序号 {segment.get('index')}）"
    )
    lines.append(
        f"- 时间：{segment.get('start_time') or '—'}"
        f" ~ {segment.get('end_time') or '—'}"
    )
    lines.append(
        f"- 参与者：{'、'.join(segment.get('participants') or [])}"
        f"（共 {segment.get('message_count') or 0} 条）"
    )
    counts = "、".join(
        f"{name} {count} 条"
        for name, count in (segment.get("sender_counts") or {}).items()
    )
    if counts:
        lines.append(f"- 每人条数：{counts}")
    reason_labels = "、".join(
        TOPIC_REASON_LABELS.get(reason, reason)
        for reason in segment.get("candidate_reasons") or []
    )
    if reason_labels:
        lines.append(f"- 入选理由：{reason_labels}")
    lines.extend(extra_lines)
    lines.append("")
    hit_set = set(hit_positions)
    for position, message in enumerate(segment.get("messages") or [], 1):
        content = message.get("content") or f"[{message.get('type')}]"
        mark = f"{MERGE_TOPIC_HIT_MARK} " if position in hit_set else ""
        lines.append(
            f"{mark}{message.get('time')} | {message.get('sender')}：{content}"
        )
        transcript = message.get("transcript") or (
            message.get("media") or {}
        ).get("transcript")
        if transcript:
            lines.append(f"    ↳ 转文字：{transcript}")
    lines.append("")
    return lines


def _write_merged_files(merged_dir: Path, stem: str, payload: dict,
                        md_lines: list[str], prompt_text: str) -> dict:
    md_path = merged_dir / f"{stem}.md"
    json_path = merged_dir / f"{stem}.json"
    prompt_path = merged_dir / f"{stem}{MERGE_PROMPT_FILE_SUFFIX}"
    with md_path.open("w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(md_lines))
    with json_path.open("w", encoding="utf-8-sig") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    with prompt_path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(prompt_text)
    return {
        "md": str(md_path.resolve()),
        "json": str(json_path.resolve()),
        "prompt": str(prompt_path.resolve()),
        "segment_total": payload["segment_total"],
        "message_total": payload["message_total"],
    }


def merge_highlights_by_person(exports_root, persons, progress=None) -> dict:
    """Merge every candidate segment a person took part in.

    Collects segments the person started, joined or monologued in,
    across all sessions and dates, sorted by time ascending. Writes
    merged/person_<人名>.md + .json + _prompt.md per person.
    """
    def log(msg):
        if progress:
            progress(msg)

    names = parse_merge_list(persons)
    if not names:
        raise ValueError("请至少指定一个人名（--merge-person）。")
    scan = scan_highlight_candidates(exports_root, progress=progress)
    merged_dir = Path(exports_root) / MERGED_DIR_NAME
    merged_dir.mkdir(parents=True, exist_ok=True)

    outputs = []
    for person in names:
        selected = []
        for segment in scan["segments"]:
            if _segment_matches_person(segment, person):
                record = dict(segment)
                record["person_role"] = _person_role(segment, person)
                selected.append(record)
        payload = _base_payload("person", scan, selected)
        payload["person"] = person

        date_range = payload["date_range"]
        lines = [
            f"# {person} 的精华合并（以人为中心）",
            "",
            f"- 导出器版本：{APP_VERSION}",
            f"- 生成时间：{payload['generated_at']}",
            f"- 导出根目录：{scan['exports_root']}",
            (
                f"- 扫描会话 {scan['sessions_scanned']} 个，"
                f"有候选 {scan['sessions_with_candidates']} 个，"
                f"跳过 {len(scan['skipped_sessions'])} 个"
                "（明细见同名 .json 的 skipped_sessions）"
            ),
            f"- 去重后话题段共 {len(scan['segments'])} 段"
            f"（重复段已去 {scan['duplicates_removed']} 段）",
            (
                f"- 与 {person} 有关的段 {payload['segment_total']} 段、"
                f"{payload['message_total']} 条消息"
            ),
            f"- 日期范围：{date_range['from'] or '—'} ~ {date_range['to'] or '—'}",
            "",
            "把同目录下 person 开头的 _prompt.md 提示词与本文件一起交给大模型，"
            "即可得到按话题归类的精华汇总。",
            "",
        ]
        for segment in selected:
            lines.extend(_segment_md_lines(
                segment,
                extra_lines=[f"- 本人角色：{segment['person_role']}"],
            ))
        stem = MERGE_PERSON_FILE_PREFIX + safe_folder_name(person)
        output = _write_merged_files(
            merged_dir,
            stem,
            payload,
            lines,
            MERGE_PERSON_LLM_PROMPT.replace("__PERSON__", person),
        )
        output["person"] = person
        outputs.append(output)
        log(
            f"以人为中心合并：{person} 共 {output['segment_total']} 段，"
            f"已写入 {output['md']}"
        )
    return {
        "mode": "person",
        "exports_root": scan["exports_root"],
        "merged_dir": str(merged_dir.resolve()),
        "sessions_scanned": scan["sessions_scanned"],
        "sessions_with_candidates": scan["sessions_with_candidates"],
        "skipped_sessions": scan["skipped_sessions"],
        "duplicates_removed": scan["duplicates_removed"],
        "segment_total": len(scan["segments"]),
        "outputs": outputs,
    }


def merge_highlights_by_topic(exports_root, keywords, progress=None) -> dict:
    """Merge candidate segments around topic keywords.

    Local pre-filter only: a segment is selected for a keyword when at
    least one message literally contains it; hit positions are marked
    in the output. Writes merged/topic_<关键词>.md + .json + _prompt.md
    per keyword.
    """
    def log(msg):
        if progress:
            progress(msg)

    words = parse_merge_list(keywords)
    if not words:
        raise ValueError("请至少指定一个关键词（--merge-topic）。")
    scan = scan_highlight_candidates(exports_root, progress=progress)
    merged_dir = Path(exports_root) / MERGED_DIR_NAME
    merged_dir.mkdir(parents=True, exist_ok=True)

    outputs = []
    for keyword in words:
        selected = []
        for segment in scan["segments"]:
            positions = _segment_keyword_hits(segment, keyword)
            if not positions:
                continue
            record = dict(segment)
            record["matched_keywords"] = [keyword]
            record["keyword_hits"] = {keyword: positions}
            selected.append(record)
        payload = _base_payload("topic", scan, selected)
        payload["keyword"] = keyword

        date_range = payload["date_range"]
        lines = [
            f"# 话题精华合并：{keyword}（以话题为中心）",
            "",
            f"- 导出器版本：{APP_VERSION}",
            f"- 生成时间：{payload['generated_at']}",
            f"- 导出根目录：{scan['exports_root']}",
            (
                f"- 扫描会话 {scan['sessions_scanned']} 个，"
                f"有候选 {scan['sessions_with_candidates']} 个，"
                f"跳过 {len(scan['skipped_sessions'])} 个"
                "（明细见同名 .json 的 skipped_sessions）"
            ),
            f"- 去重后话题段共 {len(scan['segments'])} 段"
            f"（重复段已去 {scan['duplicates_removed']} 段）",
            (
                f"- 命中关键词「{keyword}」的段 {payload['segment_total']} 段、"
                f"{payload['message_total']} 条消息"
                f"（{MERGE_TOPIC_HIT_MARK} 标注命中消息）"
            ),
            f"- 日期范围：{date_range['from'] or '—'} ~ {date_range['to'] or '—'}",
            "",
            "把同目录下 topic 开头的 _prompt.md 提示词与本文件一起交给大模型，"
            "由大模型复核相关性并合并成一份精华总结。",
            "",
        ]
        for segment in selected:
            positions = segment["keyword_hits"][keyword]
            lines.extend(_segment_md_lines(
                segment,
                extra_lines=[
                    f"- 命中关键词：{keyword}"
                    f"（命中消息序号：{'、'.join(str(p) for p in positions)}）"
                ],
                hit_positions=positions,
            ))
        stem = MERGE_TOPIC_FILE_PREFIX + safe_folder_name(keyword)
        output = _write_merged_files(
            merged_dir,
            stem,
            payload,
            lines,
            MERGE_TOPIC_LLM_PROMPT.replace("__KEYWORD__", keyword),
        )
        output["keyword"] = keyword
        outputs.append(output)
        log(
            f"以话题为中心合并：关键词「{keyword}」命中 "
            f"{output['segment_total']} 段，已写入 {output['md']}"
        )
    return {
        "mode": "topic",
        "exports_root": scan["exports_root"],
        "merged_dir": str(merged_dir.resolve()),
        "sessions_scanned": scan["sessions_scanned"],
        "sessions_with_candidates": scan["sessions_with_candidates"],
        "skipped_sessions": scan["skipped_sessions"],
        "duplicates_removed": scan["duplicates_removed"],
        "segment_total": len(scan["segments"]),
        "outputs": outputs,
    }
