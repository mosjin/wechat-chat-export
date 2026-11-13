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
            "_by_sender_exporter_core",
            ROOT / "exporter_core.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def make_message(time, sender, username, content, is_self=False, status="resolved"):
    return {
        "time": time,
        "sender": sender,
        "sender_username": username,
        "is_self": is_self,
        "sender_status": status,
        "content": content,
        "type": "文本",
        "media": None,
        "transcript": None,
        "transcript_source": None,
    }


def sample_messages():
    """张三 3 条（跨两天）、李四 2 条、我 1 条、未知发送者 2 条（两种未知形态）。"""
    return [
        make_message("2026-11-10 09:00:00", "张三", "wxid_z3", "张三第一天的话"),
        make_message("2026-11-10 09:01:00", "李四", "wxid_l4", "李四的话一"),
        make_message("2026-11-11 10:00:00", "张三", "wxid_z3", "张三第二天的话"),
        make_message("2026-11-11 10:05:00", "我", "wxid_me", "我说的", is_self=True),
        make_message("2026-11-11 11:00:00", "张三", "wxid_z3", "张三第三条"),
        make_message("2026-11-12 08:00:00", "李四", "wxid_l4", "李四的话二"),
        make_message("2026-11-12 09:00:00", "未知发送者", None, "无名氏的话",
                     is_self=None, status="unmapped_sender_id"),
        make_message("2026-11-12 09:30:00", "未知发送者（私聊身份异常）",
                     "wxid_odd", "身份异常的话", is_self=None,
                     status="unexpected_private_sender"),
    ]


class ParseSenderListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = load_core()

    def test_comma_variants_and_dunhao(self):
        self.assertEqual(
            self.core.parse_sender_list("张三,李四，王五、赵六"),
            ["张三", "李四", "王五", "赵六"],
        )

    def test_repeated_values_flattened_and_deduped(self):
        self.assertEqual(
            self.core.parse_sender_list(["张三,李四", "王五", "张三"]),
            ["张三", "李四", "王五"],
        )

    def test_empty_inputs(self):
        self.assertEqual(self.core.parse_sender_list(None), [])
        self.assertEqual(self.core.parse_sender_list(""), [])
        self.assertEqual(self.core.parse_sender_list(" , ，"), [])


class ParseExportDateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = load_core()

    def test_valid_date_and_leap_day(self):
        self.assertEqual(self.core.parse_export_date("2026-11-10"), "2026-11-10")
        self.assertEqual(self.core.parse_export_date(" 2024-02-29 "), "2024-02-29")

    def test_empty_means_no_bound(self):
        self.assertIsNone(self.core.parse_export_date(None))
        self.assertIsNone(self.core.parse_export_date(""))
        self.assertIsNone(self.core.parse_export_date("   "))

    def test_bad_format_and_impossible_date_raise(self):
        with self.assertRaises(ValueError):
            self.core.parse_export_date("2026/11/10")
        with self.assertRaises(ValueError):
            self.core.parse_export_date("2026-02-29")


class GroupMessagesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = load_core()

    def test_grouping_counts_order_and_span(self):
        groups = self.core.group_messages_by_sender(sample_messages())
        by_name = {g["sender"]: g for g in groups}
        self.assertEqual(
            set(by_name), {"张三", "李四", "我", "未知发送者"}
        )
        self.assertEqual(by_name["张三"]["message_count"], 3)
        self.assertEqual(by_name["李四"]["message_count"], 2)
        self.assertEqual(by_name["我"]["message_count"], 1)
        self.assertEqual(by_name["我"]["is_self"], True)
        self.assertEqual(by_name["张三"]["first_time"], "2026-11-10 09:00:00")
        self.assertEqual(by_name["张三"]["last_time"], "2026-11-11 11:00:00")
        # 条数多者排前面
        self.assertEqual(groups[0]["sender"], "张三")

    def test_unknown_senders_merge_into_one_group(self):
        groups = self.core.group_messages_by_sender(sample_messages())
        unknown = [g for g in groups if g["unknown"]]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]["sender"], "未知发送者")
        self.assertEqual(unknown[0]["message_count"], 2)
        self.assertIsNone(unknown[0]["sender_username"])

    def test_date_filter_includes_endpoints(self):
        groups = self.core.group_messages_by_sender(
            sample_messages(),
            date_from="2026-11-11",
            date_to="2026-11-11",
        )
        by_name = {g["sender"]: g["message_count"] for g in groups}
        self.assertEqual(by_name, {"张三": 2, "我": 1})

    def test_date_filter_single_bounds(self):
        groups = self.core.group_messages_by_sender(
            sample_messages(), date_from="2026-11-12"
        )
        self.assertEqual(
            {g["sender"]: g["message_count"] for g in groups},
            {"李四": 1, "未知发送者": 2},
        )
        groups = self.core.group_messages_by_sender(
            sample_messages(), date_to="2026-11-10"
        )
        self.assertEqual(
            {g["sender"]: g["message_count"] for g in groups},
            {"张三": 1, "李四": 1},
        )

    def test_unparseable_time_excluded_only_when_filtering(self):
        msgs = sample_messages() + [
            make_message("未知时间", "张三", "wxid_z3", "没有日期的话")
        ]
        unfiltered = self.core.group_messages_by_sender(msgs)
        self.assertEqual(
            {g["sender"]: g["message_count"] for g in unfiltered}["张三"], 4
        )
        filtered = self.core.group_messages_by_sender(
            msgs, date_from="2026-11-01"
        )
        self.assertEqual(
            {g["sender"]: g["message_count"] for g in filtered}["张三"], 3
        )

    def test_sender_filter_multiple_and_by_username(self):
        groups = self.core.group_messages_by_sender(
            sample_messages(), senders="张三,我"
        )
        self.assertEqual({g["sender"] for g in groups}, {"张三", "我"})
        groups = self.core.group_messages_by_sender(
            sample_messages(), senders=["wxid_l4"]
        )
        self.assertEqual([g["sender"] for g in groups], ["李四"])
        groups = self.core.group_messages_by_sender(
            sample_messages(), senders="未知发送者"
        )
        self.assertEqual([g["sender"] for g in groups], ["未知发送者"])

    def test_sender_and_date_filters_combine(self):
        groups = self.core.group_messages_by_sender(
            sample_messages(),
            senders=["张三", "李四"],
            date_from="2026-11-11",
            date_to="2026-11-12",
        )
        self.assertEqual(
            {g["sender"]: g["message_count"] for g in groups},
            {"张三": 2, "李四": 1},
        )

    def test_reversed_date_range_raises(self):
        with self.assertRaises(ValueError):
            self.core.group_messages_by_sender(
                sample_messages(),
                date_from="2026-11-12",
                date_to="2026-11-11",
            )


class WriteBySenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = load_core()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.chat_dir = Path(temp.name) / "测试群"
        self.chat_dir.mkdir(parents=True)

    def test_writes_per_sender_files_and_index(self):
        result = self.core.write_by_sender_exports(
            self.chat_dir, sample_messages(), True, "测试群"
        )
        by_dir = self.chat_dir / "by_sender"
        self.assertTrue(by_dir.is_dir())
        self.assertEqual(result["sender_total"], 4)
        self.assertEqual(result["message_total"], 8)

        for name in ("张三", "李四", "我", "未知发送者"):
            self.assertTrue((by_dir / f"{name}.md").is_file(), name)
            self.assertTrue((by_dir / f"{name}.txt").is_file(), name)

        index = json.loads(
            (by_dir / "by_sender_index.json").read_text(encoding="utf-8-sig")
        )
        counts = {s["sender"]: s["message_count"] for s in index["senders"]}
        self.assertEqual(counts, {"张三": 3, "李四": 2, "我": 1, "未知发送者": 2})
        zhang = next(s for s in index["senders"] if s["sender"] == "张三")
        self.assertEqual(zhang["first_time"], "2026-11-10 09:00:00")
        self.assertEqual(zhang["last_time"], "2026-11-11 11:00:00")
        self.assertEqual(zhang["sender_username"], "wxid_z3")

        index_md = (by_dir / "by_sender_index.md").read_text(encoding="utf-8")
        self.assertIn("按人分组导出索引", index_md)
        self.assertIn("张三", index_md)

    def test_per_sender_files_split_by_day_and_isolated(self):
        self.core.write_by_sender_exports(
            self.chat_dir, sample_messages(), True, "测试群"
        )
        by_dir = self.chat_dir / "by_sender"
        zhang_md = (by_dir / "张三.md").read_text(encoding="utf-8")
        self.assertIn("## 2026-11-10", zhang_md)
        self.assertIn("## 2026-11-11", zhang_md)
        self.assertIn("张三第一天的话", zhang_md)
        self.assertIn("张三第二天的话", zhang_md)
        self.assertNotIn("李四的话一", zhang_md)
        self.assertNotIn("我说的", zhang_md)

        zhang_txt = (by_dir / "张三.txt").read_text(encoding="utf-8-sig")
        self.assertIn("========== 2026-11-10 ==========", zhang_txt)
        self.assertIn("张三第三条", zhang_txt)
        self.assertNotIn("李四的话二", zhang_txt)

    def test_unsafe_sender_name_is_sanitized(self):
        msgs = [make_message("2026-11-10 09:00:00", "张/三", "wxid_x", "你好")]
        result = self.core.write_by_sender_exports(
            self.chat_dir, msgs, True, "测试群"
        )
        entry = result["senders"][0]
        self.assertNotIn("/", entry["md_file"])
        self.assertTrue((self.chat_dir / "by_sender" / entry["md_file"]).is_file())

    def test_filters_apply_to_written_files(self):
        result = self.core.write_by_sender_exports(
            self.chat_dir,
            sample_messages(),
            True,
            "测试群",
            senders="李四",
            date_from="2026-11-12",
            date_to="2026-11-12",
        )
        self.assertEqual(result["sender_total"], 1)
        self.assertEqual(result["message_total"], 1)
        by_dir = self.chat_dir / "by_sender"
        li_md = (by_dir / "李四.md").read_text(encoding="utf-8")
        self.assertIn("李四的话二", li_md)
        self.assertNotIn("李四的话一", li_md)
        self.assertFalse((by_dir / "张三.md").exists())


