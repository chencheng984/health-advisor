"""Build an offline workbench from aggregate Apple Health observations."""

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from html import escape
from math import ceil, floor, isfinite, log10
from pathlib import Path
from statistics import median
from string import Template

from analysis import analyze, date_range, format_number
from insights import clock_label, spread
from memory import next_review_note
from storage import HealthError, snapshot_info


GOALS = ("减脂", "增肌", "改善睡眠", "备赛", "维持")
GOAL_FIELDS = {"goal", "sleep_goal_hours", "steps_goal"}
SHORT_WINDOW = "所选趋势窗口少于 7 天，暂不计分。"
DIMENSIONS = {"sleep": "睡眠", "activity": "活动", "recovery": "恢复", "load": "负荷",
              "energy": "主观精力", "adherence": "依从性"}
HORIZONS = ("今天", "本周", "本月")
LAYERS = ("保命层", "恢复层", "成长层")
NO_WORKOUT = "无训练记录（可能是没戴表或器械训练未记录）"
URGENT_ACTION = "立即联系当地急救"


@dataclass(frozen=True)
class Field:
    label: str
    kind: str = "number"
    minimum: int = 0
    maximum: int | None = None


MAIN_FIELDS = {
    "sleep_hours": Field("睡眠中位数（小时）", maximum=24),
    "training_sessions": Field("训练次数（次）", "integer"),
    "training_minutes": Field("训练时长合计（分钟）"),
    "resting_hr": Field("静息心率中位数（bpm）", minimum=1),
    "energy": Field("主观精力（1–10，0 表示极低）", maximum=10),
    "stress": Field("压力（1–10）", minimum=1, maximum=10),
    "sedentary_hours": Field("日均久坐（小时）", maximum=24),
    "caffeine": Field("咖啡因（量和时间）", "text"),
    "alcohol": Field("酒精（量和时间）", "text"),
    "goal": Field("当前目标", "goal"),
    "weeks_remaining": Field("剩余周数（周）", "integer", maximum=520),
}
EXTRA_FIELDS = {
    "bedtime": Field("计划关灯时刻", "time"),
    "wake_time": Field("计划起床时刻", "time"),
    "sleep_goal_hours": Field("睡眠目标（小时，大于 4）", "number", 4, 24),
    "steps_goal": Field("步数目标（步/日）", "integer", 1),
    "energy_days": Field("同时段自评天数（天）", "integer", 0, 7),
    "load_low": Field("期前负荷下限（时长×RPE，大于 0）"),
    "load_high": Field("期前负荷上限（时长×RPE，大于 0）"),
    "plan_sessions": Field("期前已安排（次）", "integer"),
    "plan_completed": Field("已确认做完（次）", "integer"),
    "plan_unknown": Field("待核实（次）", "integer"),
}
FIELDS = {**MAIN_FIELDS, **EXTRA_FIELDS}
PROFILE_FLAGS = {
    "adult_reference", "sleep_schedule_consistent", "steps_coverage_confirmed", "workout_log_complete",
    "planned_rest", "plan_suspended", "urgent_symptoms", "_example", "demo_data",
}


def profile_field(name, field, value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if field.kind == "text" and not isinstance(value, bool):
        if isinstance(value, (int, float)):
            value = str(value)
        if isinstance(value, str):
            text = " ".join(value.split())
            if len(text) <= 120 and not any(0xD800 <= ord(char) <= 0xDFFF for char in text):
                return text
    if field.kind == "goal" and value in GOALS:
        return value
    if field.kind == "time" and isinstance(value, str) and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        return value
    if field.kind in {"number", "integer"} and isinstance(value, (int, float, str)) and not isinstance(value, bool):
        try:
            number = float(value)
        except (ValueError, OverflowError):
            number = float("nan")
        if (isfinite(number) and field.minimum <= number <= (field.maximum or 1_000_000_000)
                and (name != "sleep_goal_hours" or number > 4)
                and (name not in {"load_low", "load_high"} or number > 0)):
            if field.kind == "number" or number.is_integer():
                return int(number) if number.is_integer() else number
    raise HealthError(f"补充资料的「{field.label}」填写有误，请核对类型和范围。")


def validate_counts(values):
    low, high = values.get("load_low"), values.get("load_high")
    if low is not None and high is not None and low > high:
        return "期前负荷上限须大于或等于下限。"
    planned = values.get("plan_sessions")
    done, unknown = values.get("plan_completed"), values.get("plan_unknown")
    if planned is not None and ((done or 0) + (unknown or 0) > planned):
        return "完成和待核实的次数合计不能超过计划次数。"
    return ""


def reject_json_constant(value):
    raise ValueError(value)


def load_profile(path):
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=reject_json_constant)
    except (OSError, UnicodeError):
        raise HealthError("无法读取补充资料（--profile）。请检查文件路径、权限和 UTF-8 编码。") from None
    except (ValueError, RecursionError):
        raise HealthError("补充资料（--profile）不是有效的 JSON。请检查逗号、引号和括号。") from None
    if not isinstance(data, dict):
        raise HealthError("补充资料（--profile）须为 JSON 对象。")
    profile = {key: profile_field(key, field, data[key]) for key, field in FIELDS.items() if key in data}
    for key in sorted(PROFILE_FLAGS & data.keys()):
        if data[key] is not None and not isinstance(data[key], bool):
            raise HealthError(f"补充资料的 {key} 须填写 true、false 或 null。")
        profile[key] = data[key]
    if problem := validate_counts(profile):
        raise HealthError(f"补充资料填写有误。{problem}")
    workouts = data.get("workouts")
    if workouts is not None:
        if not isinstance(workouts, list) or any(not isinstance(row, dict) for row in workouts):
            raise HealthError("补充资料的 workouts 须为训练记录数组，每项可填 minutes 和 rpe。")
        profile["workouts"] = [
            (profile_field("minutes", Field("每节训练分钟数"), row.get("minutes")),
             profile_field("rpe", Field("每节训练的 RPE（0–10）", maximum=10), row.get("rpe")))
            for row in workouts
        ]
    return profile


