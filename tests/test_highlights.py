import contextlib
import importlib.util
import io
import json
import runpy
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def load_core():
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
            "_highlights_exporter_core",
            ROOT / "exporter_core.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def make_message(time, sender, username, content, is_self=False):
    return {
        "time": time,
        "sender": sender,
        "sender_username": username,
        "is_self": is_self,
        "sender_status": "resolved",
        "content": content,
        "type": "文本",
        "media": None,
        "transcript": None,
        "transcript_source": None,
    }


class ValidateGapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = load_core()

    def test_default_and_bounds(self):
        self.assertEqual(self.core.validate_topic_gap_minutes(None), 30)
        self.assertEqual(self.core.validate_topic_gap_minutes(5), 5)
        self.assertEqual(self.core.validate_topic_gap_minutes(120), 120)
        self.assertEqual(self.core.validate_topic_gap_minutes("45"), 45)

    def test_out_of_range_and_non_integer_raise(self):
        for bad in (4, 121, 0, -10, "abc", 30.5):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.core.validate_topic_gap_minutes(bad)


class ParseFocusListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = load_core()

    def test_comma_and_semicolon_forms(self):
        self.assertEqual(
            self.core.parse_focus_list("张三,李四；王五"),
            ["张三", "李四", "王五"],
        )
        self.assertEqual(
            self.core.parse_focus_list(["张三，李四", "王五"]),
            ["张三", "李四", "王五"],
        )

    def test_empty(self):
        self.assertEqual(self.core.parse_focus_list(None), [])
        self.assertEqual(self.core.parse_focus_list(""), [])


class SplitSegmentsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = load_core()

    def test_exactly_30_minutes_does_not_split(self):
        messages = [
            make_message("2026-11-13 09:00:00", "张三", "wxid_z3", "开始"),
            make_message("2026-11-13 09:30:00", "李四", "wxid_l4", "半小时整"),
        ]
        segments = self.core.split_topic_segments(messages)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["message_count"], 2)

    def test_over_30_minutes_splits(self):
        messages = [
            make_message("2026-11-13 09:00:00", "张三", "wxid_z3", "开始"),
            make_message("2026-11-13 09:30:01", "李四", "wxid_l4", "多一秒"),
            make_message("2026-11-13 09:31:00", "张三", "wxid_z3", "还在新段"),
        ]
        segments = self.core.split_topic_segments(messages)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["message_count"], 1)
        self.assertEqual(segments[1]["message_count"], 2)

    def test_custom_gap(self):
        messages = [
            make_message("2026-11-13 09:00:00", "张三", "wxid_z3", "一"),
            make_message("2026-11-13 09:11:00", "李四", "wxid_l4", "二"),
        ]
        self.assertEqual(
            len(self.core.split_topic_segments(messages, gap_minutes=10)), 2
        )
        self.assertEqual(
            len(self.core.split_topic_segments(messages, gap_minutes=30)), 1
        )

    def test_unsorted_input_is_sorted_by_time(self):
        messages = [
            make_message("2026-11-13 10:00:00", "李四", "wxid_l4", "后"),
            make_message("2026-11-13 09:00:00", "张三", "wxid_z3", "先"),
        ]
        segments = self.core.split_topic_segments(messages)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["starter"], "张三")
        self.assertEqual(segments[1]["starter"], "李四")

    def test_segment_metadata_participants_and_counts(self):
        messages = [
            make_message("2026-11-13 09:00:00", "李四", "wxid_l4", "我先说"),
            make_message("2026-11-13 09:01:00", "张三", "wxid_z3", "接"),
            make_message("2026-11-13 09:02:00", "李四", "wxid_l4", "再说"),
        ]
        (segment,) = self.core.split_topic_segments(messages)
        self.assertEqual(segment["starter"], "李四")
        self.assertEqual(segment["participants"], ["李四", "张三"])
        self.assertEqual(segment["sender_counts"], {"李四": 2, "张三": 1})
        self.assertEqual(segment["message_count"], 3)
        self.assertEqual(segment["start_time"], "2026-11-13 09:00:00")
        self.assertEqual(segment["end_time"], "2026-11-13 09:02:00")
        self.assertEqual(segment["start_date"], "2026-11-13")
        self.assertFalse(segment["is_monologue"])
        self.assertEqual(
            segment["title_placeholder"], "李四《待大模型命名》精华 2026-11-13"
        )

    def test_monologue_segment_is_candidate(self):
        messages = [
            make_message("2026-11-13 09:00:00", "张三", "wxid_z3", "讲个概念"),
        ]
        (segment,) = self.core.split_topic_segments(messages)
        self.assertTrue(segment["is_monologue"])
        self.assertTrue(segment["candidate"])
        self.assertIn("monologue", segment["candidate_reasons"])

    def test_discussion_of_three_is_candidate(self):
        messages = [
            make_message("2026-11-13 09:00:00", "张三", "wxid_z3", "一"),
            make_message("2026-11-13 09:01:00", "李四", "wxid_l4", "二"),
            make_message("2026-11-13 09:02:00", "王五", "wxid_w5", "三"),
        ]
        (segment,) = self.core.split_topic_segments(messages)
        self.assertTrue(segment["candidate"])
        self.assertIn("discussion_size", segment["candidate_reasons"])

    def test_two_person_two_messages_not_candidate(self):
        messages = [
            make_message("2026-11-13 09:00:00", "张三", "wxid_z3", "在吗"),
            make_message("2026-11-13 09:01:00", "李四", "wxid_l4", "在"),
        ]
        (segment,) = self.core.split_topic_segments(messages)
        self.assertFalse(segment["candidate"])
        self.assertEqual(segment["candidate_reasons"], [])

    def test_focus_segment_candidate_and_metadata(self):
        messages = [
            make_message("2026-11-13 09:00:00", "张三", "wxid_z3", "普通一句"),
            make_message("2026-11-13 09:01:00", "群主", "wxid_owner", "重点"),
        ]
        (segment,) = self.core.split_topic_segments(messages, focus="群主")
        self.assertTrue(segment["has_focus"])
        self.assertEqual(segment["focus_message_count"], 1)
        self.assertEqual(segment["focus_ratio"], 0.5)
        self.assertTrue(segment["candidate"])
        self.assertIn("focus_speaker", segment["candidate_reasons"])

    def test_focus_matches_by_username(self):
        messages = [
            make_message("2026-11-13 09:00:00", "群主", "wxid_owner", "重点"),
            make_message("2026-11-13 09:01:00", "张三", "wxid_z3", "附议"),
        ]
        (segment,) = self.core.split_topic_segments(
            messages, focus=["wxid_owner"]
        )
        self.assertTrue(segment["has_focus"])
        self.assertEqual(segment["focus_message_count"], 1)

    def test_no_focus_list_means_no_focus_flags(self):
        messages = [
            make_message("2026-11-13 09:00:00", "群主", "wxid_owner", "一"),
            make_message("2026-11-13 09:01:00", "张三", "wxid_z3", "二"),
        ]
        (segment,) = self.core.split_topic_segments(messages)
        self.assertFalse(segment["has_focus"])
        self.assertEqual(segment["focus_message_count"], 0)
        self.assertEqual(segment["focus_ratio"], 0.0)
        self.assertNotIn("focus_speaker", segment["candidate_reasons"])


def sample_messages():
    """三段：3 人讨论（候选）、2 人 2 条闲聊（不入选）、张三独白（候选）。"""
    return [
        make_message("2026-11-13 09:00:00", "张三", "wxid_z3", "提个问题"),
        make_message("2026-11-13 09:05:00", "李四", "wxid_l4", "我觉得这样"),
        make_message("2026-11-13 09:10:00", "王五", "wxid_w5", "同意"),
        make_message("2026-11-13 12:00:00", "张三", "wxid_z3", "在吗"),
        make_message("2026-11-13 12:01:00", "李四", "wxid_l4", "在"),
        make_message("2026-11-13 20:00:00", "张三", "wxid_z3", "讲个定义：……"),
    ]


class WriteHighlightsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = load_core()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.chat_dir = Path(temp.name) / "测试群"
        self.chat_dir.mkdir()

    def _write(self, **kwargs):
        return self.core.write_highlight_candidates(
            self.chat_dir,
            sample_messages(),
            True,
            "测试群",
            **kwargs,
        )

    def test_files_written_and_counts(self):
        result = self._write()
        self.assertEqual(result["segment_total"], 3)
        self.assertEqual(result["candidate_total"], 2)
        self.assertEqual(result["excluded_segment_total"], 1)
        self.assertEqual(result["excluded_message_total"], 2)
        out = Path(result["dir"])
        self.assertEqual(out.name, "highlights")
        for name in ("candidates.md", "candidates.json", "llm_prompt.md"):
            self.assertTrue((out / name).is_file(), name)

    def test_json_and_md_segment_counts_agree(self):
        result = self._write()
        data = json.loads(
            Path(result["candidates_json"]).read_text(encoding="utf-8-sig")
        )
        self.assertEqual(len(data["candidates"]), 2)
        self.assertEqual(data["candidate_total"], 2)
        self.assertEqual(data["segment_total"], 3)
        self.assertEqual(data["excluded_segment_total"], 1)
        self.assertEqual(data["excluded_message_total"], 2)
        md = Path(result["candidates_md"]).read_text(encoding="utf-8")
        headings = [
            line for line in md.splitlines()
            if line.startswith("## ") and "《待大模型命名》" in line
        ]
        self.assertEqual(len(headings), len(data["candidates"]))

    def test_md_placeholder_titles_and_content(self):
        result = self._write()
        md = Path(result["candidates_md"]).read_text(encoding="utf-8")
        self.assertIn("张三《待大模型命名》精华 2026-11-13", md)
        self.assertIn("提个问题", md)
        self.assertIn("讲个定义", md)
        # 未入选段的原文不进候选 MD。
        self.assertNotIn("在吗", md.replace("还在", ""))

    def test_llm_prompt_covers_four_requirements(self):
        result = self._write()
        prompt = Path(result["llm_prompt"]).read_text(encoding="utf-8")
        self.assertIn("话题转换", prompt)
        self.assertIn("有价值 / 可能有价值 / 无价值", prompt)
        self.assertIn("《话题》精华", prompt)
        self.assertIn("总结", prompt)

    def test_focus_metadata_flows_into_outputs(self):
        result = self._write(focus="李四")
        data = json.loads(
            Path(result["candidates_json"]).read_text(encoding="utf-8-sig")
        )
        self.assertEqual(data["focus_speakers"], ["李四"])
        first = data["candidates"][0]
        self.assertTrue(first["has_focus"])
        self.assertEqual(first["focus_message_count"], 1)
        md = Path(result["candidates_md"]).read_text(encoding="utf-8")
        self.assertIn("特别关注人物：李四", md)
        self.assertIn("含特别关注人物发言", md)


