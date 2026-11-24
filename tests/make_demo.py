"""Create a synthetic Apple Health export for health-advisor demos and tests."""

import argparse
from datetime import date, timedelta
from pathlib import Path
from xml.sax.saxutils import quoteattr
from zipfile import ZIP_DEFLATED, ZipFile


HEADER = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE HealthData [
<!ELEMENT HealthData (Record|Workout|Correlation|ActivitySummary)*>
<!ELEMENT Record (MetadataEntry*)>
]>
<HealthData>
"""
WATCH = "示例用户的 Apple Watch"
PHONE = "示例用户的 iPhone"


def tag(name, **attrs):
    return "<" + name + "".join(f" {key}={quoteattr(str(value))}" for key, value in attrs.items()) + " />"


def record(code, start, end, value, unit=None, source=WATCH):
    details = {"type": code, "startDate": start, "endDate": end, "sourceName": source, "value": value}
    if unit is not None:
        details["unit"] = unit
    return tag("Record", **details)


def quantity(code, *args, **kwargs):
    return record("HKQuantityTypeIdentifier" + code, *args, **kwargs)


def timestamp(day, clock, offset="+0800"):
    return f"{day} {clock}:00 {offset}"


def write_export(path, records, filename="apple_health_export/export.xml", attachments=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "x", compression=ZIP_DEFLATED) as archive:
        with archive.open(filename, "w") as stream:
            stream.write(HEADER.encode("utf-8"))
            for item in records:
                stream.write((item + "\n").encode("utf-8"))
            stream.write(b"</HealthData>\n")
        for name, content in (attachments or {}).items():
            archive.writestr(name, content)


def demo_records():
    for index in range(28):
        day = date(2026, 10, 1) + timedelta(days=index)
        previous = day - timedelta(days=1)
        recent = index >= 21
        watch = quantity("StepCount", timestamp(day, "08:00"), timestamp(day, "09:00"),
                         5200 if recent else 6500, "count")
        yield watch
        if index == 24:
            yield watch
        yield quantity("StepCount", timestamp(day, "08:30"), timestamp(day, "09:30"), 1200, "count", PHONE)
        yield quantity("StepCount", timestamp(day, "18:00"), timestamp(day, "19:00"), 400, "count", PHONE)
        sleep_type = "HKCategoryTypeIdentifierSleepAnalysis"
        sleep_end = timestamp(day, "05:30" if recent else "06:30")
        for state, start, end in (
            ("InBed", timestamp(previous, "22:00"), timestamp(day, "07:00")),
            ("AsleepUnspecified", timestamp(previous, "22:30"), sleep_end),
            ("AsleepDeep", timestamp(previous, "22:40"), timestamp(previous, "23:00")),
            ("AsleepCore", timestamp(previous, "23:00"), timestamp(day, "02:00")),
            ("Awake", timestamp(day, "03:00"), timestamp(day, "03:30")),
            ("AsleepREM", timestamp(day, "04:30"), timestamp(day, "05:15")),
        ):
            yield record(sleep_type, start, end, "HKCategoryValueSleepAnalysis" + state)
        for code, value, unit in (
            ("RestingHeartRate", 64 if recent else 60, "count/min"),
            ("HeartRate", 75, "count/min"),
            ("HeartRateVariabilitySDNN", 46 if recent else 49, "ms"),
            ("RespiratoryRate", 15, "count/min"),
            ("OxygenSaturation", 0.97, "%"),
            ("BodyMass", 70, "kg"),
            ("BodyFatPercentage", 0.23, "%"),
        ):
            instant = timestamp(day, "09:15")
            source = "示例体重秤" if code.startswith("Body") else WATCH
            yield quantity(code, instant, instant, value, unit, source)
        for code, value, unit in (
            ("DistanceWalkingRunning", 2.5, "mi"),
            ("ActiveEnergyBurned", 836.8, "kJ"),
            ("BasalEnergyBurned", 1450, "kcal"),
            ("AppleExerciseTime", 23, "min"),
        ):
            yield quantity(code, timestamp(day, "09:00"), timestamp(day, "10:00"), value, unit)
        if index % 2 == 0:
            sport = ("Walking", "Running", "Cycling")[index // 2 % 3]
            yield tag("Workout", startDate=timestamp(day, "17:30"), endDate=timestamp(day, "18:05"),
                      sourceName=WATCH, duration=35, durationUnit="min",
                      workoutActivityType="HKWorkoutActivityType" + sport)
        if index % 3 == 0:
            instant = timestamp(day, "10:00")
            yield quantity("VO2Max", instant, instant, 39, "mL/min·kg")
        if index == 10:
            yield tag("Correlation", type="HKCorrelationTypeIdentifierBloodPressure",
                      startDate=timestamp(day, "10:00"))


def make_demo(path):
    write_export(path, demo_records(), attachments={
        "apple_health_export/workout-routes/demo.gpx": "<gpx />",
        "apple_health_export/electrocardiograms/demo.csv": "Synthetic demo attachment\n",
        "apple_health_export/export_cda.xml": "<ClinicalDocument />",
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    make_demo(parser.parse_args().path)
