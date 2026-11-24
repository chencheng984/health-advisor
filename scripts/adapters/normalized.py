"""Read the versioned normalized-health/1 JSON Lines interchange format."""

import json
import math
from datetime import date

from catalog import METRICS, SLEEP_STATES
from insights import WORKOUT_LABELS
from import_records import HealthError, Observation, brand_source, make_sample, numeric, offset_time, quantity_value, sleep_samples
from . import BRANDS, KINDS


SCHEMA = "normalized-health/1"


def rows(path):
    with path.open(encoding="utf-8-sig") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (ValueError, RecursionError):
                raise HealthError("标准化文件须每行包含一个 JSON 对象。") from None
            if not isinstance(row, dict) or row.get("schema") != SCHEMA:
                raise HealthError("标准化记录的 schema 须为 normalized-health/1。")
            yield row


def inspect(path):
    with_rows = rows(path)
    try:
        if next(with_rows, None) is None:
            raise HealthError("标准化文件没有记录。")
    finally:
        with_rows.close()
    return {}


def iter_samples(path):
    for row in rows(path):
        key = row.get("metric")
        if not isinstance(key, str) or key not in METRICS:
            yield Observation("unsupported_metric", skipped=1)
            continue
        code = METRICS[key].identifier
        day, start, end = row.get("day"), offset_time(row.get("start")), offset_time(row.get("end"))
        device, unit, state = row.get("source"), row.get("unit"), row.get("state") or ""
        value = numeric(row.get("value"))
        if (start is None or end is None or start > end or start.date().isoformat() != day
                or not isinstance(device, dict) or not isinstance(unit, str) or not isinstance(state, str)):
            yield Observation(code, skipped=1)
            continue
        brand, kind = device.get("brand"), device.get("kind")
        if not isinstance(brand, str) or not isinstance(kind, str) or brand not in BRANDS or kind not in KINDS:
            yield Observation(code, day, skipped=1)
            continue
        source = brand_source(brand, kind if kind in ("watch", "phone", "other") else None)
        if key == "sleep":
            if (state not in SLEEP_STATES.values() or start == end or end.date() == date.max
                    or unit not in ("h", "hr", "min", "s") or not math.isfinite(value) or value < 0):
                yield Observation(code, day, skipped=1)
                continue
            yield Observation(code, day, sleep_samples(source, start, end, state))
            continue
        value = quantity_value(key, value, unit)
        if value is None:
            yield Observation(code, day, skipped=1)
            continue
        state = (state if state in WORKOUT_LABELS else "Unknown") if key == "workout_minutes" else ""
        yield Observation(code, day, (make_sample(key, day, source, start, end, value, state),))
