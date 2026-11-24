"""Compute reproducible summaries and render a local wellness report."""

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from html import escape
from itertools import groupby
from json import dumps
from math import inf, nextafter
from re import sub
from statistics import median

from brief import aggregate_privacy, source_types, workout_evidence
from catalog import GROUPS, METRICS
from insights import STAGES, clock_label, night_sleep, summarize_sleep, summarize_workouts
from memory import next_review_note
from storage import HealthError, cache_version, read_sources


CORE_METRICS = ("steps", "sleep", "resting_hr")
SOURCE_RANK = {"watch": 0, "garmin": 0, "huawei": 0, "phone": 1, "other": 2}
ONE_DAY = timedelta(days=1)


@dataclass
class DailyValue:
    value: float = 0.0
    records: int = 0
    sources: set = field(default_factory=set)
    offsets: set = field(default_factory=set)
    sleep: dict | None = None
    workouts: dict = field(default_factory=dict)

    def observe(self, sample):
        self.records += 1
        self.sources.add(sample["source_id"])
        self.offsets.update((sample["start_offset"], sample["end_offset"]))

    def add_total(self, sample, is_workout):
        if is_workout:
            event = (sample["source_id"], sample["start_utc"], sample["end_utc"], sample["value"])
            if event in self.workouts:
                if sample["state"]:
                    self.workouts[event].add(sample["state"])
                return
            self.workouts[event] = {sample["state"]} if sample["state"] else set()
        self.value += sample["value"]
        self.observe(sample)


def merge_ranges(ranges):
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def interval(sample):
    start = sample["start_utc"]
    # Treat an instantaneous cumulative sample as a point for overlap purposes.
    return start, max(sample["end_utc"], nextafter(start, inf))


def daily_totals(connection, metric, first, last, rank):
    bounds = connection.execute(
        "SELECT MIN(start_utc), MAX(end_utc) FROM samples WHERE metric = ? AND day BETWEEN ? AND ?",
        (metric, first, last),
    ).fetchone()
    if bounds[0] is None:
        return {}, {}
    # A sample dated just outside the window may still overlap across midnight.
    sources = connection.execute(
        "SELECT DISTINCT source_id FROM samples WHERE metric = ?", (metric,),
    )
    daily = defaultdict(DailyValue)
    discarded = defaultdict(int)
    is_workout = metric == "workout_minutes"
    covered = []
    for source in sorted((row["source_id"] for row in sources), key=rank):
        ends = [end for _, end in covered]
        new_coverage = []
        samples = connection.execute(
            """
            SELECT * FROM samples
            WHERE metric = ? AND source_id = ? AND end_utc >= ? AND start_utc <= ?
            ORDER BY start_utc, end_utc
            """,
            (metric, source, *bounds),
        )
        for sample in samples:
            start, end = interval(sample)
            # All higher-priority intervals can suppress a lower-priority record.
            # This keeps the decision independent of the surrounding query dates.
            if new_coverage and start <= new_coverage[-1][1]:
                left, right = new_coverage[-1]
                new_coverage[-1] = (left, max(right, end))
            else:
                new_coverage.append((start, end))
            candidate = bisect_right(ends, start)
            overlaps = candidate < len(covered) and covered[candidate][0] < end
            day = sample["day"]
            if overlaps:
                if first <= day <= last:
                    discarded[day] += 1
                continue
            if first <= day <= last:
                daily[day].add_total(sample, is_workout)
        covered = merge_ranges(covered + new_coverage)
    return daily, discarded


def daily_measurements(connection, metric, first, last, rank, sleep):
    rows = connection.execute(
        "SELECT * FROM samples WHERE metric = ? AND day BETWEEN ? AND ? ORDER BY day, source_id",
        (metric, first, last),
    )
    daily = {}
    for day, day_rows in groupby(rows, lambda row: row["day"]):
        selected = []
        value = None
        sleep_detail = None
        for source, items in groupby(day_rows, lambda row: row["source_id"]):
            if not selected or rank(source) < rank(selected[0]["source_id"]):
                candidates = list(items)
                if sleep:
                    detail = night_sleep(candidates)
                    if detail is None:
                        continue
                    sleep_detail = detail
                    value = detail["sleep_minutes"] / 60
                else:
                    value = median(row["value"] for row in candidates)
                selected = candidates
        if value is None:
            continue
        item = DailyValue(value, sleep=sleep_detail)
        for sample in selected:
            item.observe(sample)
        daily[day] = item
    return daily


