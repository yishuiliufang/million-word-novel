# 阶段 2 · 目标架构设计：Long-Context Narrative Operating System

本文档是阶段 1 审计报告（[`AUDIT.md`](AUDIT.md)）的直接产物，定义升级后的系统架构：
四层记忆、Canon DB schema、commit 事务流程、状态机、目录结构。

---

## 0. 架构第一原则（一切设计的裁决标准）

| # | 原则 | 在代码中的体现 |
|---|---|---|
| 1 | **Context 是 Cache，不是 Database** | `next` 不再截断读取 Markdown，而是由 `context_builder.py` 按需查询 Canon |
| 2 | **Canon DB 才是长期记忆** | `state/canon.db`（SQLite + JSON 字段），25 张表 |
| 3 | **Chapter Text 是第一手事实来源** | 章节文件哈希进入 `chapters.sha256` 与 snapshot；审计绑定同一哈希 |
| 4 | **Summary / Bible / Ledger 都是派生视图** | `derived.py` 每次 commit 后从 Canon 重建；`bible.md` 带 AUTO-GENERATED 标记 |
| 5 | **Commit 之前必须完成 Audit** | `commit` 校验「针对当前文本哈希的通过审计」；改稿即失效 |
| 6 | **每次修改 Canon 都必须可追踪** | `canon_changes`（who/when/what/before/after/why/source） |
| 7 | **任何状态都必须可以恢复** | `tx.py` journal + staging；`snapshots/`；`rollback`；`recover` |
| 8 | **模型不能自行修改 Canon** | Canon Delta 必须过 `extract.validate_delta`；不确定项降级为 warning |
| 9 | **文学质量与文本卫生分开** | Level 1（文本卫生，阻断）/ 语义重复与 8-gram（辅助提示） |
| 10 | **写够字数 ≠ 完成小说** | 停止条件是字数，但 `round-done` 校验结构完整性，不是 `round+1` |

---

## 1. 目标流程图

```
                    ┌────────────────────────────────────────────────────┐
                    │  Canon DB (state/canon.db) — 长期记忆 / Source of Truth │
                    │  entities / character_states / events / foreshadowings │
                    │  plot_threads / knowledge / world_rules / items / ...  │
                    └───────▲──────────────────────┬─────────────────────┘
                            │                      │ query-driven read
       canon delta          │                      ▼
   ┌────────────────────────┴───────────┐   ┌───────────────────────────────┐
   │ Extractor                          │   │ context_builder (四层记忆)     │
   │  chapter text → Canon Delta (JSON) │   │  L0 当前 / L1 局部 / L2 全书结构 │
   │  → validate_delta → warnings       │   │  L3 Canon 切片                  │
   └────────────▲───────────────────────┘   └──────────────┬────────────────┘
                │                                          │ 动态上下文
                │                                          ▼
   ┌────────────┴──────────────┐   chapter_plan      ┌──────────────────────┐
   │ Writer (模型/skill)        │◄────────────────────│ Planner               │
   │  按 plan + context 写正文  │   Chapter Plan      │  skeleton+validate    │
   └────────────┬──────────────┘   (含 must_change)   └──────────▲───────────┘
                │  draft                                          │
                ▼                                                 │
   ┌──────────────────────────┐   阻断项(Level1/Level2)            │
   │ Style Auditor (polish)    │──────────────┐                    │
   │  只做不改变事实的润色      │              ▼                    │
   └────────────┬──────────────┘   ┌──────────────────────────┐    │
                │  polished draft  │ Consistency Auditor       │    │
                └─────────────────►│  L1 文本卫生 + L2 语义一致性│    │
                                   │  → audits 表 + 报告 JSON   │    │
                                   └────────────┬─────────────┘    │
                                                │ PASS               │
                                                ▼                    │
   ┌────────────────────────────────────────────────────────────┐   │
   │ Commit (Transaction Manager)                                │   │
   │  1 preflight: length / plan / audit(hash) / canon + plan-diff│   │
   │  2 staging:   chapter, snapshot, derived views, progress     │   │
   │  3 SQLite 事务: chapters + canon delta + snapshots + gates    │───┘
   │  4 publish:   os.replace 原子落盘                             │
   │  5 finish:    journal=committed, 清理 staging                 │
   └────────────┬───────────────────────────────────────────────┘
                │
                ▼
   ┌──────────────────────────┐   失败/中断     ┌───────────────────────────┐
   │ Snapshot + Index         │────────────────►│ Recovery Manager (recover) │
   │  state/snapshots/ch-N.json│                │  补齐发布 / 丢弃 staging    │
   │  scene_index / timeline   │                └───────────────────────────┘
   └────────────┬─────────────┘
                ▼
   ┌──────────────────────────┐   所有条件满足   ┌───────────────────────────┐
   │ round-done (严格校验)     │─────────────────►│ REINCARNATE / STOP         │
   │  不满足 → ROUND_BLOCKED   │  exit 3          │ rounds_completed += 1      │
   └──────────────────────────┘                  └───────────────────────────┘
```

