# CLI 使用说明（实际可执行命令）

所有命令都假设当前工作目录是仓库根目录，`<项目>` 是小说项目目录。
无第三方依赖，只需 Python 3.8+（实测 3.11 / 3.12 / 3.14）。

---

## 0. 一分钟总览

```bash
# 1) 建项目（同时建立 state/canon.db）
python scripts/novel_state.py init --root "<项目>" --title "我的书" \
  --premise "一句话故事" --target 1000000 --chars-per-chapter 3000 \
  --chapters-per-round 5 --volumes 5

# 2) 填 state/bible.md 与 state/outline.md，然后把圣经灌进 Canon（只做一次）
python scripts/canon.py --root "<项目>" import-md

# 3) 取本轮简报（转世入口；自动执行 crash recovery）
python scripts/novel_state.py next --root "<项目>"

# 4) 规划本章（不通过不允许写）
python scripts/novel_state.py plan --root "<项目>" --chapter 1 \
  --file "<项目>/state/plans/ch-0001.plan.json" --check
python scripts/novel_state.py scene-plan --root "<项目>" --chapter 1

# 5) 写草稿（模型/skill 写正文，只写 drafts/）
#    然后把正文抽成 Canon Delta
python scripts/novel_state.py extract --root "<项目>" --chapter 1 \
  --file "<项目>/drafts/ch-0001.md" --out "<项目>/state/plans/ch-0001.delta.json"

# 6) 审计（Level 1 文本卫生 + Level 2 语义一致性）
python scripts/novel_state.py audit --root "<项目>" --chapter 1 \
  --file "<项目>/drafts/ch-0001.md" --strict

# 7) 提交（事务；plan/audit/canon/length 四道门）
python scripts/novel_state.py commit --root "<项目>" --chapter 1

# 8) 收轮（严格校验；不合法返回 ROUND_BLOCKED，退出码 3）
python scripts/novel_state.py round-done --root "<项目>"

# 9) 达标后导出
python scripts/export_novel.py --root "<项目>" --by-volume --zip
```

---

## 1. `novel_state.py` — 主编排器

| 子命令 | 作用 | 关键参数 |
|---|---|---|
| `init` | 建项目、`state/canon.db`、目录结构 | `--root --title --premise --target --chars-per-chapter --chapters-per-round --volumes --count-mode --force` |
| `migrate` | 把旧版（schema 1、无 Canon）项目原地升级 | `--root [--no-backfill-snapshots]` |
| `status` | 进度 + Canon 概况 | `--root --json` |
| `next` | 本轮简报（转世入口，含四层记忆） | `--root [--no-recover]` |
| `plan` | Chapter Plan（或旧版分卷/字数规划） | `--chapter --file --out --check`；旧版：`--volume-plan --chars-per-chapter --chapters-per-round --target` |
| `scene-plan` | 把 Chapter Plan 展开为逐场工作单 | `--chapter [--json]` |
| `extract` | 正文 → Canon Delta（不写 Canon） | `--chapter --file [--delta] [--llm] [--state-source delta\|plan] --out` |
| `audit` | 双层级审计并登记到 `audits` 表 | `--chapter [--file] [--delta] [--llm] --strict [--json]` |
| `commit` | 事务性提交（四道门 + 快照 + 派生视图） | `--chapter [--file] [--delta] [--llm] [--state-source] [--allow-plan] [--allow-audit] [--allow-canon] [--allow-short] [--allow-long] [--overwrite]` |
| `part` | 长章分节追加到 staging | `--chapter --file` / `--chapter --check` |
| `import` | 承接已有稿件 | `--group <glob>`（可重复，按章序）、`--start-chapter --limit --summary-prefix --overwrite` |
| `round-done` | 收轮：全条件校验 | `--root [--context-file] [--abandon --reason]` |
| `recover` | 补齐/丢弃中断的提交，并重建派生视图 | `--root [--dry-run]` |
| `rollback` | 回滚 Canon 与章节到某章 | `--to-chapter N`（保留 1..N）或 `--chapter N`（撤销第 N 章）`[--reason]` |
| `validate` | 全量状态校验（`validate_state.py` 的入口） | `--root [--json] [--report <path>]` |
| `auto-next` | 自动 plan→write→audit→extract→commit | `--writer <cmd> [--extractor <cmd>] [--llm] [--max-chapters N] [--continue-on-fail] [--writer-file <path>]` |

