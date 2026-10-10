# million-word-novel

**中文** | [English](README.en.md)

百万字长篇小说 · 长篇叙事持续生产操作系统。

🎯 让 AI 写几百章、一百万字，写完还记得自己写过什么。

## 理念

单章生成早就不是难点了。难的是第 300 章。

模型的记忆是一个不断截断的窗口。写到几十万字时，前面发生的事要么被压缩成模糊的摘要，要么干脆掉出上下文。于是人物会莫名其妙地换了性格，死过的角色重新登场，伏笔埋了再也没有回收，同一个桥段换个说法又写一遍。**这不是模型变笨了，是它真的不记得了。**

这个项目的前提是一句反直觉的话：

> **上下文是缓存，磁盘才是内存。**

所以正文不是放在对话里攒着，而是落盘；长期记忆不是一段越截越短的摘要，而是一个可查询的 Canon 数据库。每写完一章，系统把正文抽成结构化的 Canon Delta（谁在哪、知道了什么、拿到了什么、什么关系变了、哪些伏笔推进了），校验后写进库。下一章开始时，按需要**检索**相关信息，而不是把所有东西硬塞回提示词。

写下来的顺序不可颠倒：

```
章节正文 > 校验过的 Canon Delta > canon.db > 派生摘要 > Markdown 视图
```

Markdown 都是派生视图，随时可以从库里重建。真正的事实来源只有正文和库。

## 特性

| | |
| --- | --- |
| 🧠 四层记忆 | 全局 Canon + 结构记忆 + 当前卷 + 工作记忆，按需检索而非截断 |
| 🔒 审计绑定哈希 | audit 绑定文本哈希，commit 校验哈希 —— 改一个字，审计即失效 |
| 🗄️ 事务化提交 | staging + journal + 单 SQLite 事务 + 原子 rename，崩溃可 recover |
| 🎭 语义一致性检查 | 人物 / 知识边界 / 时间线 / 地点 / 物品 / 关系 / 因果 / 伏笔八类 |
| 🧵 伏笔与线程 | 埋点登记、逾期检测、悬空线程拦截 |
| ⏪ 可回放 | canon.py state --at-chapter N 重建当时的世界，快照可验证 |
| 🚦 严格收轮 | 12 项条件全过才允许进入下一轮，否则 ROUND_BLOCKED |
| 🧪 172 个用例 | 含 10 章端到端集成测试 |
| 🪶 零依赖 | 只要 Python 3.8+ 与标准库 |

## 运行环境

| | |
| --- | --- |
| **Python** | 3.8 或更高。开发与验收在 3.11 / 3.12 上完成 |
| **依赖** | **无。** 只用标准库（sqlite3 也是标准库） |
| **不需要** | Redis / Kafka / Docker / 云数据库 / 任何第三方 Python 包 |
| **平台** | 任意（纯 Python + SQLite，Windows / Linux / macOS 均可） |

## 安装

没有安装步骤，git clone 或直接下载即可用：

```bash
git clone https://github.com/yishuiliufang/million-word-novel.git
cd million-word-novel
python tests/run_tests.py        # 先确认环境正常
```

脚本都在 scripts/，从仓库根目录调用。novels/caoye/ 是一部正在写的示例作品（31 章 / 8.2 万字），可以拿来观察各目录的实际长相。

## 快速开始

```bash
# 1. 建项目（同时建立 state/canon.db）
python scripts/novel_state.py init --root "<项目>" --title "我的书" \
  --premise "一句话故事" --target 1000000 --chars-per-chapter 3000 \
  --chapters-per-round 5 --volumes 5

# 2. 填 state/bible.md（世界规则/人物/伏笔/线程）与 state/outline.md（每卷的卷末不可逆变化），
#    然后把圣经灌进 Canon（只做一次；原件备份为 state/bible.source.md）
python scripts/canon.py --root "<项目>" import-md

# 3. 取本轮简报（转世入口；自动执行崩溃恢复）
python scripts/novel_state.py next --root "<项目>"

# 4. 先规划，再写（没有通过校验的 plan，commit 会拒绝）
python scripts/novel_state.py plan --root "<项目>" --chapter 1 --check
python scripts/novel_state.py scene-plan --root "<项目>" --chapter 1

# 5. 写草稿（只写 drafts/），然后把正文抽成 Canon Delta
python scripts/novel_state.py extract --root "<项目>" --chapter 1 \
  --file "<项目>/drafts/ch-0001.md" --out "<项目>/state/plans/ch-0001.delta.json"

# 6. 审计：Level 1 文本卫生 + Level 2 语义一致性
python scripts/novel_state.py audit --root "<项目>" --chapter 1 \
  --file "<项目>/drafts/ch-0001.md" --strict

# 7. 事务性提交（plan / audit / canon / length 四道门 → 快照 → 索引）
python scripts/novel_state.py commit --root "<项目>" --chapter 1

# 8. 收轮（严格校验；不合法返回 ROUND_BLOCKED，退出码 3）
python scripts/novel_state.py round-done --root "<项目>"

# 9. 达标后导出
python scripts/export_novel.py --root "<项目>" --by-volume --zip
```

