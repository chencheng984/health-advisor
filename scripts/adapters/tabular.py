"""Shared conversion for daily and activity summaries."""

import csv
import re
from datetime import date, datetime, time, timedelta, timezone

from catalog import METRICS
from import_records import HealthError, Observation, brand_source, make_sample, numeric, quantity_value


DATE_FIELDS = ("starttime", "开始时间", "recordtime", "startdate", "date", "day", "日期", "统计日期", "recorddate")
END_FIELDS = ("endtime", "enddate", "结束时间")
UNUSED_METADATA = {*DATE_FIELDS, *END_FIELDS, "brand", "source", "platform", "vendor"}


def column_map(*groups):
    return {name: (metric, unit) for metric, unit, names in groups for name in names.split()}


def header_key(value):
    return re.sub(r"[^\w]|_", "", value.casefold())


def reader(stream):
    first = stream.readline()
    stream.seek(0)
    try:
        dialect = csv.Sniffer().sniff(first, delimiters=",;\t")
    except csv.Error:
        dialect = "excel"
    return csv.DictReader(stream, dialect=dialect)


def csv_header(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return reader(stream).fieldnames or []


def csv_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        yield from reader(stream)


def moment(value):
    if not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for pattern in ("%m/%d/%Y", "%b %d, %Y", "%Y/%m/%d", "%Y/%m/%d %H:%M:%S", "%A, %B %d, %Y"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        else:
            return None
    return parsed if parsed.utcoffset() is not None else parsed.replace(tzinfo=timezone.utc)


def duration_minutes(value, unit):
    if isinstance(value, str) and ":" in value:
        parts = value.strip().split(":")
        if len(parts) in (2, 3) and all(part.isdecimal() and len(part) <= 6 for part in parts):
            hours, minutes, seconds = map(int, (parts + ["0"])[:3])
            if minutes < 60 and seconds < 60:
                return hours * 60 + minutes + seconds / 60
        return None
    return quantity_value("workout_minutes", numeric(value), unit)


def mapped_rows(rows, mapping, brand):
    source = brand_source(brand)
    unknown = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise HealthError("品牌导出须包含对象记录。")
        row = {header_key(column): value for column, value in raw.items() if isinstance(column, str)}
        unknown.update(row.keys() - mapping.keys() - UNUSED_METADATA)
        start = next(filter(None, (moment(row.get(key)) for key in DATE_FIELDS)), None)
        day = start.date().isoformat() if start else None
        used = set()
        for header, cell in row.items():
            if header not in mapping or cell is None or cell == "":
                continue
            key, unit = mapping[header]
            if key in used:
                continue
            used.add(key)
            code = METRICS[key].identifier
            if start is None:
                yield Observation(code, skipped=1)
                continue
            end, state, left = start, "", start
            if key == "sleep":
                value = duration_minutes(cell, unit)
                if value is None or not 0 < value <= 1440 or start.date() == date.min:
                    yield Observation(code, day, skipped=1)
                    continue
                end = datetime.combine(start.date(), time(12), tzinfo=start.tzinfo)
                left = end - timedelta(minutes=value)
                value, state = 0.0, "duration_only"
            elif key == "workout_minutes":
                value = duration_minutes(cell, unit)
                if value is not None and 0 < value <= 1440:
                    end, state = start + timedelta(minutes=value), "Unknown"
                else:
                    value = None
            else:
                if unit == "%" and isinstance(cell, str):
                    cell = cell.strip().removesuffix("%")
                value = quantity_value(key, numeric(cell), unit)
                supplied_end = next(filter(None, (moment(row.get(name)) for name in END_FIELDS)), None)
                if supplied_end is not None and supplied_end >= start:
                    end = supplied_end
                elif METRICS[key].reducer == "total" and start.time() == time.min and start.date() < date.max:
                    end = start + timedelta(days=1)
            if value is None:
                yield Observation(code, day, skipped=1)
            else:
                yield Observation(code, day, (make_sample(key, day, source, left, end, value, state),))
    if unknown:
        yield Observation("unsupported_columns", skipped=len(unknown), records=len(unknown))


def inspect_table(header, mapping):
    keys = {header_key(name) for name in header}
    if not keys.intersection(DATE_FIELDS) or not keys.intersection(mapping):
        raise HealthError("导出表缺少可识别的日期和健康指标列。")
    return {}
