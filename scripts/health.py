#!/usr/bin/env python3
"""Local CLI for health export analysis."""

import argparse
import csv
import io
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from xml.parsers.expat import ExpatError
from zipfile import BadZipFile

from adapters import FORMATS
from analysis import analyze, render_report
from brief import DEFAULT_METRICS, prepare_brief
from catalog import GROUPS, METRICS
from memory import load, memory_lock, review_context, run_memory
from privacy import MEMORY_FILE, purge, scan_cache, scan_memory
from storage import HealthError, import_export, migrate_cache, open_cache, snapshot_info
from workbench import load_profile, prepare_workbench, write_workbench


DEFAULT_DB = Path.home() / ".health-advisor" / "health.sqlite3"
LEGACY_DB = Path.home() / ".apple-health-advisor" / "health.sqlite3"


def resolve_default_db():
    if not DEFAULT_DB.exists() and LEGACY_DB.is_file():
        home = Path.home()
        old_location = f"~/{LEGACY_DB.relative_to(home).as_posix()}" if LEGACY_DB.is_relative_to(home) else str(LEGACY_DB)
        sys.stderr.write(f"正在使用旧位置缓存 {old_location}；可用 --db 指定新位置\n")
        return LEGACY_DB
    return DEFAULT_DB


def iso_date(value):
    try:
        day = date.fromisoformat(value)
        if day.isoformat() == value:
            return day
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("日期格式为 YYYY-MM-DD。")


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必须为正整数。")
    return number


def add_memory_parser(commands):
    memory = commands.add_parser("memory", help="管理本地复盘记忆")
    memory.add_argument("--memory", type=Path, metavar="PATH")
    actions = memory.add_subparsers(dest="memory_command", required=True)
    for action in ("init", "show", "export"):
        actions.add_parser(action)
    setting = actions.add_parser("set")
    setting.add_argument("kind", choices=("goal", "constraint", "device"))
    setting.add_argument("text")
    setting.add_argument("--id", help="覆盖此 ID 的现有条目")
    review = actions.add_parser("review")
    review.add_argument("one_line")
    review.add_argument("--recheck-on", type=iso_date, required=True)
    review.add_argument("--id", help="覆盖此 ID 的现有复盘")
    forget = actions.add_parser("forget")
    target = forget.add_mutually_exclusive_group(required=True)
    target.add_argument("id", nargs="?")
    target.add_argument("--all", action="store_true")
    forget.add_argument("--yes", action="store_true", help="确认删除选定记忆")