完整命令见 docs/CLI.md。

## 它解决什么问题

| 旧系统的病 | 现在 |
|---|---|
| 记忆 = bible.md[:4000] 截断 | 四层记忆 + query-driven Canon 检索（context_builder.py） |
| outline.md 从不参与轮次简报 | 解析成卷目标/卷末不可逆变化，进入 L2 结构记忆 |
| 先 commit 后 audit | **audit 绑定文本哈希，commit 校验哈希**；改一个字即失效 |
| commit 四步裸执行，崩溃留半提交 | staging + journal + 单 SQLite 事务 + 原子 rename + recover |
| round-done 只做 round+1 | 12 项全条件校验，不满足即 ROUND_BLOCKED |
| 只有文本卫生检查 | Level 2 语义一致性（人物/知识边界/时间线/地点/物品/关系/因果/伏笔） |
| 润色器凭空新造人物动作 | 默认拒绝 CANON_AFFECTING_EDIT，放行后强制重审 |
| 8-gram 是唯一重复判据且判失败 | 降级为提示；新增 POSSIBLE_PLOT_REPETITION 语义重复检测 |
| 全靠 --summary/--place 手工输入 | 正文 → 结构化 Canon Delta → 校验 → 派生摘要 |
| 没有测试 | 172 个用例，含 10 章端到端集成（python tests/run_tests.py） |

## 流程（一轮 = 一次转世）

```
next → chapter_plan → scene_plan → write → audit(L1+L2) → extract(canon delta)
     → canon consistency validation → commit(事务) → snapshot → index → round-done
```

角色拆分（每个角色单一职责，见 docs/ARCHITECTURE.md）：

| 角色 | 入口 |
|---|---|
| Planner | novel_state.py plan / scene-plan |
| Writer | 模型 / subagent / auto-next --writer |
| Extractor | novel_state.py extract、auto-next --extractor、--llm |
| Consistency Auditor | novel_state.py audit |
| Style Auditor | audit_quality.py、polish_prose.py、fix_quotes.py |
| Canon Manager | canon.py |
| Recovery Manager | novel_state.py recover、validate_state.py |

## 三条硬规矩

1. **先 plan，先 audit，再 commit。** 没有 plan、没有针对当前文本哈希的通过审计，commit 会拒绝。--allow-plan / --allow-audit 可放行，但会写入 GATE_WAIVED warning 并在 round-done 汇总。
2. **不确定的判断不改 Canon，只记 warning。** Canon Delta 中标 uncertain: true 或 confidence < 0.5 的条目会被剔除并降级为 warning，等待人工确认。
3. **停止条件只有 total_chars >= target_chars；但收轮必须合法。** 达标就转世成涅槃（STOP），字数没到就继续轮回（REINCARNATE）。

## 目录结构（init 生成）

```
<项目>/
├── chapters/     ch-0001.txt                  # 正文
├── drafts/       ch-0003.md, committed/       # 草稿（提交后归档）
├── parts/                                     # 长章分节
├── state/
│   ├── progress.json                          # 进度（schema 2）
│   ├── canon.db                               # ★ Canon DB（Source of Truth）
│   ├── snapshots/                             # 每章 canon snapshot + 人工修正补丁
│   ├── plans/  audits/  derived/  tx/         # 计划 / 审计报告 / 派生 JSON / 事务 staging
│   ├── bible.md                               # 派生视图（手写源：bible.source.md）
│   ├── outline.md                             # ★ 人写计划视图（next 的一等输入）
│   ├── plot-ledger.md  summaries/             # 派生视图
│   ├── last-context.md                        # 转世交接（自动生成）
│   └── banned-phrases.txt
├── exports/      book.txt, vol-N.txt, STATS.md, <书名>.zip
├── references/   技法笔记与按角色拆分的提示词
├── scripts/      全部脚本
└── tests/        测试套件
```

