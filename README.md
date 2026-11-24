# 健康管家

把本地健康导出和 `normalized-health/1` 数据变成可回查的个人报告。支持步数、睡眠、心率与 HRV、活动和训练、呼吸、体重等 15 项指标。每项展示日期、有效天数、数据来源和个人基线，便于下一次用相同口径复盘。

当前版本为 **v0.8.2**，发行包为 `health-advisor-0.8.2.zip`。本地 HTML 工作台将设备记录与用户补充资料放在同一页，调整目标后可即时试算。复盘可以带出已保存的目标、约束及上次结论。 [查看版本变更](CHANGELOG.md)

## 先试一份合成数据

需要 Python 3.10 或以上。运行和测试均使用标准库。

在这个项目目录下运行以下命令。

```text
python tests/make_demo.py local-demo/export.zip
python scripts/health.py --db local-demo/health.sqlite3 import local-demo/export.zip
python scripts/health.py --db local-demo/health.sqlite3 report --output local-demo/report.md
python scripts/health.py --db local-demo/health.sqlite3 workbench --profile templates/profile.example.json --output local-demo/health-workbench.html
```

打开 `local-demo/report.md` 查看报告，双击 `local-demo/health-workbench.html` 查看交互式工作台。生成器与报告命令需要新的输出文件名，重复演示时可使用新的目录。`templates/profile.example.json` 是示例补充资料，页面会把它标成"示例/待确认"，不会被当成你已确认的数据。

## 分析自己的数据

