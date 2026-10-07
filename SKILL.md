---
name: million-word-novel
description: 生成百万字长篇小说的轮回写作引擎。以「状态落盘 + 上下文耗尽即转世」为协议，每一轮新上下文只读取状态文件与轮次简报，续写若干章后立刻归档并转世，循环直到全书字数达标才停止。适用于「写一本 X 万/百万字的小说」「长篇小说连载」「分卷长篇创作」等超长文本任务。当目标字数远超单次上下文容量（≥5 万字）时使用本技能。
---

# 百万字长篇小说·轮回引擎

> 单次上下文写不完一百万字的唯一原因是：**记忆放在上下文里，而上下文会死**。
> 本技能把记忆全部搬到磁盘，让上下文变成可抛弃的消耗品——这就是「轮回」。

## 0. 心法（先读这三条，再动手）

1. **上下文是缓存，磁盘是内存。** 章节、设定、台账、进度全部落盘。任何只存在于对话里的信息都算已经丢失。
2. **每一轮都是转世。** 新轮次不继承上一轮的对话记忆，只继承 `state/` 下的文件。转世时必须能仅凭简报无缝续写。
3. **到上限就转世，达标就涅槃。** 上下文将满时收尾归档并转世；字数达标时导出全书并永久停止。

**停止条件只有一个：`total_chars >= target_chars`。** 轮次上限、疲劳、写得好不好都不是停止理由。

---

## 1. 首次启动（只做一次）

先问清楚（如果用户没说）：**题材、主角、金手指/核心设定、基调、目标字数**。目标字数默认 1,000,000。

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

初始化会生成：

```
<项目目录>/
├── chapters/                 # 正文，一章一文件 ch-0001.txt
├── state/
│   ├── progress.json         # 进度真相（唯一权威）
│   ├── bible.md              # 设定圣经：世界规则/人物/伏笔登记
│   ├── outline.md            # 分卷总纲
│   ├── plot-ledger.md        # 剧情台账：每章一行
│   ├── last-context.md       # 上一轮的收尾交接（含未解悬念）
│   ├── summaries/            # 每章摘要
│   └── banned-phrases.txt    # AI 套话黑名单
└── export/                   # 成书产物
```

**初始化后立刻做两件事：**

1. 把 `state/bible.md` 从模板填成真设定：世界规则编号化（R1/R2…），人物写清「核心欲望 + 致命缺陷」，并规划至少 12 条横跨全书的伏笔（F1…），每条都写清计划回收章节。
2. 把 `state/outline.md` 的每一卷填上「卷末不可逆变化」。**卷末必须有人再也回不去**，否则百万字会变成原地踏步。

---

## 2. 轮回协议（每一轮严格照做）

### 2.1 开轮：取简报，不读旧章

```bash
python scripts/novel_state.py next --root "<项目目录>"
```

返回的 JSON 就是这一轮的**全部依据**（`action=WRITE` 或 `STOP`）：

| 字段 | 用途 |
|---|---|
| `chapters_to_write` | 本轮要写的章节号，**一章不多写** |
| `chars_tolerance` | 每章字数区间，低于下限重写、高于上限拆章 |
| `bible_digest` | 设定摘要（硬约束） |
| `ledger_tail` | 最近 12 条台账（防遗忘、防重复） |
| `previous_chapter_tail` | 上一章结尾 1200 字（**衔接语感用，禁止复述**） |
| `last_context` | 上一轮转世交接 |
| `hard_rules` | 硬性禁令，逐条遵守 |

> **绝对不要**为了「找感觉」而通读前面所有章节。那是最快的上下文自杀方式。要细节就查 `state/summaries/` 或 `grep` 台账。

### 2.2 写作：一章一提交

对 `chapters_to_write` 里的每一章：

1. 写入草稿文件（如 `<项目目录>/drafts/ch-0007.txt`），**按 `chars_tolerance` 控制字数**。
2. 立刻提交（脚本会校验字数、禁止跳章、追加台账与摘要）：

```bash
python scripts/novel_state.py commit \
  --root "<项目目录>" \
  --chapter 7 \
  --file "<项目目录>/drafts/ch-0007.txt" \
  --summary "本章发生的事（一句话）" \
  --place "2077年·麦田" \
  --threads "埋F3" \
  --people "李禾禾：决意离开"
```

