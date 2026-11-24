"""Registry and shared import records for local health exports."""

import json
from importlib import import_module
from zipfile import BadZipFile, is_zipfile


from import_records import CHUNK_BYTES, HealthError


FORMATS = ("apple", "garmin", "huawei", "normalized")
KINDS = {"watch", "phone", "other", "garmin", "huawei"}
BRANDS = {"apple", "garmin", "huawei", "other"}


def adapter(name):
    return import_module(f"{__name__}.{name}")


def sniff(path):
    from .tabular import DATE_FIELDS, csv_header, header_key

    if is_zipfile(path):
        return "apple"
    with path.open("rb") as stream:
        prefix = stream.read(CHUNK_BYTES).decode("utf-8-sig", errors="replace").lstrip()
    if prefix.startswith("{"):
        try:
            row = json.loads(prefix.splitlines()[0])
        except ValueError:
            row = None
        if isinstance(row, dict) and str(row.get("schema", "")).startswith("normalized-health/"):
            return "normalized"
    if prefix.startswith(("{", "[")):
        return "huawei"
    header = {header_key(cell) for cell in csv_header(path)} if "\ufffd" not in prefix else set()
    if header.intersection(DATE_FIELDS):
        if header.intersection(adapter("huawei").COLUMNS.keys() - adapter("garmin").COLUMNS.keys()):
            return "huawei"
        if header.intersection(adapter("garmin").COLUMNS):
            return "huawei" if header.intersection({"日期", "统计日期", "华为运动健康", "huawei"}) else "garmin"
    if path.suffix.casefold() == ".zip":
        raise BadZipFile
    raise HealthError("无法识别导出内容。请检查文件格式，或明确指定 import --format。")
