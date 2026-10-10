# 阶段 1 · 仓库现状审计报告

审计对象：`million-word-novel`（百万字长篇小说 · 轮回引擎）
审计方式：逐文件阅读源码，不依赖 README 描述。
审计范围：仓库全部文件（见 §1）。

---

## 1. 仓库清单（实际文件，非 README 描述）

| 文件 | 行数级 | 职责 |
|---|---|---|
| `SKILL.md` | 318 行 | 技能主体：轮回协议、命令速查、失败模式 |
| `README.md` | 95 行 | 快速开始、目录结构 |
| `agents/openai.yaml` | 4 行 | 接口定义（display_name / default_prompt） |
| `references/craft-notes.md` | — | 长篇技法笔记 |
| `references/loop-prompts.md` | 121 行 | 提示词模板 A–E |
| `scripts/_common.py` | 241 行 | 路径、progress 读写、字数统计（no_ws/cjk/all） |
| `scripts/novel_state.py` | 720 行 | 状态机：init/status/next/commit/import/plan/part/round-done |
| `scripts/audit_quality.py` | 206 行 | 质量门：字数/段落/禁用词/句式/8-gram/复述率/对话密度 |
| `scripts/polish_prose.py` | 327 行 | 文风抛光：回声对话/代词标签/称呼台词/短应答/填充动作 |
| `scripts/fix_quotes.py` | 180 行 | 修补丢失引号的对话行 |
| `scripts/check_seams.py` | 85 行 | 分节接缝重复检测 |
| `scripts/count_words.py` | 78 行 | 字数统计 CLI |
| `scripts/export_novel.py` | 127 行 | 合并成书 + zip |
| `scripts/scan_beats.py` | 46 行 | 动作行性别一致性（启发式） |
| `scripts/strip_beats.py` | 78 行 | 删除打断对话的动作行 |
| `scripts/scan_tags.py` | 59 行 | 陈述句误配「问」检测 |
| `scripts/autodiversify.py` | 103 行 | 重复短语诊断（只报告） |
| `scripts/__pycache__/_common.cpython-314.pyc` | 编译产物 | 说明曾在 Python 3.14 下运行 |

**不存在**：`tests/`、`state/`、`chapters/`、`drafts/`、任何数据库、任何 schema、任何单元测试。
**结论**：当前仓库是「可运行的写作流水线 + 约定式协议」，不是「有状态基础设施」。

---

## 2. 运行时目录结构（init 实际生成）

由 `novel_state.py: cmd_init` 决定：

```
<root>/
├── chapters/            ch-0001.txt（四位补零，仅 .txt 被识别）
├── state/
│   ├── progress.json    唯一权威（schema=1）
│   ├── bible.md         手写 Markdown，被当作事实来源
│   ├── outline.md       手写 Markdown，分卷总纲
│   ├── plot-ledger.md   追加式 Markdown 表格
│   ├── last-context.md  上一轮交接
│   ├── round-brief.md   （在 paths() 里声明，但全程从未写入）
│   ├── summaries/ch-NNNN.md
│   └── banned-phrases.txt
└── export/              成书产物
```

`drafts/`、`parts/` 只在文档与提示词里出现，`init` **不创建**。

---

## 3. 现有机制逐项现状

### 3.1 `progress.json`（`_common.save_progress`）
- 现有字段：`schema/title/premise/root/target_chars/count_mode/chars_per_chapter/chars_per_chapter_min/chars_per_chapter_tolerance/chapters_per_round/estimated_chapters/volume_count/volumes/rounds_completed/chapters_committed/created_at/updated_at/loop_protocol`。
- 写盘方式：`tmp + os.replace`，本身是原子的（**可保留**）。
- 问题：`chapters_committed` 由 `len(chapter_numbers())` 现算，属于**冗余派生字段**，一旦文件被外部改动就与事实脱节；没有任何校验。
- 问题：没有 `schema` 迁移机制；`schema=1` 写死。

### 3.2 `next`（轮次简报）
- `_bible_digest()` = `bible.md` **前 4000 字符硬截断**。
- `_ledger_tail()` = 台账最后 12 行。
- `previous_chapter_tail` = 上一章末 1200 字。
- `outline.md` **完全不参与** `next`（`grep` 全仓：`outline` 只出现在 init/路径/模板/plan，从未进入 brief）。
- 没有任何实体级检索：不认人物、不认地点、不认伏笔、不认 Canon。
- 结论：**截断式 Markdown 读取**，即任务书问题 1/问题 2 描述的核心病灶。

