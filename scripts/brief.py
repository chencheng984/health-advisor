"""Prepare the aggregate evidence used by an agent to write a health review."""

import re
from datetime import date

from insights import STAGES, WORKOUT_LABELS
from storage import SOURCE_LABELS


DEFAULT_METRICS = ("sleep", "resting_hr", "hrv", "steps", "exercise_minutes", "workout_minutes")
SOURCE_TYPE = re.compile(r"(" + "|".join(map(re.escape, SOURCE_LABELS.values())) + r")(?: #[1-9][0-9]*)?\Z")


def aggregate_privacy(with_identifiers=False, *, includes_memory=False):
    notes = ["读取本地缓存的聚合证据。"]
    if with_identifiers:
        notes.append("已包含区间来源计数。")
    if includes_memory:
        notes.append("已包含用户保存的复盘要点。")
    return {"level": "L2", "raw_files_read": False, "identifiers_exposed": False, "notes": notes}


def fields_from(data, *fields):
    return {key: data[key] for key in fields}


def source_types(sources):
    labels = []
    for source in sources:
        match = SOURCE_TYPE.fullmatch(source["label"])
        labels.append(match[1] if match else SOURCE_LABELS["other"])
    return labels


def period_evidence(period, with_identifiers):
    evidence = fields_from(
        period, "covered_days", "calendar_days", "first_observation", "last_observation",
        "median", "period_total", "utc_offsets_minutes", "overlapping_records_skipped",
    )
    evidence["sources"] = list(dict.fromkeys(source_types(period["sources"])))
    if with_identifiers:
        evidence["source_count"] = len(period["sources"])
    return evidence


def sleep_evidence(period):
    return {
        **fields_from(period, "sleep_minutes", "stage_coverage_percent", "staged_nights", "conflict_minutes"),
        "stage_minutes": fields_from(period["stage_minutes"], *STAGES),
        "schedule": fields_from(period["schedule"], "nights", "main_start", "main_end",
                                "start_iqr_minutes", "end_iqr_minutes"),
    }


def workout_evidence(period):
    totals = {}
    for item in period["by_type"]:
        kind = item["type"] if item["type"] in WORKOUT_LABELS else "Other"
        total = totals.setdefault(kind, {"type": kind, "label": WORKOUT_LABELS[kind], "sessions": 0, "minutes": 0})
        total["sessions"] += item["sessions"]
        total["minutes"] += item["minutes"]
    return {"sessions": period["sessions"], "by_type": sorted(totals.values(), key=lambda row: (-row["minutes"], row["type"]))}


def prepare_brief(result, last_indexed_date, with_identifiers=False, *, memory=None, archive=None):
    period = result["report_period"]
    days_after_data = None
    if last_indexed_date is not None:
        days_after_data = max(0, (date.fromisoformat(period["to"]) - date.fromisoformat(last_indexed_date)).days)
    metrics = {}
    for key, item in result["metrics"].items():
        evidence = {
            **fields_from(item, "label", "unit"),
            "recent": period_evidence(item["recent"], with_identifiers),
            "comparison_period": period_evidence(item["baseline"], with_identifiers),
            "comparison": fields_from(item["comparison"], "state", "difference", "percent"),
        }
        for detail, available, summary in (
            ("sleep_details", "stage_data_available", sleep_evidence),
            ("workout_details", "type_data_available", workout_evidence),
        ):
            if detail in item:
                data = item[detail]
                evidence[detail] = {available: data[available], **{span: summary(data[span]) for span in ("recent", "baseline")}}
        metrics[key] = evidence
    includes_memory = bool(memory and (memory["goals"] or memory["constraints"] or memory["one_line"]))
    return {
        "privacy": aggregate_privacy(with_identifiers, includes_memory=includes_memory),
        **({"memory": memory} if memory is not None else {}),
        **({"archive": archive} if archive is not None else {}),
        "report_period": fields_from(period, "from", "to"),
        "comparison_period": fields_from(result["baseline_period"], "from", "to"),
        "comparison_window": result["comparison_window"],
        "coverage": {
            "last_indexed_date": last_indexed_date,
            "days_after_last_indexed_date": days_after_data,
            "metrics_with_recent_data": sum(item["recent"]["covered_days"] > 0 for item in metrics.values()),
        },
        "metrics": metrics,
    }
