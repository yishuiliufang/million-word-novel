---
name: million-word-novel
description: 长篇叙事持续生产操作系统。以「Canon DB 长期记忆 + 上下文转世」为协议，每一轮新上下文只从 state/canon.db 查询与本章相关的信息，按 plan → write → audit → extract canon → commit 的顺序生成并提交章节，收轮时做全条件校验，循环直到全书字数达标。适用于「写一本 X 万/百万字的小说」「长篇小说连载」「分卷长篇创作」等超长文本任务。当目标字数远超单次上下文容量（≥5 万字）时使用本技能。
---

# 百万字长篇小说 · 长篇叙事持续生产操作系统

> 单次上下文写不完一百万字的唯一原因是：**记忆放在上下文里，而上下文会死**。
> 本技能把长期记忆放进 `state/canon.db`，把 Markdown 降级为派生视图，让上下文变成可抛弃的消耗品。

## 0. 心法（先读这六条，再动手）

1. **上下文是缓存，磁盘是内存。** 任何只存在于对话里的信息都算已经丢失。
2. **Canon DB 才是长期记忆。** `bible.md` / `plot-ledger.md` / `summaries/` 都是派生视图，
   会被自动重建；事实来源永远是 `state/canon.db`。
3. **章节正文是第一手事实来源。** 一切摘要、台账、状态都从正文与结构化 Canon Delta 派生。
4. **commit 之前必须 audit。** 审计绑定文本哈希；改一个字，旧审计立刻失效。
5. **任何不确定的自动判断都不修改 Canon，只记 warning，由人工确认。**
6. **停止条件只有一个：`total_chars >= target_chars`。** 但「写够字数」≠「完成小说」：
   `round-done` 会校验结构完整性、伏笔与线程、审计与 Canon 同步。

**没有 plan 不写，没有 audit 不提交，校验不过不收轮。**

---

## 1. 首次启动（只做一次）

先问清楚（如果用户没说）：**题材、主角、核心设定、基调、目标字数**。目标字数默认 1,000,000。

```bash
python scripts/novel_state.py init \
  --root "<项目目录>" \
  --title "<书名>" \
  --premise "<一句话故事>" \
  --target 1000000 \
  --chars-per-chapter 3000 \
  --chapters-per-round 5 \
  --volumes 5
```

初始化会生成（详见 `README.md` 与 `docs/ARCHITECTURE.md` §7）：

```
<项目目录>/
├── chapters/  drafts/  parts/
├── state/
│   ├── progress.json          # 进度
│   ├── canon.db               # ★ Canon DB（Source of Truth，25 张表）
│   ├── snapshots/ plans/ audits/ derived/ tx/
│   ├── bible.md               # 派生视图（手写源：bible.source.md）
│   ├── outline.md             # ★ 人写计划视图，next 的一等输入
│   ├── plot-ledger.md  summaries/  last-context.md
│   └── banned-phrases.txt
├── exports/
├── references/  scripts/  tests/
```

**初始化后立刻做两件事：**

1. 把 `state/bible.md` 填成真设定：世界规则编号化（R1/R2…），人物写清「核心欲望 + 致命缺陷」，
   规划至少 12 条横跨全书的伏笔（F001…）并写明**计划回收区间**（例如 `4-6`），
   登记主线/支线线程（T1…）。然后**灌进 Canon**：

   ```bash
   python scripts/canon.py --root "<项目目录>" import-md
   ```

   原件会备份为 `state/bible.source.md`；此后 `bible.md` 变成自动派生视图，
   需要改设定请用 `python scripts/canon.py --root "<项目>" update ...`。

2. 把 `state/outline.md` 的每一卷填上「卷末不可逆变化」。
   **卷末必须有人再也回不去**，否则百万字会变成原地踏步。
   `next` 会解析这份文件并把卷目标/不可逆变化放进本章的 L2 结构记忆。

---

## 2. 轮回协议（每一轮严格照做）

### 2.1 开轮：取简报，不读旧章

```bash
python scripts/novel_state.py next --root "<项目目录>"
```

`next` 会先自动执行崩溃恢复（`recover`），然后返回这一轮的**全部依据**。不要通读旧章。

关键字段：