def context_key(row):
    offsets = row["utc_offsets_minutes"]
    return (tuple(row["source_ids"]), offsets[0]) if len(offsets) == 1 else None


def comparable_group(rows, eligible):
    groups = defaultdict(list)
    for row in rows:
        key = context_key(row)
        if key is not None and eligible(row):
            groups[key].append(row)
    return max(groups.values(), key=len, default=[])


def sleep_context(item, profile, labels):
    rows = [] if item is None else item["daily"]
    same_schedule = profile.get("sleep_schedule_consistent") is not False
    comparable = comparable_group(rows, lambda row: same_schedule and row["sleep"]["main_end_from_noon"] is not None)
    starts = [row["sleep"]["main_start_from_noon"] for row in comparable]
    ends = [row["sleep"]["main_end_from_noon"] for row in comparable]
    return {
        "observed": len(rows), "n": len(comparable),
        "median": median(row["value"] for row in comparable) if comparable else None,
        "start": clock_label(median(starts)) if starts else None,
        "end": clock_label(median(ends)) if ends else None,
        "start_iqr": spread(starts), "end_iqr": spread(ends),
        "short": sum(row["value"] < 6 for row in comparable),
        "source": "、".join(labels[s] for s in comparable[0]["source_ids"]) if comparable else "",
    }


def activity_context(item, profile, sources):
    rows = [] if item is None else item["daily"]
    confirmed = profile.get("steps_coverage_confirmed")

    def eligible(row):
        return confirmed is True or (confirmed is None and any(sources[s]["kind"] == "watch" for s in row["source_ids"]))

    comparable = comparable_group(rows, eligible)
    return {
        "observed": len(rows), "n": len(comparable),
        "median": median(row["value"] for row in comparable) if comparable else None,
        "source": "、".join(sources[s]["label"] for s in comparable[0]["source_ids"]) if comparable else "",
    }


def paired_rows(hrv, heart, field):
    pairs = defaultdict(list)
    heart_by_date = {row["date"]: row for row in heart[field]}
    for left in hrv[field]:
        right = heart_by_date.get(left["date"])
        if right is None or min(left["value"], right["value"]) <= 0:
            continue
        left_key, right_key = context_key(left), context_key(right)
        if left_key is not None and right_key is not None and left_key[1] == right_key[1]:
            pairs[(left_key, right_key)].append((left["value"], right["value"]))
    return pairs


def recovery_context(metrics, labels):
    empty = {"n": 0, "baseline_n": 0, "hrv": None, "rhr": None,
             "baseline_hrv": None, "baseline_rhr": None, "source": ""}
    if not {"hrv", "resting_hr"} <= metrics.keys():
        return empty
    hrv, heart = metrics["hrv"], metrics["resting_hr"]
    current = paired_rows(hrv, heart, "daily")
    baseline = paired_rows(hrv, heart, "baseline_daily")
    if not current:
        return empty
    key = max(current, key=lambda item: (len(current[item]) >= 4 and len(baseline[item]) >= 7,
                                         len(current[item]), len(baseline[item])))
    now, before = current[key], baseline[key]
    return {
        "n": len(now), "baseline_n": len(before),
        "hrv": median(pair[0] for pair in now), "rhr": median(pair[1] for pair in now),
        "baseline_hrv": median([pair[0] for pair in before]) if before else None,
        "baseline_rhr": median([pair[1] for pair in before]) if before else None,
        "source": "、".join(labels[s] for s in sorted(set(key[0][0] + key[1][0]))),
    }