### 3.3 `commit`
- 顺序：字数校验 → 章号校验 → `shutil.copyfile` 落章 → 追台账 → 写摘要 → 存 progress。
- **先落盘再审计**：`commit` 完全不检查 `audit_quality.py` 是否跑过。任务书问题 4「已经 commit 了，之后 audit 才发现失败」在代码层面成立。
- **非事务**：copyfile、append ledger、write summary、save progress 四步任一失败即留下半提交状态，且没有 journal / 恢复路径。
- `--allow-short` / `--allow-long` 可绕过字数门，且不记录任何痕迹。
- 全部叙事信息（`--summary/--place/--threads/--people`）靠**人工输入**，默认 `（未填）`。

### 3.4 `round-done`
- 实现只有 4 行有效逻辑：`rounds_completed += 1` + 写 `last_round_at` + 打印 verdict。
- **不检查**计划章数/实际章数/审计状态/失败章/draft 残留/Canon 同步/progress 一致性。
- 任何状态都能「收轮」，包括「一章没写」。任务书问题 6 完全成立。

### 3.5 `audit_quality.py`
- Level 1 文本卫生齐备：字数区间、平均段长 >60、段数下限、禁用词、7 条句式雷同正则、8-gram 自我重复（长度自适应阈值）、与上一章 8-gram 重合率 >12%、对话密度。
- **8-gram 是唯一的重复判据**，且直接导致 FAIL。任务书问题 12 要求降级为辅助指标。
- **没有任何语义级检查**：人物一致性、知识边界、时间线、地点、物品、关系、因果全部为空。任务书问题 7 完全成立。
- 允许把「复述上一章」做成硬失败，这点**可保留**。

### 3.6 `polish_prose.py`（问题最严重）
- `thin_tags`：合并同人分句（安全，说话人不变）；池内轮换标签（`TAG_POOLS` 按人名分池，说话人不变，**相对安全**）。
- `thin_pronoun_tags`：`"A。"他说，"B。"` → `"A。B。"`（同一说话人，**安全**）。
- `fix_vocatives`：把 `"陈老师。"她说，` 整行替换成 **新造的动作句**（`他叫住她。`/`李禾叫了一声。`/`他把话头收住。`）——**凭空创造人物行为**。
- `fix_replies`：把 `"是。"` 替换成 `REPLY_BEATS` 里的 **新造动作**（点头/站起/坐下/看了他一眼）——**凭空创造行为**，且用启发式 `infer_gender` 猜性别，猜错即产生连续性错误（`scan_beats.py`/`strip_beats.py` 两个脚本就是为擦这个屁股而存在的）。
- `diversify`：把 `李禾站在那里` 换成 `李禾把脚跟钉在地上`——**改变物理动作与语义**。
- 工具自称「never changes facts, only attribution and stage directions」，但 stage direction 就是 canon。
- 结论：四个 pass 里有两个（`fix_vocatives`/`fix_replies`）+ `diversify` 属于**润色器偷偷创作剧情**。任务书问题 10 完全成立。

### 3.7 `fix_quotes.py`（文档不一致）
- 实现：默认 **report-only**，`--apply` 才写盘；有待修复时 **返回退出码 1**；另有未使用的 `--report-only`「(default) alias」。
- 文档：`README.md:34` 写 `python scripts/fix_quotes.py --file drafts/ch-0043.md` 并注释「修补丢失引号的对话行」；`SKILL.md:175` 同样列在「提交前必须先跑」的写盘步骤里。
- 结果：照文档执行会**报告 1 处、退出码 1、文件未变**，使用者会以为「修好了」。任务书问题 11 成立。
- 附带：`SKILL.md:256` 附近表格被字面 `n| ` 污染，命令速查表已损坏。

### 3.8 派生视图 vs 真相
- `bible.md` 是手写 Markdown，却被 `next` 当作硬约束、被 SKILL.md 称为「全书唯一的事实来源」。
- `plot-ledger.md` 是追加式 Markdown 表格，每章一行，无主键、无校验、可被任意文本编辑器破坏。
- `summaries/ch-NNNN.md` 只是复写 `--summary` 字符串。
- 三者都**没有结构化校验**，也没有任何「从正文提取」的路径。任务书问题 14/15/16 成立。

### 3.9 其他脚本
- `scan_beats.py` / `strip_beats.py`：硬编码人名与性别表（`MALE` 元组），用 `open(path)` 无 `encoding=`，在 Windows GBK 控制台会抛 `UnicodeDecodeError`。`strip_beats.py` 直接以 `sys.argv` 解析，无 `--help`、无退出码语义。
- `scan_tags.py`：`open()` 同样缺 `encoding=`；`ASK_TAG` 只覆盖「引号紧跟问」的形式。
- `check_seams.py`：逻辑合理，**可保留**。
- `count_words.py` / `export_novel.py` / `_common.py` 的字数三口径、原子写 progress、`volume_bounds`（显式 `volume_plan` 优先）**可保留**。
- `export_novel.py`：写死 `export/`，把 `progress.json` 与 Markdown 打进 zip 当作「状态归档」，但没有 Canon 数据。
- `autodiversify.py`：只报告不修改，注释里明确记录了「早期版本自动改写导致连续性崩坏」——**这是仓库里唯一一处已经吸取教训的地方**，其结论应被制度化。