class ExportChatImplHighlightsTests(unittest.TestCase):
    """全链路：不开选项时产物与现状一致；开时主文件逐字节不变。"""

    @classmethod
    def setUpClass(cls):
        cls.core = load_core()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def _rows(self):
        def ts(day, hh, mm):
            return int(datetime(2026, 11, day, hh, mm, 0).timestamp())

        def row(local_id, day, hh, mm, username, content):
            return {
                "local_id": local_id,
                "server_id": local_id,
                "_db_rel": "message_0.db",
                "real_sender_id": local_id,
                "local_type": 1,
                "message_content": content,
                "compress_content": None,
                "create_time": ts(day, hh, mm),
                "sort_seq": local_id,
                "_sender_username": username,
                "_sender_status": "resolved",
            }

        return [
            row(1, 13, 9, 0, "wxid_z3", "提个问题"),
            row(2, 13, 9, 5, "wxid_me", "我回复"),
            row(3, 13, 9, 10, "wxid_z3", "补充"),
        ]

    def _run_impl(self, out_name, **kwargs):
        fake_db = Mock()
        fake_db.get_self_info.return_value = {"username": "wxid_me"}
        fake_db._nickname_index.return_value = {
            "wxid_z3": "张三",
            "wxid_me": "我自己",
        }
        with patch.object(self.core, "load_rows", return_value=self._rows()):
            return self.core._export_chat_impl(
                out_root=self.root / out_name,
                db=fake_db,
                contact={"username": "123@chatroom", "name": "测试群"},
                **kwargs,
            )

    def test_default_export_has_no_highlights(self):
        result = self._run_impl("plain")
        chat_dir = Path(result["output_dir"])
        self.assertIsNone(result["highlights"])
        self.assertFalse((chat_dir / "highlights").exists())
        self.assertEqual(
            sorted(p.name for p in chat_dir.iterdir()),
            ["chat_full_for_llm.md", "chat_full_for_llm.txt",
             "chat_full_parsed.json"],
        )

    def test_highlights_keeps_main_files_byte_identical(self):
        plain = self._run_impl("plain")
        enriched = self._run_impl("enriched", highlights=True)
        for key in ("txt", "md", "json"):
            self.assertEqual(
                Path(plain[key]).read_bytes(),
                Path(enriched[key]).read_bytes(),
                key,
            )
        info = enriched["highlights"]
        self.assertIsNotNone(info)
        self.assertEqual(info["segment_total"], 1)
        self.assertEqual(info["candidate_total"], 1)
        out = Path(info["dir"])
        self.assertTrue((out / "candidates.md").is_file())
        self.assertTrue((out / "candidates.json").is_file())
        self.assertTrue((out / "llm_prompt.md").is_file())


class CliHighlightsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = load_core()

    def _run_cli(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = 0
        with (
            patch.dict(sys.modules, {"exporter_core": self.core}),
            patch.object(sys, "argv", argv),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                runpy.run_path(str(ROOT / "cli.py"), run_name="__main__")
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def _fake_result(self):
        return {
            "chat_name": "测试群",
            "output_dir": "/tmp/out/测试群",
            "sensitive_cache_cleanup": True,
        }

    def test_plain_name_defaults_highlights_off(self):
        with patch.object(
            self.core, "export_chat", return_value=self._fake_result()
        ) as single:
            code, _out, _err = self._run_cli(["cli.py", "测试群"])
        self.assertEqual(code, 0)
        kwargs = single.call_args.kwargs
        self.assertFalse(kwargs["highlights"])
        self.assertEqual(kwargs["topic_gap_minutes"], 30)
        self.assertEqual(kwargs["focus_speakers"], [])

    def test_highlights_flag_and_gap(self):
        with patch.object(
            self.core, "export_chat", return_value=self._fake_result()
        ) as single:
            code, _out, _err = self._run_cli(
                ["cli.py", "测试群", "--highlights",
                 "--topic-gap-minutes", "45"]
            )
        self.assertEqual(code, 0)
        kwargs = single.call_args.kwargs
        self.assertTrue(kwargs["highlights"])
        self.assertEqual(kwargs["topic_gap_minutes"], 45)

    def test_focus_repeat_and_list_forms_auto_enable(self):
        with patch.object(
            self.core, "export_chat", return_value=self._fake_result()
        ) as single:
            code, _out, _err = self._run_cli(
                ["cli.py", "测试群", "--focus", "张三,李四",
                 "--focus", "王五"]
            )
        self.assertEqual(code, 0)
        kwargs = single.call_args.kwargs
        self.assertTrue(kwargs["highlights"])
        self.assertEqual(kwargs["focus_speakers"], ["张三", "李四", "王五"])

    def test_invalid_gap_is_cli_error(self):
        code, _out, err = self._run_cli(
            ["cli.py", "测试群", "--highlights", "--topic-gap-minutes", "3"]
        )
        self.assertEqual(code, 2)
        self.assertIn("5-120", err)


if __name__ == "__main__":
    unittest.main()