def command_parser():
    parser = argparse.ArgumentParser(description="本地导入和分析健康数据。")
    parser.add_argument("--db", type=Path, help="本地 SQLite 缓存路径")
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import", help="导入本地健康导出，输出覆盖清单")
    importer.add_argument("export", type=Path, help="本地导出文件")
    importer.add_argument("--format", choices=("auto", *FORMATS), default="auto")
    importer.add_argument(
        "--merge", action="store_true",
        help="把新导出的记录合并进已有缓存，不清空历史；无缓存时等同全量导入",
    )
    commands.add_parser("migrate", help="把 schema 2、3 或 4 缓存原地升级到 schema 5")
    status = commands.add_parser("status", help="查看已导入的数据、来源和覆盖范围")
    status.add_argument("--show-source-names", action="store_true", help="在 name 字段显示脱敏来源名称")
    archive = commands.add_parser("archive", help="查看 ECG 记录与运动路线档案")
    archive.add_argument("--detail", action="store_true", help="列出逐条 ECG 与路线记录")
    query = commands.add_parser("query", help="查询所选指标并输出 JSON 或每日 CSV")
    query.add_argument("--daily", action="store_true", help="在 JSON 里包含本期逐日聚合")
    query.add_argument("--format", choices=("json", "csv"), default="json")
    report = commands.add_parser("report", help="生成 Markdown 健康报告")
    report.add_argument("--output", type=Path, help="将 UTF-8 报告写入一个新文件")
    brief = commands.add_parser("brief", help="提取供 AI 撰写护理和运动报告的聚合数据")
    privacy = commands.add_parser("privacy")
    privacy.add_argument("--json", action="store_true")
    cleanup = commands.add_parser("purge", help="预览或清理本工具生成的本地数据")
    cleanup.add_argument("--scope", choices=("cache", "memory", "all"), required=True)
    action = cleanup.add_mutually_exclusive_group()
    action.add_argument("--dry-run", dest="execute", action="store_false", help="仅列出清单与大小，默认")
    action.add_argument("--yes", dest="execute", action="store_true", help="执行清理")
    cleanup.set_defaults(execute=False)
    for command in (brief, report):
        command.add_argument("--with-identifiers", action="store_true", help="附加区间来源计数，仍为聚合输出")
    workbench = commands.add_parser(
        "workbench", help="生成可离线试算的 HTML 健康工作台",
        description="趋势默认截至缓存最新日。所选窗口至少 7 天时，评分取截至该末日的最近 7 个已结束的本地日。",
    )
    workbench.add_argument("--output", type=Path, metavar="PATH.html", help="必填，写入一个新的 HTML 文件")
    workbench.add_argument("--profile", type=Path, metavar="PATH.json", help="可选的 UTF-8 JSON 补充资料")
    add_memory_parser(commands)
    for command in (brief, report, workbench):
        memory = command.add_mutually_exclusive_group()
        memory.add_argument("--no-memory", action="store_true", help="跳过本地复盘记忆")
        memory.add_argument("--memory", type=Path, help="使用指定的记忆 JSON")
    for command in (query, report, brief, workbench):
        selection = command.add_mutually_exclusive_group()
        selection.add_argument("--metric", action="append", choices=METRICS, help="指定指标，可重复")
        selection.add_argument("--group", action="append", choices=GROUPS, help="指定指标组，可重复")
        window = command.add_mutually_exclusive_group()
        window.add_argument("--from", dest="first", type=iso_date, help="本期起日，含当日")
        window.add_argument("--days", type=positive, default=7, help="本期窗口天数，默认 7 天")
        command.add_argument("--to", dest="last", type=iso_date, help="本期末日，默认取最新索引日")
        baseline = command.add_mutually_exclusive_group()
        default_baseline = None if command in (brief, workbench) else 90
        baseline.add_argument("--baseline-days", type=positive, default=default_baseline,
                              help="基线窗口天数。brief 和 workbench 默认用上一等长时段，其余默认 90 天")
        baseline.add_argument("--compare-previous", action="store_const", const=None, dest="baseline_days",
                              help="改为比较紧邻本期的同等天数")
        command.add_argument("--prefer-source", type=positive, action="append", default=[],
                             help="优先采用的来源 ID，可按优先顺序重复")
    return parser


def daily_csv(result):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["metric", "date", "value", "unit", "records_used", "source_ids", "overlapping_records_skipped"])
    for key, item in result["metrics"].items():
        for day in item["daily"]:
            source_ids = "|".join(str(source) for source in day["source_ids"])
            writer.writerow([key, day["date"], day["value"], item["unit"], day["records_used"], source_ids,
                             day["overlapping_records_skipped"]])
    return output.getvalue()


def describe_json(data):
    return json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + "\n"


def run_privacy(args):
    if args.db.exists():
        with open_cache(args.db) as connection:
            checks = scan_cache(connection, args.db)
    else:
        checks = [{"check": "cache", "status": "warn", "detail": "未创建缓存。", "fix": ""},
                  scan_memory(args.db.parent / MEMORY_FILE)]
    if args.json:
        output = describe_json(checks)
    else:
        output = "".join(f"{item['status']} {item['check']} {item['detail']} {item['fix']}\n" for item in checks)
    sys.stdout.write(output)
    return int(any(item["status"] == "fail" for item in checks))