旧项目若已存在 export/，系统继续复用它（不会被拆到两个目录）。

## 常见操作

```bash
# 查 Canon
python scripts/canon.py --root "<项目>" show
python scripts/canon.py --root "<项目>" character 李禾
python scripts/canon.py --root "<项目>" state --at-chapter 40        # 回放重建当时世界
python scripts/canon.py --root "<项目>" verify-snapshots            # 证明快照可完整重建

# 人工修正 Canon（必须给原因，全部可追踪）
python scripts/canon.py --root "<项目>" update --kind character --ref 李禾 \
  --field location --value "旧码头" --actor "作者" --reason "第四章已到旧码头"

# 校验与恢复
python scripts/validate_state.py --root "<项目>"                    # 全量状态校验
python scripts/novel_state.py recover --root "<项目>" --dry-run      # 看有没有中断残留

# 回滚（不删除：被撤销的章节与产物隔离到 state/rolled-back/）
python scripts/novel_state.py rollback --root "<项目>" --to-chapter 40 --reason "改大纲"

# 承接已有稿件
python scripts/novel_state.py import --root "<项目>" --group "<手稿>/ch*.txt"
python scripts/novel_state.py plan --root "<项目>" --volume-plan "42,50,50,50,50"

# 升级旧项目（不删任何文件）
python scripts/novel_state.py migrate --root "<旧项目>"
```

## 字数口径与停止条件

| 口径 | 含义 |
|---|---|
| no_ws（默认） | 非空白字符数，与中文平台口径一致 |
| cjk | 仅汉字 |
| all | 含空白原始字符数 |

**只有 total_chars >= target_chars 才停止。** 但「写够一百万字」不等于「完成小说」：round-done 会检查伏笔是否逾期、线程是否悬空、审计是否通过、Canon 是否同步。

## 项目结构

```
million-word-novel
├── README.md / README.en.md   # 本文件
├── SKILL.md                   # 技能主体：转世协议与各角色职责
├── agents/openai.yaml         # Agent 配置
├── scripts/                   # 全部脚本（24 个 .py + schema.sql）
│   ├── novel_state.py         # 主入口：init / next / plan / commit / round-done / recover
│   ├── canon.py  canon_db.py  # Canon 管理与数据库层
│   ├── chapter_plan.py  context_builder.py  extract.py
│   ├── narrative_audit.py  audit_quality.py  polish_prose.py  fix_quotes.py
│   └── schema.sql             # Canon DB 的规范 DDL 源
├── tests/                     # 172 个用例 + 端到端集成
├── docs/                      # 架构 / 审计 / CLI / 验收 / 变更说明
├── references/                # 技法笔记与按角色拆分的提示词
└── novels/caoye/              # 示例作品《草色三千里》（31 章 / 8.2 万字）
```

## 测试

```bash
python tests/run_tests.py            # 全部用例，输出 PASS/FAIL 计数并写 docs/test-report.json
python tests/run_tests.py -v         # 详细
python tests/run_tests.py --clean    # 跑完清理 .tmp-tests
```

## 文档

| 文档 | 内容 |
|---|---|
| docs/AUDIT.md | 阶段 1：升级前的仓库审计（问题清单 P1–P24、可保留设计 R1–R17） |
| docs/ARCHITECTURE.md | 阶段 2：目标架构、四层记忆、流程图、状态机、目录结构 |
| docs/DDL.sql | Canon DB 的规范 DDL（由 canon.py ddl 从 scripts/schema.sql 生成） |
| docs/CHANGES.md | 修改说明：改了什么/为什么/保留了什么/**破坏了哪些兼容点** |
| docs/CLI.md | 完整 CLI 使用说明（实际可执行命令） |
| docs/ACCEPTANCE.md | 阶段 6：端到端验收报告（命令、退出码、产物、测试结果） |
| docs/test-report.json | 机器可读的测试报告 |
| SKILL.md | 技能主体：转世协议与各角色职责 |
| references/loop-prompts.md | 按角色拆分的提示词模板 |

## 许可证

MIT
