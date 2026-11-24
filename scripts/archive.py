"""Read ECG metadata and workout routes from Apple Health attachments.

Only attachment metadata and route geometry are read. ECG waveform samples are
counted, never interpreted, and the export's patient fields (name, birth date)
are never stored.
"""

import csv
import io
import math
import re
from collections import Counter
from datetime import datetime
from pathlib import PurePosixPath
from xml.etree import ElementTree

EARTH_RADIUS_M = 6371008.8
ASCENT_STEP_M = 1.0
TIME_OFFSET = re.compile(r" ?([+-]\d{2}):?(\d{2})\Z")

ECG_LABELS = {
    "记录日期": "recorded",
    "recorded date": "recorded",
    "分类": "classification",
    "classification": "classification",
    "症状": "symptoms",
    "symptoms": "symptoms",
    "设备": "device",
    "device": "device",
    "采样速率": "sample_rate",
    "sample rate": "sample_rate",
}
TEXT_LIMIT = 80
ECG_CLASSIFICATIONS = {
    label.casefold(): label for label in (
        "窦性心律", "心房颤动", "房颤", "心率过低", "心率过高", "低心率", "高心率",
        "心率高于120", "心率高于 120", "心率高于120次/分", "心率高于 120 BPM",
        "心率低于50", "心率低于 50", "不确定", "结果不明确", "记录结果不佳", "记录不佳", "无法分类",
        "Sinus Rhythm", "Atrial Fibrillation", "Atrial Fibrillation — High Heart Rate", "Inconclusive",
        "High Heart Rate", "Low Heart Rate", "Poor Recording", "Unclassifiable",
    )
}
WATCH_DEVICE = re.compile(r"apple\s*watch|\bwatch\d+,\d+\b", re.I)


def classification_label(value):
    return ECG_CLASSIFICATIONS.get(value.strip().casefold(), "其他设备分类")[:TEXT_LIMIT]


def device_label(value):
    return "Apple Watch" if WATCH_DEVICE.search(value) else "其他或未识别设备"


def symptoms_present(value):
    return value.strip().casefold() not in {"", "none", "no symptoms", "无", "无症状", "未填写"}