（同一流程的文字版，便于逐项对照）

```
next → chapter_plan → scene_plan → write → audit(L1+L2) → extract(canon delta)
     → validate_delta → commit(事务) → snapshot → index → round-done
```

---

## 2. 四层记忆架构

| 层 | 名称 | 内容 | 实现 |
|---|---|---|---|
| L0 | 当前上下文层 | 本章任务、场景目标、核心冲突、上一章结尾（1200 字）、本章必须解决的问题、本章禁止发生的事 | `context_builder.layer0` |
| L1 | 局部剧情记忆层 | 最近 8 章摘要与事件、最近状态变化、最近冲突、当前 Arc、未解决线程、近期出场人物 | `context_builder.layer1`（查询 `snapshots` + `character_state_changes`，**不是文本尾部截断**） |
| L2 | 全书结构记忆层 | 当前卷/Arc、卷目标、**卷末不可逆变化**、主线/支线线程、人物弧、未回收伏笔+计划回收区间、世界规则、关键剧情节点（不可逆事件） | `context_builder.layer2` + `_common.parse_outline`（outline.md 成为一等输入） |
| L3 | 全书 Canon Database | 人物当前状态、知识边界、物品归属、关系、事件、伏笔索引 | `context_builder.layer3`（只取本章涉及实体相关行） |

**动态检索链路**：本章任务 → 识别人物 → 识别地点 → 识别冲突 → 识别相关伏笔 → 查询 Canon → 查询最近剧情 → 查询当前卷/Arc → 组装 `canon_digest`。

`canon_digest` 取代了旧的 `bible_digest = bible.md[:4000]`：它是**按本章实体生成的**，因此在第 5 章和第 900 章都保持同一量级，同时始终准确。

---

## 3. Canon DB schema（SQLite + JSON 字段）

完整 DDL 见 [`DDL.sql`](DDL.sql)（由 `python scripts/canon.py --root <项目> ddl --out docs/DDL.sql`
从 `scripts/schema.sql` 生成，代码与文档不会漂移）。

实体分组：

| 组 | 表 | 说明 |
|---|---|---|
| 元信息 | `meta` | schema 版本、创建/重建时间 |
| 章节 | `chapters` | 章号、哈希、字数、卷、Arc、轮次、时间、plan_id、门状态 |
| 实体 | `entities` | kind ∈ character/location/organization/item，含 aliases(JSON)、data(JSON) |
| 人物状态机 | `character_states`, `character_state_changes` | 当前 9 字段状态；逐字段 BEFORE→AFTER 变更日志 |
| 事件 | `events` | code/chapter/kind/participants/location/causes/effects/irreversible |
| 关系 | `relationships` | 有向、带 kind/status/时间区间 |
| 伏笔 | `foreshadowings` | 生命周期状态、埋设章、计划回收区间、实际回收、泄露标记 |
| 线程 | `plot_threads` | main/sub、状态、开启/关闭章 |
| 世界规则 | `world_rules` | 编号化、不可变标记 |
| 知识 | `knowledge` | 谁在何时通过什么获知哪个事实、是否机密 |
| 物品 | `items` | 持有者、位置、状态 |
| 卷/Arc | `volumes`, `arcs` | 由 outline.md 解析同步 |
| 时间线 | `timeline` | 每章世界时间，用于单调性校验 |
| 快照 | `snapshots` | 章节 delta 快照与人工修正补丁的登记 |
| 计划/审计/门 | `plans`, `audits`, `gates` | Chapter Plan、双层级审计记录、四道门状态 |
| 事务/轮次 | `transactions`, `round_runs`, `round_tasks` | 事务审计、轮次计划与任务 |
| 可追踪性 | `canon_changes`, `warnings` | who/when/what/why；所有不确定判断 |
| 语义索引 | `scene_index` | 场景签名，用于 POSSIBLE_PLOT_REPETITION |

关键约束（写进 DDL，不靠约定）：

