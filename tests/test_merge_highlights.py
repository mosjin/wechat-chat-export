import contextlib
import importlib.util
import io
import json
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def load_modules():
    wechatauto = ModuleType("wechatauto")
    wechatauto.WeChatDB = Mock(side_effect=AssertionError("Real WeChatDB is forbidden"))
    wechatauto.MediaDownloader = Mock(side_effect=AssertionError("Real media is forbidden"))

    zstandard = ModuleType("zstandard")
    zstandard.ZstdDecompressor = Mock(side_effect=AssertionError("No zstd fixtures"))

    pillow = ModuleType("PIL")
    pillow.Image = Mock()
    pillow.ImageStat = Mock()

    with patch.dict(sys.modules, {
        "wechatauto": wechatauto,
        "zstandard": zstandard,
        "PIL": pillow,
    }):
        spec = importlib.util.spec_from_file_location(
            "exporter_core",
            ROOT / "exporter_core.py",
        )
        core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(core)

    with patch.dict(sys.modules, {"exporter_core": core}):
        spec = importlib.util.spec_from_file_location(
            "merge_highlights",
            ROOT / "merge_highlights.py",
        )
        merge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(merge)
    return core, merge


def make_message(time, sender, username, content, server_id):
    return {
        "time": time,
        "sender": sender,
        "sender_username": username,
        "is_self": False,
        "sender_status": "resolved",
        "content": content,
        "type": "文本",
        "media": None,
        "transcript": None,
        "transcript_source": None,
        "server_id": server_id,
        "local_id": server_id,
    }


def make_segment(index, messages, starter, starter_username, reasons,
                 monologue=False):
    participants = []
    usernames = []
    counts = {}
    for message in messages:
        if message["sender"] not in counts:
            participants.append(message["sender"])
            usernames.append(message["sender_username"])
            counts[message["sender"]] = 0
        counts[message["sender"]] += 1
    first, last = messages[0], messages[-1]
    return {
        "index": index,
        "starter": starter,
        "starter_username": starter_username,
        "participants": participants,
        "participant_usernames": usernames,
        "sender_counts": counts,
        "message_count": len(messages),
        "start_time": first["time"],
        "end_time": last["time"],
        "start_date": first["time"][:10],
        "has_focus": False,
        "focus_message_count": 0,
        "focus_ratio": 0.0,
        "is_monologue": monologue,
        "candidate": True,
        "candidate_reasons": reasons,
        "title_placeholder": (
            f"{starter}《待大模型命名》精华 {first['time'][:10]}"
        ),
        "messages": messages,
    }


def group_a_segments():
    monologue = make_segment(
        1,
        [
            make_message("2026-11-10 09:00:00", "张三", "wxid_z3",
                         "讲个概念：什么是递归", 101),
            make_message("2026-11-10 09:05:00", "张三", "wxid_z3",
                         "递归就是函数调用自己", 102),
        ],
        "张三", "wxid_z3", ["monologue"], monologue=True,
    )
    discussion = make_segment(
        2,
        [
            make_message("2026-11-11 10:00:00", "张三", "wxid_z3",
                         "编程课怎么选？", 201),
            make_message("2026-11-11 10:05:00", "李四", "wxid_l4",
                         "看老师和班型", 202),
            make_message("2026-11-11 10:10:00", "张三", "wxid_z3",
                         "编程课还要看练习量", 203),
        ],
        "张三", "wxid_z3", ["discussion_size"],
    )
    return [monologue, discussion]


def group_b_segments():
    outing = make_segment(
        1,
        [
            make_message("2026-11-09 20:00:00", "李四", "wxid_l4",
                         "周末去爬山吗", 301),
            make_message("2026-11-09 20:03:00", "王五", "wxid_w5",
                         "去", 302),
            make_message("2026-11-09 20:06:00", "张三", "wxid_z3",
                         "我也去", 303),
        ],
        "李四", "wxid_l4", ["discussion_size"],
    )
    textbook = make_segment(
        2,
        [
            make_message("2026-11-12 08:00:00", "李四", "wxid_l4",
                         "编程课教材我整理好了", 401),
        ],
        "李四", "wxid_l4", ["monologue"], monologue=True,
    )
    return [outing, textbook]


