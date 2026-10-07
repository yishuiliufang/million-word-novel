# million-word-novel（百万字长篇小说）

一个能真正写完 100 万字长篇小说的 DSH skill。

核心思想：**上下文是缓存，磁盘是内存。** 每轮写完就把记忆落盘、丢弃上下文、开新轮次续写——直到全书字数达标才停止。

## 快速开始

```bash
# 1. 建项目
python scripts/novel_state.py init --root "C:\work up\我的书" --title "我的书" \
  --premise "一句话故事" --target 1000000

# 2. 取本轮简报（转世入口）
python scripts/novel_state.py next --root "C:\work up\我的书"

# 3. 写完一章立刻提交
python scripts/novel_state.py commit --root "C:\work up\我的书" \
  --chapter 1 --file "drafts/ch-0001.txt" --summary "本章事件"

# 4. 质检
python scripts/audit_quality.py --root "C:\work up\我的书" --chapter 1 --strict

# 5. 收轮：REINCARNATE（转世）或 STOP（达标停止）
python scripts/novel_state.py round-done --root "C:\work up\我的书"

# 6. 达标后导出打包
python scripts/export_novel.py --root "C:\work up\我的书" --by-volume --zip
```

## 提交前必做：文风抛光与接缝检查

```bash
python scripts/polish_prose.py --file drafts/ch-0043.md   # 消除回声对话/称呼台词/填充动作
python scripts/check_seams.py --dir parts               # 抓分节接缝重复
```

## 长章分节（每章 ≥8000 字）

一次输出写不完 2 万字，所以长章分节写、分节追加：

```bash
python scripts/novel_state.py part --root "<项目>" --chapter 43 --file parts/p1.txt
python scripts/novel_state.py part --root "<项目>" --chapter 43 --check   # 看累计
python scripts/novel_state.py commit --root "<项目>" --chapter 43 --file drafts/ch-0043.md
```

`chars_per_chapter >= 8000` 时，`next` 会自动返回 `draft_mode: "parts"` 与分节预算。
`round_char_budget`（默认 40000）会自动压缩每轮章数，避免一轮塞进 10 万字而烂尾。

## 承接已有稿件（续写模式）

```bash
python scripts/novel_state.py import --root "<项目目录>" \
  --group "<手稿>\ch*.txt" --group "<手稿>\v2-ch*.txt" --start-chapter 1

# 必做：显式分卷，否则续写章节会被分错卷
python scripts/novel_state.py plan --root "<项目目录>" --volume-plan "42,50,50,50,50"

python scripts/audit_quality.py --root "<项目目录>" --all   # 先体检旧稿
python scripts/novel_state.py next --root "<项目目录>"       # 开始续写
```

## 目录结构

```
million-word-novel/
├── SKILL.md                      # 技能主体：轮回协议
├── agents/openai.yaml            # 接口定义
├── references/
│   ├── craft-notes.md            # 长篇技法与失败模式
│   └── loop-prompts.md           # 可直接复制的轮回提示词
└── scripts/
    ├── _common.py                # 共享工具（字数统计/状态读写）
    ├── novel_state.py            # 状态机：init/status/next/commit/round-done
    ├── count_words.py            # 字数统计（三种口径）
    ├── audit_quality.py          # 质量门：套话/重复/字数/对话密度
    └── export_novel.py           # 合并成书 + 打包 zip
```

## 停止条件

**只有 `total_chars >= target_chars`。** 到上限就转世，达标就停止。

## 三种字数口径

| 口径 | 含义 |
|---|---|
| `no_ws`（默认） | 非空白字符数，与中文平台口径一致 |
| `cjk` | 仅汉字 |
| `all` | 含空白原始字符数 |

## 实测状态

已在 Windows + Python 3.14 下完整跑通：init → next → commit（含字数拦截）→ audit（含套话/重复拦截）→ round-done（REINCARNATE / STOP）→ export --zip。