3. 提交被拒（字数不足/超标）就**改稿重交，不要用 `--allow-short` 绕过**。

#### 长章分节（每章 ≥8000 字时必须用）

一次模型输出写不完 2 万字——会截断，而且几千字之后文笔会明显滑坡。所以长章必须**分节写、分节追加**：

```bash
# 每节写成一个独立文件（约 3000–4000 字），逐节追加到同一章的 staging 文件
python scripts/novel_state.py part --root "<项目目录>" --chapter 43 --file "<项目目录>/parts/ch43-p1.txt"
python scripts/novel_state.py part --root "<项目目录>" --chapter 43 --file "<项目目录>/parts/ch43-p2.txt"
...

# 随时查看累计进度
python scripts/novel_state.py part --root "<项目目录>" --chapter 43 --check

# 累计达标后再提交整章
python scripts/novel_state.py commit --root "<项目目录>" --chapter 43 --file "<项目目录>/drafts/ch-0043.md" --summary "..."
```

当 `progress.json` 里 `chars_per_chapter >= 8000` 时，`next` 简报会自动切换到 `draft_mode: "parts"`，并给出
`parts_per_chapter`（分几节）与 `part_chars`（每节多少字），照它执行即可。

**分节的铁律：每一节必须换场景/换冲突/换时间点。** 把同一段内容换个说法重写一遍来凑字数，
是长章写作最常见的失败方式，也正是存量旧稿里 `"对。"沈默说。` 重复 36 次的成因。

**轮次预算：** 一轮塞不下太多字。`round_char_budget` 默认 40000 字，脚本会自动把「每轮 5 章 × 2 万字」
压到「每轮 2 章」。**不要试图在一轮里写完 5 个 2 万字长章**——那是 10 万字，必然烂尾。宁可多转世几轮。

**每章的质量红线：**

- 必须推进主线冲突；禁止无冲突的过场章，禁止总结式收尾。
- 禁止复述前文已交代的信息。
- 对话要占相当比例（审计脚本会检查）；全章无对话视为说明文，不合格。
- 新设定、新伏笔、人物状态变化 → **回写 `state/bible.md`**。
- 命中 `banned-phrases.txt` 的句子必须重写。

**可选：并行产出草稿。** 当一轮章节较多（≥4 章）时，可用 subagent 并行起草多章，但**提交必须串行**（台账是追加写），且每章仍要过审计。

> **⚠ 并行起草的硬约束（实测踩过的坑）：**
> 起草者会**自作主张直接写入 `chapters/ch-NNNN.txt`**，绕过 `commit` 的字数校验与台账登记，
> 结果是"章号跳号 + 字数不足 + 进度不一致"。派活时必须明确：
> ① **只许写 `drafts/` 下的文件**，绝对不许碰 `chapters/`；
> ② 写完只汇报字数，由主流程统一抛光、质检、commit；
> ③ 每轮开始前核对 `chapters/` 的最大章号与台账行数是否一致，发现越权写入立即删除重来。
> 校验命令：`python scripts/novel_state.py status --root <项目目录>`（看 next_chapter 是否连续）。

### 2.3 质检：过闸才算数

```bash
python scripts/audit_quality.py --root "<项目目录>" --chapter 7 --strict
```

检查项：字数区间、段落长度（平均 >60 字判过长）、段数下限、AI 套话黑名单、句式雷同、本章内 8-gram 自我重复、与上一章重复率（>12% 判复述）、对话密度。

不合格 → 重写该章 → 重新提交。**不许带着 FAIL 进入下一章。**

> **长章注意**：重复阈值会随字数自动抬高（3000 字章按 ≥3 次判重，16000 字章按 ≥5 次）。
> 这是必须的——否则 1.6 万字的长章会永远卡在"某个短语出现 4 次"这种无关痛痒的问题上，
> 反复重写却永远写不完。

### 2.3.1 文风抛光：提交前的必经一步

长章写作（每章 ≥8000 字）几乎必然产生四种**机器式重复病**。它们在任何一章里都不显眼，
但乘以 60 轮就是灾难（《守望》旧稿单章出现同一句 36 次）：