退出码约定：

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | `--strict` 审计未通过 / `validate` 发现错误 |
| 2 | 参数或前置条件错误（缺 plan、门禁拒绝、文件不存在、Canon 校验失败） |
| 3 | `round-done` 返回 `ROUND_BLOCKED` |
| 130 | `auto-next` 被 Ctrl+C 中断（可恢复） |

`auto-next` 的 Agent 协议（stdin/stdout 均为 UTF-8），一个极端重要的细节：
Windows 默认 stdio 编码是 ANSI 代码页，会破坏 JSON 简报，所以
`novel_state.py` 会给子进程注入 `PYTHONIOENCODING=utf-8`，Agent 侧请读写
`sys.stdin.buffer` / `sys.stdout.buffer`（见 `tests/fake_writer.py`）。

---

## 2. `canon.py` — Canon Manager（人工修正与查询）

```bash
python scripts/canon.py --root "<项目>" show
python scripts/canon.py --root "<项目>" character 李禾
python scripts/canon.py --root "<项目>" event E17
python scripts/canon.py --root "<项目>" foreshadow --code F001
python scripts/canon.py --root "<项目>" thread
python scripts/canon.py --root "<项目>" knowledge --fact FACT-003
python scripts/canon.py --root "<项目>" item --code I03
python scripts/canon.py --root "<项目>" list --kind character
python scripts/canon.py --root "<项目>" state --at-chapter 40
python scripts/canon.py --root "<项目>" snapshot --chapter 7
python scripts/canon.py --root "<项目>" compare 3 9
python scripts/canon.py --root "<项目>" history --kind character --ref 李禾
python scripts/canon.py --root "<项目>" warnings --all
python scripts/canon.py --root "<项目>" schema
python scripts/canon.py --root "<项目>" verify-snapshots
python scripts/canon.py --root "<项目>" ddl --out docs/DDL.sql
python scripts/canon.py --root "<项目>" import-md
python scripts/canon.py --root "<项目>" rollback --to-chapter 40 --reason "改大纲"
```

人工修正 Canon（必须给出 `--reason`，who/when/what/why 全部入库）：

```bash
# 人物状态
python scripts/canon.py --root "<项目>" update --kind character --ref 李禾 \
  --field location --value "旧码头" --actor "作者" --reason "第四章已到旧码头"

# 列表字段（知识/持有物/关系）用 JSON 值
python scripts/canon.py --root "<项目>" update --kind character --ref 李禾 \
  --field knowledge --json-value '[{"fact_code":"FACT-003"}]' \
  --actor "作者" --reason "补记第二章获知的信息"

# 伏笔状态与计划回收区间
python scripts/canon.py --root "<项目>" update --kind foreshadow --ref F017 \
  --field status --value PAYOFF_READY --reason "已到回收窗口"

# 世界规则 / 线程 / 物品 / 关系 / 别名 / 改名
python scripts/canon.py --root "<项目>" update --kind rule --ref R3 --value "新的规则文本" \
  --reason "补齐设定"
python scripts/canon.py --root "<项目>" update --kind thread --ref T1 \
  --field status --value dormant --reason "本卷暂缓"
python scripts/canon.py --root "<项目>" update --kind item --ref I03 \
  --field holder --value "沈默" --reason "第三章已转手"
python scripts/canon.py --root "<项目>" update --kind relationship --ref 李禾 \
  --target 沈默 --field trust --value "broken" --reason "决裂"
python scripts/canon.py --root "<项目>" update --kind alias --ref 李禾 \
  --json-value '["禾禾"]' --reason "补别名"
```

---

## 3. `validate_state.py` — 状态校验

