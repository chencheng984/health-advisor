# 指标与聚合口径

`status` 会盘点导出中常见的顶层记录类型。`indexed` 表示至少有一行进入缓存，实际可用天数见 `query`。`inventory_only` 表示可以看到类型、条数和可识别的记录日期。`skipped` 计入缺少时间、单位不支持、无效数值等记录，`excluded_by_definition` 计入定义上不参与统计的记录。观察日期和入库后的归属日期可能不同，例如跨夜睡眠。CDA 只计附件数。心电图和 GPX 的附件计数与有效档案条数分别统计。

| 指标 | 查询键 | 汇总口径 |
| --- | --- | --- |
| 步数 | `steps` | 日合计，单位步 |
| 步行与跑步距离 | `distance` | 日合计，单位 km，支持 m 和 mi 换算 |
| Apple 运动时间 | `exercise_minutes` | 日合计，单位分钟 |
| 活动能量 / 静息能量 | `active_energy` / `basal_energy` | 分别按日合计，单位 kcal，支持 kJ 和 J 换算 |
| 训练时长 | `workout_minutes` | 来自 `Workout.duration`，按开始日期统计分钟。保留训练类型，汇总次数、分钟和类型分布 |
| 睡眠时长 | `sleep` | 按夜汇总实际睡眠区间，单位小时。附分期覆盖、主睡眠起止和起止时刻的四分位距 |
| 静息心率 / 心率 | `resting_hr` / `heart_rate` | 每日同来源观测中位数，单位 bpm |
| 心率变异性 SDNN | `hrv` | 每日同来源观测中位数，单位 ms |
| 最大摄氧量 | `vo2_max` | 每日同来源观测中位数，单位 mL/(kg·min) |
| 呼吸频率 | `respiratory_rate` | 每日同来源观测中位数，单位次/分钟 |
| 血氧饱和度 | `oxygen_saturation` | 每日同来源观测中位数，HealthKit 的 0–1 分数转换为百分比 |
| 体重 | `body_mass` | 每日同来源观测中位数，单位 kg，支持 g 和 lb 换算 |
| 体脂率 | `body_fat` | 每日同来源观测中位数，HealthKit 的 0–1 分数转换为百分比 |

## 时间和来源

- 记录时间保留起止 UTC 和各自的原始时区偏移。日指标归属到起点日期，累积值整条归入该日。睡眠按起点偏移，以中午分界并由所在时段的上午日期命名，跨中午时拆分。时长按 UTC 计算。
- 累积指标去除同来源的相同观测。Apple Watch、Garmin 和华为来源同属最高优先级，其后为 iPhone 和其他来源。组内按缓存来源 ID 稳定排序。`--prefer-source ID` 可以指定一个或多个首选来源。低优先来源与任意高优先来源发生时间重叠时，低优先整条记录剔除，部分重叠可能造成少计。Apple 健康支持自己的来源顺序，两者汇总可能有差异。
- 心率、身体测量等采样指标按每天一个优先来源归并。睡眠每晚采用有可用睡眠的优先来源，合并已睡区间，再扣除该来源有记录的清醒区间。卧床样本单列计数。
- 缓存内的来源 ID 仅在该快照内有效。设备识别主要使用导出中的设备型号或硬件字段，缺少时参考来源名称。换过设备或发生同步变化时，可在本地查原始设备名称并用新快照核对 ID。

## 覆盖和比较

本期默认是最新索引日及此前六天。基线默认取本期之前连续 90 个日历日，两段互不重叠。报告显示两段有效日中位数及各自覆盖。基线比较需要至少 7 个基线有效日，本期需要至少一半的日历日且至少 3 天有效。若本期本身短于 3 天，则要求每一天有效。比较要求两期使用的来源集合相同。百分比指标的绝对差值用百分点。

`--compare-previous` 改为比较紧邻本期的等长窗口。两期都应用本期的有效天数条件，来源变化仍会暂停计算差值。训练复盘单独展示两个区间找到的训练记录数和时长。

## 睡眠和训练明细