### 3.10 兼容性现状（必须保留的对外契约）
- CLI 子命令：`init / status / next / commit / import / plan / part / round-done`，参数名与默认值。
- `next` 返回 JSON 的既有键：`action/chapters_to_write/chars_tolerance/bible_digest/ledger_tail/previous_chapter_tail/last_context/hard_rules/draft_mode/part_chars/parts_per_chapter/...`。
- 章文件命名 `ch-%04d.txt`；`audit_quality.py --strict` 失败退出码 1；`check_seams.py` 失败退出码 1；`fix_quotes.py` 待修复退出码 1。
- 目录：`chapters/ state/ export/`，以及 `state/{progress.json,bible.md,outline.md,plot-ledger.md,last-context.md,summaries/,banned-phrases.txt}`。
- 三个字数口径 `no_ws|cjk|all`。

---

## 4. 问题清单（按任务书问题编号对齐）

| # | 问题 | 代码位置证据 | 严重度 |
|---|---|---|---|
| P1 | 记忆不是全书级，靠截断式 Markdown | `_bible_digest` 前 4000 字符；无实体检索 | 致命 |
| P2 | `next` 非 query-driven | 无 Canon、无人物/地点/伏笔识别 | 致命 |
| P3 | 无 Canon Snapshot | 全仓无 snapshot 概念 | 致命 |
| P4 | commit 与 audit 顺序错误（先落盘后审计） | `cmd_commit` 无审计检查 | 致命 |
| P5 | commit 非事务 | copyfile+append+write+save 四步裸执行 | 致命 |
| P6 | round-done 只做 +1 | `cmd_round_done` 4 行有效逻辑 | 致命 |
| P7 | 无语义审计 | `audit_quality.py` 仅文本卫生 | 致命 |
| P8 | 无伏笔生命周期 | `bible.md` 表格「未回收」是自由文本 | 高 |
| P9 | 无人物状态机 | 人物状态是台账单元格里的一句人话 | 高 |
| P10 | 润色器创造 Canon | `fix_vocatives`/`fix_replies`/`diversify` | 致命 |
| P11 | fix_quotes 文档与行为不符 | 默认 report-only + exit 1，文档写成写盘 | 中 |
| P12 | 8-gram 是唯一重复判据且硬失败 | `repeated_phrases` 直接进 issues | 中 |
| P13 | 无 Chapter Planning | 流程 next → write → audit | 高 |
| P14 | 只有自然语言摘要，无状态变化 | ledger 列「人物状态变化」是人写的一句话 | 高 |
| P15 | 章节提交无 Canon 自动提取 | 全靠 `--summary/--place/--threads/--people` | 致命 |
| P16 | Markdown 被当作最终真相 | `bible_digest` + SKILL.md 措辞 | 高 |
| P17 | 无人工修正 Canon 的入口 | 无 `canon.py` | 高 |
| P18 | 无状态校验 | 无 `validate_state.py` | 高 |
| P19 | 无测试 | 无 `tests/` | 致命 |
| P20 | 无失败恢复 | 无 journal，`next` 不检测残留 | 致命 |
| P21 | 目录结构缺 drafts/parts/snapshots/plans/audits | `init` 只建 4 个目录 | 中 |
| P22 | CLI 无 auto-next | 无自动模式 | 低 |
| P23 | 单一巨型 prompt 倾向 | `loop-prompts.md` A/B 把全部职责塞给一个上下文 | 中 |
| P24 | 违反 Context is Cache 原则 | 无 DB，无派生/真相分层 | 致命 |

### 附加问题（审计新发现，任务书未列）
| # | 问题 | 证据 |
|---|---|---|
| X1 | `state/round-brief.md` 在 `paths()` 中声明但从未写入 | 死路径 |
| X2 | `scan_beats/strip_beats/scan_tags` 缺 `encoding="utf-8"`，Windows GBK 下崩溃 | `open(path, encoding="utf-8")` 缺失 |
| X3 | `strip_beats.py` 无 argparse，位置参数靠 `sys.argv` 手撸 | 无法 `--help` |
| X4 | `SKILL.md` 命令速查表被 `n| ` 污染 | 第 256 行附近 |
| X5 | `export_novel.py` 的 `vol_counts` 计算后未使用（`+ 0` 死代码） | 第 70 行 |
| X6 | `progress.json` 的 `chapters_committed` 与磁盘事实可能脱节且无人校验 | `save_progress` |
| X7 | `import` 绕过字数门是合理设计，但导入的旧章同样没有任何 Canon | `cmd_import` |
| X8 | `polish_prose.py --seed` 接受参数但从未用于任何随机选择（`random.seed` 后无 `random.*` 调用） | 第 297 行 |

