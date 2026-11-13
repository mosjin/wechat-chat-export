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
            "_multi_exporter_core",
            ROOT / "exporter_core.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class ParseNameListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = load_core()

    def test_splits_on_ascii_semicolon(self):
        self.assertEqual(
            self.core.parse_name_list("群A;群B"),
            ["群A", "群B"],
        )

    def test_splits_on_chinese_semicolon(self):
        self.assertEqual(
            self.core.parse_name_list("群A；群B"),
            ["群A", "群B"],
        )

    def test_mixed_separators_strip_dedup_and_drop_empties(self):
        self.assertEqual(
            self.core.parse_name_list(" 群A ;； 群B ;群A;; "),
            ["群A", "群B"],
        )

    def test_single_name_unchanged(self):
        self.assertEqual(self.core.parse_name_list("群A"), ["群A"])

    def test_empty_inputs(self):
        self.assertEqual(self.core.parse_name_list(""), [])
        self.assertEqual(self.core.parse_name_list(None), [])
        self.assertEqual(self.core.parse_name_list(";;；"), [])

    def test_iterable_input_is_split_per_item(self):
        self.assertEqual(
            self.core.parse_name_list(["群A;群B", "群C", "群A"]),
            ["群A", "群B", "群C"],
        )


class MultiExportImplTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = load_core()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.out = Path(temp.name) / "out"

    def _success_result(self, contact, folder, count=3):
        target = self.out / folder
        return {
            "chat_name": contact["name"],
            "is_group": contact["username"].endswith("@chatroom"),
            "message_count": count,
            "sender_resolution_counts": {"resolved": count},
            "media_stats": {"images_requested": 0, "images_exported": 0},
            "output_dir": str(target),
            "txt": str(target / "chat_full_for_llm.txt"),
            "md": str(target / "chat_full_for_llm.md"),
            "json": str(target / "chat_full_parsed.json"),
        }

    def _run_batch(self, names):
        contacts = {
            "群A": {"username": "111@chatroom", "nick_name": "群A"},
            "群A别名": {"username": "111@chatroom", "nick_name": "群A"},
            "空群": {"username": "222@chatroom", "nick_name": "空群"},
            "炸群": {"username": "333@chatroom", "nick_name": "炸群"},
        }

        def fake_find(db, keyword):
            if keyword not in contacts:
                raise ValueError(f"没有找到联系人或群聊：{keyword}")
            return contacts[keyword]

        seen = []

        def fake_impl(**kwargs):
            seen.append(kwargs)
            username = kwargs["contact"]["username"]
            if username == "222@chatroom":
                raise self.core.EmptyConversationError("没有读取到该会话的消息。")
            if username == "333@chatroom":
                raise RuntimeError("synthetic failure")
            return self._success_result(kwargs["contact"], kwargs["folder_name"])

        fake_db = object()
        with (
            patch.object(self.core, "WeChatDB", return_value=fake_db) as ctor,
            patch.object(self.core, "find_contact", side_effect=fake_find),
            patch.object(self.core, "_export_chat_impl", side_effect=fake_impl),
        ):
            result = self.core._export_chats_impl(names, out_root=self.out)
        return result, seen, ctor

    def test_batch_isolates_failures_and_writes_multi_index(self):
        result, seen, ctor = self._run_batch(
            ["群A", "空群", "炸群", "不存在", "群A别名"]
        )

        ctor.assert_called_once()
        # 未找到与重复指向同一会话的输入不会调用单会话导出；
        # 其余三个输入都进过 impl，且整批共用同一个 db 与 index_cache。
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(k["db"] is seen[0]["db"] for k in seen))
        self.assertTrue(
            all(k["index_cache"] is seen[0]["index_cache"] for k in seen)
        )

        self.assertEqual(result["selected_total"], 5)
        self.assertEqual(result["exported"], 1)
        self.assertEqual(result["skipped_empty"], 1)
        self.assertEqual(result["failed"], 2)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(result["total_messages"], 3)

        data = json.loads(
            Path(result["index_json"]).read_text(encoding="utf-8-sig")
        )
        self.assertEqual(
            {item["input_name"]: item["status"] for item in data["chats"]},
            {
                "群A": "exported",
                "空群": "skipped_empty",
                "炸群": "failed",
                "不存在": "failed",
                "群A别名": "duplicate",
            },
        )
        md = Path(result["index_md"]).read_text(encoding="utf-8")
        self.assertIn("多会话导出索引", md)
        self.assertIn("炸群", md)
        self.assertIn("失败", md)

    def test_string_input_is_parsed(self):
        result, _seen, _ctor = self._run_batch("群A;空群")
        self.assertEqual(result["selected_total"], 2)
        self.assertEqual(result["exported"], 1)
        self.assertEqual(result["skipped_empty"], 1)

    def test_empty_names_raise(self):
        with self.assertRaisesRegex(ValueError, "没有提供"):
            self.core._export_chats_impl(" ; ； ", out_root=self.out)


class GroupFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = load_core()

    def test_only_groups_kept_and_order_preserved(self):
        chats = [
            {"username": "222@chatroom", "name": "新群", "is_group": True},
            {"username": "wxid_friend", "name": "好友", "is_group": False},
            {"username": "111@chatroom", "name": "旧群", "is_group": True},
        ]
        groups = self.core.group_chats_from_candidates(chats)
        self.assertEqual(
            [g["username"] for g in groups],
            ["222@chatroom", "111@chatroom"],
        )


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = load_core()

    def _run_cli(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.dict(sys.modules, {"exporter_core": self.core}),
            patch.object(sys, "argv", argv),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            with self.assertRaises(SystemExit) as caught:
                runpy.run_path(str(ROOT / "cli.py"), run_name="__main__")
        return caught.exception.code, stdout.getvalue(), stderr.getvalue()

    def test_list_groups_prints_only_group_names(self):
        groups = [
            {"name": "项目群", "username": "111@chatroom",
             "last_time": 200, "last_time_text": "2026-01-01 10:00:00"},
            {"name": "家庭群", "username": "222@chatroom",
             "last_time": 100, "last_time_text": "2026-01-01 09:00:00"},
        ]
        with patch.object(
            self.core, "list_group_chats", return_value=groups
        ) as listing:
            code, stdout, _stderr = self._run_cli(["cli.py", "--list-groups"])
        listing.assert_called_once()
        self.assertEqual(code, 0)
        self.assertEqual(stdout.strip().splitlines(), ["项目群", "家庭群"])

    def test_semicolon_names_take_multi_path(self):
        summary = {
            "selected_total": 2, "exported": 2, "skipped_empty": 0,
            "failed": 0, "duplicates": 0, "total_messages": 5,
            "output_dir": "/tmp/out", "index_md": "/tmp/out/multi_index.md",
            "sensitive_cache_cleanup": True,
            "sensitive_cache_cleanup_error": None,
        }
        with (
            patch.object(self.core, "export_chats", return_value=summary) as multi,
            patch.object(
                self.core, "export_chat",
                side_effect=AssertionError("single path must not run"),
            ),
        ):
            code, _stdout, _stderr = self._run_cli(["cli.py", "群A;群B"])
        self.assertEqual(code, 0)
        self.assertEqual(multi.call_args.args[0], ["群A", "群B"])

    def test_multi_path_exit_code_1_when_any_failed(self):
        summary = {
            "selected_total": 2, "exported": 1, "skipped_empty": 0,
            "failed": 1, "duplicates": 0, "total_messages": 5,
            "output_dir": "/tmp/out", "index_md": "/tmp/out/multi_index.md",
            "sensitive_cache_cleanup": True,
            "sensitive_cache_cleanup_error": None,
        }
        with patch.object(self.core, "export_chats", return_value=summary):
            code, _stdout, _stderr = self._run_cli(["cli.py", "群A;群B"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