def write_candidates(chat_dir, chat_name, segments):
    highlights = Path(chat_dir) / "highlights"
    highlights.mkdir(parents=True, exist_ok=True)
    data = {
        "exporter_version": "1.4.0",
        "chat_name": chat_name,
        "chat_type": "group",
        "generated_at": "2026-11-13 21:00:00",
        "topic_gap_minutes": 30,
        "focus_speakers": [],
        "segment_total": len(segments),
        "candidate_total": len(segments),
        "excluded_segment_total": 0,
        "excluded_message_total": 0,
        "candidates": segments,
    }
    (highlights / "candidates.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_exports_root(base):
    """两个有候选的会话 + 群A 的重复导出副本 + 两个应跳过的会话。"""
    root = Path(base) / "exports"
    root.mkdir(parents=True)
    write_candidates(root / "群A", "群A", group_a_segments())
    write_candidates(root / "群B", "群B", group_b_segments())
    # 同一会话（username 相同）被重复导出到另一个目录：靠 multi_index 认出。
    write_candidates(root / "群A副本", "群A", group_a_segments())
    (root / "multi_index.json").write_text(
        json.dumps({
            "chats": [
                {"name": "群A", "username": "111@chatroom", "folder": "群A"},
                {"name": "群A", "username": "111@chatroom", "folder": "群A副本"},
                {"name": "群B", "username": "222@chatroom", "folder": "群B"},
            ]
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "群C").mkdir()  # 没有 highlights，应跳过
    broken = root / "群D" / "highlights"
    broken.mkdir(parents=True)
    (broken / "candidates.json").write_text("{不是合法JSON", encoding="utf-8")
    return root


class ScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _core, cls.merge = load_modules()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = build_exports_root(temp.name)

    def test_collects_across_sessions_sorted_by_time(self):
        scan = self.merge.scan_highlight_candidates(self.root)
        self.assertEqual(scan["sessions_scanned"], 5)
        self.assertEqual(scan["sessions_with_candidates"], 3)
        starts = [s["start_time"] for s in scan["segments"]]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(
            starts,
            [
                "2026-11-09 20:00:00",
                "2026-11-10 09:00:00",
                "2026-11-11 10:00:00",
                "2026-11-12 08:00:00",
            ],
        )

    def test_dedupes_same_session_reexport(self):
        scan = self.merge.scan_highlight_candidates(self.root)
        self.assertEqual(scan["duplicates_removed"], 2)
        self.assertEqual(len(scan["segments"]), 4)
        folders = [s["source"]["folder"] for s in scan["segments"]]
        self.assertNotIn("群A副本", folders)
        chat_keys = {s["source"]["chat_key"] for s in scan["segments"]}
        self.assertEqual(chat_keys, {"111@chatroom", "222@chatroom"})

    def test_missing_and_broken_candidates_are_skipped_and_listed(self):
        scan = self.merge.scan_highlight_candidates(self.root)
        skipped = {item["folder"]: item["reason"]
                   for item in scan["skipped_sessions"]}
        self.assertEqual(set(skipped), {"群C", "群D"})
        self.assertIn("未生成精华候选", skipped["群C"])
        self.assertIn("读取失败", skipped["群D"])

    def test_missing_root_raises(self):
        with self.assertRaises(ValueError):
            self.merge.scan_highlight_candidates(self.root / "不存在")


class PersonMergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _core, cls.merge = load_modules()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = build_exports_root(temp.name)

    def test_person_collects_monologue_discussion_and_participation(self):
        report = self.merge.merge_highlights_by_person(self.root, ["张三"])
        output = report["outputs"][0]
        self.assertEqual(output["segment_total"], 3)
        data = json.loads(Path(output["json"]).read_text(encoding="utf-8-sig"))
        self.assertEqual(data["person"], "张三")
        self.assertEqual(data["segment_total"], 3)
        roles = [s["person_role"] for s in data["segments"]]
        self.assertEqual(roles, ["参与讨论", "独白", "发起"])
        starts = [s["start_time"] for s in data["segments"]]
        self.assertEqual(starts, sorted(starts))
        sources = {s["source"]["chat_name"] for s in data["segments"]}
        self.assertEqual(sources, {"群A", "群B"})
        # 跳过明细随产物留档
        self.assertEqual(
            {item["folder"] for item in data["skipped_sessions"]},
            {"群C", "群D"},
        )

    def test_person_matches_by_username_too(self):
        report = self.merge.merge_highlights_by_person(
            self.root, ["wxid_z3"]
        )
        self.assertEqual(report["outputs"][0]["segment_total"], 3)

    def test_person_md_and_prompt_written(self):
        report = self.merge.merge_highlights_by_person(self.root, "李四")
        output = report["outputs"][0]
        md = Path(output["md"]).read_text(encoding="utf-8")
        self.assertIn("李四 的精华合并（以人为中心）", md)
        self.assertIn("来源会话：群B", md)
        self.assertIn("本人角色：独白", md)
        self.assertIn("编程课教材我整理好了", md)
        prompt = Path(output["prompt"]).read_text(encoding="utf-8")
        self.assertIn("李四《话题》精华 <日期范围>", prompt)
        self.assertIn("按话题归类", prompt)

    def test_multiple_persons_produce_multiple_outputs(self):
        report = self.merge.merge_highlights_by_person(
            self.root, ["张三", "李四"]
        )
        self.assertEqual(len(report["outputs"]), 2)
        totals = {o["person"]: o["segment_total"] for o in report["outputs"]}
        self.assertEqual(totals, {"张三": 3, "李四": 3})

    def test_empty_person_list_raises(self):
        with self.assertRaises(ValueError):
            self.merge.merge_highlights_by_person(self.root, [])


class TopicMergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _core, cls.merge = load_modules()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = build_exports_root(temp.name)

    def test_topic_hits_and_misses(self):
        report = self.merge.merge_highlights_by_topic(self.root, ["编程课"])
        output = report["outputs"][0]
        self.assertEqual(output["segment_total"], 2)
        data = json.loads(Path(output["json"]).read_text(encoding="utf-8-sig"))
        starts = [s["start_time"] for s in data["segments"]]
        self.assertEqual(
            starts, ["2026-11-11 10:00:00", "2026-11-12 08:00:00"]
        )

    def test_topic_hit_positions_are_annotated(self):
        report = self.merge.merge_highlights_by_topic(self.root, ["编程课"])
        output = report["outputs"][0]
        data = json.loads(Path(output["json"]).read_text(encoding="utf-8-sig"))
        hits = {
            s["start_time"]: s["keyword_hits"]["编程课"]
            for s in data["segments"]
        }
        self.assertEqual(hits["2026-11-11 10:00:00"], [1, 3])
        self.assertEqual(hits["2026-11-12 08:00:00"], [1])
        md = Path(output["md"]).read_text(encoding="utf-8")
        self.assertIn("命中消息序号：1、3", md)
        self.assertIn("【命中】", md)
        # 未命中的消息不带命中标注
        self.assertNotIn("【命中】 2026-11-11 10:05:00", md)

    def test_topic_without_hits_writes_empty_result(self):
        report = self.merge.merge_highlights_by_topic(
            self.root, ["不存在的词"]
        )
        output = report["outputs"][0]
        self.assertEqual(output["segment_total"], 0)
        self.assertTrue(Path(output["md"]).is_file())

    def test_multiple_keywords_produce_multiple_outputs(self):
        report = self.merge.merge_highlights_by_topic(
            self.root, "编程课,爬山"
        )
        self.assertEqual(len(report["outputs"]), 2)
        totals = {
            o["keyword"]: o["segment_total"] for o in report["outputs"]
        }
        self.assertEqual(totals, {"编程课": 2, "爬山": 1})

    def test_topic_prompt_asks_for_relevance_review(self):
        report = self.merge.merge_highlights_by_topic(self.root, ["编程课"])
        prompt = Path(report["outputs"][0]["prompt"]).read_text(
            encoding="utf-8"
        )
        self.assertIn("复核相关性", prompt)
        self.assertIn("剔除", prompt)
        self.assertIn("《编程课》精华 <日期范围>", prompt)

    def test_empty_keyword_list_raises(self):
        with self.assertRaises(ValueError):
            self.merge.merge_highlights_by_topic(self.root, None)


class CliMergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core, cls.merge = load_modules()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = build_exports_root(temp.name)

    def _run_cli(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.dict(sys.modules, {
                "exporter_core": self.core,
                "merge_highlights": self.merge,
            }),
            patch.object(sys, "argv", argv),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            with self.assertRaises(SystemExit) as caught:
                runpy.run_path(str(ROOT / "cli.py"), run_name="__main__")
        return caught.exception.code, stdout.getvalue(), stderr.getvalue()

    def test_cli_merge_person(self):
        code, stdout, stderr = self._run_cli([
            "cli.py", "--merge-person", "张三",
            "--exports-root", str(self.root),
        ])
        self.assertEqual(code, 0)
        merged = self.root / "merged" / "person_张三.json"
        self.assertTrue(merged.is_file())
        self.assertIn("合并完成：3 段", stdout)
        self.assertIn("群C", stderr)  # 跳过会话在 stderr 列明

    def test_cli_merge_topic(self):
        code, stdout, _stderr = self._run_cli([
            "cli.py", "--merge-topic", "编程课",
            "--exports-root", str(self.root),
        ])
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "merged" / "topic_编程课.md").is_file())
        self.assertIn("合并完成：2 段", stdout)

    def test_cli_merge_missing_root_exits_1(self):
        code, _stdout, _stderr = self._run_cli([
            "cli.py", "--merge-person", "张三",
            "--exports-root", str(self.root / "不存在"),
        ])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