---

## 5. 可保留设计清单（渐进式重构的基础）

| 保留项 | 理由 |
|---|---|
| R1 | **上下文是缓存，磁盘是内存** 的核心心法 | 与目标架构完全一致，就是升级为 Canon DB 的动机 |
| R2 | 章文件 `ch-%04d.txt` 命名 + 只允许顺序提交（禁止跳章） | 确定性、可机械校验 |
| R3 | 三种字数口径 `no_ws/cjk/all` 与 `count_chars` | 与中文平台口径一致，测试稳定 |
| R4 | `save_progress` 的 tmp+`os.replace` 原子写 | 正确做法，扩展为完整 staging/atomic rename |
| R5 | 显式 `volume_plan` 优先于线性估算 | 承接旧稿场景唯一正确的分卷方式 |
| R6 | `chars_per_chapter_tolerance` 区间 + 不足重写/超标拆章 | 有效且确定性的长度门 |
| R7 | 长章 `part` 分节追加 + `parts_per_chapter` 预算 | 解决单次输出截断，实测有效 |
| R8 | `round_char_budget` 压缩每轮章数 | 防止一轮塞 10 万字烂尾 |
| R9 | `audit_quality.py` 的 Level 1 全部检查项（禁用词/句式/段落/对话密度/复述率） | 文本卫生有效，原样保留为 Level 1 |
| R10 | 8-gram 检测的**算法**（长度自适应阈值 + 剥离 Markdown 脚手架） | 算法正确，只把「结论等级」从 FAIL 降为 advisory |
| R11 | `thin_tags` / `thin_pronoun_tags` / `check_seams.py` / `autodiversify.py`（只报告） | 不改说话人、不改事实、或本就只读 |
| R12 | `fix_quotes.py` 的四种修复形态与 `looks_like_indirect` 防护 | 检测逻辑经过实战打磨，只修 CLI 文档一致性 |
| R13 | `next` 的 JSON 契约（`action/chapters_to_write/chars_tolerance/...`） | 兼容性契约，新增字段而非替换 |
| R14 | `--strict` 退出码语义（0/1） | 脚本化与 CI 依赖 |
| R15 | 转世（REINCARNATE/STOP）双态模型 | 概念正确，只是判定条件要从「+1」升级为「全条件校验」 |
| R16 | 无第三方依赖（纯标准库） | 任务书禁止引入额外服务，SQLite 是标准库 |
| R17 | `references/craft-notes.md`、`loop-prompts.md` | 技法积累有效，提示词需按单一职责拆分 |

---

## 6. 风险清单

| 风险 | 影响 | 处置 |
|---|---|---|
| 改造破坏既有 CLI 契约 | 已有项目无法迁移 | `next` 只增字段；老子命令参数不动；Markdown 路径不变 |
| `round-done` 从「永远成功」变为「可阻断」 | 旧流程脚本会拿到退出码 3 | 列为**破坏点**，写进修改说明；提供 `--abandon` 显式放弃 |
| 审计从「只看文本」变为「先审计后提交」 | 必须先 audit 再 commit，顺序反了会失败 | 列为**破坏点**，属正确流程优先 |
| 8-gram 从 FAIL 降为 advisory | 旧项目里原本被卡的章会变通过 | 列为**破坏点**；同时新增语义重复检查补位 |
| `bible.md` 从真相降为派生视图 | 直接手改 bible.md 会被下一次导出覆盖 | 提供 `canon.py import-md` 回灌；派生文件带 AUTO-GENERATED 头 |
| SQLite 事务 + 文件 staging 的交叉一致性 | 崩溃点难以穷举 | journal 记录每步 + `recover` 幂等补齐；集成测试注入崩溃点 |
| 润色器默认变保守 | 抛光效果下降 | `--allow-canon-edits` 显式开启并强制重审 |

---

## 7. 完成判定证据

| 判定项 | 证据 |
|---|---|
| 能指出每个核心模块的现状 | §3.1–§3.10 逐模块，附行号级证据 |
| 能指出每个核心模块的问题 | §4 问题清单（P1–P24 + X1–X8），每项给出代码位置 |
| 能指出每个核心模块的风险 | §6 风险清单 |
| 可保留设计已识别 | §5（R1–R17），全部有理由 |
| 未根据 README 猜测结构 | §1 清单来自文件系统枚举；§3 结论来自源码阅读 |

**STAGE_COMPLETE: 仓库审计与现状报告**
- 输入：仓库全部 17 个文件（含 `scripts/__pycache__` 编译产物）
- 输出：本审计报告 `docs/AUDIT.md`
- 完成判定：P1–P24 全部定位到具体代码位置；可保留项 R1–R17 全部给出理由
- 交付物：`docs/AUDIT.md`