class ExportChatImplBySenderTests(unittest.TestCase):
    """通过 _export_chat_impl 全链路验证：不开选项时产物与现状一致。"""

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
            row(1, 10, 9, 0, "wxid_z3", "张三第一天"),
            row(2, 10, 9, 1, "wxid_me", "我回复"),
            row(3, 11, 10, 0, "wxid_z3", "张三第二天"),
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

    def test_default_export_has_no_by_sender(self):
        result = self._run_impl("plain")
        chat_dir = Path(result["output_dir"])
        self.assertIsNone(result["by_sender"])
        self.assertFalse((chat_dir / "by_sender").exists())
        self.assertEqual(
            sorted(p.name for p in chat_dir.iterdir()),
            ["chat_full_for_llm.md", "chat_full_for_llm.txt",
             "chat_full_parsed.json"],
        )

    def test_by_sender_keeps_main_files_byte_identical(self):
        plain = self._run_impl("plain")
        grouped = self._run_impl("grouped", by_sender=True)
        for key in ("txt", "md", "json"):
            self.assertEqual(
                Path(plain[key]).read_bytes(),
                Path(grouped[key]).read_bytes(),
                key,
            )
        info = grouped["by_sender"]
        self.assertIsNotNone(info)
        self.assertEqual(info["sender_total"], 2)
        self.assertEqual(info["message_total"], 3)
        by_dir = Path(info["dir"])
        self.assertTrue((by_dir / "张三.md").is_file())
        self.assertTrue((by_dir / "我.md").is_file())
        zhang = (by_dir / "张三.md").read_text(encoding="utf-8")
        self.assertIn("## 2026-11-10", zhang)
        self.assertIn("## 2026-11-11", zhang)


class CliBySenderTests(unittest.TestCase):
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

    def test_plain_name_defaults_by_sender_off(self):
        with patch.object(
            self.core, "export_chat", return_value=self._fake_result()
        ) as single:
            code, _out, _err = self._run_cli(["cli.py", "测试群"])
        self.assertEqual(code, 0)
        kwargs = single.call_args.kwargs
        self.assertFalse(kwargs["by_sender"])
        self.assertEqual(kwargs["sender_filter"], [])
        self.assertIsNone(kwargs["date_from"])
        self.assertIsNone(kwargs["date_to"])

    def test_sender_repeat_and_comma_forms(self):
        with patch.object(
            self.core, "export_chat", return_value=self._fake_result()
        ) as single:
            code, _out, _err = self._run_cli(
                ["cli.py", "测试群", "--sender", "张三,李四",
                 "--sender", "王五"]
            )
        self.assertEqual(code, 0)
        kwargs = single.call_args.kwargs
        self.assertTrue(kwargs["by_sender"])
        self.assertEqual(kwargs["sender_filter"], ["张三", "李四", "王五"])

    def test_date_range_auto_enables_by_sender(self):
        with patch.object(
            self.core, "export_chat", return_value=self._fake_result()
        ) as single:
            code, _out, _err = self._run_cli(
                ["cli.py", "测试群", "--from", "2026-11-01",
                 "--to", "2026-11-30"]
            )
        self.assertEqual(code, 0)
        kwargs = single.call_args.kwargs
        self.assertTrue(kwargs["by_sender"])
        self.assertEqual(kwargs["date_from"], "2026-11-01")
        self.assertEqual(kwargs["date_to"], "2026-11-30")

    def test_invalid_date_is_cli_error(self):
        code, _out, err = self._run_cli(
            ["cli.py", "测试群", "--from", "2026-13-01"]
        )
        self.assertEqual(code, 2)
        self.assertIn("--from", err)


if __name__ == "__main__":
    unittest.main()