```bash
python scripts/validate_state.py --root "<项目>"                 # 人读报告，有 error 退出码 1
python scripts/validate_state.py --root "<项目>" --json          # 机器可读
python scripts/validate_state.py --root "<项目>" --report out.md --warn-as-error
```

检查项：progress 与章节数量一致、章号连续、**章节哈希与提交时一致**、
每章 snapshot/审计/plan/门状态、Canon 孤儿实体与悬空引用、事件参与者存在、
关系引用人物、知识知情人存在、伏笔章节引用与回收区间、线程状态合法、
时间线单调、物品持有者、轮次未完成任务、半开事务、未提交草稿。

---

## 4. 文本工具（Level 1 与润色）

```bash
# 单章/全书质检（Level 1 + Level 2），失败退出码 1
python scripts/audit_quality.py --root "<项目>" --chapter 7 --strict
python scripts/audit_quality.py --root "<项目>" --all --json
python scripts/audit_quality.py --root "<项目>" --chapter 7 --legacy-json   # 旧版 JSON 形状

# 润色：默认只做“不可能改变事实”的合并/代词标签合并；任何会新造动作的改动被拒绝并报告
python scripts/polish_prose.py --file "<项目>/drafts/ch-0043.md"
python scripts/polish_prose.py --file "<项目>/drafts/ch-0043.md" --strict-canon   # 检出即退出码 1
python scripts/polish_prose.py --file "<项目>/drafts/ch-0043.md" --allow-canon-edits  # 显式放行（必须重审）

# 引号修补：默认只报告（退出码 1 表示有待修复），--apply 才写盘
python scripts/fix_quotes.py --file "<项目>/drafts/ch-0043.md"
python scripts/fix_quotes.py --file "<项目>/drafts/ch-0043.md" --apply

# 其他
python scripts/check_seams.py --dir "<项目>/parts"
python scripts/count_words.py --root "<项目>" --per-chapter
python scripts/autodiversify.py --file "<项目>/drafts/ch-0043.md" --min-count 4
python scripts/scan_tags.py "<项目>/chapters/ch-0043.txt"        # 陈述句误配「问」
python scripts/scan_beats.py "<项目>/chapters/ch-0043.txt"       # 动作行性别一致性
python scripts/strip_beats.py "<项目>/chapters/ch-0043.txt" --apply
python scripts/export_novel.py --root "<项目>" --by-volume --zip
```

---

## 5. 续写已有稿件（承接模式）

```bash
python scripts/novel_state.py init --root "<项目>" --title "书名" --target 1000000 \
  --chars-per-chapter 4000
python scripts/novel_state.py import --root "<项目>" \
  --group "<手稿>/ch*.txt" --group "<手稿>/v2-ch*.txt" --start-chapter 1
python scripts/novel_state.py plan --root "<项目>" --volume-plan "42,50,50,50,50" \
  --chars-per-chapter 4000
python scripts/audit_quality.py --root "<项目>" --all        # 先体检旧稿
python scripts/novel_state.py next --root "<项目>"
```

导入的章节不受字数门限制（旧稿长度是历史事实），但会被登记为
`audit_status=imported` 并生成最小快照；`validate_state` 以 INFO 提示而不是报错。

## 6. 升级旧项目

```bash
python scripts/novel_state.py migrate --root "<旧项目>"
python scripts/validate_state.py --root "<旧项目>"
python scripts/canon.py --root "<旧项目>" verify-snapshots
```

`migrate` 会：把 `progress.json` 升到 schema 2、建立 `state/canon.db`、
把 `bible.md` 灌入 Canon（原件备份为 `state/bible.source.md`）、
为每个已有章节建立 Canon 行与快照、解析 `outline.md`、重建派生视图。**不删除任何文件。**

## 7. 测试

```bash
python tests/run_tests.py                     # 全部用例 + PASS/FAIL 汇总 + JSON 报告
python tests/run_tests.py test_workflow       # 单个模块
python tests/run_tests.py --clean             # 跑完清理 .tmp-tests
python tests/run_tests.py -v                  # 详细输出
```