def training_context(profile):
    rows = profile.get("workouts")
    complete = profile.get("workout_log_complete") is True
    all_minutes = rows is not None and all(minutes is not None for minutes, _ in rows)
    total = sum(minutes for minutes, _ in rows) if all_minutes else None
    full = complete and all_minutes and all(rpe is not None for _, rpe in rows)
    return {"sessions": len(rows) if rows is not None else None, "minutes": total,
            "load": sum(minutes * rpe for minutes, rpe in rows) if full else None,
            "rest": profile.get("planned_rest") is True}


def initial_inputs(metrics, profile, sleep, recovery, training, *, example):
    workout = metrics.get("workout_minutes")
    workout_recent = workout["recent"] if workout else {}
    device = {
        "sleep_hours": sleep["median"] if sleep["n"] >= 4 else metrics.get("sleep", {}).get("recent", {}).get("median"),
        "resting_hr": recovery["rhr"] if recovery["n"] >= 4 and recovery["baseline_n"] >= 7
        else metrics.get("resting_hr", {}).get("recent", {}).get("median"),
        "training_sessions": workout["workout_details"]["recent"]["sessions"] if workout_recent.get("covered_days") else None,
        "training_minutes": workout_recent.get("period_total"),
    }
    initial = dict.fromkeys(FIELDS)
    origins = dict.fromkeys(FIELDS, "待填")
    for key, value in device.items():
        if value is not None and (key != "resting_hr" or value > 0):
            initial[key], origins[key] = value, "设备汇总"
    if not example and profile.get("workout_log_complete") is True:
        for key, column in (("training_sessions", "sessions"), ("training_minutes", "minutes")):
            if training[column] is not None:
                initial[key], origins[key] = training[column], "补充资料"
    for key in FIELDS.keys() & profile.keys():
        if profile[key] is None:
            continue
        initial[key] = profile[key]
        if example:
            origins[key] = "示例补充资料 · 待确认目标" if key in GOAL_FIELDS else "示例补充资料"
        else:
            origins[key] = "已确认目标" if key in GOAL_FIELDS else "补充资料"
    return initial, origins, device


def prepare_workbench(connection, metrics, start, end, days, baseline_days, preferred, profile, *, memory=None):
    connection.execute("BEGIN")
    snapshot = snapshot_info(connection)
    observations = analyze(connection, metrics, start, end, days, baseline_days, preferred, include_daily=True)
    window = observations["report_period"]
    first, last = date.fromisoformat(window["from"]), date.fromisoformat(window["to"])
    score_period = None
    score_metrics = observations["metrics"]
    if (last - first).days >= 6:
        # The trend can include a partial today. The scores always use a closed week.
        last_completed = min(last, date.today() - timedelta(days=1))
        score_start = last_completed - timedelta(days=6)
        score = analyze(connection, [key for key in metrics if key in {
            "sleep", "steps", "resting_hr", "hrv", "workout_minutes",
        }], score_start, last_completed, 7, 90, preferred, include_daily_context=True)
        score_period = score["report_period"]
        score_metrics = score["metrics"]
    sources = {row["id"]: row for row in snapshot["sources"]}
    labels = {key: value["label"] for key, value in sources.items()}
    example = profile.get("_example") is True or profile.get("demo_data") is True
    confirmed = {} if example else profile
    context_metrics = score_metrics if score_period else {}
    sleep = sleep_context(context_metrics.get("sleep"), confirmed, labels)
    steps = activity_context(context_metrics.get("steps"), confirmed, sources)
    recovery = recovery_context(context_metrics, labels)
    training = training_context(confirmed)
    initial, origins, device = initial_inputs(score_metrics, profile, sleep, recovery, training, example=example)
    model = {
        "initial": initial, "device": device, "score_period": score_period, "sleep": sleep, "steps": steps,
        "recovery": recovery, "training": training, "suspended": confirmed.get("plan_suspended") is True,
        "adult": confirmed.get("adult_reference") is True, "urgent": confirmed.get("urgent_symptoms") is True,
        "selected": list(metrics), "goal_actions": GOAL_ACTIONS, "goal_fields": sorted(GOAL_FIELDS),
        "example": example, "short_window": SHORT_WINDOW,
    }
    return render_workbench(observations, snapshot, model, origins, memory=memory)


def clamp(value):
    return max(0, min(1, value))


def card(state, note, value=None):
    return {"state": state, "note": note, "value": floor(value + .5) if value is not None else None}