* `entities(kind, name)` 唯一；`events.code`、`foreshadowings.code`、`plot_threads.code`、
  `world_rules.code`、`items.code` 唯一。
* 外键：状态、关系、知识、物品持有者均引用 `entities(id)`，杜绝悬空引用。
* `foreshadowings.status` 的合法集合由 `foreshadow.FORESHADOW_STATES` 校验；
  `plot_threads.status` 由 `canon_db.THREAD_STATES` 校验。

---

## 4. Canon Snapshot 设计

每个章节提交成功后写入 `state/snapshots/ch-NNNN.json`，结构固定：

```json
{
  "schema": 1, "chapter": 7, "commit_id": "...", "created_at": "...",
  "chars": 3120, "chapter_sha256": "...", "volume": 1, "arc": "A2",
  "timeline": {"world_time": "2077-05-03"},
  "plan_id": "PL-....", "gates": {"plan":"pass","audit":"pass","canon":"pass","length":"pass"},
  "before": {"characters": {...}, "foreshadowing": {...}, "plot_threads": {...}},
  "events": [{"code":"E7-1","kind":"decision","summary":"...","participants":["李禾"],
              "location":"黑塔","causes":["E6-1"],"effects":[],"irreversible":false}],
  "after":  {"characters": {...}},
  "character_changes": [{"character":"李禾","field":"location","before":"黑塔","after":"麦田"}],
  "knowledge_added": [...], "relationship_changes": [...], "item_changes": [...],
  "world_rule_changes": [...], "foreshadow_changes": [...], "thread_changes": [...],
  "scenes": [...], "entities_created": [...], "entities_touched": [...],
  "warnings": [...], "uncertain": [...]
}
```

由此获得四项能力：

| 能力 | 命令 |
|---|---|
| 查看当时世界状态 | `canon.py --root <项目> snapshot --chapter 7`、`canon.py state --at-chapter 7` |
| 重新构建上下文 | `canon.py state --at-chapter N`（回放到临时库） |
| 回滚 / 重做 | `novel_state.py rollback --to-chapter N`、`canon.py rollback --to-chapter N` |
| 比较版本 | `canon.py compare 3 9` |

**快照必须自足**：`entities_touched` + `after` + 完整变更字段，使 `replay` 无需实时库即可重建。
这条不变量由 `canon.py verify-snapshots` 机器验证（逻辑投影级比较，忽略时间戳与自增 id）。

人工修正（`canon.py update`）与 `bible.md` 导入同样写成
`state/snapshots/manual-*.json` 补丁，因此**回滚与重放不会丢失人工决策**。

---

## 5. commit 事务流程

```
PREPARE   把本次提交将拥有的每个文件写入 state/tx/<txid>/（staging）
          对已存在的目标文件先做备份；写 journal.json(state=prepared)
DB        单个 SQLite 事务 BEGIN IMMEDIATE：
            chapters 行 + canon delta 应用 + snapshot 行 + gates + canon_changes
            + warnings + round_tasks
          COMMIT（失败则 ROLLBACK，staging 丢弃，章节保持未提交）
PUBLISH   journal.state=db_committed → 逐个 os.replace 原子发布
FINISH    journal.state=committed → 写 journal.jsonl → 删除 staging 目录
```

崩溃恢复语义（`tx.recover`，`next` 自动调用）：

| 崩溃点 | journal.state | 恢复动作 |
|---|---|---|
| staging 期间 | `prepared` | 丢弃 staging；章节未提交（数据库未动） |
| 数据库提交后、发布前 | `db_committed` | **补齐发布**（章节文本是第一手事实，绝不丢） |
| 发布中途 | `published` | 继续补齐剩余文件（幂等） |
| 完成后 | `committed` | 清理 staging |

`recover` 之后 `next` 还会调用 `_resync`：从 Canon 重建派生视图与 `chapters_committed`，
使 Markdown 与计数永远收敛到数据库。

---

## 6. 状态机

### 6.1 伏笔生命周期

```
PLANTED ──touch──► ACTIVE ──develop──► DEVELOPING ──ready──► PAYOFF_READY ──resolve──► RESOLVED
   │                  │                    │                      │
   └──────────────────┴────────────────────┴──────────────────────┴──leak──► STALE_LEAK
```

* 合法迁移表在 `foreshadow.LEGAL_TRANSITIONS`；非法迁移在审计中**阻断**提交。
* 计划回收窗口 `planned_payoff_start/end` 决定「相关度评分」与「逾期」判定
  （逾期 = 超过窗口 `OVERDUE_GRACE=3` 章仍未 RESOLVED）。
