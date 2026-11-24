"""Sleep stage, schedule and training summaries over accepted local records."""

from collections import Counter, defaultdict
from statistics import median, quantiles


STAGES = {"core": "核心睡眠", "deep": "深睡", "rem": "REM", "unclassified": "未分期"}
WORKOUT_LABELS = {
    "Walking": "步行", "Running": "跑步", "Cycling": "骑行", "Swimming": "游泳",
    "Hiking": "徒步", "Yoga": "瑜伽", "Pilates": "普拉提",
    "TraditionalStrengthTraining": "传统力量训练", "FunctionalStrengthTraining": "功能性力量训练",
    "HighIntensityIntervalTraining": "高强度间歇训练", "Elliptical": "椭圆机",
    "Rowing": "划船", "CoreTraining": "核心训练", "MixedCardio": "混合有氧",
    "StairClimbing": "爬楼", "Dance": "舞蹈", "MindAndBody": "身心训练", "Other": "其他运动",
    "Unknown": "未分类训练",
}


def night_sleep(samples):
    events = defaultdict(Counter)
    duration_only = False
    for sample in samples:
        state = sample["state"]
        if state == "duration_only":
            state = "asleep"
            duration_only = True
        events[sample["start_utc"]][state] += 1
        events[sample["end_utc"]][state] -= 1

    changes = sorted(events)
    active = Counter()
    seconds = dict.fromkeys(STAGES, 0.0)
    conflict = 0.0
    episodes = []
    for start, end in zip(changes, changes[1:]):
        active.update(events[start])
        if active["awake"] > 0:
            continue
        known_stages = [stage for stage in STAGES if stage != "unclassified" and active[stage] > 0]
        if not known_stages and active["asleep"] <= 0:
            continue
        elapsed = end - start
        if len(known_stages) == 1:
            seconds[known_stages[0]] += elapsed
        else:
            seconds["unclassified"] += elapsed
            if len(known_stages) > 1:
                conflict += elapsed
        # A sleep gap of up to 90 minutes can be part of the same main sleep episode.
        if episodes and start - episodes[-1][1] <= 90 * 60:
            left, _, previous = episodes[-1]
            episodes[-1] = (left, end, previous + elapsed)
        else:
            episodes.append((start, end, elapsed))

    if not episodes:
        return None
    offsets = {sample[key] for sample in samples for key in ("start_offset", "end_offset")}
    onset = end_time = None
    if len(offsets) == 1 and not duration_only:
        offset = next(iter(offsets))
        first, last, _ = max(episodes, key=lambda item: item[2])
        onset = (first / 60 + offset - 720) % 1440
        end_time = onset + (last - first) / 60
    return {
        "sleep_minutes": sum(seconds.values()) / 60,
        "stage_minutes": {stage: duration / 60 for stage, duration in seconds.items()},
        "conflict_minutes": conflict / 60,
        "main_start_from_noon": onset,
        "main_end_from_noon": end_time,
    }


def clock_label(minutes_from_noon):
    if minutes_from_noon is None:
        return None
    hours, minutes = divmod(round(minutes_from_noon + 720) % 1440, 60)
    return f"{hours:02}:{minutes:02}"


def spread(minutes):
    if len(minutes) < 4:
        return None
    lower, _, upper = quantiles(minutes, n=4, method="inclusive")
    return upper - lower


def summarize_sleep(nights):
    stages = {
        stage: sum(night["stage_minutes"][stage] for night in nights)
        for stage in STAGES
    }
    total = sum(stages.values())
    classified = total - stages["unclassified"]
    starts = [night["main_start_from_noon"] for night in nights if night["main_start_from_noon"] is not None]
    ends = [night["main_end_from_noon"] for night in nights if night["main_end_from_noon"] is not None]
    return {
        "sleep_minutes": total if nights else None,
        "stage_minutes": stages,
        "stage_coverage_percent": classified / total * 100 if total else None,
        "staged_nights": sum(night["sleep_minutes"] > night["stage_minutes"]["unclassified"] for night in nights),
        "conflict_minutes": sum(night["conflict_minutes"] for night in nights),
        "schedule": {
            "nights": len(starts),
            "main_start": clock_label(median(starts)) if starts else None,
            "main_end": clock_label(median(ends)) if ends else None,
            "start_iqr_minutes": spread(starts),
            "end_iqr_minutes": spread(ends),
        },
    }


def summarize_workouts(days):
    totals = defaultdict(lambda: {"sessions": 0, "minutes": 0.0})
    for day in days:
        for (_, _, _, minutes), states in day.items():
            known = states - {"Unknown"}
            code = next(iter(known)) if len(known) == 1 else "Unknown"
            totals[code]["sessions"] += 1
            totals[code]["minutes"] += minutes
    return {
        "sessions": sum(item["sessions"] for item in totals.values()),
        "by_type": [
            {"type": code, "label": WORKOUT_LABELS.get(code, code), **item}
            for code, item in sorted(totals.items(), key=lambda pair: (-pair[1]["minutes"], pair[0]))
        ],
    }