def score_sleep(model, values):
    sleep = model["sleep"]
    value, goal = values["sleep_hours"], values["sleep_goal_hours"]
    observation = f"睡眠中位数 {format_number(value)} 小时。" if value is not None else ""
    if sleep["n"] < 4 or value is None:
        return card("数据不足", observation + f"同口径主睡眠时刻 {sleep['n']}/7 夜，需至少 4 夜。")
    schedule = (f"主睡眠通常为 {sleep['start']} 至 {sleep['end']}（{sleep['n']}/7 夜，{sleep['source']}）。"
                f"入睡和结束时刻中间一半的跨度为 {format_number(sleep['start_iqr'])} 和 "
                f"{format_number(sleep['end_iqr'])} 分钟。")
    if goal is None:
        return card("待确认目标", observation + schedule)
    relation = "低于" if value < goal else "已达到"
    target = "示例目标" if model["example"] else "目标"
    note = f"睡眠中位数 {format_number(value)} 小时，{relation} {format_number(goal)} 小时的{target}。" + schedule
    state = "待确认目标" if model["example"] else "睡眠与作息"
    return card(state, note, 100 * (.7 * clamp((value - 4) / (goal - 4))
                                   + .3 * clamp((120 - sleep["end_iqr"]) / 90)))


def score_activity(model, values):
    steps = model["steps"]
    if steps["n"] < 4:
        return card("数据不足", f"有 {steps['observed']}/7 天步数记录，同口径可用 {steps['n']}/7 天。请核对佩戴情况。")
    note = f"步数中位数 {format_number(steps['median'])} 步（{steps['n']}/7 天，{steps['source']}）。"
    goal = values["steps_goal"]
    if goal is None:
        return card("待确认目标", note)
    state = "待确认目标" if model["example"] else "步数与目标"
    target = "每日示例目标" if model["example"] else "每日目标"
    return card(state, note + f"{target} {format_number(goal)} 步。", 100 * clamp(steps["median"] / goal))


def score_recovery(model, values):
    pair = model["recovery"]
    rhr = values["resting_hr"]
    if pair["n"] < 4 or pair["baseline_n"] < 7 or rhr is None:
        return card("数据不足", f"本期同口径配对 {pair['n']}/7 天，90 日基线配对 {pair['baseline_n']}/90 天。"
                    "至少需要本期 4 天、基线 7 天同时有静息心率和 HRV。")
    h = abs(pair["hrv"] - pair["baseline_hrv"]) / pair["baseline_hrv"]
    r = abs(rhr - pair["baseline_rhr"])
    note = (f"HRV 从 {format_number(pair['baseline_hrv'])} 到 {format_number(pair['hrv'])} ms，静息心率从 "
            f"{format_number(pair['baseline_rhr'])} 到 {format_number(rhr)} bpm。"
            f"同口径配对 {pair['n']}/7 天、基线 {pair['baseline_n']}/90 天（{pair['source']}）。")
    return card("和个人基线相比", note, 100 * (1 - .5 * clamp((h - .1) / .2) - .5 * clamp((r - 3) / 7)))


def score_load(model, values):
    if model["example"]:
        return card("数据不足，待补充", "实际负荷需补齐当周每节训练的时长与整堂用力感（RPE），并确认期前计划区间。")
    training = model["training"]
    if training["rest"]:
        return card("不适用", "补充资料标记了计划休息周。")
    sessions, minutes = values["training_sessions"], values["training_minutes"]
    if training["load"] is None or sessions != training["sessions"] or minutes is None or (
        abs(minutes - training["minutes"]) > .001
    ):
        return card("数据不足，待补充", "请核对当周训练是否都已登记，并补齐每节训练的时长与整堂用力感（RPE）。")
    load, low, high = training["load"], values["load_low"], values["load_high"]
    note = f"补充资料确认 {sessions} 次训练，周负荷为 {format_number(load)}。"
    if low is None or high is None:
        return card("待确认目标", note)
    note += f"计划区间 {format_number(low)}–{format_number(high)}。"
    if load < low:
        return card("低于计划", note + f"距下沿 {format_number(low - load)}。", 100 * clamp(load / low))
    if load > high:
        return card("高于计划", note + f"超上沿 {format_number(load - high)}。", 100 * clamp((2 * high - load) / high))
    return card("在计划内", note, 100)


def score_energy(model, values):
    energy, days = values["energy"], values["energy_days"]
    if energy is None or days is None or days < 4:
        return card("数据不足", "补记一周中至少 4 天的同时段精力自评，再填中位数和记录天数。")
    subject = "示例自评覆盖" if model["example"] else "你在同一时段记了"
    return card("精力自评", f"{subject} {days}/7 天，精力中位数 {format_number(energy)}/10。", 10 * energy)


