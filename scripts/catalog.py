"""Apple Health identifiers and the units exposed by the CLI."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Metric:
    identifier: str
    label: str
    unit: str
    group: str
    reducer: str
    factors: dict[str, float]


def quantity(identifier, label, unit, group, factors, reducer="sample"):
    return Metric("HKQuantityTypeIdentifier" + identifier, label, unit, group, reducer, factors)


MINUTES = {"min": 1, "s": 1 / 60, "hr": 60, "h": 60}
ENERGY = {"kcal": 1, "kJ": 1 / 4.184, "J": 1 / 4184}
RATES = {"count/min": 1}
PERCENT = {"%": 100}

METRICS = {
    "steps": quantity("StepCount", "步数", "步", "activity", {"count": 1}, "total"),
    "sleep": Metric("HKCategoryTypeIdentifierSleepAnalysis", "睡眠时长", "小时", "sleep", "sleep", {}),
    "resting_hr": quantity("RestingHeartRate", "静息心率", "bpm", "heart", RATES),
    "distance": quantity(
        "DistanceWalkingRunning", "步行与跑步距离", "km", "activity",
        {"km": 1, "m": 0.001, "mi": 1.609344}, "total",
    ),
    "exercise_minutes": quantity(
        "AppleExerciseTime", "Apple 运动时间", "分钟", "activity", MINUTES, "total",
    ),
    "active_energy": quantity("ActiveEnergyBurned", "活动能量", "kcal", "activity", ENERGY, "total"),
    "basal_energy": quantity("BasalEnergyBurned", "静息能量", "kcal", "activity", ENERGY, "total"),
    "workout_minutes": Metric("Workout", "训练时长", "分钟", "activity", "total", MINUTES),
    "heart_rate": quantity("HeartRate", "心率", "bpm", "heart", RATES),
    "hrv": quantity("HeartRateVariabilitySDNN", "心率变异性 SDNN", "ms", "heart", {"ms": 1, "s": 1000}),
    "vo2_max": quantity("VO2Max", "最大摄氧量", "mL/(kg·min)", "heart", {"mL/min·kg": 1, "mL/(kg*min)": 1}),
    "respiratory_rate": quantity("RespiratoryRate", "呼吸频率", "次/分钟", "respiratory", RATES),
    "oxygen_saturation": quantity("OxygenSaturation", "血氧饱和度", "%", "respiratory", PERCENT),
    "body_mass": quantity("BodyMass", "体重", "kg", "body", {"kg": 1, "g": 0.001, "lb": 0.45359237}),
    "body_fat": quantity("BodyFatPercentage", "体脂率", "%", "body", PERCENT),
}

METRIC_KEYS = {metric.identifier: key for key, metric in METRICS.items()}
GROUPS = {"activity": "活动与训练", "sleep": "睡眠", "heart": "心脏与恢复", "respiratory": "呼吸", "body": "身体测量"}
SLEEP_STATES = {
    "HKCategoryValueSleepAnalysisAsleep": "asleep",
    "HKCategoryValueSleepAnalysisAsleepUnspecified": "asleep",
    "HKCategoryValueSleepAnalysisAsleepCore": "core",
    "HKCategoryValueSleepAnalysisAsleepDeep": "deep",
    "HKCategoryValueSleepAnalysisAsleepREM": "rem",
    "HKCategoryValueSleepAnalysisAwake": "awake",
}
IN_BED = "HKCategoryValueSleepAnalysisInBed"
