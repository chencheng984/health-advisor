"use strict";

(() => {
  const model = JSON.parse(document.getElementById("workbench-data").textContent);
  const example = model.example;
  const form = document.getElementById("inputs");
  const symptom = form.elements.namedItem("urgent_symptoms");
  const controls = Object.keys(model.initial).map(name => form.elements.namedItem(name));
  const error = document.getElementById("input-error");
  const numberFormat = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
  let applied = model.initial;

  const clamp = value => Math.max(0, Math.min(1, value));
  const num = value => value == null ? "—" : numberFormat.format(value);
  const card = (state, note, value = null) => ({ state, note, value: value === null ? null : Math.round(value) });

  function sleepScore(p) {
    const s = model.sleep;
    const value = p.sleep_hours;
    const goal = p.sleep_goal_hours;
    const observation = value === null ? "" : `睡眠中位数 ${num(value)} 小时。`;
    if (s.n < 4 || value === null) return card("数据不足", `${observation}同口径主睡眠时刻 ${s.n}/7 夜，需至少 4 夜。`);
    const schedule = `主睡眠通常为 ${s.start} 至 ${s.end}（${s.n}/7 夜，${s.source}）。` +
      `入睡和结束时刻中间一半的跨度为 ${num(s.start_iqr)} 和 ${num(s.end_iqr)} 分钟。`;
    if (goal === null) return card("待确认目标", observation + schedule);
    const target = example ? "示例目标" : "目标";
    const state = example ? "待确认目标" : "睡眠与作息";
    const note = `睡眠中位数 ${num(value)} 小时，${value < goal ? "低于" : "已达到"} ${num(goal)} 小时的${target}。`;
    return card(state, note + schedule,
      100 * (.7 * clamp((value - 4) / (goal - 4)) + .3 * clamp((120 - s.end_iqr) / 90)));
  }

  function activityScore(p) {
    const s = model.steps;
    if (s.n < 4) return card("数据不足", `有 ${s.observed}/7 天步数记录，同口径可用 ${s.n}/7 天。请核对佩戴情况。`);
    const note = `步数中位数 ${num(s.median)} 步（${s.n}/7 天，${s.source}）。`;
    if (p.steps_goal === null) return card("待确认目标", note);
    const target = example ? "每日示例目标" : "每日目标";
    const state = example ? "待确认目标" : "步数与目标";
    return card(state, note + `${target} ${num(p.steps_goal)} 步。`, 100 * clamp(s.median / p.steps_goal));
  }

  function recoveryScore(p) {
    const s = model.recovery;
    if (s.n < 4 || s.baseline_n < 7 || p.resting_hr === null) {
      return card("数据不足", `本期同口径配对 ${s.n}/7 天，90 日基线配对 ${s.baseline_n}/90 天。` +
        "至少需要本期 4 天、基线 7 天同时有静息心率和 HRV。");
    }
    const h = Math.abs(s.hrv - s.baseline_hrv) / s.baseline_hrv;
    const r = Math.abs(p.resting_hr - s.baseline_rhr);
    const note = `HRV 从 ${num(s.baseline_hrv)} 到 ${num(s.hrv)} ms，静息心率从 ${num(s.baseline_rhr)} 到 ${num(p.resting_hr)} bpm。` +
      `同口径配对 ${s.n}/7 天、基线 ${s.baseline_n}/90 天（${s.source}）。`;
    return card("和个人基线相比", note, 100 * (1 - .5 * clamp((h - .1) / .2) - .5 * clamp((r - 3) / 7)));
  }

  function loadScore(p) {
    if (example) return card("数据不足，待补充", "实际负荷需补齐当周每节训练的时长与整堂用力感（RPE），并确认期前计划区间。");
    const t = model.training;
    if (t.rest) return card("不适用", "补充资料标记了计划休息周。");
    if (t.load === null || p.training_sessions !== t.sessions || p.training_minutes === null ||
        Math.abs(p.training_minutes - t.minutes) > .001) {
      return card("数据不足，待补充", "请核对当周训练是否都已登记，并补齐每节训练的时长与整堂用力感（RPE）。");
    }
    const low = p.load_low;
    const high = p.load_high;
    let note = `补充资料确认 ${p.training_sessions} 次训练，周负荷为 ${num(t.load)}。`;
    if (low === null || high === null) return card("待确认目标", note);
    note += `计划区间 ${num(low)}–${num(high)}。`;
    if (t.load < low) return card("低于计划", note + `距下沿 ${num(low - t.load)}。`, 100 * clamp(t.load / low));
    if (t.load > high) return card("高于计划", note + `超上沿 ${num(t.load - high)}。`, 100 * clamp((2 * high - t.load) / high));
    return card("在计划内", note, 100);
  }

  function energyScore(p) {
    if (p.energy === null || p.energy_days === null || p.energy_days < 4) {
      return card("数据不足", "补记一周中至少 4 天的同时段精力自评，再填中位数和记录天数。");
    }
    const subject = example ? "示例自评覆盖" : "你在同一时段记了";
    return card("精力自评", `${subject} ${p.energy_days}/7 天，精力中位数 ${num(p.energy)}/10。`, 10 * p.energy);
  }

  function adherenceScore(p) {
    if (example) return card("待确认计划", "请补记该周开始前计划的次数与实际完成情况。");
    const { plan_sessions: planned, plan_completed: done, plan_unknown: unknown } = p;
    if (model.suspended || !planned) {
      return card("不适用", model.suspended ? "本周计划已暂停。" : "本周未填写期前已确认的计划。");
    }
    let note = `期前计划 ${planned} 次。`;
    if (done === null) return card("数据不足", note + "请补记确认完成次数。");
    note += `已确认完成 ${done}/${planned} 次。`;
    if (unknown || (unknown === null && done < planned)) {
      return card("数据不足", note + (unknown ? `另有 ${unknown} 次待核实。` : "请补记待核实次数。"));
    }
    return card("计划完成情况", note, 100 * done / planned);
  }

  function updateScores(p) {
    const scores = [
      ["sleep", sleepScore, ["sleep"]], ["activity", activityScore, ["steps"]],
      ["recovery", recoveryScore, ["resting_hr", "hrv"]], ["load", loadScore, []],
      ["energy", energyScore, []], ["adherence", adherenceScore, []],
    ];
    for (const [key, compute, required] of scores) {
      let item;
      if (model.score_period === null) item = card("数据不足", model.short_window);
      else if (required.some(metric => !model.selected.includes(metric))) {
        item = card("指标未选", "生成页面时可用 --metric 选入对应指标。");
      } else item = compute(p);
      const node = document.getElementById(`score-${key}`);
      node.querySelector(".score-value").textContent = item.value ?? "—";
      node.querySelector(".score-state").textContent = example ? `示例试算 · ${item.state}` : item.state;
      node.querySelector(".score-note").textContent = item.note;
      const meter = node.querySelector("meter");
      meter.hidden = item.value === null;
      meter.value = item.value ?? 0;
    }
  }

  function actionPlan(p) {
    if (model.urgent || symptom.checked) {
      const title = "立即联系当地急救";
      const reason = "目前的症状需要立即评估。请身边的人帮忙联系当地急救，避免自行驾车。";
      const followup = "先完成急症评估，后续安排依医疗专业人员的指引。";
      return { urgent: true, title, reason,
        layers: [["优先求助", reason], ["等医疗评估", followup], ["等医疗评估", followup]],
        actions: [[title, reason], ["按医疗指引复核", followup], ["评估后再安排", followup]] };
    }
    const sleep = model.sleep;
    const pair = model.recovery;
    const s = p.sleep_hours;
    const goal = p.sleep_goal_hours;
    const short = model.adult && sleep.n >= 4 && sleep.short >= 3;
    const below = s !== null && goal !== null && s < goal;
    const rhythm = sleep.end_iqr !== null && sleep.end_iqr > 60;
    const rhr = p.resting_hr;
    const change = pair.n >= 4 && pair.baseline_n >= 7 &&
      ((rhr !== null && rhr - pair.baseline_rhr >= 5) || pair.hrv / pair.baseline_hrv <= .8);
    const strain = (p.energy !== null && p.energy <= 4) || (p.stress !== null && p.stress >= 8);
    const sleepStep = "试 7 天给睡眠多留 30 分钟，记实际关灯、起床和白天困倦。";
    const safety = short ? ["先核对并补睡眠机会", sleepStep] :
      ["按当下症状判断", "如果出现持续胸痛或严重气短，请立即联系当地急救。"];
    const recover = change && (short || below || strain) ?
      ["先复核恢复", "未来 7 天保持同一测量条件，记起床时刻、白天精力，留出一段休息时间。"] :
      ["复盘作息与精力", below ? sleepStep : "连续 7 天在同一时段记精力，并记实际起床时刻。"];
    if (p.caffeine || p.alcohol) recover[1] += "把最后一杯的时间记在同一天。";
    if (p.bedtime || p.wake_time) recover[1] += `按关灯 ${p.bedtime ?? "待填"}、起床 ${p.wake_time ?? "待填"} 的计划核对。`;
    const goalName = p.goal || "";
    const weeks = p.weeks_remaining;
    let growth = model.goal_actions[goalName];
    const growthTitle = example && goalName ? `示例目标 · ${goalName}` : (goalName || "目标待确认");
    let monthlyTitle = goalName ? `复核${goalName}目标` : "先选一个目标";
    const deadline = weeks === null ? "" : (weeks === 0 ? "目标期已到，先用现有记录复盘。" : `按剩余 ${weeks} 周安排复核。`);
    let title = "今天先核对这一周的记录";
    let reason = "先确认睡眠、训练和精力有哪些缺测；有记录的项目沿用相同口径复盘。";
    if (short || below) {
      title = "今晚先给睡眠多留半小时";
      const goalLabel = example ? "示例目标" : "目标";
      reason = short ? `近 7 日有 ${sleep.short} 个同口径记录夜少于 6 小时，先核对这些夜晚的实际睡眠。` :
        `睡眠中位数 ${num(s)} 小时，距 ${num(goal)} 小时的${goalLabel}还有 ${num((goal - s) * 60)} 分钟。`;
    } else if (change && strain) {
      title = "先留出一段可以休息的时间";
      reason = "静息心率或 HRV 相对基线有变化，" +
        (example ? "试算采用了偏低的精力或较高的压力。" : "同期也填了精力偏低或压力较高。");
    } else if (rhythm) {
      title = "这周先选一个起床时刻";
      reason = `起床端中间一半记录相差 ${num(sleep.end_iqr)} 分钟。`;
    } else if (p.sedentary_hours !== null && p.sedentary_hours >= 6) {
      title = "今天挑一段久坐时间起来走走";
      const sitting = example ? "示例日均久坐" : "你填的日均久坐";
      reason = `${sitting}为 ${num(p.sedentary_hours)} 小时。先试一次饭后走 10 分钟。`;
    } else if (strain) {
      title = "今天先留一个 10 分钟的休息时段";
      reason = "记一下休息前后的精力，连续观察一周。";
    }
    if (p.training_sessions !== null || p.training_minutes !== null) {
      growth += ` 当前计入 ${num(p.training_sessions)} 次、${num(p.training_minutes)} 分钟，复核时补齐每节的 RPE。`;
    }
    if (example) {
      title = `示例行动 · ${title}`;
      monthlyTitle = `示例行动 · ${monthlyTitle}`;
      recover[0] = `示例复核 · ${recover[0]}`;
    }
    return { urgent: false, title, reason,
      layers: [safety, recover, [growthTitle, growth]],
      actions: [[title, reason], recover, [monthlyTitle, growth + deadline]] };
  }

  function updatePlan(p) {
    const plan = actionPlan(p);
    document.getElementById("priority").classList.toggle("urgent", plan.urgent);
    document.getElementById("priority-title").textContent = plan.title;
    document.getElementById("priority-note").textContent = plan.reason;
    for (const [id, prefix] of [["layers", "layer"], ["actions", "action"]]) {
      document.querySelectorAll(`#${id} li`).forEach((row, index) => {
        row.querySelector(`.${prefix}-title`).textContent = plan[id][index][0];
        row.querySelector(`.${prefix}-note`).textContent = plan[id][index][1];
      });
    }
  }

  function updateChart(chart, target) {
    const peak = Math.max(Number(chart.dataset.limit), target ?? 0);
    const quantum = 10 ** Math.floor(Math.log10(peak) - .3);
    const limit = Math.ceil(peak / quantum) * quantum;
    const y = value => (134 - value / limit * 112).toFixed(2);
    chart.querySelector(".upper").textContent = num(limit);
    chart.querySelector(".middle").textContent = num(limit / 2);
    const line = chart.querySelector(".target");
    line.setAttribute("visibility", target === null ? "hidden" : "visible");
    if (target !== null) {
      line.setAttribute("y1", y(target));
      line.setAttribute("y2", y(target));
    }
    const targetLabel = example ? "示例目标线" : "目标线";
    chart.querySelector(".target-caption").textContent = target === null ? "" : `${targetLabel} ${num(target)} ${chart.dataset.unit}`;
    let last = -2;
    const segments = Array.from(chart.querySelectorAll("circle"), point => {
      const index = Number(point.dataset.index);
      const height = y(Number(point.dataset.value));
      const command = index === last + 1 ? "L" : "M";
      last = index;
      point.setAttribute("cy", height);
      return `${command}${point.getAttribute("cx")} ${height}`;
    });
    chart.querySelector(".trace").setAttribute("d", segments.join(" "));
  }

  function readInputs() {
    const p = {};
    for (const control of controls) {
      if (!control.validity.valid) throw new Error(`请核对「${control.labels[0].textContent}」的范围和格式。`);
      const raw = control.value.trim();
      p[control.name] = raw ? (control.type === "number" ? control.valueAsNumber : raw) : null;
    }
    if (p.sleep_goal_hours !== null && p.sleep_goal_hours <= 4) throw new Error("睡眠目标须大于 4 小时。");
    if (p.load_low === 0 || p.load_high === 0) throw new Error("期前负荷上下限须大于 0。");
    if (p.load_low !== null && p.load_high !== null && p.load_low > p.load_high) {
      throw new Error("期前负荷上限须大于或等于下限。");
    }
    if (p.plan_sessions !== null && ((p.plan_completed ?? 0) + (p.plan_unknown ?? 0) > p.plan_sessions)) {
      throw new Error("完成和待核实的次数合计不能超过计划次数。");
    }
    return p;
  }

  function update() {
    let next;
    try {
      next = readInputs();
    } catch (problem) {
      error.textContent = `${problem.message} 已保留上一组有效参数。`;
      updatePlan(applied);
      return;
    }
    error.textContent = "";
    const trialLabel = example ? "示例试算" : "本页试算";
    let trial = example;
    for (const [name, value] of Object.entries(next)) {
      const source = document.getElementById(`source-${name}`);
      const edited = value !== model.initial[name];
      const observed = model.device[name];
      let tag = source.dataset.initial;
      if (edited) {
        const isGoal = model.goal_fields.includes(name);
        const provenance = isGoal ? "待确认目标" : "补充资料";
        tag = value === null ? (isGoal ? "待确认目标" : "待填") : `${trialLabel} · ${provenance}`;
      }
      if (observed != null && value !== null && value !== observed) tag += ` · 设备汇总 ${num(observed)}`;
      source.textContent = tag;
      trial ||= edited;
    }
    document.getElementById("input-status").textContent = trial ? trialLabel : "生成时的记录和参数";
    updateScores(next);
    updatePlan(next);
    for (const chart of document.querySelectorAll(".chart[data-field]:not([data-field=''])")) {
      if (next[chart.dataset.field] !== applied[chart.dataset.field]) updateChart(chart, next[chart.dataset.field]);
    }
    applied = next;
  }

  form.addEventListener("submit", event => event.preventDefault());
  form.addEventListener("input", update);
  form.addEventListener("reset", () => requestAnimationFrame(update));
})();