def score_adherence(model, values):
    if model["example"]:
        return card("待确认计划", "请补记该周开始前计划的次数与实际完成情况。")
    planned, done, unknown = (values[key] for key in ("plan_sessions", "plan_completed", "plan_unknown"))
    if model["suspended"] or not planned:
        return card("不适用", "本周计划已暂停。" if model["suspended"] else "本周未填写期前已确认的计划。")
    note = f"期前计划 {planned} 次。"
    if done is None:
        return card("数据不足", note + "请补记确认完成次数。")
    note += f"已确认完成 {done}/{planned} 次。"
    if unknown or (unknown is None and done < planned):
        return card("数据不足", note + (f"另有 {unknown} 次待核实。" if unknown else "请补记待核实次数。"))
    return card("计划完成情况", note, 100 * done / planned)


SCORERS = (score_sleep, score_activity, score_recovery, score_load, score_energy, score_adherence)


def score_cards(model, values):
    required = {"sleep": {"sleep"}, "activity": {"steps"}, "recovery": {"hrv", "resting_hr"}}
    selected = set(model["selected"])
    scores = {}
    for key, compute in zip(DIMENSIONS, SCORERS):
        if model["score_period"] is None:
            item = card("数据不足", SHORT_WINDOW)
        elif not required.get(key, set()) <= selected:
            item = card("指标未选", "生成页面时可用 --metric 选入对应指标。")
        else:
            item = compute(model, values)
        if model["example"]:
            item["state"] = f"示例试算 · {item['state']}"
        scores[key] = item
    return scores


GOAL_ACTIONS = {
    "改善睡眠": "挑一个容易坚持的关灯或起床时刻，连续记 7 天。",
    "减脂": "如果你愿意跟踪体重，挑 4 个早晨按相同条件记录，月底看同口径周中位数。",
    "增肌": "挑 1 次已有的力量训练，记下动作、用力感和次日恢复，再决定下月如何安排。",
    "备赛": "挑 1 次熟悉的专项训练，记下表现和用力感，再用同类训练复核。",
    "维持": "选一项已经做得顺手的习惯，复盘时核对一周完成了几次。",
    "": "选一项这周能执行的目标，记录计划和完成情况。",
}


def action_plan(model, values, urgent=False):
    if model["urgent"] or urgent:
        detail = "目前的症状需要立即评估。请身边的人帮忙联系当地急救，避免自行驾车。"
        followup = "先完成急症评估，后续安排依医疗专业人员的指引。"
        return {"urgent": True, "title": URGENT_ACTION, "reason": detail,
                "layers": [("优先求助", detail), ("等医疗评估", followup), ("等医疗评估", followup)],
                "actions": [(URGENT_ACTION, detail), ("按医疗指引复核", followup), ("评估后再安排", followup)]}
    example = model["example"]
    sleep, pair = model["sleep"], model["recovery"]
    s, goal = values["sleep_hours"], values["sleep_goal_hours"]
    energy, stress, rhr = values["energy"], values["stress"], values["resting_hr"]
    short = model["adult"] and sleep["n"] >= 4 and sleep["short"] >= 3
    below = s is not None and goal is not None and s < goal
    rhythm = sleep["end_iqr"] is not None and sleep["end_iqr"] > 60
    recovery_change = pair["n"] >= 4 and pair["baseline_n"] >= 7 and (
        (rhr is not None and rhr - pair["baseline_rhr"] >= 5) or (pair["hrv"] / pair["baseline_hrv"] <= .8)
    )
    strain = (energy is not None and energy <= 4) or (stress is not None and stress >= 8)
    sleep_step = "试 7 天给睡眠多留 30 分钟，记实际关灯、起床和白天困倦。"
    safety = ("先核对并补睡眠机会", sleep_step) if short else (
        "按当下症状判断", "如果出现持续胸痛或严重气短，请立即联系当地急救。")
    recover = ("先复核恢复", "未来 7 天保持同一测量条件，记起床时刻、白天精力，留出一段休息时间。")
    if not (recovery_change and (short or below or strain)):
        recover = ("复盘作息与精力", sleep_step if below else "连续 7 天在同一时段记精力，并记实际起床时刻。")
    if values["caffeine"] or values["alcohol"]:
        recover = (recover[0], recover[1] + "把最后一杯的时间记在同一天。")
    if values["bedtime"] or values["wake_time"]:
        recover = (recover[0], recover[1] + f"按关灯 {values['bedtime'] or '待填'}、起床 {values['wake_time'] or '待填'} 的计划核对。")
    goal_name = values["goal"] or ""
    weeks = values["weeks_remaining"]
    growth = GOAL_ACTIONS[goal_name]
    growth_title = f"示例目标 · {goal_name}" if example and goal_name else (goal_name or "目标待确认")
    monthly_title = f"复核{goal_name}目标" if goal_name else "先选一个目标"
    deadline = "" if weeks is None else ("目标期已到，先用现有记录复盘。" if weeks == 0 else f"按剩余 {weeks} 周安排复核。")
    if short or below:
        title = "今晚先给睡眠多留半小时"
        goal_label = "示例目标" if example else "目标"
        reason = (f"近 7 日有 {sleep['short']} 个同口径记录夜少于 6 小时，先核对这些夜晚的实际睡眠。" if short
                  else f"睡眠中位数 {format_number(s)} 小时，距 {format_number(goal)} 小时的{goal_label}还有 "
                       f"{format_number((goal - s) * 60)} 分钟。")
    elif recovery_change and strain:
        title = "先留出一段可以休息的时间"
        condition = "试算采用了偏低的精力或较高的压力。" if example else "同期也填了精力偏低或压力较高。"
        reason = f"静息心率或 HRV 相对基线有变化，{condition}"
    elif rhythm:
        title, reason = "这周先选一个起床时刻", f"起床端中间一半记录相差 {format_number(sleep['end_iqr'])} 分钟。"
    elif values["sedentary_hours"] is not None and values["sedentary_hours"] >= 6:
        sitting = "示例日均久坐" if example else "你填的日均久坐"
        title, reason = "今天挑一段久坐时间起来走走", f"{sitting}为 {format_number(values['sedentary_hours'])} 小时。先试一次饭后走 10 分钟。"
    elif strain:
        title, reason = "今天先留一个 10 分钟的休息时段", "记一下休息前后的精力，连续观察一周。"
    else:
        title, reason = "今天先核对这一周的记录", "先确认睡眠、训练和精力有哪些缺测；有记录的项目沿用相同口径复盘。"
    if (values["training_sessions"], values["training_minutes"]) != (None, None):
        growth += (f" 当前计入 {format_number(values['training_sessions'])} 次、"
                   f"{format_number(values['training_minutes'])} 分钟，复核时补齐每节的 RPE。")
    if example:
        title = f"示例行动 · {title}"
        monthly_title = f"示例行动 · {monthly_title}"
        recover = (f"示例复核 · {recover[0]}", recover[1])
    return {"urgent": False, "title": title, "reason": reason,
            "layers": [safety, recover, (growth_title, growth)],
            "actions": [(title, reason), recover, (monthly_title, growth + deadline)]}