| 字段 | 用途 |
|---|---|
| `action` | `WRITE` 写本章 / `ROUND_DONE` 本轮写完该收轮 / `STOP` 达标停止 |
| `chapters_to_write` | 本轮要写的章节号，**一章不多写** |
| `chars_tolerance` | 每章字数区间，低于下限重写、高于上限拆章 |
| `draft_mode` / `parts_per_chapter` / `part_chars` | 长章必须分节写时的预算 |
| `context` | **四层记忆全量**：`L0_current` / `L1_local` / `L2_structure` / `L3_canon` |
| `canon_digest`（= `bible_digest`） | 按本章实体生成的硬约束摘要（不再是文件前 4000 字符） |
| `entities` | 本章涉及的人物/地点/物品 |
| `character_states` | 本章人物的 Canon 当前状态 |
| `relevant_foreshadowing` | 相关伏笔（带 score 与 reasons，逾期会标出） |
| `volume_goal` / `volume_irreversible_change` | 本卷目标与卷末不可逆变化 |
| `unresolved_threads` | 未解决的主支线 |
| `recovery` | 上一章是否已提交、有无残留草稿、是否需要 recover |
| `plan_required` / `plan_command` / `audit_command` / `delta_file` | 后续步骤的确切命令 |

> **绝对不要**为了「找感觉」而通读前面所有章节。要细节就查 `canon.py`。

### 2.2 规划：先 plan，再写

```bash
# 生成骨架（从 Canon 自动填：人物当前状态、必推进的主支线、相关伏笔、禁止泄露、时间线）
python scripts/novel_state.py plan --root "<项目目录>" --chapter 7 --out "<项目>/state/plans/ch-0007.plan.json"

# 用 agent 或人把叙事字段填好后校验（不通就不许写正文）
python scripts/novel_state.py plan --root "<项目目录>" --chapter 7 \
  --file "<项目>/state/plans/ch-0007.plan.json" --check

# 展开为逐场工作单
python scripts/novel_state.py scene-plan --root "<项目目录>" --chapter 7
```

Chapter Plan 必填：`chapter_goal`（本章目标）、`core_conflict`（核心冲突，禁止空）、
`must_advance_main` / `must_advance_sub`（必须推进的主/支线）、`characters`（涉及人物）、
`must_change`（必须发生的人物状态变化，`{character, field, before, after}`）、
`forbidden_reveals`（禁止提前泄露的信息）、`scenes`（每场含 conflict + participants）。
没有声明任何状态变化只是警告——但它正是注水章节的典型特征，请不要忽略。

### 2.3 写作：一章一提交

对 `chapters_to_write` 里的每一章：