1. 从健康应用导出数据并保存在本机。Apple 健康的入口在个人资料里的「导出所有健康数据」。 [Apple 导出说明](https://support.apple.com/zh-cn/guide/iphone/-iph5ede58c3d/ios)
2. 在项目目录中执行 `python scripts/health.py import "导出文件路径"`。导入后用 `python scripts/health.py status` 查看指标和覆盖。相同导出会复用缓存，新的导出成功导入后替换该缓存快照。不同用户或需要分别保留的导出可用 `--db` 指定各自的数据库。
   想把多份导出累积到同一个缓存，在导入时加 `--merge`。历史样本保留，新样本按唯一键去重，`status` 的 `imports` 列出导入格式、模式和新增条数。Apple 的导出仍需解析全部 XML。

   `import --format auto|apple|garmin|huawei|normalized` 默认按内容识别。宿主可在取得权限后将健康数据写成 `normalized-health/1` JSONL，再以 `--format normalized` 导入本地缓存。Garmin/华为适配器按公开通用字段实现，未经真实导出样例校验。 [字段与导入口径](references/record_types.md#本地导出与标准化中间格式)
3. 运行 `python scripts/health.py report` 查看报告，或用 `--output` 保存 UTF-8 Markdown。默认查看最近有数据的一周，基线取此前 90 天。
4. 想要交互式版本时运行 `workbench`：`python scripts/health.py workbench --profile 你的补充资料.json --output 健康工作台.html`。页面上方是设备记录（只读），下方是你补的作息、目标、精力、久坐、咖啡因与训练；评分窗口固定取最近一个**已结束**的 7 天，改任何字段都会即时试算，唯一按钮是「恢复初始输入」。

```text
python scripts/health.py query --metric sleep --metric resting_hr --metric hrv --days 14
python scripts/health.py query --metric steps --days 7 --format csv
python scripts/health.py report --group activity --from 2026-10-01 --to 2026-10-31
python scripts/health.py report --compare-previous
python scripts/health.py query --metric sleep --metric workout_minutes --days 14 --daily
python scripts/health.py brief --days 7
python scripts/health.py workbench --profile templates/profile.example.json --output local-demo/week.html
python scripts/health.py archive
python scripts/health.py archive --detail
```

`report` 和 `brief` 带出本期心电图与运动路线摘要，只含聚合计数和受控标签。心电图摘要包含设备分类、通用设备类型、采样点数和有症状填写的记录数。路线摘要包含距离、时长、爬升和最长距离。`archive` 可查看本地档案的覆盖情况，`--detail` 带出逐条记录的日期与受控字段。

查询支持 `--daily` 输出逐日数据、`--baseline-days` 调整基线窗口、`--prefer-source ID` 指定来源优先级。指标和来源规则见 [指标与聚合口径](references/record_types.md)。宿主需要实际日历周时，可显式指定结束日期。

v0.1 缓存仍可直接生成报告，也可查看主睡眠起止与训练次数。如果需要补充原导出已有的睡眠分期和训练类型，用相同的 `--db` 路径重新导入原 ZIP。

## 在 Agent 中使用

把整个 `health-advisor` 目录放到宿主约定的 Skill 位置，保留 `SKILL.md`、`scripts/` 和 `references/` 的相对结构。宿主需要能运行 Python 并读取你的本地缓存。启用后，可以说「给我最近有数据的一周的健康报告」或「只看过去两周的睡眠和静息心率」。 [Agent Skills 格式规范](https://agentskills.io/specification)

需要顾问报告时，可以说「对比最近有数据的一周和前一周，告诉我该优先调整作息还是训练，把判断和复核条件写成 Markdown」。`brief` 提供聚合证据，Agent 先作 [综合判断](references/review.md)，再按 [报告模板](references/dual-perspective.md) 撰写文件。直接运行 `report --output PATH` 可以离线保存统计版。

完整复盘时，Agent 应先接住你的处境、给出一条首选，再生成 `workbench` 工作台；想被问得更准，可以直接说「这周早会多」「健身房器械训练没戴表」「在减脂平台期」这类背景，[分阶段问卷](references/intake.md) 只会追问 1–2 个会改变结论的问题。需要纯文字时仍可要求 Markdown 复盘。

## 数据与隐私

本地脚本不联网。默认缓存为 `~/.health-advisor/health.sqlite3`。新位置还没有缓存且旧版缓存存在时，命令会提示并继续在原位置读写。也可在子命令前加 `--db PATH` 选定位置。复盘记忆随选中的缓存读取同目录的 `memory.json`。`status` 和 `query` 使用「Apple Watch #1」这样的来源标签，`status --show-source-names` 也只在 `name` 字段返回这种脱敏标签。schema 5 缓存用品牌和来源指纹去重。已有 schema 2、3 或 4 缓存可执行 `python scripts/health.py migrate` 原地升级。schema 2 和 3 升级时会清理旧来源字段，整个过程只需缓存。使用其他缓存路径时，在 `migrate` 前加 `--db PATH`。成功的合并导入也会升级并清理旧缓存。使用云端 Agent 时，读入会话的摘要可能发送给模型服务并留在聊天记录中。希望全程离线时，可用本地命令生成并打开 Markdown 或工作台。工作台 HTML 同样包含个人健康数据，只写入你指定的本地路径，页面自身不发起任何网络请求（`Content-Security-Policy: default-src 'none'`）。要清除本地产物，可在命令停止后删除相应 SQLite 缓存与导出的报告，原始 ZIP 由用户自行管理。

Apple 健康的来源优先顺序可按指标更改，与本项目的结果可能存在差异。首版将血压、设备事件等未支持的记录类型纳入覆盖清单；ECG 与 GPX 在 0.7.0 已读元数据和几何摘要，CDA 仍只识别附件数量。本版本的报告用于日常观察和复盘。

## 参考实现

- `goutoujunshi` 的接题、取证、首选与反馈复核，对应健康分析的 [取数与证据判断](references/evidence.md) 和 [综合判断与复核](references/review.md)。
- 「财睿——家庭财富规划师」的主顾问、分领域分析、综合与报告，转化为 [SKILL.md](SKILL.md) 的按需方法选择和统一报告。
- 健康问询、训练证据和周期复盘参考 [Health Skill](https://github.com/googlarz/health-skill/blob/main/SKILL.md)、[Strava Coach](https://github.com/srothgan/strava-claude-skill/blob/main/skills/strava-coach/SKILL.md) 和 [Fitness Weekly Review](https://github.com/philipyaz/cos/blob/main/board/.claude/skills/fitness-weekly-review/SKILL.md)。
- 查看指标覆盖与设备来源后再解读，借鉴 [Apple Health Claude Skill](https://github.com/HomuraTokido/apple-health-claude-skill) 的工作流。
- 本地导入 SQLite 参考 [healthkit-to-sqlite](https://github.com/dogsheep/healthkit-to-sqlite)。按需取数据、区分日查与周报的方式参考 [Apple Health Export Analyzer](https://github.com/krumjahn/applehealth#openclaw-support)。
- 本地数据与会话聚合摘要的交接方式，参考 [OpenClaw HealthKit 摘要文档](https://docs.openclaw.ai/platforms/ios-healthkit)。

## 开发

```text
python -m unittest discover -s tests -v
```

测试只使用合成导出。贡献约定见 [CONTRIBUTING.md](CONTRIBUTING.md)，版本范围见 [CHANGELOG.md](CHANGELOG.md)。