def render_field(name, field, value, origin, observed):
    identifier = f"field-{name}"
    text = "" if value is None else escape(str(value))
    if field.kind == "goal":
        options = "".join(f'<option value="{option}"{" selected" if option == (value or "") else ""}>{option or "待确认"}</option>'
                          for option in ("", *GOALS))
        control = f'<select id="{identifier}" name="{name}">{options}</select>'
    else:
        kind = field.kind if field.kind in {"text", "time"} else "number"
        attrs = ' maxlength="120"' if kind == "text" else ""
        if kind == "number":
            attrs = f' min="{field.minimum}" max="{field.maximum or 1_000_000_000}" step="{"1" if field.kind == "integer" else "any"}"'
        control = f'<input type="{kind}" id="{identifier}" name="{name}" value="{text}"{attrs}>'
    if observed is not None and value != observed and value is not None:
        source = f"{origin} · 设备汇总 {format_number(observed)}"
    else:
        source = origin
    return (f'<div class="field"><label for="{identifier}">{field.label}</label>{control}'
            f'<small id="source-{name}" data-initial="{escape(origin)}">{escape(source)}</small></div>')


def render_cards(scores, *, example):
    cards = []
    for key, label in DIMENSIONS.items():
        item = scores[key]
        number = item["value"]
        value = "—" if number is None else str(number)
        hidden = " hidden" if number is None else ""
        rating = "示例试算" if example else "评分"
        cards.append(f'''<article class="score" id="score-{key}">
  <div class="score-head"><h3>{label}</h3><span class="score-value">{value}</span></div>
  <meter min="0" max="100" value="{number or 0}" aria-label="{label}{rating}"{hidden}></meter>
  <p class="score-state">{escape(item['state'])}</p><p class="score-note">{escape(item['note'])}</p>
</article>''')
    return "\n".join(cards)


def render_plan_rows(labels, rows, cls):
    return "\n".join(f'<li><span>{label}</span><div><h3 class="{cls}-title">{escape(title)}</h3>'
                     f'<p class="{cls}-note">{escape(note)}</p></div></li>'
                     for label, (title, note) in zip(labels, rows))


def describe(value, unit):
    return "无记录" if value is None else f"{format_number(value)} {unit}"


def period_cell(period, unit):
    summary = describe(period["median"], unit)
    sources = "、".join(source["label"] for source in period["sources"])
    small = f"{period['covered_days']}/{period['calendar_days']} 天"
    if sources:
        small += f" · {sources} · 至 {period['last_observation']}"
    return f'{escape(summary)}<small>{escape(small)}</small>'