| 病名 | 症状 |
|---|---|
| 回声对话 | `"对。"沈默点头。"XXX。"` 反复堆叠 |
| 称呼式台词 | `"陈老师。"她说。` 整行只有称呼 |
| 短应答连排 | `"是。"` 独占一行的确认乒乓 |
| 填充动作 | `李禾站在那里` / `沈默把笔放下` 成为唯一动作 |

**提交前必须先跑：**

```bash
python scripts/polish_prose.py --file "<项目目录>/drafts/ch-0043.md"
python scripts/fix_quotes.py  --file "<项目目录>/drafts/ch-0043.md"
```

`polish_prose.py` 会：合并同人分句（顺带删掉标签）→ **合并代词式标签**（`"A。"他说，"B。"`）→ 轮换句末标签 → 打散称呼式台词 → 归并短应答 → 轮换填充动作，最后打印最高频 8-gram 供自查。**幂等**，重复跑不会破坏文本。

> **代词式标签是隐藏杀手**：`。"他说，"` 只有 5 个字符，8-gram 重复检测看不见它。
> 实测一章 1.6 万字里出现过 46 次 `他说，` + 25 次 `她说，`，审计完全没报警。

`fix_quotes.py` 会：修补**丢失引号的对话行**（`拿石头做什么。他问。` → `"拿石头做什么。"他问。`）。
这在使用 shell heredoc / 多层引号写文件时极易发生——其它所有检查都发现不了，但会让书变得无法阅读。

预期效果：1.6 万字的章通常能消掉 100+ 处回声、40+ 处代词标签，最高频重复降到 3 次。

### 2.3.2 接缝检查（分节写作的隐藏陷阱）

分节写作时**相邻两节的接缝处极易写重**（上一节结尾与下一节开头重复）。
提交前务必检查：

```bash
python scripts/check_seams.py --dir "<项目目录>/parts"
```

发现接缝重复要**改分节源文件**，不要只改 staging —— 否则下次重建又会带回来。

### 2.4 收轮：转世 or 涅槃

一轮写完（或上下文用到 ~70%）就收尾：

```bash
python scripts/novel_state.py round-done --root "<项目目录>"
```

返回 `verdict`：

- **`REINCARNATE`** → 本轮到此为止。写到 `state/last-context.md`（未解悬念、下一步计划、语感要点），然后**开新上下文重新加载本技能**，从 2.1 开始下一轮。
- **`STOP`** → 全书达标，进入第 3 节导出，**永久停止**。

### 2.5 转世的具体做法（关键）

轮回靠**丢弃上下文**实现，因此：

- 优先用 **subagent（每轮一个全新子 agent）**：把 `next` 的简报作为唯一提示词交给它，让它完成本轮，返回简报式结论。这样每轮都是干净的上下文。
- 需要跨轮持久推进时，用 **`create_goal`** 建立长期目标并配合自动续轮；每轮结束用 `update_goal` 报告进度。
- 上下文将满时**不要硬撑**：先 `round-done`，再开新轮。硬撑的结果是忘设定、烂尾、重复。

**转世检查清单**（新上下文开局自检）：

- [ ] 已读 `state/progress.json`（用 `status` 命令，别直接读文件也行）
- [ ] 已跑 `next` 拿到简报
- [ ] 已确认 `next_chapter` 与磁盘上的最大章节号连续
- [ ] 已知道本轮要写哪几章、每章多少字

---

## 3. 涅槃：达标即停止并导出

```bash
python scripts/novel_state.py status --root "<项目目录>"     # 确认 complete: true
python scripts/audit_quality.py --root "<项目目录>" --all --json   # 全量质检
python scripts/export_novel.py --root "<项目目录>" --by-volume --zip
```

产出：`export/book.txt`（全书）、`export/vol-N.txt`（分卷）、`export/STATS.md`（报告）、`export/<书名>.zip`（归档）。

**然后停止。** 不要再写「番外」加字数——达标即涅槃，多写反而破坏结构。

---

## 4. 命令速查