def summarize(daily, discarded, first, last, additive, labels):
    days = sorted(day for day in daily if first <= day <= last)
    covered = [daily[day] for day in days]
    values = [item.value for item in covered]
    sources = sorted({source for item in covered for source in item.sources})
    offsets = sorted({offset for item in covered for offset in item.offsets})
    return {
        "covered_days": len(values),
        "calendar_days": (date.fromisoformat(last) - date.fromisoformat(first)).days + 1,
        "first_observation": days[0] if days else None,
        "last_observation": days[-1] if days else None,
        "records_used": sum(item.records for item in covered),
        "median": median(values) if values else None,
        "period_total": sum(values) if values and additive else None,
        "sources": [{"id": source, "label": labels[source]} for source in sources],
        "utc_offsets_minutes": offsets,
        "overlapping_records_skipped": sum(count for day, count in discarded.items() if first <= day <= last),
    }


def required_days(summary):
    days = summary["calendar_days"]
    return min(days, max(3, (days + 1) // 2))


def compare(recent, baseline, previous_window=False):
    if recent["covered_days"] == 0:
        return {"state": "no_recent_data", "difference": None, "percent": None}
    baseline_minimum = required_days(baseline) if previous_window else 7
    if recent["covered_days"] < required_days(recent) or baseline["covered_days"] < baseline_minimum:
        return {"state": "insufficient_coverage", "difference": None, "percent": None}
    if recent["sources"] != baseline["sources"]:
        return {"state": "source_changed", "difference": None, "percent": None}
    difference = recent["median"] - baseline["median"]
    percent = difference / baseline["median"] * 100 if baseline["median"] != 0 else None
    return {"state": "comparable", "difference": difference, "percent": percent}


def in_window(daily, first, last):
    return [item for day, item in daily.items() if first <= day <= last]


def format_day(day, item, discarded, include_context=False):
    result = {
        "date": day, "value": item.value, "records_used": item.records,
        "source_ids": sorted(item.sources), "overlapping_records_skipped": discarded.get(day, 0),
    }
    if include_context:
        result["utc_offsets_minutes"] = sorted(item.offsets)
    if item.sleep is not None:
        detail = item.sleep
        result["sleep"] = {
            "stage_minutes": detail["stage_minutes"], "conflict_minutes": detail["conflict_minutes"],
            "main_start": clock_label(detail["main_start_from_noon"]),
            "main_end": clock_label(detail["main_end_from_noon"]),
        }
        if include_context:
            for key in ("main_start_from_noon", "main_end_from_noon"):
                result["sleep"][key] = detail[key]
    if item.workouts:
        result["workouts"] = summarize_workouts([item.workouts])
    return result


def analyze(connection, metrics, start, end, days, baseline_days, preferred, include_daily=False,
            *, include_daily_context=False):
    source_rows = read_sources(connection)
    labels = {source["id"]: source["label"] for source in source_rows}
    preferred_rank = {source: i for i, source in enumerate(dict.fromkeys(preferred))}
    if preferred_rank.keys() - labels.keys():
        raise HealthError("--prefer-source 包含未知 ID。请用 status 查询此缓存的来源。")
    ranks = {
        source["id"]: (
            preferred_rank.get(source["id"], len(preferred_rank)),
            SOURCE_RANK[source["kind"]],
            source["id"],
        )
        for source in source_rows
    }
    if end is None:
        latest = connection.execute("SELECT MAX(last_day) FROM metric_coverage").fetchone()[0]
        if latest is None:
            raise HealthError("缓存里没有可用于报告的记录。请先用 status 查看数据类型和跳过计数。")
        end = date.fromisoformat(latest)
    start = start if start is not None else end - timedelta(days=days - 1)
    if end < start:
        raise HealthError("--to 必须晚于或等于 --from。")
    previous_window = baseline_days is None
    if previous_window:
        baseline_days = (end - start).days + 1
    baseline_start = (start - timedelta(days=baseline_days)).isoformat()
    baseline_end = (start - ONE_DAY).isoformat()
    first, last = start.isoformat(), end.isoformat()
    result = {
        "report_period": {"from": first, "to": last},
        "baseline_period": {"from": baseline_start, "to": baseline_end},
        "comparison_window": "previous_period" if previous_window else "personal_baseline",
        "metrics": {},
    }
    detailed = cache_version(connection) >= 2
    for key in metrics:
        metric = METRICS[key]
        additive = metric.reducer == "total"
        if additive:
            daily, discarded = daily_totals(connection, key, baseline_start, last, ranks.__getitem__)
        else:
            daily = daily_measurements(
                connection, key, baseline_start, last, ranks.__getitem__, metric.reducer == "sleep",
            )
            discarded = {}
        recent = summarize(daily, discarded, first, last, additive, labels)
        baseline = summarize(daily, discarded, baseline_start, baseline_end, additive, labels)
        item = {
            "label": metric.label, "unit": metric.unit, "group": metric.group, "reducer": metric.reducer,
            "recent": recent, "baseline": baseline, "comparison": compare(recent, baseline, previous_window),
        }
        if key == "sleep":
            item["sleep_details"] = {
                "stage_data_available": detailed,
                "recent": summarize_sleep([day.sleep for day in in_window(daily, first, last)]),
                "baseline": summarize_sleep([day.sleep for day in in_window(daily, baseline_start, baseline_end)]),
            }
        if key == "workout_minutes":
            item["workout_details"] = {
                "type_data_available": detailed,
                "recent": summarize_workouts(day.workouts for day in in_window(daily, first, last)),
                "baseline": summarize_workouts(day.workouts for day in in_window(daily, baseline_start, baseline_end)),
            }
        if include_daily or include_daily_context:
            item["daily"] = [
                format_day(day, value, discarded, include_daily_context)
                for day, value in sorted(daily.items()) if first <= day <= last
            ]
        if include_daily_context:
            item["baseline_daily"] = [
                format_day(day, value, discarded, include_context=True)
                for day, value in sorted(daily.items()) if baseline_start <= day <= baseline_end
            ]
        result["metrics"][key] = item
    return result


def format_number(value, signed=False):
    if value is None:
        return "—"
    if round(value, 2) == 0:
        return "0"
    return format(value, "+,.2f" if signed else ",.2f").rstrip("0").rstrip(".")


def date_range(period):
    return f"{period['from']} 至 {period['to']}"


def sleep_section(item, previous_window):
    data = item["sleep_details"]
    recent = data["recent"]
    nights = item["recent"]["covered_days"]
    if not nights:
        return []
    lines = ["", "### 睡眠与作息", ""]
    if not data["stage_data_available"]:
        lines.append("- 此缓存可查看睡眠时长和作息。重新导入原 ZIP 后可查看有记录的睡眠分期。")
    elif recent["staged_nights"]:
        coverage = format_number(recent["stage_coverage_percent"])
        lines.append(f"- {recent['staged_nights']}/{nights} 夜有分期，覆盖本期记录睡眠时长的 {coverage}%。")
        stage_items = "、".join(
            f"{label} {format_number(recent['stage_minutes'][stage])} 分钟"
            for stage, label in STAGES.items()
            if recent["stage_minutes"][stage] > 0
        )
        lines.append(f"- 各分期合计为 {stage_items}。")
    else:
        lines.append(f"- 本期有 {nights} 夜睡眠时长记录，均为未分期。")
    if recent["conflict_minutes"]:
        lines.append(f"- 未分期中有 {format_number(recent['conflict_minutes'])} 分钟来自分期记录冲突。")
    schedule = recent["schedule"]
    if schedule["nights"]:
        lines.append(f"- 主睡眠记录通常为 {schedule['main_start']} 至 {schedule['main_end']}，"
                     f"来自 {schedule['nights']} 夜的起止时刻中位数。")
        if schedule["start_iqr_minutes"] is not None:
            start_spread = format_number(schedule["start_iqr_minutes"])
            end_spread = format_number(schedule["end_iqr_minutes"])
            lines.append(f"- 中间一半夜晚的起始时刻跨度为 {start_spread} 分钟，结束时刻跨度为 {end_spread} 分钟。")
    if schedule["nights"] < nights:
        lines.append("- 缺少起止时刻或同一夜内时区偏移变化时，只汇总可用的时长和分期。")
    previous = data["baseline"]["schedule"]
    same_clock = len(item["recent"]["utc_offsets_minutes"]) == 1 and (
        item["recent"]["utc_offsets_minutes"] == item["baseline"]["utc_offsets_minutes"]
    )
    if previous_window and item["comparison"]["state"] == "comparable" and same_clock and previous["nights"]:
        lines.append(f"- 上期主睡眠起止中位数为 {previous['main_start']} 至 {previous['main_end']}，"
                     f"来自 {previous['nights']} 夜记录。")
    return lines


def workout_section(item, previous_window):
    data = item["workout_details"]
    recent = data["recent"]
    baseline = data["baseline"]
    if not recent["sessions"] and not (previous_window and baseline["sessions"]):
        return []
    lines = ["", "### 训练复盘", ""]
    if recent["sessions"]:
        minutes = format_number(item["recent"]["period_total"])
        days = item["recent"]["covered_days"]
        lines.append(f"- 本期 {days} 天有训练记录，共 {recent['sessions']} 次、{minutes} 分钟。")
    else:
        lines.append("- 本期未找到训练记录。")
    if previous_window and baseline["sessions"]:
        minutes = format_number(item["baseline"]["period_total"])
        lines.append(f"- 上一等长时段记录 {baseline['sessions']} 次训练，共 {minutes} 分钟。"
                     "记录量会受设备使用与同步影响。")
    if recent["sessions"] and not data["type_data_available"]:
        lines.append("- 此缓存可查看训练次数和时长。重新导入原 ZIP 后可查看运动类型。")
    elif recent["sessions"]:
        lines.extend(["", "| 运动类型 | 次数 | 记录时长（分钟） |", "| --- | ---: | ---: |"])
        for kind in workout_evidence(recent)["by_type"]:
            lines.append(f"| {kind['label']} | {kind['sessions']} | {format_number(kind['minutes'])} |")
    return lines


def archive_limitations(window):
    overall = window["overall"]
    lines = []
    if not window["ecg"]["records"] and overall["ecg_records"]:
        lines.append(f"- 本期无心电图记录。档案共 {overall['ecg_records']} 条，最近日期为 {overall['ecg_last_day']}。")
    if not window["routes"]["records"] and (overall["route_records"] or overall["route_files"]):
        latest = f"，最近日期为 {overall['route_last_day']}" if overall["route_last_day"] else ""
        lines.append(f"- 本期无可汇总的运动路线。档案共 {overall['route_records']} 条可算路线{latest}。"
                     f"最近一次 Apple 导出的附件清单有 {overall['route_files']} 个路线文件。")
    return lines


def archive_section(window):
    ecg, routes = window["ecg"], window["routes"]
    if not (ecg["records"] or routes["records"]):
        return []
    lines = ["", "### 心电图与运动路线", ""]
    if ecg["records"]:
        classes = "、".join(f"{item['label']} {item['count']} 条" for item in ecg["classifications"])
        devices = "、".join(f"{item['label']} {item['count']} 条" for item in ecg["devices"])
        lines.append(f"- 本期有 {ecg['records']} 条心电图记录，设备分类为 {classes}。"
                     f"设备类型为 {devices}。共 {format_number(ecg['samples'])} 个采样点，"
                     f"有症状填写的记录 {ecg['records_with_symptoms']} 条。")
        lines.append("- 设备分类用于回查记录。波形解读与症状评估请咨询医疗专业人员。")
    if routes["records"]:
        lines.append(f"- 本期有 {routes['records']} 条运动路线可算，合计 {format_number(routes['total_distance_km'])} km，"
                     f"爬升 {format_number(routes['total_ascent_m'])} m，最长 {format_number(routes['longest_km'])} km，"
                     f"合计时长 {format_number(routes['total_duration_h'])} 小时。")
    return lines


def render_report(result, include_empty=False, archive=None, *, with_identifiers=False, memory=None):
    metrics = result["metrics"]
    visible = {
        key: item for key, item in metrics.items()
        if include_empty or key in CORE_METRICS or item["recent"]["covered_days"] or item["baseline"]["covered_days"]
    }
    available = {key: item for key, item in visible.items() if item["recent"]["covered_days"]}
    comparisons = [
        item for item in available.values()
        if item["comparison"]["state"] == "comparable" and round(item["comparison"]["difference"], 2)
    ]
    previous_window = result["comparison_window"] == "previous_period"
    window_label = "上一等长时段" if previous_window else "个人基线窗口"
    compare_label = "上期" if previous_window else "基线"
    previous = memory and memory["one_line"]
    privacy = dumps(aggregate_privacy(with_identifiers, includes_memory=bool(previous)), ensure_ascii=False)
    lines = [
        "---", f"privacy: {privacy}", "---", "",
        f"# 个人健康报告 · {date_range(result['report_period'])}",
        "",
        f"{window_label}为 {date_range(result['baseline_period'])}。中位数和合计均使用实际有记录的日或夜。",
        "",
    ]
    if previous:
        summary = sub(r"([\\`*_\[\]~])", r"\\\1", escape(previous, quote=False))
        lines.extend(["## 上次结论与本次核对", "", f"上次结论为「{summary}」。", next_review_note(memory), ""])
    lines.extend(["## 摘要", ""])
    if available:
        groups = "、".join(label for group, label in GROUPS.items() if any(
            item["group"] == group for item in available.values()
        ))
        lines.append(f"本期有 {len(available)} 项指标可用，覆盖{groups}。")
    else:
        lines.append("所选指标在这段时间没有可用记录。")
    if comparisons:
        first = comparisons[0]
        latest = format_number(first["recent"]["median"])
        historical = format_number(first["baseline"]["median"])
        lines.append(f"本期{first['label']}中位数为 {latest} {first['unit']}，"
                     f"{compare_label}为 {historical} {first['unit']}。")
    lines.extend([
        "",
        "## 关键指标",
        "",
        f"| 指标 | {compare_label}中位数 | 本期中位数 | 本期减{compare_label} | 本期合计 | "
        f"覆盖天数（本期 / {compare_label}） | 本期来源 |",
        "| --- | ---: | ---: | ---: | ---: | --- | --- |",
    ])
    for item in visible.values():
        recent, baseline, change = item["recent"], item["baseline"], item["comparison"]
        delta = "—"
        if change["state"] == "comparable":
            unit = "百分点" if item["unit"] == "%" else item["unit"]
            delta = f"{format_number(change['difference'], signed=True)} {unit}"
        sources = "、".join(dict.fromkeys(source_types(recent["sources"]))) or "—"
        if with_identifiers:
            sources += f"（{len(recent['sources'])} 个来源）"
        coverage = (
            f"{recent['covered_days']}/{recent['calendar_days']} · "
            f"{baseline['covered_days']}/{baseline['calendar_days']}"
        )
        lines.append(
            f"| {item['label']}（{item['unit']}） | {format_number(baseline['median'])} | "
            f"{format_number(recent['median'])} | {delta} | {format_number(recent['period_total'])} | "
            f"{coverage} | {sources} |"
        )
    lines.extend(["", "## 变化与数据限制", ""])
    for item in comparisons[1:3]:
        unit = "百分点" if item["unit"] == "%" else item["unit"]
        change = format_number(item["comparison"]["difference"], signed=True)
        lines.append(f"- {item['label']}的本期中位数与{compare_label}相差 {change} {unit}。")
    if not comparisons:
        lines.append("- 当前覆盖范围内，没有可描述的区间差值。")
    for state, description in (
        ("insufficient_coverage", f"本期或{compare_label}覆盖不足，暂缓比较"),
        ("source_changed", "两个窗口采用的来源不同，暂缓比较"),
    ):
        affected = "、".join(item["label"] for item in available.values() if item["comparison"]["state"] == state)
        if affected:
            lines.append(f"- {affected}，{description}。")
    if any(item["recent"]["overlapping_records_skipped"] or item["baseline"]["overlapping_records_skipped"]
           for item in visible.values()):
        lines.append("- 部分累积记录因来源间重叠被剔除，相关日合计可能偏低。")
    if any(len(set(item["recent"]["utc_offsets_minutes"] + item["baseline"]["utc_offsets_minutes"])) > 1
           for item in available.values()):
        lines.append("- 所用记录出现多个 UTC 偏移，跨时区或时钟切换可能影响按日归属。")
    if not lines[-1]:
        lines.append("- 其他指标的区间差值和有效样本见表。")
    if archive is not None:
        lines.extend(archive_limitations(archive))
    if "sleep" in visible:
        lines.extend(sleep_section(visible["sleep"], previous_window))
    if "workout_minutes" in visible:
        lines.extend(workout_section(visible["workout_minutes"], previous_window))
    if archive is not None:
        lines.extend(archive_section(archive))
    lines.extend(["", "## 生活方式与下次复盘", ""])
    if "sleep" in available:
        lines.append("下一次可用相同日期长度复盘睡眠。若近期作息有变化，记下变化的日期，便于和每晚记录一起看。")
    elif "steps" in available:
        lines.append("下一次可用相同日期长度和数据来源复盘步数。若手机携带习惯有变化，记下对应日期。")
    else:
        lines.append("下次复盘时可选择相同长度的区间，并保留一致的数据来源和测量条件。")
    lines.extend([
        "",
        "## 异常提醒",
        "",
        "本期检查了指标覆盖、来源差异和区间变化。设备通知和临床阈值在此版本的检查范围之外。",
        "",
        "本报告用于日常健康管理参考。症状评估、诊断和治疗请咨询医疗专业人员。",
        "",
    ])
    return "\n".join(lines)
