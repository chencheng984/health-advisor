"""Huawei Health CSV and JSON summary fields."""

import json

from import_records import HealthError
from .garmin import COLUMNS as ENGLISH_COLUMNS
from .tabular import column_map, csv_header, csv_rows, inspect_table, mapped_rows


COLUMNS = {**ENGLISH_COLUMNS, **column_map(
    ("steps", "count", "步数 总步数 totalsteps"),
    ("distance", "km", "步行跑步距离 距离公里 距离"),
    ("distance", "m", "距离米 distancem"),
    ("active_energy", "kcal", "活动能量 活动消耗千卡"),
    ("active_energy", "kJ", "activecalorieskj"),
    ("workout_minutes", "min", "训练时长分钟 workoutminutes"),
    ("workout_minutes", "s", "训练时长秒 workoutdurationseconds"),
    ("sleep", "h", "睡眠时长 睡眠时长小时"),
    ("sleep", "min", "睡眠时长分钟 睡眠分钟 totalsleepminutes"),
    ("sleep", "s", "睡眠时长秒 sleepdurationseconds"),
    ("heart_rate", "bpm", "心率 平均心率"),
    ("resting_hr", "bpm", "静息心率"),
    ("body_mass", "kg", "体重 体重公斤"),
    ("oxygen_saturation", "%", "血氧 血氧饱和度"),
    ("body_fat", "%", "体脂率 bodyfat"),
    ("respiratory_rate", "bpm", "呼吸频率"),
)}


def is_json(stream):
    first = next((char for char in iter(lambda: stream.read(1), "") if not char.isspace()), "")
    stream.seek(0)
    return first in ("{", "[")


def json_rows(stream):
    try:
        data = json.load(stream)
    except (ValueError, RecursionError):
        raise HealthError("无法读取华为 JSON。") from None
    if isinstance(data, dict):
        data = next((data[key] for key in ("records", "data", "dailyData", "dataList") if key in data), [data])
    if not isinstance(data, list):
        raise HealthError("华为 JSON 须包含一组汇总记录。")
    return data


def inspect(path):
    with path.open(encoding="utf-8-sig") as stream:
        if is_json(stream):
            headers = {key for row in json_rows(stream) if isinstance(row, dict) for key in row}
            return inspect_table(headers, COLUMNS)
    return inspect_table(csv_header(path), COLUMNS)


def iter_samples(path):
    with path.open(encoding="utf-8-sig") as stream:
        if is_json(stream):
            yield from mapped_rows(json_rows(stream), COLUMNS, "huawei")
            return
    yield from mapped_rows(csv_rows(path), COLUMNS, "huawei")
