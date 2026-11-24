# 健康管家 · health-advisor

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-69%20passing-brightgreen)](tests/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Stdlib only](https://img.shields.io/badge/dependencies-none-lightgrey)](scripts/)

把 **Apple 健康 / 华为运动健康 / Garmin** 的本地导出，变成**可回查、可比较**的个人健康复盘与本地 HTML 工作台。纯 Python 标准库、不联网、数据不出本机。

它刻意不做三件事：不诊断、不调药、不解读设备波形。它做的是把设备记录按固定口径整理出来——每项指标都标日期、有效天数、来源和个人基线，让「这周比上周如何」这类问题有据可查，而不是凭印象。

当前版本 **v0.8.2**，变更见 [CHANGELOG.md](CHANGELOG.md)。

## 特性

- **固定口径，拒绝漂移**：日/夜中位数、有效覆盖天数、来源优先级统一。报告不会因为换了一周就换了算法，下次还能按同一口径复盘。
- **个人基线与等长对照**：默认拿最近 7 天和此前 90 天比，也可切到紧邻的等长窗口（`--compare-previous`），两期窗口一样长才谈差值。
- **睡眠分期与作息**：核心 / 深睡 / REM / 未分期时长、分期覆盖率、主睡眠起止与四分位距、跨夜合并、清醒区间扣除。
- **多品牌入同一缓存**：`scripts/adapters/` 支持 Apple ZIP、Garmin CSV、华为 CSV/JSON，以及 `normalized-health/1` 中间格式；`import --merge` 累积多份导出并按唯一键去重。
- **长期记忆**：记住你的目标、约束和每次复盘的一句话结论；下次报告会带出「上次那条行动做了没有」，到期自动标记。
- **隐私优先**：来源只存类型与指纹不存设备名；`privacy` 自检、`purge` 清理；`brief`/`report` 只输出聚合级字段；ECG 只读分类不读波形，路线只算几何不出坐标。
- **本地交互式工作台**：`workbench` 生成单文件 HTML，设备记录只读、补充资料可改，六维评分 / 健康三层 / SVG 趋势随输入即时试算，CSP `default-src 'none'` 不发任何网络请求。

## 快速开始

需要 **Python 3.10+**，无需安装任何第三方包。

```bash
git clone https://github.com/chencheng984/health-advisor.git
cd health-advisor
```

先用合成数据跑一遍，确认环境正常：

```bash
python tests/make_demo.py local-demo/export.zip
python scripts/health.py --db local-demo/health.sqlite3 import local-demo/export.zip
python scripts/health.py --db local-demo/health.sqlite3 report --output local-demo/report.md
python scripts/health.py --db local-demo/health.sqlite3 workbench --profile templates/profile.example.json --output local-demo/workbench.html
```

打开 `local-demo/report.md` 看报告，双击 `local-demo/workbench.html` 看交互式工作台。`templates/profile.example.json` 是示例资料，页面会标「示例/待确认」，不会被当成你确认过的数据。

## 用自己的数据

1. **导出**：Apple 健康在「个人资料 → 导出所有健康数据」；华为运动健康与 Garmin Connect 也在各自设置里导出为 CSV/JSON。
2. **导入**：
   ```bash
   python scripts/health.py import "导出文件路径"                 # Apple ZIP，自动识别
   python scripts/health.py import 导出.csv --format garmin
   python scripts/health.py import 导出.jsonl --format normalized
   python scripts/health.py import 新导出.zip --merge             # 并入已有缓存
   ```
3. **看覆盖**：`python scripts/health.py status`，确认哪些指标有数据、来自哪些来源、最近一条是哪天。
4. **出报告**：`python scripts/health.py report --output 报告.md`（默认最近有数据的一周，基线前 90 天）。

> 缓存默认在 `~/.health-advisor/health.sqlite3`。若旧版缓存仍在 `~/.apple-health-advisor/`，命令会自动沿用并提示一行，不必手动搬。用 `--db PATH` 可指定任意位置。

## 命令总览

| 命令 | 作用 |
| --- | --- |
| `import <文件>` | 导入导出文件；`--format auto\|apple\|garmin\|huawei\|normalized`，`--merge` 合并去重 |
| `status` | 指标覆盖、来源、导入历史；`--show-source-names` 只出脱敏标签 |
| `query --metric …` | 选指标查询；`--daily` 逐日、`--format csv` 导出、`--baseline-days` 调基线 |
| `report` | 生成 Markdown 报告；`--output` 存文件、`--group` 按组、`--from/--to` 指定区间 |
| `brief` | 供 AI 写作的聚合证据包（含 `privacy`、`memory`、`archive` 块） |
| `archive` | ECG 与运动路线档案；`--detail` 逐条列受控字段 |
| `workbench` | 生成本地交互式 HTML 工作台，需 `--output X.html` |
| `memory` | 长期记忆：`init` / `show` / `set` / `review` / `forget` / `export` |
| `migrate` | 原地升级旧 schema 缓存（2/3/4 → 5），清理旧来源字段 |
| `privacy` | 内部隐私自检（库字节、来源列、journal、坐标），**不生成面向用户的报告** |
| `purge` | 清理本工具生成的本地数据；默认 dry-run，需 `--yes` 才执行 |

常用例子：

```bash
python scripts/health.py query --metric sleep --metric resting_hr --metric hrv --days 14
python scripts/health.py query --metric steps --days 7 --format csv
python scripts/health.py report --group activity --from 2026-10-01 --to 2026-10-31
python scripts/health.py report --compare-previous
python scripts/health.py memory init
python scripts/health.py memory set --goal "把入睡时间稳定在 00:30 前"
python scripts/health.py brief --days 7 --no-memory
```

## 支持的指标

15 项，取不到的指标如实显示「无记录」，不插值、不猜。

| 分组 | 指标 | 查询键 |
| --- | --- | --- |
| 活动与训练 | 步数 / 步行跑步距离 / Apple 运动时间 / 活动能量 / 静息能量 / 训练时长 | `steps` `distance` `exercise_minutes` `active_energy` `basal_energy` `workout_minutes` |
| 睡眠 | 睡眠时长（含分期） | `sleep` |
| 心脏与恢复 | 静息心率 / 心率 / 心率变异性 SDNN / 最大摄氧量 | `resting_hr` `heart_rate` `hrv` `vo2_max` |
| 呼吸 | 呼吸频率 / 血氧饱和度 | `respiratory_rate` `oxygen_saturation` |
| 身体测量 | 体重 / 体脂率 | `body_mass` `body_fat` |

另可从档案读取 **ECG**（记录时间、设备分类、症状、采样点数，不读波形）与 **运动路线**（距离、时长、爬升、最长一条，不存轨迹坐标）。口径细节见 [指标与聚合口径](references/record_types.md)。

## 多品牌与中间格式

`import --format auto` 按内容嗅探四种格式：

- **apple**：健康 App 导出的 ZIP（`export.xml`）
- **garmin**：Garmin Connect 导出的 CSV（按英文表头关键词匹配，不硬编码列序号）
- **huawei**：华为运动健康导出的 CSV / JSON（兼容中英文混合表头）
- **normalized**：`normalized-health/1` JSONL，一行一条

**宿主拿到健康应用权限时**，只需把数据写成这个格式再导入，全程不联网、不经模型：

```json
{"schema":"normalized-health/1","metric":"sleep","day":"2026-11-22","start":"2026-11-21T23:30:00+08:00","end":"2026-11-22T07:10:00+08:00","value":7.67,"unit":"hr","state":"asleep","source":{"kind":"watch","brand":"huawei","label":"HUAWEI Watch #1"}}
```

> ⚠️ **Garmin 与华为适配器按公开的通用表头字段实现，未经真实导出样例校验。** 字段不匹配的列会被跳过并计数，不会静默丢弃。拿到真实样例后欢迎提 issue 补校验。

## 隐私设计

数据只在本机，脚本零网络请求（`scripts/` 无 socket / urllib / requests / subprocess）。

- **分级**：原始导出 / 可识别字段 / 聚合数据 / 结论，四层各有限定去向，见 [references/privacy.md](references/privacy.md)
- **脱敏**：`sources` 只存类型与指纹，不存设备名；`status` 输出 `Apple Watch #1` 这类标签
- **白名单**：`brief` / `report` 只输出聚合字段；`--with-identifiers` 也只多给区间来源数量
- **自检与清理**：`privacy` 检查库文件字节里是否残留姓名/账号/序列号/坐标；`purge` 按范围清理缓存、记忆与临时产物
- **记忆可控**：`memory.json` 只存目标、约束与一句话结论，不存逐日数值序列；`memory export` 可带走，`memory forget` 可单条或全部删除

> 走云端 Agent 时，读进会话的聚合摘要可能进入模型上下文与聊天记录。要全程离线，用 `report --output` 与 `workbench` 在本地生成。

## 目录结构

```text
health-advisor/
├── SKILL.md              # 技能入口：接题判断、按问题选方法、健康边界
├── README.md
├── CHANGELOG.md
├── CONTRIBUTING.md
├── LICENSE               # MIT
├── docs/                 # 对话体验规格、健康领域框架（六维公式、证据分级、转介边界）
├── references/           # 方法文件与口径基准（含 privacy.md、record_types.md）
├── scripts/
│   ├── health.py         # CLI 主入口，11 个子命令
│   ├── storage.py        # 流式解析与 SQLite 缓存（schema 5）
│   ├── analysis.py       # 指标聚合、基线比较、跨夜睡眠合并
│   ├── insights.py       # 睡眠分期、作息、训练汇总
│   ├── catalog.py        # 15 项指标定义与来源归并规则
│   ├── brief.py          # 供 AI 写作的聚合证据包
│   ├── archive.py        # ECG 元数据与路线几何摘要
│   ├── privacy.py        # 隐私自检
│   ├── memory.py         # 长期记忆读写
│   ├── import_records.py # 共用写库路径
│   ├── workbench.py / workbench.js / workbench_page.html
│   └── adapters/         # apple / garmin / huawei / normalized / tabular
├── templates/            # 补充资料示例
└── tests/                # 69 项单元测试，全部使用合成数据
```

## 测试

```bash
python -m unittest discover -s tests -v   # 69 项
```

测试只用合成导出，不碰任何真实健康数据。临时目录落在项目内 `_tmp/`，在受限环境里也能跑。贡献约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 已知限制

- **Garmin / 华为适配器未经真实样例校验**（见上文警告）
- **CDA 临床文档**只统计附件数量，未解析内容
- **ECG / 路线**只出档案摘要，不做专项分析，也不解读波形
- **不联网**，没有同步适配器：换设备后需要手动导出再 `import --merge`
- Apple 健康自身的来源优先级可按指标调整，与本工具结果可能有差异

## 免责声明

本项目用于**日常观察与复盘**，不构成医疗建议、诊断或治疗。设备测量的准确性由设备与佩戴条件决定；任何症状、用药与训练限制请咨询医疗专业人员。若出现胸痛、严重呼吸困难等急症，请立即联系当地急救。

## 参考实现

- `goutoujunshi` 的接题—取证—首选—复核骨架 → [取数与证据判断](references/evidence.md)、[综合判断与复核](references/review.md)
- 「财睿——家庭财富规划师」的主顾问 / 分领域 / 统一报告编排 → [SKILL.md](SKILL.md)
- [Health Skill](https://github.com/googlarz/health-skill)、[Strava Coach](https://github.com/srothgan/strava-claude-skill)、[Fitness Weekly Review](https://github.com/philipyaz/cos) 的健康问询与训练证据
- [healthkit-to-sqlite](https://github.com/dogsheep/healthkit-to-sqlite) 的本地导入思路
- [Agent Skills 格式规范](https://agentskills.io/specification)

## 许可

[MIT](LICENSE)