def comparison_note(item, inventory):
    recent, before = item["recent"], item["baseline"]
    state = item["comparison"]["state"]
    if state == "no_recent_data":
        last = inventory.get("last_indexed_date")
        return f"缓存该项最新日期 {last}" if last else "缓存该项无可用记录"
    if before["sources"] and recent["sources"] != before["sources"]:
        return "来源有变化，差值仅供参考"
    if len(set(recent["utc_offsets_minutes"] + before["utc_offsets_minutes"])) > 1:
        return "记录时区偏移有变化，暂缓比较"
    if state == "comparable":
        unit = "百分点" if item["unit"] == "%" else item["unit"]
        return f"{format_number(item['comparison']['difference'], signed=True)} {unit}"
    return "本期或对照期覆盖不足"


def render_evidence(evidence, inventory):
    indexed = {row["metric"]: row for row in inventory}
    rows = []
    for key, item in evidence["metrics"].items():
        label, unit = escape(item["label"]), item["unit"]
        note = escape(comparison_note(item, indexed.get(key, {})))
        current = period_cell(item["recent"], unit)
        if key == "workout_minutes":
            if item["recent"]["covered_days"]:
                current += f"<small>共 {item['workout_details']['recent']['sessions']} 次、{describe(item['recent']['period_total'], unit)}</small>"
            else:
                current = f"{NO_WORKOUT}<small>0/{item['recent']['calendar_days']} 天</small>"
        rows.append(f'<tr><th scope="row">{label}</th><td>{current}</td>'
                    f'<td>{period_cell(item["baseline"], unit)}</td><td>{note}</td></tr>')
    return "\n".join(rows)


def unavailable_row(model):
    selected, workout = model["selected"], model["device"]["training_sessions"]
    if "workout_minutes" in selected and workout is None:
        return NO_WORKOUT
    return "训练与 Apple 运动时间分别记录。"


CHART_TARGETS = {"sleep": "sleep_goal_hours", "steps": "steps_goal"}
PLOT = {"left": 54, "right": 394, "top": 22, "bottom": 134}


def chart_height(value, limit):
    return PLOT["bottom"] - value / limit * (PLOT["bottom"] - PLOT["top"])


def chart_limit(points, target):
    peak = max(1, target or 0, *[value for _, value in points])
    quantum = 10 ** floor(log10(peak) - .3)
    return ceil(peak / quantum) * quantum


def chart_path(points, days, limit):
    parts, last = [], -2
    for day, value in points:
        left = PLOT["left"] + (day + .5) / days * (PLOT["right"] - PLOT["left"])
        top = chart_height(value, limit)
        parts.append(f"{'L' if day == last + 1 else 'M'}{left:.2f} {top:.2f}")
        last = day
    return " ".join(parts)