| 命令 | 作用 |
|---|---|
| `novel_state.py init` | 建项目与状态文件 |
| `novel_state.py import` | **承接已有稿件**：把现存章节按顺序导入为已提交章节 |
| `novel_state.py plan` | **设定显式分卷**与每章字数目标 |
| `novel_state.py status [--json]` | 进度（字数/章节/卷/轮次） |
| `novel_state.py next` | 本轮简报（转世入口） |
| `novel_state.py commit` | 校验并提交一章 |
| `novel_state.py part` | **长章分节**：追加一节到 staging 文件（`--check` 看进度） |
| `novel_state.py round-done` | 收轮，返回 REINCARNATE/STOP |
| `count_words.py --per-chapter` | 逐章字数 |
| `polish_prose.py --file <draft>` | **文风抛光**：消除回声对话/代词标签/称呼台词/短应答/填充动作 |`n| `fix_quotes.py --file <draft>` | **修补丢失引号的对话行**（默认只报告，--apply 才写） |`n| `scan_beats.py <chapter>` | **性别一致性检查**：抓"两人对话冒出他" |`n| `strip_beats.py <chapter> --apply` | 删除打断对话的僵硬动作行 |`n| `scan_tags.py <chapter...>` | **问/说错配检查**：陈述句误配"问" |
| `check_seams.py --dir <parts>` | **接缝检查**：抓分节之间的重复 |
| `audit_quality.py --chapter N --strict` | 单章质检（失败退出码 1） |
| `audit_quality.py --all` | 全书质检 |
| `export_novel.py --by-volume --zip` | 合并 + 打包 |

---

## 4.5 承接已有稿件（续写模式）

不想从零开始、要接着一份现存手稿写下去时：

```bash
# 1. 建项目（字数目标填全书目标，不是剩余字数）
python scripts/novel_state.py init --root "<项目目录>" --title "<书名>" \
  --premise "<故事内核>" --target 1000000 --chars-per-chapter 4000

# 2. 导入现存章节。--group 按书的章节顺序重复给出，支持 glob
python scripts/novel_state.py import --root "<项目目录>" \
  --group "<手稿目录>\ch*.txt" \
  --group "<手稿目录>\v2-ch*.txt" \
  --start-chapter 1

# 3. 关键：显式声明分卷，否则 loop 会按线性估算把续写章节分错卷
python scripts/novel_state.py plan --root "<项目目录>" \
  --volume-plan "42,50,50,50,50" --chars-per-chapter 4000

# 4. 先体检存量稿件
python scripts/audit_quality.py --root "<项目目录>" --all

# 5. 从 next 开始正常轮回
python scripts/novel_state.py next --root "<项目目录>"
```

**为什么第 3 步不能省：** 现存稿件的章节密度往往与新建目标不一致（例如旧稿每章 2700 字、新目标每章 4000 字）。
若不定 `--volume-plan`，脚本会按「总字数 ÷ 每章字数 ÷ 卷数」线性切分，把续写的第一章算进已经写完的第一卷。
`--volume-plan` 用「每卷章数」直接钉死边界，是承接场景唯一正确的分卷方式。

**导入的章节不受字数门限制**（旧稿长度是历史事实），但**必须过质检**。存量稿件里常见的病灶：
章内自我重复（同一句话出现几十次）、复述上一章、字数逐章衰减。
发现这些问题时，先决定是「返工旧章」还是「带着旧账继续」——不要假装没看见，否则轮回只会把病灶复制到第 100 万字。

---

## 5. 失败模式与对策

| 症状 | 根因 | 对策 |
|---|---|---|
| 前后设定打架 | bible 没回写 | 每章提交后立即回写圣经；下一轮先读 `bible_digest` |
| 原地踏步、剧情注水 | 卷目标没有不可逆变化 | 重写 `outline.md`，给每卷设「回不去的事件」 |
| 章节越写越短 | 上下文疲劳 | 收轮转世，别硬撑；不足下限的稿直接拒绝 |
| 复述前文、读者跳读 | 读了太多旧章 | 只读 `ledger_tail` + `previous_chapter_tail`，禁止通读 |
| 满篇 AI 套话 | 没跑审计 | `audit_quality.py --strict` 卡住，命中即重写 |
| 进度对不上 | 手改文件 | 只通过脚本改状态；`progress.json` 是唯一真相 |

---

## 6. 边界

- 本技能**只产出文本**，不自动发布、不自动投稿。
- 目标字数默认按「非空白字符数」计（`count_mode=no_ws`），与常见中文平台口径一致；可用 `--count-mode cjk` 只算汉字。
- 每章建议 3000 字、每轮 5 章：这是「一轮能稳定写完且不爆上下文」的经验值。要调就调 `progress.json`，或 init 时用 `--chars-per-chapter` / `--chapters-per-round`。
