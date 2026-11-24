"""Stream Apple Health XML and retain the existing parsing rules."""

import math
import re
from datetime import date
from pathlib import PurePosixPath
from xml.parsers import expat
from zipfile import ZipFile

from catalog import IN_BED, METRIC_KEYS, METRICS, SLEEP_STATES
from import_records import CHUNK_BYTES, HealthError, Observation, make_sample, offset_time, sleep_samples, source_fingerprint


IDENTIFIER = re.compile(r"HK[A-Za-z0-9_]{1,126}\Z")
WORKOUT_TYPE = re.compile(r"HKWorkoutActivityType([A-Za-z][A-Za-z0-9]{0,79})\Z")
DEVICE_DETAILS = re.compile(r"\b(hardware|model):\s*([^,>]+)")
TIME_OFFSET = re.compile(r" ?([+-]\d{2}):?(\d{2})\Z")


def read_time(value):
    return offset_time(TIME_OFFSET.sub(r"\1:\2", value)) if value else None


def source_identity(attrs):
    name = attrs.get("sourceName", "").strip()
    details = dict(DEVICE_DETAILS.findall(attrs.get("device", "")))
    hardware = details.get("hardware", "").strip()
    model = details.get("model", "").strip()
    recognized = (hardware + " " + model).strip().casefold() or name.casefold()
    if recognized.startswith("watch") or "apple watch" in recognized:
        kind = "watch"
    elif "iphone" in recognized:
        kind = "phone"
    else:
        kind = "other"
    return kind, "apple", source_fingerprint(name, hardware or model)


def observation(tag, attrs):
    identifier = tag
    if tag in {"Record", "Correlation", "ClinicalRecord"} and IDENTIFIER.fullmatch(attrs.get("type", "")):
        identifier = attrs["type"]
    start = read_time(attrs.get("startDate"))
    day = start.date().isoformat() if start is not None else None
    if tag == "ActivitySummary":
        try:
            day = date.fromisoformat(attrs.get("dateComponents", "")).isoformat()
        except ValueError:
            pass
    key = METRIC_KEYS.get(identifier)
    if key is None:
        return Observation(identifier, day)
    end = read_time(attrs.get("endDate"))
    if start is None or end is None or end < start:
        return Observation(identifier, day, skipped=1)
    if key == "sleep":
        category = attrs.get("value")
        if category == IN_BED:
            return Observation(identifier, day, excluded=1)
        state = SLEEP_STATES.get(category)
        if state is None or start == end:
            return Observation(identifier, day, skipped=1)
        return Observation(identifier, day, sleep_samples(source_identity(attrs), start, end, state))
    value_attr, unit_attr = ("duration", "durationUnit") if key == "workout_minutes" else ("value", "unit")
    metric = METRICS[key]
    factor = metric.factors.get(attrs.get(unit_attr))
    try:
        value = float(attrs.get(value_attr, ""))
    except ValueError:
        value = math.nan
    if factor is None or not math.isfinite(value * factor) or value < 0 or (metric.unit == "%" and value > 1):
        return Observation(identifier, day, skipped=1)
    match = WORKOUT_TYPE.fullmatch(attrs.get("workoutActivityType", ""))
    state = (match.group(1) if match else "Unknown") if key == "workout_minutes" else ""
    sample = make_sample(key, day, source_identity(attrs), start, end, value * factor, state)
    return Observation(identifier, day, (sample,))


def check_doctype(name, system_id, public_id, has_internal_subset):
    if name != "HealthData" or system_id is not None or public_id is not None:
        raise HealthError("导出 XML 含有不受支持的文档声明。")


def reject_entity(*args):
    raise HealthError("导出 XML 含有不受支持的自定义实体。")


def parser(start, end=None):
    reader = expat.ParserCreate()
    reader.StartElementHandler = start
    reader.EndElementHandler = end
    reader.StartDoctypeDeclHandler = check_doctype
    reader.EntityDeclHandler = reject_entity
    reader.ExternalEntityRefHandler = reject_entity
    return reader


class FoundRoot(Exception):
    pass


def is_health_xml(archive, member):
    def root(tag, _attrs):
        raise FoundRoot(tag)

    reader = parser(root)
    try:
        with archive.open(member) as stream:
            for block in iter(lambda: stream.read(CHUNK_BYTES), b""):
                reader.Parse(block, False)
            reader.Parse(b"", True)
    except FoundRoot as found:
        return found.args[0] == "HealthData"
    except (expat.ExpatError, HealthError):
        pass
    return False


def members_of(archive):
    return {item: PurePosixPath(item.filename.replace("\\", "/"))
            for item in archive.infolist() if not item.is_dir()}


def locate_data_file(archive):
    members = members_of(archive)
    data_files = [item for item, path in members.items() if path.name.casefold() == "export.xml"]
    if not data_files:
        data_files = [item for item, path in members.items()
                      if path.suffix.casefold() == ".xml" and is_health_xml(archive, item)]
    if len(data_files) != 1:
        raise HealthError("ZIP 内应包含一份 Apple 健康 XML。请使用 Apple 健康导出的完整 ZIP。")
    return data_files[0]


def inspect(path):
    with ZipFile(path) as archive:
        locate_data_file(archive)
        files = list(members_of(archive).values())
    return {"ecg_csv": sum(path.suffix.casefold() == ".csv" and "electrocardiograms" in path.parts for path in files),
            "gpx": sum(path.suffix.casefold() == ".gpx" for path in files),
            "cda_xml": sum(path.name.casefold() == "export_cda.xml" for path in files)}


def iter_samples(path):
    depth = 0
    pending = []

    def start(tag, attrs):
        nonlocal depth
        depth += 1
        if depth == 1 and tag != "HealthData":
            raise HealthError("XML 根节点应为 Apple 健康的 HealthData。")
        if depth == 2 and tag in {"Record", "Workout", "Correlation", "ActivitySummary",
                                 "ClinicalRecord", "Audiogram", "VisionPrescription"}:
            pending.append(observation(tag, attrs))

    def end(_tag):
        nonlocal depth
        depth -= 1

    reader = parser(start, end)
    with ZipFile(path) as archive, archive.open(locate_data_file(archive)) as stream:
        for block in iter(lambda: stream.read(CHUNK_BYTES), b""):
            reader.Parse(block, False)
            yield from pending
            pending.clear()
        reader.Parse(b"", True)
        yield from pending