def render_chart(key, item, window, initial, *, example):
    first = date.fromisoformat(window["from"])
    days = (date.fromisoformat(window["to"]) - first).days + 1
    points = [((date.fromisoformat(row["date"]) - first).days, row["value"]) for row in item["daily"]]
    target_field = CHART_TARGETS.get(key)
    target = initial[target_field] if target_field else None
    limit = chart_limit(points, target)
    width = PLOT["right"] - PLOT["left"]
    gaps, labels, dots = [], [], []
    last = -1
    for index, _ in [*points, (days, None)]:
        if index > last + 1:
            left = PLOT["left"] + (last + 1) / days * width
            column = (index - last - 1) / days * width
            gaps.append(f'<rect class="gap" x="{left:.2f}" y="22" width="{column:.2f}" height="112"/>')
            start, end = first + timedelta(days=last + 1), first + timedelta(days=index - 1)
            labels.append(start.isoformat() if start == end else f"{start} 至 {end}")
        last = index
    for index, value in points:
        x = PLOT["left"] + (index + .5) / days * width
        day = first + timedelta(days=index)
        dots.append(f'<circle cx="{x:.2f}" cy="{chart_height(value, limit):.2f}" r="{2.8 if len(points) <= 45 else 1.3}"'
                    f' data-index="{index}" data-value="{value}"><title>{day} · {escape(describe(value, item["unit"]))}</title></circle>')
    ticks = []
    for i in sorted({0, (days - 1) // 2, days - 1}):
        x = PLOT["left"] + (i + .5) / days * width
        ticks.append(f'<text x="{x:.2f}" y="156" text-anchor="middle">{(first + timedelta(days=i)).strftime("%m-%d")}</text>')
    title = escape(f"{item['label']}（{item['unit']}）")
    missing = f"缺测 {days - len(points)} 天" if labels else "每天都有可用记录"
    missing_details = f'<details><summary>{missing} · 查看日期</summary><p>{"、".join(labels)}</p></details>' if labels else f"<p>{missing}</p>"
    target_y = chart_height(target, limit) if target is not None else 0
    target_label = "示例目标线" if example else "目标线"
    target_caption = f"{target_label} {describe(target, item['unit'])}" if target is not None else ""
    return f'''<figure class="chart" data-field="{target_field or ''}" data-limit="{chart_limit(points, None)}" data-unit="{escape(item['unit'])}">
  <figcaption><h3>{title}</h3><span>{len(points)}/{days} 天</span></figcaption>
  <svg viewBox="0 0 412 165" role="img" aria-label="{title}趋势图，{missing}">
    <title>{title}逐日汇总。阴影表示缺测。</title>
    {''.join(gaps)}
    <path class="gridline" d="M54 22H394 M54 78H394 M54 134H394"/>
    <text x="46" y="26" text-anchor="end" class="upper">{format_number(limit)}</text>
    <text x="46" y="82" text-anchor="end" class="middle">{format_number(limit / 2)}</text>
    <text x="46" y="138" text-anchor="end">0</text>
    <line class="target" x1="54" x2="394" y1="{target_y:.2f}" y2="{target_y:.2f}" visibility="{'visible' if target is not None else 'hidden'}"/>
    <path class="trace" d="{chart_path(points, days, limit)}"/>
    {''.join(dots)}
    {''.join(ticks)}
  </svg>
  <p class="target-caption">{escape(target_caption)}</p>
  {missing_details}
</figure>'''


def json_script(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).replace("<", "\\u003c")


def render_workbench(evidence, snapshot, model, origins, *, memory=None):
    review = ""
    if memory and memory["one_line"]:
        review = ('<section class="panel" aria-labelledby="review-title">'
                  '<h2 id="review-title">上次结论与本次核对</h2>'
                  f'<p>上次结论为「{escape(memory["one_line"])}」。</p>'
                  f'<p>{escape(next_review_note(memory))}</p></section>')
    observations, inventory = evidence["metrics"], snapshot["inventory"]
    example = model["example"]
    scores = score_cards(model, model["initial"])
    plan = action_plan(model, model["initial"])
    data_end = snapshot["last_indexed_date"] or "无可用日期"
    compare_label = "上一等长时段" if evidence["comparison_window"] == "previous_period" else "所选基线"
    score_window = date_range(model["score_period"]) if model["score_period"] else SHORT_WINDOW
    input_status = "示例试算" if example else "生成时的记录和参数"
    window = evidence["report_period"]
    charts = [key for key in ("sleep", "steps", "resting_hr") if key in observations]
    charts.extend(key for key in observations if key not in charts)
    unused = [f'<li><code>{escape(item["identifier"])}</code><span>{item["records_seen"]:,} 条</span></li>'
              for item in inventory if item["status"] == "inventory_only"]
    controls_urgent = ' checked disabled' if model["urgent"] else ''

    def fields(spec):
        return "\n".join(render_field(key, field, model["initial"][key], origins[key], model["device"].get(key))
                         for key, field in spec.items())

    folder = Path(__file__).resolve().parent
    page = Template((folder / "workbench_page.html").read_text(encoding="utf-8"))
    return page.substitute(
        window=escape(date_range(window)), end=escape(data_end), score_window=escape(score_window),
        input_status=input_status, review=review,
        input_window_note="补充资料按评分窗口填写。" if model["score_period"] else "",
        demo_note='<p class="muted">本页含示例补充资料。示例目标待确认，评分和行动仅供试算。</p>' if example else "",
        compare_window=escape(f"{compare_label} · {date_range(evidence['baseline_period'])}"),
        urgent_class=" urgent" if plan["urgent"] else "", hero_title=escape(plan["title"]), hero_note=escape(plan["reason"]),
        urgent_attrs=controls_urgent, main_fields=fields(MAIN_FIELDS), extra_fields=fields(EXTRA_FIELDS),
        workout_note=escape(unavailable_row(model)), cards=render_cards(scores, example=example),
        layers=render_plan_rows(LAYERS, plan["layers"], "layer"),
        actions=render_plan_rows(HORIZONS, plan["actions"], "action"),
        evidence=render_evidence(evidence, inventory),
        charts="\n".join(render_chart(key, observations[key], window, model["initial"], example=example) for key in charts),
        inventory_count=len(unused), inventory="\n".join(unused) if unused else '<li>没有仅盘点的记录类型。</li>',
        model=json_script(model), script=(folder / "workbench.js").read_text(encoding="utf-8"),
    )


def write_workbench(output, html):
    # Exclusive creation also protects the output from concurrent invocations.
    stream = output.open("x", encoding="utf-8", newline="\n")
    try:
        with stream:
            stream.write(html)
    except (OSError, UnicodeError):
        output.unlink(missing_ok=True)
        raise