* `next` 输出 `relevant_foreshadowing`（带 score 与 reasons）优先提醒写作者。

### 6.2 人物状态机

9 个字段：`location | goal | emotion | belief | relationship | knowledge | health | possession | status`。

* 每章提交写入 `character_state_changes`（BEFORE → AFTER 逐字段）。
* 不可逆迁移（`dead → alive`、`destroyed → intact`）默认**拒绝自动写入**并记 warning；
  只有人工 `canon.py update` 才能改（人工决策可追踪、可回滚）。
* `after` 缺失 → 不改 Canon，只记 warning（保守默认）。

### 6.3 轮次状态机

```
next(开轮, 写 round_runs.planned_chapters)
   → 每章 commit 成功后 round_tasks(stage=committed, done)
   → round-done 全条件校验
        ├─ 全部满足 → ROUND_COMPLETE → rounds_completed+1 → REINCARNATE/STOP
        └─ 任一不满足 → ROUND_BLOCKED(exit 3)，轮次保持 open，计数不变
```

`round-done` 检查项：本轮计划章数 / 实际提交章数 / 每章审计（L1+L2）与四道门 /
未处理草稿 / parts 残留 / 半开事务 / Canon 章节行与磁盘一致 / 每章 snapshot /
章节号连续 / progress 一致 / `validate_state` 无 error。

---

## 7. 目录结构

```
<项目>/
├── chapters/     ch-0001.txt                 # 正文（第一手事实）
├── drafts/       ch-0003.md, committed/      # 草稿；提交后归档到 committed/
├── parts/                                    # 长章分节源文件
├── state/
│   ├── progress.json                         # 进度（schema 2，原子写）
│   ├── canon.db                              # ★ Canon DB（Source of Truth）
│   ├── snapshots/  ch-NNNN.json, manual-*.json, base-ch-*.db, pre-rollback-*.db
│   ├── plans/      ch-NNNN.plan.json, ch-NNNN.delta.json
│   ├── audits/     ch-NNNN.json
│   ├── derived/    bible.md, *.json          # 机器可读派生视图
│   ├── tx/                                   # 提交 staging（正常应始终为空）
│   ├── bible.md                              # 派生视图（手写源见 bible.source.md）
│   ├── bible.source.md                       # import-md 前的原始手写圣经
│   ├── outline.md                            # ★ 人写的计划视图（next 的一等输入）
│   ├── plot-ledger.md                        # 派生视图
│   ├── summaries/ch-NNNN.md                  # 派生视图（BEFORE→AFTER 结构化）
│   ├── last-context.md                       # 转世交接（自动生成，可 --context-file 覆盖）
│   ├── banned-phrases.txt                    # Level 1 禁用词
│   ├── warnings.jsonl, journal.jsonl, auto.log
│   └── rolled-back/                          # 回滚时被隔离的章节与其产物
├── exports/      book.txt, vol-N.txt, STATS.md, <书名>.zip
│                 （旧项目若已有 export/ 则继续复用 export/，见兼容性说明）
├── references/   craft-notes.md, loop-prompts.md
├── scripts/      全部脚本
└── tests/        测试套件
```

---

## 8. 单一职责的 Agent 划分（不使用万能超级 Prompt）

| 角色 | 职责 | 入口 |
|---|---|---|
| Planner | 生成 Chapter Plan / Scene Plan（本章目标、冲突、必须推进的主支线、必须改变的状态、禁止泄露、相关伏笔、场景分解） | `novel_state.py plan --chapter N`、`scene-plan` |
| Writer | 依据 plan + 动态上下文写正文 | 模型 / subagent / `auto-next --writer` |
| Extractor | 正文 → 结构化 Canon Delta（JSON） | `novel_state.py extract`、`auto-next --extractor`、`--llm` |
| Consistency Auditor | Level 2 语义一致性（人物/知识边界/时间线/地点/物品/关系/因果/伏笔生命周期） | `novel_state.py audit` |
| Style Auditor | Level 1 文本卫生 + 不改变事实的润色 | `audit_quality.py`、`polish_prose.py`、`fix_quotes.py` |
| Canon Manager | Canon 查询、人工修正、导入、回滚、Schema/DDL | `canon.py` |
| Recovery Manager | 崩溃恢复、残留检测、状态校验 | `novel_state.py recover`、`validate_state.py` |

每个角色只输出**结构化、可校验、可追踪**的东西；确定性工作（章号、哈希、事务、
schema、时间线、门禁）全部由 Python 完成，不交给模型。