1. 写草稿到 `drafts/ch-NNNN.md`（**只写 drafts/**，绝对不要直接写 `chapters/`）。
   按 `chars_tolerance` 控制字数。
2. 抽 Canon Delta（正文 → 结构化事实）：

```bash
python scripts/novel_state.py extract --root "<项目目录>" --chapter 7 \
  --file "<项目目录>/drafts/ch-0007.md" \
  --out "<项目目录>/state/plans/ch-0007.delta.json"
```

   也可以由 agent 直接写 `state/plans/ch-0007.delta.json`（`commit` 会自动发现它），
   或用 `--llm command:<提取器命令>` 让模型产出后仍走同一套校验。
   **任何不确定的字段请标 `uncertain: true` 或 `confidence: 0.4`**：它们会被剔除并记为 warning，
   而不是写进 Canon。

3. 审计（Level 1 文本卫生 + Level 2 语义一致性）：

```bash
python scripts/novel_state.py audit --root "<项目目录>" --chapter 7 \
  --file "<项目目录>/drafts/ch-0007.md" --strict
```

4. 提交（事务；四道门 + 快照 + 索引）：

```bash
python scripts/novel_state.py commit --root "<项目目录>" --chapter 7
```

   被拒就按提示修：字数不足补场景（不许注水）、缺 plan 先规划、
   审计未通过按具体阻断项重写、Canon 校验失败先修 Delta。
   **不要**习惯性使用 `--allow-*`；每次放行都会写入 `GATE_WAIVED` warning 并被 `round-done` 汇总。

#### 长章分节（每章 ≥8000 字时必须用）

一次模型输出写不完 2 万字——会截断，几千字之后文笔也会明显滑坡。所以长章必须**分节写、分节追加**：

```bash
python scripts/novel_state.py part --root "<项目目录>" --chapter 43 --file "<项目>/parts/ch43-p1.txt"
python scripts/novel_state.py part --root "<项目目录>" --chapter 43 --file "<项目>/parts/ch43-p2.txt"
python scripts/novel_state.py part --root "<项目目录>" --chapter 43 --check
python scripts/novel_state.py commit --root "<项目目录>" --chapter 43 \
  --file "<项目目录>/drafts/ch-0043.md"
```

当 `chars_per_chapter >= 8000` 时 `next` 自动切换 `draft_mode: "parts"` 并给出预算。
**分节铁律：每一节必须换场景/换冲突/换时间点。** 把同一段内容换个说法重写一遍凑字数，
是长章写作最常见的失败方式。`round_char_budget`（默认 40000）会自动压缩每轮章数——
不要试图一轮写完 5 个 2 万字长章（那是 10 万字，必然烂尾）。宁可多转世几轮。

### 2.4 文本卫生与润色（提交前的冗余保险）

```bash
python scripts/polish_prose.py --file "<项目目录>/drafts/ch-0043.md"
python scripts/fix_quotes.py  --file "<项目目录>/drafts/ch-0043.md"          # 只报告
python scripts/fix_quotes.py  --file "<项目目录>/drafts/ch-0043.md" --apply  # 确认后写盘
python scripts/check_seams.py --dir "<项目目录>/parts"
```

`polish_prose.py` 分两类 pass：

* **CANON-SAFE（默认执行）**：同人分句合并、代词标签合并（`"A。"他说，"B。"` → `"A。B。"`）、
  整句重复合并、同人标签池轮换。
* **CANON-AFFECTING（默认拒绝）**：称呼式台词、短应答、填充动作。
  这三个 pass 会**新造人物动作**（`"陈老师。"她说。` → `李禾抬头看他。`），
  属于创作而非润色。它们会被报告为 `CANON_AFFECTING_EDIT`；确需应用时加
  `--allow-canon-edits`，**然后必须重新 audit**（commit 的哈希校验会强制这一点）。

`fix_quotes.py` 默认**不写任何字节**，发现待修复行时返回退出码 1；加 `--apply` 才写盘。

### 2.5 收轮：转世 or 涅槃

```bash
python scripts/novel_state.py round-done --root "<项目目录>"
```

严格校验以下全部条件，任一不满足即 `ROUND_BLOCKED`（**退出码 3**）并保持轮次 open：

本轮计划章数 / 实际提交章数 / 每章的 L1+L2 审计与 plan·audit·canon 门 / 未处理草稿 /
`parts/` 残留 / 半开事务 / Canon 章节行与磁盘一致 / 每章 snapshot / 章号连续 /
`progress.json` 一致 / `validate_state` 无 error。

* `verdict == REINCARNATE` → 本轮结束，`state/last-context.md` 已自动生成（未回收伏笔、
  未解决线程、人物位置、待确认 warning）。**开新上下文，从 2.1 重新开始。**
* `verdict == STOP` → 全书达标，进入第 3 节导出，**永久停止**。
* 确实要放弃本轮 → `round-done --abandon --reason "改大纲"`：
  记录原因、不计轮次、把未提交草稿隔离到 `drafts/abandoned/`。

### 2.6 转世的具体做法

轮回靠**丢弃上下文**实现：

* 优先用 **subagent（每轮一个全新子 agent）**，把 `next` 的简报作为唯一提示词交给它。
* 需要跨轮持久推进时用 `create_goal` 建立长期目标；每轮结束用 `update_goal` 报告进度。
* 上下文将满时**不要硬撑**：先 `round-done`，再开新轮。

**转世检查清单（新上下文开局自检）：**

- [ ] 已跑 `next`（它已自动做过 recover）
- [ ] 已看 `brief.recovery.resume`，确认没有残留草稿/半开事务
- [ ] 已确认 `next_chapter` 与磁盘最大章号连续
- [ ] 已知本轮写哪几章、每章多少字、哪些伏笔在回收窗口
- [ ] 已确认 `chapters_to_write` 里的第一章有 plan（没有就先 `plan`）

---

## 3. 涅槃：达标即停止并导出

```bash
python scripts/validate_state.py --root "<项目目录>"                    # 0 个 error
python scripts/canon.py --root "<项目目录>" verify-snapshots            # 快照可完整重建
python scripts/audit_quality.py --root "<项目目录>" --all --json        # 全量审计
python scripts/novel_state.py status --root "<项目目录>"                # complete: true
python scripts/export_novel.py --root "<项目目录>" --by-volume --zip
```

产出：`book.txt`（全书）、`vol-N.txt`（分卷）、`STATS.md`（报告）、`<书名>.zip`（含 canon.db 与快照）。

**然后停止。** 不要再写「番外」加字数——达标即涅槃，多写反而破坏结构。

---

## 4. 命令速查

| 命令 | 作用 |
|---|---|
| `novel_state.py init` | 建项目、Canon DB 与目录结构 |
| `novel_state.py migrate` | 把旧版项目原地升级到 Canon 系统（不删文件） |
| `novel_state.py status [--json]` | 进度 + Canon 概况（人物/事件/未回收伏笔/逾期/快照数） |
| `novel_state.py next` | 本轮简报（转世入口，含四层记忆；自动 recover） |
| `novel_state.py plan` | Chapter Plan（新）或分卷/字数规划（旧，`--volume-plan`） |
| `novel_state.py scene-plan` | 把 Chapter Plan 展开为逐场工作单 |
| `novel_state.py extract` | 正文 → Canon Delta（不写 Canon），输出 JSON |
| `novel_state.py audit` | Level 1 + Level 2 审计并登记（`--strict` 失败退出码 1） |
| `novel_state.py commit` | 事务性提交：四道门 → 快照 → 索引 → 派生视图 |
| `novel_state.py part` | 长章分节追加（`--check` 看累计） |
| `novel_state.py import` | 承接已有稿件（按章序 glob 导入） |
| `novel_state.py round-done` | 收轮全条件校验（不合法 `ROUND_BLOCKED`，退出码 3） |
| `novel_state.py recover` | 补齐/丢弃中断的提交并重建派生视图（`--dry-run` 只看） |
| `novel_state.py rollback` | 回滚到某章（被撤销的内容隔离到 `state/rolled-back/`） |
| `novel_state.py validate` | 全量状态校验（exit 1 表示有 error） |
| `novel_state.py auto-next` | 自动 plan→write→audit→extract→commit（默认关闭） |
| `canon.py show/character/event/foreshadow/thread/knowledge/item/list` | Canon 查询 |
| `canon.py state --at-chapter N` | 回放重建「第 N 章时的世界状态」 |
| `canon.py snapshot --chapter N` / `compare A B` | 查看/比较 canon snapshot |
| `canon.py update ...` | **人工修正 Canon**（强制 `--reason`，who/when/what/why 入库） |
| `canon.py history` / `warnings` | 变更追踪 / 未解决 warning |
| `canon.py import-md` | 手写 `bible.md` → Canon（原件备份） |
| `canon.py ddl [--out <path>]` / `schema` | 规范 DDL / 表结构清单 |
| `canon.py verify-snapshots` | 证明快照日志能完整重建 canon.db（PASS/FAIL） |
| `canon.py rollback --to-chapter N` | 回滚 Canon |
| `validate_state.py --root <项目> [--json]` | 状态校验报告 |
| `count_words.py --per-chapter` | 逐章字数 |
| `audit_quality.py --chapter N --strict` | 单章质检（失败退出码 1） |
| `audit_quality.py --all [--legacy-json]` | 全书质检 / 旧版 JSON 形状 |
| `polish_prose.py --file <draft>` | 文风抛光（默认只做不改变事实的 pass） |
| `fix_quotes.py --file <draft>` | 引号修补（默认只报告；`--apply` 才写） |
| `check_seams.py --dir <parts>` | 分节接缝重复检查 |
| `scan_beats.py <chapter>` | 性别一致性检查（抓「两人对话冒出他」） |
| `strip_beats.py <chapter> [--apply]` | 删除打断对话的僵硬动作行 |
| `scan_tags.py <chapter...>` | 问/说错配检查（陈述句误配「问」） |
| `autodiversify.py --file <draft>` | 重复短语诊断（只报告） |
| `export_novel.py --by-volume --zip` | 合并成书 + 打包（含 canon.db 与快照） |
| `tests/run_tests.py` | 测试套件（PASS/FAIL 计数 + JSON 报告） |

---

## 5. 承接已有稿件（续写模式）

```bash
python scripts/novel_state.py init --root "<项目目录>" --title "<书名>" \
  --premise "<故事内核>" --target 1000000 --chars-per-chapter 4000

python scripts/novel_state.py import --root "<项目目录>" \
  --group "<手稿目录>/ch*.txt" --group "<手稿目录>/v2-ch*.txt" --start-chapter 1

# 关键：显式声明分卷，否则续写章节会被分错卷
python scripts/novel_state.py plan --root "<项目目录>" --volume-plan "42,50,50,50,50"

python scripts/audit_quality.py --root "<项目目录>" --all    # 先体检旧稿
python scripts/novel_state.py next --root "<项目目录>"        # 开始续写
```

**为什么第 3 步不能省：** 旧稿章节密度往往与新目标不一致（旧稿每章 2700 字、新目标 4000 字）。
不钉死分卷边界，脚本会按线性估算把续写第一章算进已写完的第一卷。

导入章节**不受字数门限制**（旧稿长度是历史事实），登记为 `audit_status=imported`
并生成最小快照，`validate_state` 只给 INFO。但旧稿常见的病灶（章内自我重复、复述上一章、
字数逐章衰减）必须处理——不要假装没看见，否则轮回只会把病灶复制到第 100 万字。

旧版项目（`schema 1`、没有 canon.db）用 `novel_state.py migrate` 原地升级：
升 schema、建 Canon、导入 bible、为每章补 Canon 行与快照、重建派生视图，**不删任何文件**。

---

## 6. 失败模式与对策

| 症状 | 根因 | 对策 |
|---|---|---|
| 前后设定打架 | Canon 没更新 | Delta 要如实写状态变化；用 `canon.py character <名字>` 核对当前状态 |
| 人物「死而复生」或跳跃 | 状态变化没走 Canon | 不可逆迁移会被拒绝；人工确认请用 `canon.py update` |
| 伏笔逾期、读者觉得「忘了收」 | 长期伏笔没有回收窗口 | 登记 `planned_payoff_start/end`；`next` 会提示相关伏笔，`validate_state` 会报 FORESHADOW_OVERDUE |
| 原地踏步、剧情注水 | 卷目标没有不可逆变化 / plan 没有 must_change | 重写 `outline.md`；plan 必须声明要改变的状态 |
| 章节越写越短 | 上下文疲劳 | 收轮转世，别硬撑；字数不足的稿直接拒绝 |
| 复述前文、读者跳读 | 读了太多旧章 | 只读 `next` 的简报；`POSSIBLE_PLOT_REPETITION`/`PREV_OVERLAP` 会提示 |
| 满篇 AI 套话 | 没跑审计 | `audit_quality.py --strict`，命中即重写 |
| commit 被拒说「没有同哈希的审计」 | 审计后又改了稿（含润色） | 重新 `audit`；这正是审计门的意义 |
| commit 被拒说「Canon 校验失败」 | Delta 有非法状态/时间线倒退/计划未兑现 | 按提示修 Delta 或补正文，不要先 `--allow-canon` |
| round-done 返回 ROUND_BLOCKED | 有草稿/审计缺失/快照缺失/校验错误 | 按 `blockers` 逐条修复；确实放弃用 `--abandon` |
| 回到旧状态需要重做 | 想撤销几章 | `novel_state.py rollback --to-chapter N`；被撤销内容在 `state/rolled-back/` |
| 进度对不上 | 手改文件 | 只用脚本改状态；`validate_state.py` 会指出具体不一致 |

---

## 7. 边界

* 本技能**只产出文本**，不自动发布、不自动投稿。
* 目标字数默认按「非空白字符数」计（`count_mode=no_ws`），可用 `--count-mode cjk` 只算汉字。
* 每章建议 3000 字、每轮 5 章；长章（≥8000 字）会自动切换分节模式。
* 语义判断依赖模型，但**所有确定性工作由 Python 保证**：章号、哈希、事务、schema、
  时间线、门禁、引用完整性。模型只提出 Canon Delta，写入前一律校验。
* 不需要 Redis / Kafka / Docker / 云数据库 / 任何第三方 Python 包。