def run(args):
    memory_path = args.db.parent / MEMORY_FILE
    if args.command == "memory":
        return run_memory(args, args.memory or memory_path)
    if args.command == "purge":
        if args.execute and args.scope in ("memory", "all") and memory_path.parent.is_dir():
            with memory_lock(memory_path):
                return purge(args.db, args.scope, True)
        return purge(args.db, args.scope, args.execute)
    if args.command == "import":
        return describe_json(import_export(args.export, args.db, merge=args.merge, format_name=args.format))
    if args.command == "migrate":
        return describe_json(migrate_cache(args.db))
    is_workbench = args.command == "workbench"
    if is_workbench:
        if args.output is None or args.output.suffix.lower() != ".html":
            raise HealthError("workbench 必须提供 --output PATH.html。请指定一个新的 HTML 文件名。")
        if args.output.exists():
            raise FileExistsError(args.output)
        profile = load_profile(args.profile)
    context = None
    if args.command in ("brief", "report", "workbench") and not args.no_memory:
        context = review_context(load(args.memory or memory_path, missing_ok=args.memory is None))
    with open_cache(args.db) as connection:
        if args.command == "status":
            return describe_json(snapshot_info(connection, args.show_source_names))
        if args.command == "archive":
            from archive import archive_summary
            return describe_json(archive_summary(connection, args.detail))
        if args.metric:
            metrics = list(dict.fromkeys(args.metric))
        elif args.group:
            metrics = [key for key, metric in METRICS.items() if metric.group in args.group]
        else:
            metrics = DEFAULT_METRICS if args.command in ("brief", "workbench") else METRICS
        if is_workbench:
            html = prepare_workbench(
                connection, metrics, args.first, args.last, args.days, args.baseline_days, args.prefer_source, profile,
                memory=context,
            )
            write_workbench(args.output, html)
            return f"已写入 HTML 工作台 {args.output.resolve()}\n"
        is_query = args.command == "query"
        include_daily = is_query and (args.daily or args.format == "csv")
        result = analyze(
            connection, metrics, args.first, args.last, args.days, args.baseline_days, args.prefer_source, include_daily,
        )
        if args.command in ("brief", "report"):
            from archive import archive_window

            period = result["report_period"]
            archive = archive_window(connection, period["from"], period["to"])
        if args.command == "brief":
            latest = connection.execute("SELECT MAX(last_day) FROM metric_coverage").fetchone()[0]
            return describe_json(prepare_brief(result, latest, args.with_identifiers, memory=context, archive=archive))
    if args.command == "report":
        markdown = render_report(result, include_empty=bool(args.metric or args.group), archive=archive,
                                 with_identifiers=args.with_identifiers, memory=context)
        if args.output is not None:
            with args.output.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(markdown)
            return "已写入 Markdown 报告。\n"
        return markdown
    return daily_csv(result) if args.format == "csv" else describe_json(result)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    args = command_parser().parse_args()
    try:
        if args.db is None:
            args.db = resolve_default_db()
        if args.command == "privacy":
            return run_privacy(args)
        sys.stdout.write(run(args))
    except HealthError as error:
        sys.stderr.write(f"{error}\n")
    except BadZipFile:
        sys.stderr.write("无法读取 ZIP。请使用 Apple 健康完整导出的压缩包。\n")
    except ExpatError as error:
        sys.stderr.write(f"导出 XML 损坏或不完整（第 {error.lineno} 行）。请重新导出。\n")
    except FileExistsError:
        sys.stderr.write("输出文件已存在。请指定一个新文件名。\n")
    except FileNotFoundError:
        sys.stderr.write("找不到 ZIP、缓存或输出目录。请检查路径。\n")
    except PermissionError:
        sys.stderr.write("无法读写指定文件。请检查权限，或关闭正在占用缓存的程序后重试。\n")
    except (ValueError, OverflowError):
        sys.stderr.write("导出或日期参数超出可处理的范围。\n")
    except (OSError, sqlite3.Error, RuntimeError):
        sys.stderr.write("无法读取或建立缓存。请检查导出文件、缓存路径和可用空间。\n")
    else:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
