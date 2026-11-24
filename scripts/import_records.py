"""Shared record types and conversions used by adapters and cache storage."""

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from catalog import METRICS


CHUNK_BYTES = 1024 * 1024
UNITS = {"bpm": "count/min", "次/分钟": "count/min", "步": "count", "分钟": "min", "秒": "s", "小时": "h",
         "公里": "km", "千米": "km", "米": "m", "千卡": "kcal", "公斤": "kg", "磅": "lb",
         "mL/(kg·min)": "mL/min·kg"}


class HealthError(Exception):
    """An actionable message safe to display without quoting an export."""


@dataclass(frozen=True, slots=True)
class Sample:
    metric: str
    day: str
    source_kind: str
    start_utc: float
    end_utc: float
    start_offset: int
    end_offset: int
    value: float
    state: str
    brand: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class Observation:
    identifier: str
    day: str | None = None
    samples: tuple[Sample, ...] = ()
    skipped: int = 0
    excluded: int = 0
    records: int = 1


def source_fingerprint(name, hardware):
    identity = json.dumps([name, hardware], ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def brand_source(brand, kind=None):
    if brand in ("garmin", "huawei"):
        kind = brand
    elif brand != "apple" or kind is None:
        kind = "other"
    return kind, brand, source_fingerprint(brand, kind)


def offset_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def make_sample(key, day, source, start, end, value, state, limits=None):
    kind, brand, fingerprint = source
    left, right = limits or (start.timestamp(), end.timestamp())
    return Sample(key, day, kind, left, right, int(start.utcoffset().total_seconds() / 60),
                  int(end.utcoffset().total_seconds() / 60), value, state, brand, fingerprint)


def numeric(value):
    if isinstance(value, bool):
        return math.nan
    try:
        return float(value.replace(",", "").strip() if isinstance(value, str) else value)
    except (TypeError, ValueError, OverflowError):
        return math.nan


def quantity_value(key, value, unit):
    metric = METRICS[key]
    unit = UNITS.get(unit, unit)
    if metric.unit == "%":
        if unit == "%":
            value /= 100
        elif unit not in ("fraction", "ratio", "1"):
            return None
        if value > 1:
            return None
        unit = "%"
    factor = metric.factors.get(unit)
    if factor is None:
        factor = next((f for name, f in metric.factors.items() if name.casefold() == unit.casefold()), None)
    if factor is None or not math.isfinite(value * factor) or value < 0:
        return None
    return value * factor


def sleep_samples(source, start, end, state):
    return tuple(make_sample("sleep", day, source, start, end, 0.0, state, (left, right))
                 for day, left, right in local_sleep_segments(start, end))


def local_sleep_segments(start, end):
    end_utc = end.timestamp()
    noon = datetime.combine(start.date(), time(12), tzinfo=start.tzinfo)
    if start >= noon:
        noon += timedelta(days=1)
    left = start.timestamp()
    while left < end_utc:
        right = min(end_utc, noon.timestamp())
        yield noon.date().isoformat(), left, right
        left = right
        if left < end_utc:
            noon += timedelta(days=1)
