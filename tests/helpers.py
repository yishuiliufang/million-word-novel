"""Shared fixtures for the million-word-novel test suite (stdlib unittest only).

No pytest: the project must stay dependency-free, and `python -m unittest` is the
delivery mechanism. Every helper here is deterministic so the suite can assert
exact hashes, exit codes and canon rows.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

# Test projects are created INSIDE the workspace (not %TEMP%) so the suite runs
# under a workspace-confined file sandbox, and so a failed run leaves inspectable
# artifacts. `python tests/run_tests.py --clean` removes the directory.
TMP_ROOT = os.path.join(ROOT, ".tmp-tests")


def _mkdtemp(prefix: str) -> str:
    os.makedirs(TMP_ROOT, exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=TMP_ROOT)

import canon  # noqa: E402
import canon_db as db  # noqa: E402
import narrative_audit  # noqa: E402
import novel_state  # noqa: E402
import validate_state  # noqa: E402
from _common import count_chars, paths, read_text, tolerance  # noqa: E402

DEFAULT_TITLE = "测试小说"
DEFAULT_PREMISE = "一句话故事：一个记账的人发现账本在替世界记账。"

CHARACTERS = ("李禾", "沈默", "陈老师", "老赵", "周显", "秦川", "小吴", "苏晚")
LOCATIONS = ("黑塔", "麦田", "南巷", "旧码头", "钟楼", "灯塔")

_MOVES = (
    "把账本翻到新的一页", "在纸上画了一条横线", "把灯往桌角挪了半寸",
    "把窗缝掩上一线", "把口袋里的零钱摊在桌面", "用指甲刮掉纸边的一处墨点",
    "把椅子往后拖出半尺", "把抽屉拉开又推回去", "把水杯里的凉茶倒掉",
    "把袖子卷到小臂中间", "在门槛上停住脚", "把伞靠在门框内侧",
    "把围巾重新绕了一圈", "把火柴盒推到桌子另一边", "在墙上比了比身高",
    "把钥匙串挂上钉子", "把旧报纸叠成四方", "把鞋底的泥在台阶上蹭掉",
    "把钢笔灌满墨水", "把算盘珠子拨回原位", "把窗台上的灰抹了一道",
    "把麻绳量出三拃", "把炉子里的火拨旺一点", "把米袋扎紧了口",
    "把地图卷起来", "把盐罐挪到灶台左边", "把门闩插好",
    "把晒着的被单收进来", "把车链子重新套上齿轮", "把挂号信塞进邮筒",
    "把工具箱合上盖", "把剪刀放回抽屉", "把湿衣服搭到竹竿上",
    "把断掉的鞋带接起来", "把墨水瓶盖拧紧", "把窗帘拉成一条缝",
)

_SAYS = (
    "路是走出来的，不是算出来的。", "账上差三毛，人心就差三丈。",
    "你先别急着说话。", "这件事得从去年秋天算起。",
    "我数过三遍，还是这个数。", "塔里的灯昨晚亮到后半夜。",
    "风从北边来，麦子就要低头。", "你要找的人三年前就走了。",
    "别把名字写错，写错就找不回来了。", "这笔钱不是我的，也不是你的。",
    "船票我留着，等你想走再说。", "南巷那扇门今天没锁。",
    "你手上的印子是墨水，不是血。", "钟楼敲过十二下就该回来了。",
    "旧码头的水位比上个月低了两指。", "我认得这个字迹，是往前三年写的。",
    "欠条还在，人却没了。", "别对着灯看太久，眼睛会花。",
    "麦子收完就该翻地了。", "灯塔那儿没有人住，只有风。",
    "你问错了人，可我不怪你。", "这本账记得太干净了，不像是真的。",
    "有人替你签过一次名。", "箱子底下那层是空的。",
    "你要的答案在第二本账上。", "三年前那场雨，把路冲断了。",
    "我不欠你，你也不欠我。", "别抖，抖了就说不清。",
    "把他留下，比带走更难。", "你先看看纸背，再看正面。",
    "这件事我只说一次。", "数到第七页就该停了。",
    "麦田边上那间屋子空着。", "钥匙不在我这儿，在他那儿。",
    "我信账本，不信人。", "你走的时候把门带上。",
)

_NARRATE = (
    "灯芯短了一截，火苗缩成黄豆大小",
    "屋檐的水滴在青石上砸出浅坑",
    "院里的鸡叫过两遍就没了动静",
    "纸页边缘起了毛，翻动时有细响",
    "远处传来货船靠岸的闷响",
    "巷口的狗趴着，尾巴扫了一下地面",
    "天色从灰转白，又慢慢压下来",
    "炉膛里剩一块暗红的炭",
    "窗纸上映着一个弯腰的影子",
    "算盘上的浮灰被推到一边",
    "桌上那杯茶凉透了，浮着一层膜",
    "木门开合时咬住门框，发出短促的声",
    "雨点先是稀，后来连成一片",
    "风把挂着的牌子吹得偏了半寸",
    "脚步声在楼梯上停了停",
    "煤油味混着旧纸的味道",
    "墙角的蛛网上挂着一点灰",
    "水面浮着几片枯叶，慢慢打转",
    "他把手心的汗在裤子上擦了擦",
    "钟摆的声音忽远忽近",
    "半截蜡烛立在碟子上，火苗歪着",
    "门外有人在低声数数",
    "信封的封口被人拆开又粘上",
    "天井里的水缸映出一角屋檐",
    "铁锹靠在墙上，刃口缺了一小块",
    "他把账本合上，又打开",
    "院子里晾着的衣服被风掀起一角",
    "桌上的盐粒排成一条线",
    "有人在楼下敲了三下门",
    "屋檐下的燕子窝空着",
    "他把灯捻亮了一点",
    "窗外的麦子伏下去又直起来",
    "纸上的墨迹还没干透",
    "他把笔杆在指间转了半圈",
)

_WORLD_TIME = "2077-03-%02d"

_SCENE_CONFLICTS = (
    "对账时发现数目对不上", "追问三年前的旧事", "关于去留的争执",
    "钥匙归属的试探", "时间对不上的一次盘问", "账本真伪的怀疑",
    "被要求当场立字据", "谁先说真话的较量", "旧债与旧情的拉扯",
    "门锁与退路的僵持", "关于一个名字的追问", "灯油与时间的争执",
)


def line_for(chapter: int, index: int, location: str, dialogue_index: int = 0) -> str:
    """One deterministic prose line.

    Conversation lines get their own counter: tagging them off the line index
    would make the speaker cycle with period 2 (since dialogue sits on every 4th
    line), and the repetition detector would legitimately fire on the resulting
    `。"A说。B把` pattern.
    """
    mi = (index * 13 + chapter * 7) % len(_MOVES)
    ni = (index * 11 + chapter * 5) % len(_NARRATE)
    si = (index * 3 + chapter * 5) % len(CHARACTERS)
    if index % 4 == 1:
        who = CHARACTERS[(dialogue_index * 5 + chapter * 3) % len(CHARACTERS)]
        say = _SAYS[(dialogue_index * 7 + chapter * 11) % len(_SAYS)]
        return '"%s"%s说。' % (say, who)
    if index % 11 == 5:
        return "%s的%s，%s。" % (location, _MOVES[mi], _NARRATE[ni])
    return "%s%s，%s。" % (CHARACTERS[si], _MOVES[mi], _NARRATE[ni])


def chapter_text(chapter: int, lo: int, hi: int, location: str = None) -> str:
    """Generate a chapter that passes Level 1 (length, paragraphs, dialogue)."""
    location = location or LOCATIONS[(chapter - 1) % len(LOCATIONS)]
    lines, i, d = [], 0, 0
    mid = (lo + hi) // 2
    while True:
        lines.append(line_for(chapter, i, location, d))
        if i % 4 == 1:
            d += 1
        i += 1
        if count_chars("\n".join(lines)) >= mid or i > 400:
            break
    while count_chars("\n".join(lines)) > hi and len(lines) > 12:
        lines.pop()
    return "\n".join(lines) + "\n"


def scene_payload(chapter: int, location: str, characters) -> dict:
    return {
        "scene_no": 1,
        "location": location,
        "participants": list(characters)[:3],
        "conflict": _SCENE_CONFLICTS[(chapter - 1) % len(_SCENE_CONFLICTS)],
        "summary": "第%d章在%s由%s处理「%s」"
                   % (chapter, location, list(characters)[0] if characters else "众人",
                      _SCENE_CONFLICTS[(chapter - 1) % len(_SCENE_CONFLICTS)]),
        "dialogue_function": ("试探" if chapter % 2 else "摊牌") + "第%d轮" % chapter,
    }


def plan_payload(chapter: int, *, characters=("李禾", "沈默"), location=None,
                 must_change=None, forbidden=(), payoff=None) -> dict:
    location = location or LOCATIONS[(chapter - 1) % len(LOCATIONS)]
    must_change = must_change if must_change is not None else [
        {"character": characters[0], "field": "goal",
         "before": None, "after": "第%d章的目标" % chapter}]
    return {
        "schema": 1,
        "chapter": chapter,
        "chapter_goal": "第%d章：在%s推进主线并改变一次人物状态" % (chapter, location),
        "core_conflict": "%s下的对峙（第%d章）" % (_SCENE_CONFLICTS[(chapter - 1) % 12], chapter),
        "must_advance_main": ["T1"],
        "must_advance_sub": [],
        "characters": list(characters),
        "location": location,
        "timeline": {"world_time": _WORLD_TIME % chapter},
        "must_change": must_change,
        "allowed_new_info": ["FACT-001"],
        "forbidden_reveals": list(forbidden),
        "relevant_foreshadowing": [],
        "required_end_state": [],
        "scenes": [scene_payload(chapter, location, characters)],
        "scene_count": 1,
        "questions_to_resolve": ["第%d章要解决的一个问题" % chapter],
        "forbidden_events": [],
        "notes": "",
    }


def delta_payload(chapter: int, *, characters=("李禾", "沈默"), location=None,
                  must_change=None, foreshadowing=None, knowledge=None,
                  threads=None, forbidden=(), thread_status="active") -> dict:
    location = location or LOCATIONS[(chapter - 1) % len(LOCATIONS)]
    must_change = must_change if must_change is not None else [
        {"character": characters[0], "field": "goal",
         "before": None, "after": "第%d章的目标" % chapter}]
    events = [{
        "code": "E%d-1" % chapter,
        "kind": "conflict",
        "summary": "第%d章：%s在%s完成一次交锋" % (chapter, characters[0], location),
        "participants": list(characters),
        "location": location,
        "causes": ([] if chapter == 1 else ["E%d-1" % (chapter - 1)]),
        "effects": [],
        "irreversible": chapter % 5 == 0,
    }]
    if threads is None:
        threads = [{"code": "T1", "name": "找到种子库", "kind": "main",
                    "action": "open" if chapter == 1 else "advance",
                    "status": thread_status, "goal": "找到并打开种子库"}]
    delta = {
        "schema": 1,
        "chapter": chapter,
        "summary": "第%d章事件：%s与%s在%s交锋" % (chapter, characters[0], characters[1]
                                                if len(characters) > 1 else characters[0],
                                            location),
        "place": location,
        "timeline": {"world_time": _WORLD_TIME % chapter, "note": "第%d日" % chapter},
        "entities": {"characters": [{"name": c} for c in characters],
                     "locations": [{"name": location}],
                     "organizations": [], "items": []},
        "events": events,
        "character_changes": must_change,
        "knowledge_added": list(knowledge or []),
        "relationships": [],
        "items": [],
        "world_rules": [],
        "foreshadowing": list(foreshadowing or []),
        "plot_threads": threads,
        "scenes": [scene_payload(chapter, location, characters)],
        "warnings": [],
        "uncertain": [],
    }
    if chapter == 1:
        delta["world_rules"] = [{"code": "R1", "text": "账本上的数字会改变现实",
                                 "immutable": True}]
    return delta


def last_json(text: str):
    """Parse the last complete JSON document printed by a multi-line CLI run.

    `auto-next` streams one JSON object per step, so its stdout is a sequence of
    documents rather than one; the final one is the result.
    """
    try:
        return json.loads(text)
    except ValueError:
        pass
    for line in reversed([ln for ln in text.splitlines() if ln.strip()]):
        try:
            return json.loads(line)
        except ValueError:
            continue
    raise AssertionError("no JSON document found in output:\n%s" % text[:2000])


class Project:
    """A disposable novel project driven through the real CLI entry points."""

    def __init__(self, *, target=12000, chars_per_chapter=1200, chapters_per_round=3,
                 volumes=2, name="proj"):
        self.dir = _mkdtemp("mwn-%s-" % name)
        self.target = target
        self.chars_per_chapter = chars_per_chapter
        self.chapters_per_round = chapters_per_round
        self.volumes = volumes

    # ------------------------------------------------------------ plumbing
    @property
    def root(self) -> str:
        return self.dir

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run(self, module, argv):
        """Run a CLI main() in-process; return (rc, stdout, stderr).

        Handles both conventions in this repo: `main(argv)` and the historical
        `main()` that parses sys.argv directly.
        """
        import inspect
        out, err = io.StringIO(), io.StringIO()
        argv = [str(a) for a in argv]
        params = inspect.signature(module.main).parameters
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            saved = sys.argv
            try:
                if params:
                    rc = module.main(argv)
                else:
                    sys.argv = ["%s.py" % getattr(module, "__name__", "cli")] + argv
                    rc = module.main()
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
            finally:
                sys.argv = saved
        return rc, out.getvalue(), err.getvalue()

    def ns(self, argv):
        return self.run(novel_state, argv)

    def cs(self, argv):
        return self.run(canon, argv)

    def json_run(self, module, argv):
        rc, out, err = self.run(module, argv)
        try:
            return rc, json.loads(out), err
        except ValueError:
            return rc, {"_raw": out}, err

    def paths(self):
        return paths(self.root)

    def conn(self):
        return db.connect(self.root)

    def progress(self):
        return json.loads(read_text(self.paths()["progress"]))

    # ------------------------------------------------------------ lifecycle
    def init(self, **overrides):
        argv = ["init", "--root", self.root, "--title", DEFAULT_TITLE,
                "--premise", DEFAULT_PREMISE, "--target", str(self.target),
                "--chars-per-chapter", str(self.chars_per_chapter),
                "--chapters-per-round", str(self.chapters_per_round),
                "--volumes", str(self.volumes)]
        for k, v in overrides.items():
            argv += ["--%s" % k.replace("_", "-"), str(v)]
        return self.json_run(novel_state, argv)

    def write_bible(self, characters=CHARACTERS, rules=(("R1", "账本上的数字会改变现实"),),
                    foreshadow=(("F001", "铜钥匙的来历", 1, 4, 6, "未回收"),),
                    threads=(("T1", "找到种子库", "main", "找到并打开种子库", "planned"),)):
        text = ["# 设定圣经（Bible）", "", "## 3. 世界规则（硬设定）",
                "| 编号 | 规则 | 首次出现章节 | 不可违反 |", "|---|---|---|---|"]
        for code, rule in rules:
            text.append("| %s | %s | 1 | 是 |" % (code, rule))
        text += ["", "## 4. 主要人物",
                 "| 姓名 | 身份 | 核心欲望 | 致命缺陷 | 结局走向 | 状态 |",
                 "|---|---|---|---|---|---|"]
        for i, name in enumerate(characters):
            text.append("| %s | 记账人%d | 查清账本的来历 | 不肯认错 | 未知 | 存活 |"
                        % (name, i))
        text += ["", "## 5. 地理与组织", "| 名称 | 类型 | 说明 |", "|---|---|---|",
                 "| 黑塔 | 地点 | 城里最高的塔 |", "| 麦田 | 地点 | 城北的麦田 |",
                 "| 南巷 | 地点 | 老城区的一条巷子 |"]
        text += ["", "## 7. 伏笔登记表",
                 "| 编号 | 伏笔 | 埋设章节 | 计划回收章节 | 状态 |", "|---|---|---|---|---|"]
        for code, title, planted, start, end, status in foreshadow:
            text.append("| %s | %s | %s | %s-%s | %s |"
                        % (code, title, planted, start, end, status))
        text += ["", "## 8. 剧情线程", "| 编号 | 名称 | 类型 | 目标 | 状态 |",
                 "|---|---|---|---|---|"]
        for code, name, kind, goal, status in threads:
            text.append("| %s | %s | %s | %s | %s |" % (code, name, kind, goal, status))
        from _common import atomic_write_text
        atomic_write_text(self.paths()["bible"], "\n".join(text) + "\n")
        return self.json_run(canon, ["--root", self.root, "import-md"])

    def outline_volumes(self):
        return read_text(self.paths()["outline"])

    def write_outline(self, volumes=((1, "查清账本的来历", "李禾再也回不到不知道账本存在的生活"),)):
        from _common import atomic_write_text
        lines = ["# 分卷总纲", ""]
        for num, goal, irr in volumes:
            lines += ["## 第%d卷（第 %d–%d 章）" % (num, 1 + (num - 1) * 5, num * 5), "",
                      "- 卷目标（不可逆变化）：%s" % goal,
                      "- 主要冲突：账本与现实的对账",
                      "- 关键转折：第二本账出现",
                      "- 卷末钩子：有人替他签过名",
                      "- 卷末不可逆变化：%s" % irr, ""]
        atomic_write_text(self.paths()["outline"], "\n".join(lines))
        return "\n".join(lines)

    def write_plan(self, chapter, payload=None):
        from _common import atomic_write_json
        payload = payload or plan_payload(chapter)
        path = os.path.join(self.paths()["plans"], "ch-%04d.plan.json" % chapter)
        atomic_write_json(path, payload)
        return self.json_run(novel_state, ["plan", "--root", self.root,
                                           "--chapter", str(chapter), "--file", path])

    def write_delta(self, chapter, payload=None):
        from _common import atomic_write_json
        payload = payload or delta_payload(chapter)
        path = os.path.join(self.paths()["plans"], "ch-%04d.delta.json" % chapter)
        atomic_write_json(path, payload)
        return path

    def write_draft(self, chapter, text=None, location=None):
        from _common import atomic_write_text
        location = location or LOCATIONS[(chapter - 1) % len(LOCATIONS)]
        lo, hi = tolerance(self.progress())
        text = text if text is not None else chapter_text(chapter, lo, hi, location)
        path = os.path.join(self.paths()["drafts"], "ch-%04d.md" % chapter)
        atomic_write_text(path, text)
        return path

    def brief(self):
        return self.json_run(novel_state, ["next", "--root", self.root])[1]

    def commit(self, chapter, extra=()):
        argv = ["commit", "--root", self.root, "--chapter", str(chapter)]
        return self.json_run(novel_state, argv + list(extra))

    def full_chapter(self, chapter, *, plan=None, delta=None, text=None, location=None,
                     extra_commit=(), forbid_in_text=()):
        """plan -> write -> audit -> commit, i.e. the mandated order."""
        location = location or LOCATIONS[(chapter - 1) % len(LOCATIONS)]
        self.write_plan(chapter, plan or plan_payload(chapter, location=location))
        self.write_delta(chapter, delta or delta_payload(chapter, location=location))
        draft = self.write_draft(chapter, text, location)
        audit_rc, audit_out, audit_err = self.json_run(
            novel_state, ["audit", "--root", self.root, "--chapter", str(chapter),
                          "--file", draft, "--json", "--strict"])
        if audit_rc != 0:
            raise AssertionError("audit failed for ch-%04d: %s" % (chapter, audit_out))
        rc, out, err = self.commit(chapter, extra_commit)
        return {"audit": audit_out, "commit_rc": rc, "commit": out, "draft": draft}

    def advance_one(self):
        """One step of the real loop: returns ('chapter', n) / ('round', None) / ('stop', None)."""
        brief = self.brief()
        action = brief.get("action")
        if action == "STOP":
            return "stop", None
        if action == "ROUND_DONE":
            rc, out, err = self.json_run(novel_state, ["round-done", "--root", self.root])
            if rc != 0:
                raise AssertionError("round-done blocked: %s" % out)
            return "round", None
        chapter = brief["chapters_to_write"][0]
        self.full_chapter(chapter)
        return "chapter", chapter


def fresh_project(**kwargs) -> Project:
    p = Project(**kwargs)
    rc, out, err = p.init()
    assert rc == 0, "init failed: %s %s" % (out, err)
    p.write_bible()
    p.write_outline(volumes=[(1, "查清账本的来历", "李禾再也回不到不知道账本存在的生活"),
                             (2, "找到种子库", "沈默的名字从账本上消失")])
    return p