- 清醒区间从已睡时长中扣除。未分期记录和明确的核心、深睡或 REM 重叠时，采用明确分期。不同明确分期互相冲突时，该段计入未分期并单列冲突分钟。分期覆盖以实际睡眠总时长为分母。
- 把间隔不超过 90 分钟的睡眠片段归入同一睡眠段，再取实际睡眠累计最长的一段作为该夜主睡眠记录。单夜内出现多个时区偏移时，只用该夜的时长和分期。起止时刻用当地钟表时间，按中午起算后计算中位数和四分位距。四分位距至少用 4 夜，表示排在中间一半夜晚的时刻跨度。
- 同来源相同起止和时长的训练计一次。若重复记录只有一个明确类型，就采用该类型。有两个冲突类型则归入未分类。训练采用导出里的记录时长，时长可以短于起止时刻之间的间隔。训练日之外的空白表示没有导出记录。
- v0.1 缓存可继续读取。重新导入原 ZIP 时，当前版本会重建同一个缓存以保存分期和训练类型。分期或类型仍取决于原导出实际包含的数据。

## 心电图与运动路线

`report` 和 `brief` 按本机时区和本期日期窗口汇总档案。心电图按记录日期归属，只提供受控设备分类、通用设备类型、采样点数与有症状填写的记录数。路线按起点日期归属，仅汇总可算距离、时长、爬升与最长距离。缺少日期的路径仍可计入全档有效条数。`brief` 的 `overall.route_files` 取最近一次 Apple 导出附件清单，`overall.route_records` 统计缓存内可算路线。两者可能因无效附件或多次合并而不同。

## 本地导出与标准化中间格式

`import` 默认按文件内容识别格式。具有相同英文表头的品牌 CSV 可用 `--format garmin` 或 `--format huawei` 指定。Garmin 读取 CSV 的日期、步数、时长、距离和心率等通用列。华为读取中英文 CSV，或读取 JSON 汇总对象、对象数组和 `records`、`data`、`dailyData`、`dataList` 里的对象数组。使用带单位列名可明确换算口径，例如 `Distance (mi)`、`距离(米)`、`睡眠时长(分钟)`。未映射列计入 `unsupported_columns`，其中的名称和值都不写入缓存。两种品牌适配器未用真实导出样例校验。

- 有时区的时刻保留原偏移。品牌汇总只有日期或时间无偏移时，以 UTC 作为导入锚点。按天提供的步数等累积指标以一整天为区间，避免与其他来源的部分时段重复加总。只提供睡眠时长的记录归入该日，计算时长，但没有主睡眠起止和分期证据。
- `normalized-health/1` 使用 UTF-8 JSONL，每行一个对象。`metric` 须为表中 15 个查询键之一，未支持的键计入 `unsupported_metric`。`day` 为起点本地日期，`start` 和 `end` 须带 UTC 偏移。睡眠按中午归夜，以区间长度计时，`state` 为 `asleep`、`core`、`deep`、`rem` 或 `awake`。非分类指标用空状态，训练可用受支持的类型键。体脂和血氧的 `%` 用 0–100，`fraction` 用 0–1。
- `source.brand` 使用 `apple`、`garmin`、`huawei` 或 `other`，`source.kind` 使用 `watch`、`phone`、`other`、`garmin` 或 `huawei`。`source.label` 为可选说明，导入时丢弃，来源只按受控品牌和类型归并。各来源内的相同样本按现有唯一键去重。不同来源共存，汇总时继续用来源优先级处理重叠。

```jsonl
{"schema":"normalized-health/1","metric":"steps","day":"2026-10-08","start":"2026-10-08T09:00:00+08:00","end":"2026-10-08T09:30:00+08:00","value":120,"unit":"count","state":"","source":{"kind":"watch","brand":"garmin","label":"Garmin"}}
```

官方定义见 [HealthKit 指标目录](https://developer.apple.com/documentation/healthkit/data-types)、[睡眠分类](https://developer.apple.com/documentation/healthkit/hkcategoryvaluesleepanalysis?language=objc)、[训练时长](https://developer.apple.com/documentation/healthkit/hkworkout/duration?language=objc)、[百分比单位](https://developer.apple.com/documentation/healthkit/hkunit/percent%28%29?language=objc) 和 [Apple 健康数据源管理](https://support.apple.com/zh-cn/108779)。