def label_counts(counts):
    return [{"label": key, "count": value} for key, value in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def table_exists(connection, name):
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def read_ecg_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(TIME_OFFSET.sub(r"\1:\2", value.strip()))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def parse_ecg_csv(text):
    """Return one ECG record's metadata, or None when the file is not an ECG CSV."""
    header = {}
    samples = 0
    for row in csv.reader(io.StringIO(text.lstrip("\ufeff"))):
        if not row:
            continue
        key = row[0].strip()
        field = ECG_LABELS.get(key.casefold())
        if field is not None:
            header.setdefault(field, ",".join(row[1:]).strip())
            continue
        try:
            float(key.replace(",", "."))
        except ValueError:
            continue
        samples += 1
    recorded = read_ecg_time(header.get("recorded"))
    classification = header.get("classification", "").strip()
    if recorded is None or not classification:
        return None
    rate = "".join(character for character in header.get("sample_rate", "") if character.isdigit())
    return {
        "recorded_utc": recorded.timestamp(),
        "classification": classification_label(classification),
        "symptoms": "provided" if symptoms_present(header.get("symptoms", "")) else "",
        "device": device_label(header.get("device", "")),
        "sample_rate": int(rate) if rate else 0,
        "samples": samples,
    }


def haversine(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = phi2 - phi1
    delta_lambda = math.radians(lon2 - lon1)
    haversine_term = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(haversine_term))


def parse_gpx(blob):
    """Return one route's geometry summary, or None when the track is unusable."""
    try:
        root = ElementTree.fromstring(blob)
    except ElementTree.ParseError:
        return None
    points = []
    for element in root.iter():
        if not element.tag.endswith("trkpt"):
            continue
        try:
            latitude = float(element.get("lat"))
            longitude = float(element.get("lon"))
        except (TypeError, ValueError):
            continue
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            continue
        elevation = None
        moment = None
        for child in element:
            if child.tag.endswith("ele"):
                try:
                    height = float(child.text)
                    elevation = height if math.isfinite(height) else None
                except (TypeError, ValueError):
                    elevation = None
            elif child.tag.endswith("time"):
                try:
                    parsed = datetime.fromisoformat((child.text or "").strip().replace("Z", "+00:00"))
                except ValueError:
                    parsed = None
                moment = parsed.timestamp() if parsed is not None else None
        points.append((latitude, longitude, elevation, moment))
    if len(points) < 2:
        return None
    distance = ascent = 0.0
    previous = None
    for latitude, longitude, elevation, _moment in points:
        if previous is not None:
            distance += haversine(previous[0], previous[1], latitude, longitude)
            if previous[2] is not None and elevation is not None:
                climb = elevation - previous[2]
                if climb >= ASCENT_STEP_M:
                    ascent += climb
        previous = (latitude, longitude, elevation)
    moments = [moment for *_rest, moment in points if moment is not None]
    return {
        "start_utc": min(moments) if moments else 0.0,
        "name": "",
        "points": len(points),
        "distance_m": round(distance, 1),
        "duration_s": round(max(moments) - min(moments), 1) if len(moments) >= 2 else 0.0,
        "ascent_m": round(ascent, 1),
    }


def load_archive(archive, connection):
    """Insert ECG and route summaries from an open export ZIP; duplicates are kept out."""
    members = [
        (item, PurePosixPath(item.filename.replace("\\", "/")))
        for item in archive.infolist() if not item.is_dir()
    ]
    ecg_rows = []
    route_rows = []
    for item, path in members:
        suffix = path.suffix.casefold()
        if suffix == ".csv" and "electrocardiograms" in path.parts:
            try:
                text = archive.read(item).decode("utf-8-sig")
            except (UnicodeDecodeError, KeyError):
                continue
            row = parse_ecg_csv(text)
            if row is not None:
                ecg_rows.append(row)
        elif suffix == ".gpx":
            row = parse_gpx(archive.read(item))
            if row is not None:
                route_rows.append(row)
    connection.executemany(
        "INSERT OR IGNORE INTO ecg VALUES (?, ?, ?, ?, ?, ?)",
        [
            (row["recorded_utc"], row["classification"], row["symptoms"], row["device"],
             row["sample_rate"], row["samples"])
            for row in ecg_rows
        ],
    )
    connection.executemany(
        "INSERT OR IGNORE INTO routes VALUES (?, ?, ?, ?, ?, ?)",
        [
            (row["start_utc"], row["name"], row["points"], row["distance_m"],
             row["duration_s"], row["ascent_m"])
            for row in route_rows
        ],
    )
    return {"ecg": len(ecg_rows), "routes": len(route_rows)}


def local_day(timestamp):
    return datetime.fromtimestamp(timestamp).date().isoformat() if timestamp else None


def in_window(timestamp, first, last):
    day = local_day(timestamp)
    return day is not None and first <= day <= last


def archive_window(connection, first, last):
    """Summarize a local-date window separately from the whole archive."""
    ecg = {"records": 0, "classifications": [], "devices": [], "records_with_symptoms": 0, "samples": 0}
    routes = {"records": 0, "total_distance_km": 0.0, "total_ascent_m": 0.0,
              "longest_km": 0.0, "total_duration_h": 0.0}
    overall = {"ecg_records": 0, "ecg_last_day": None, "route_records": 0, "route_last_day": None, "route_files": 0}
    if table_exists(connection, "ecg"):
        classifications, devices = Counter(), Counter()
        for stamp, classification, device, symptoms, samples in connection.execute(
            "SELECT recorded_utc, classification, device, symptoms, samples FROM ecg"
        ):
            if in_window(stamp, first, last):
                ecg["records"] += 1
                ecg["samples"] += samples
                ecg["records_with_symptoms"] += symptoms_present(symptoms)
                classifications[classification_label(classification)] += 1
                devices[device_label(device)] += 1
        ecg.update(classifications=label_counts(classifications), devices=label_counts(devices))
        count, latest = connection.execute("SELECT COUNT(*), MAX(recorded_utc) FROM ecg").fetchone()
        overall.update(ecg_records=count, ecg_last_day=local_day(latest))
    if table_exists(connection, "routes"):
        distance = ascent = duration = longest = 0.0
        for stamp, length, climb, seconds in connection.execute(
            "SELECT start_utc, distance_m, ascent_m, duration_s FROM routes"
        ):
            if in_window(stamp, first, last):
                routes["records"] += 1
                distance += length
                ascent += climb
                duration += seconds
                longest = max(longest, length)
        routes.update(total_distance_km=round(distance / 1000, 2), total_ascent_m=round(ascent, 0),
                      longest_km=round(longest / 1000, 2), total_duration_h=round(duration / 3600, 2))
        count, latest = connection.execute("SELECT COUNT(*), MAX(start_utc) FROM routes").fetchone()
        overall.update(route_records=count, route_last_day=local_day(latest))
        if table_exists(connection, "meta"):
            files = connection.execute("SELECT CAST(value AS INTEGER) FROM meta WHERE key = 'gpx'").fetchone()
            overall["route_files"] = files[0] if files else 0
    return {"ecg": ecg, "routes": routes, "overall": overall}


def archive_summary(connection, detail=False):
    summary = {
        "ecg": {
            "records": 0, "first_day": None, "last_day": None,
            "classifications": [], "devices": [], "samples": 0,
        },
        "routes": {
            "records": 0, "first_day": None, "last_day": None,
            "total_distance_km": 0.0, "total_ascent_m": 0.0, "longest_km": 0.0,
            "total_duration_h": 0.0,
        },
    }
    if detail:
        summary["ecg"]["items"] = []
        summary["routes"]["items"] = []
    if table_exists(connection, "ecg"):
        row = connection.execute(
            "SELECT COUNT(*), MIN(recorded_utc), MAX(recorded_utc), SUM(samples) FROM ecg"
        ).fetchone()
        summary["ecg"].update(
            records=row[0], first_day=local_day(row[1]), last_day=local_day(row[2]),
            samples=row[3] or 0,
        )
        classifications, devices = Counter(), Counter()
        for classification, device in connection.execute("SELECT classification, device FROM ecg"):
            classifications[classification_label(classification)] += 1
            devices[device_label(device)] += 1
        summary["ecg"].update(classifications=label_counts(classifications), devices=label_counts(devices))
        if detail:
            summary["ecg"]["items"] = [
                {"day": local_day(item[0]), "classification": classification_label(item[1]),
                 "device": device_label(item[2]), "sample_rate": item[3], "samples": item[4]}
                for item in connection.execute(
                    "SELECT recorded_utc, classification, device, sample_rate, samples "
                    "FROM ecg ORDER BY recorded_utc DESC"
                )
            ]
    if table_exists(connection, "routes"):
        row = connection.execute(
            "SELECT COUNT(*), MIN(start_utc), MAX(start_utc), SUM(distance_m), "
            "SUM(ascent_m), SUM(duration_s), MAX(distance_m) FROM routes"
        ).fetchone()
        summary["routes"].update(
            records=row[0], first_day=local_day(row[1]), last_day=local_day(row[2]),
            total_distance_km=round((row[3] or 0) / 1000, 2),
            total_ascent_m=round(row[4] or 0, 0),
            total_duration_h=round((row[5] or 0) / 3600, 1),
            longest_km=round((row[6] or 0) / 1000, 2),
        )
        if detail:
            summary["routes"]["items"] = [
                {"day": local_day(item[0]), "distance_km": round(item[1] / 1000, 2),
                 "ascent_m": item[2], "duration_min": round(item[3] / 60, 0)}
                for item in connection.execute(
                    "SELECT start_utc, distance_m, ascent_m, duration_s "
                    "FROM routes ORDER BY start_utc DESC"
                )
            ]
    return summary
