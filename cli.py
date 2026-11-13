# -*- coding: utf-8 -*-
import argparse
import sys

from exporter_core import (
    clear_sensitive_cache,
    export_all_chats,
    export_chat,
    export_chats,
    install_local_asr_model,
    install_rust_silk,
    list_group_chats,
    parse_export_date,
    parse_name_list,
    parse_sender_list,
)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="微信聊天信息导出给大模型")
    p.add_argument(
        "name",
        nargs="?",
        help="好友备注/昵称或群名；多个可用 ; 或 ； 分隔，一次导出",
    )
    p.add_argument(
        "--list-groups",
        action="store_true",
        help="列出本机可可靠识别的群聊名称（每行一个）后退出，便于抄写群名",
    )
    p.add_argument("--out", default="exports")
    p.add_argument(
        "--images",
        action="store_true",
        help="同时导出本机仍有缓存的聊天图片",
    )
    p.add_argument(
        "--files",
        action="store_true",
        help="同时导出本机仍有缓存的聊天文件",
    )
    p.add_argument("--voices", action="store_true", help="同时导出语音；有解码器时生成 WAV")
    p.add_argument("--videos", action="store_true", help="同时导出本机仍有缓存的视频")
    p.add_argument(
        "--transcribe-voices",
        action="store_true",
        help="用本地 SenseVoice 将导出的语音转成文字；会自动开启语音导出",
    )
    p.add_argument(
        "--media",
        action="store_true",
        help="同时开启图片、文件、语音和视频导出",
    )
    p.add_argument(
        "--all",
        dest="export_all",
        action="store_true",
        help=(
            "导出本机所有能够从本地消息表可靠识别的私聊和群聊，"
            "并生成 all_chats_index.json / all_chats_index.md 总索引"
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="配合 --all 使用：只导出最近的 N 个会话，便于先小范围试跑",
    )
    p.add_argument(
        "--install-asr",
        action="store_true",
        help="下载安装本地 SenseVoice 语音识别模型后退出",
    )
    p.add_argument(
        "--install-rust-silk",
        action="store_true",
        help="下载安装 rust-silk 语音解码器后退出",
    )
    p.add_argument(
        "--clear-sensitive-cache",
        action="store_true",
        help=(
            "清除当前版本临时敏感缓存，以及 v1.3.3 及更早版本 / "
            "wechatauto-replica 默认留下的密钥和解密数据库缓存后退出"
        ),
    )
    p.add_argument(
        "--db-dir",
        default=None,
        help=(
            "微信数据目录（微信「设置 → 文件管理」里显示的那个）。"
            "默认自动探测；数据目录被自行迁移过、或自动探测读到错误副本时指定。"
        ),
    )
    p.add_argument(
        "--by-sender",
        action="store_true",
        help=(
            "在每个会话目录下额外生成 by_sender/ 按人分组导出"
            "（每人一份 Markdown + TXT，文件内按自然日分段，"
            "另有 by_sender_index.json / .md 索引）；不改变原导出文件"
        ),
    )
    p.add_argument(
        "--sender",
        action="append",
        default=None,
        metavar="人名",
        help=(
            "只看这些人：可重复使用，也可用逗号分隔多个人名"
            "（按导出中显示的发送者名或账号匹配）；"
            "指定后自动开启 --by-sender"
        ),
    )
    p.add_argument(
        "--from",
        dest="date_from",
        default=None,
        metavar="YYYY-MM-DD",
        help="按人分组只导出该日期（含）之后的消息；指定后自动开启 --by-sender",
    )
    p.add_argument(
        "--to",
        dest="date_to",
        default=None,
        metavar="YYYY-MM-DD",
        help="按人分组只导出该日期（含）之前的消息；指定后自动开启 --by-sender",
    )
    a = p.parse_args()

    if a.limit is not None and a.limit <= 0:
        p.error("--limit 必须是正整数。")
    if a.limit is not None and not a.export_all:
        p.error("--limit 只能配合 --all 使用。")

    sender_filter = parse_sender_list(a.sender)
    try:
        date_from = parse_export_date(a.date_from, "--from 日期")
        date_to = parse_export_date(a.date_to, "--to 日期")
    except ValueError as exc:
        p.error(str(exc))
    if date_from and date_to and date_from > date_to:
        p.error(f"--from {date_from} 晚于 --to {date_to}。")
    by_sender = bool(
        a.by_sender or sender_filter or date_from or date_to
    )

    if a.install_asr:
        install_local_asr_model(progress=print)
        raise SystemExit(0)
    if a.install_rust_silk:
        install_rust_silk(progress=print)
        raise SystemExit(0)
    if a.clear_sensitive_cache:
        report = clear_sensitive_cache()
        for path in report.get("removed") or []:
            print("已清除：" + path)
        if not report.get("removed"):
            print("没有发现需要清理的敏感缓存。")
        failed = report.get("failed") or []
        if failed:
            for item in failed:
                print(
                    "清理失败："
                    + str(item.get("path"))
                    + "："
                    + str(item.get("error"))
                )
            raise SystemExit(1)
        raise SystemExit(0)
    if a.list_groups:
        if a.name or a.export_all:
            print("注意：已指定 --list-groups，忽略其他导出参数。")
        groups = list_group_chats(
            db_dir=a.db_dir,
            progress=lambda m: print(m, file=sys.stderr),
        )
        for group in groups:
            print(group["name"])
        print(f"共 {len(groups)} 个可可靠识别的群聊。", file=sys.stderr)
        raise SystemExit(0)
    if a.export_all:
        if a.name:
            print(f"注意：已指定 --all，忽略会话关键词“{a.name}”。")
        result = export_all_chats(
            a.out,
            progress=print,
            export_images=(a.images or a.media),
            export_files=(a.files or a.media),
            export_voices=(a.voices or a.media or a.transcribe_voices),
            export_videos=(a.videos or a.media),
            transcribe_voices=a.transcribe_voices,
            db_dir=a.db_dir,
            limit=a.limit,
            by_sender=by_sender,
            sender_filter=sender_filter,
            date_from=date_from,
            date_to=date_to,
        )
        print(
            f"可可靠识别会话 {result['discovered_total']}，"
            f"本次处理 {result['selected_total']}；"
            f"成功 {result['exported']}，无消息 {result['skipped_empty']}，"
            f"失败 {result['failed']}，消息总数 {result['total_messages']}。"
        )
        if result.get("unresolved_message_tables"):
            print(
                f"另有 {result['unresolved_message_tables']} 个消息表无法可靠反查身份，"
                "已跳过而不是猜测。"
            )
        print("输出目录：" + result["output_dir"])
        print("总索引：" + result["index_md"])
        if not result["sensitive_cache_cleanup"]:
            print(
                "敏感临时缓存自动清理失败："
                + str(result.get("sensitive_cache_cleanup_error"))
            )
            raise SystemExit(1)
        raise SystemExit(1 if result["failed"] else 0)

    if not a.name:
        p.error(
            "请提供好友备注/昵称或群名（多个可用 ; 分隔）；"
            "或用 --all 导出全部聊天；或用 --list-groups 列出群聊；"
            "或使用 --install-asr / --install-rust-silk / "
            "--clear-sensitive-cache"
        )

    names = parse_name_list(a.name)
    if len(names) > 1:
        result = export_chats(
            names,
            a.out,
            progress=print,
            export_images=(a.images or a.media),
            export_files=(a.files or a.media),
            export_voices=(a.voices or a.media or a.transcribe_voices),
            export_videos=(a.videos or a.media),
            transcribe_voices=a.transcribe_voices,
            db_dir=a.db_dir,
            by_sender=by_sender,
            sender_filter=sender_filter,
            date_from=date_from,
            date_to=date_to,
        )
        print(
            f"本次处理 {result['selected_total']}；"
            f"成功 {result['exported']}，无消息 {result['skipped_empty']}，"
            f"失败 {result['failed']}，重复跳过 {result['duplicates']}，"
            f"消息总数 {result['total_messages']}。"
        )
        print("输出目录：" + result["output_dir"])
        print("总索引：" + result["index_md"])
        if not result["sensitive_cache_cleanup"]:
            print(
                "敏感临时缓存自动清理失败："
                + str(result.get("sensitive_cache_cleanup_error"))
            )
            raise SystemExit(1)
        raise SystemExit(1 if result["failed"] else 0)

    result = export_chat(
        names[0],
        a.out,
        progress=print,
        export_images=(a.images or a.media),
        export_files=(a.files or a.media),
        export_voices=(a.voices or a.media or a.transcribe_voices),
        export_videos=(a.videos or a.media),
        transcribe_voices=a.transcribe_voices,
        db_dir=a.db_dir,
        by_sender=by_sender,
        sender_filter=sender_filter,
        date_from=date_from,
        date_to=date_to,
    )
    print(result)
