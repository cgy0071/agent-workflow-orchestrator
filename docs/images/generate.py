"""Generate README SVG images into docs/images/. Run from the repository root.

The terminal image uses real captured output:
  python -B scripts/demo_scheduler.py --directory runtime/first-run > demo.txt
  python -B scripts/workflow_control.py --state runtime/first-run/state status --summary --format text > status.txt
  python -B docs/images/generate.py demo.txt status.txt
"""
from html import escape
from pathlib import Path
import sys

OUT = Path("docs/images")
STYLE = """<style>
.bg{fill:#ffffff;stroke:#d0d7de}.box{fill:#f6f8fa;stroke:#d0d7de}.t{fill:#1f2328}.m{fill:#59636e}
.ar{stroke:#59636e;fill:none;stroke-width:1.6}.ah{fill:#59636e}.dash{stroke-dasharray:5 4}
.agent{fill:#ddf4ff;stroke:#54aeff}.tool{fill:#dafbe1;stroke:#4ac26b}.warn{fill:#fff8c5;stroke:#d4a72c}
.bad{fill:#ffebe9;stroke:#ff8182}.chip{fill:#ffffff;stroke:#d0d7de}
@media (prefers-color-scheme: dark){
.bg{fill:#0d1117;stroke:#3d444d}.box{fill:#161b22;stroke:#3d444d}.t{fill:#f0f6fc}.m{fill:#9198a1}
.ar{stroke:#9198a1}.ah{fill:#9198a1}.agent{fill:#0c2d6b;stroke:#1f6feb}.tool{fill:#0f2d1a;stroke:#2ea043}
.warn{fill:#341a00;stroke:#9e6a03}.bad{fill:#3c0f13;stroke:#da3633}.chip{fill:#0d1117;stroke:#3d444d}}
text{font-family:-apple-system,"Segoe UI","Noto Sans","Helvetica Neue",Arial,"Microsoft YaHei","PingFang SC","Noto Sans CJK SC",sans-serif}
.mono{font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",Menlo,monospace}
</style>"""
MARKER = ('<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
          'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="ah"/></marker></defs>')


def svg(width, height, title, body):
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" role="img">'
            '<title>%s</title>%s%s<rect x="0.5" y="0.5" width="%d" height="%d" rx="10" class="bg"/>%s</svg>\n'
            % (width, height, width, height, escape(title), STYLE, MARKER, width - 1, height - 1, "".join(body)))


def text(x, y, value, size=13, cls="t", weight=None, anchor="middle", mono=False):
    extra = (' font-weight="%s"' % weight if weight else "") + (' class="%s mono"' % cls if mono else ' class="%s"' % cls)
    return '<text x="%g" y="%g" font-size="%g" text-anchor="%s"%s>%s</text>' % (x, y, size, anchor, extra, escape(value))


def box(x, y, w, h, title, lines=(), cls="box", mono_title=False, size=15):
    parts = ['<rect x="%g" y="%g" width="%g" height="%g" rx="8" class="%s"/>' % (x, y, w, h, cls)]
    top = y + h / 2 - (len(lines) * 17) / 2 + 5
    parts.append(text(x + w / 2, top, title, size, weight="600", mono=mono_title))
    for i, line in enumerate(lines):
        parts.append(text(x + w / 2, top + 20 + i * 17, line, 12.5, "m"))
    return "".join(parts)


def arrow(points, dashed=False):
    path = " ".join(("M" if i == 0 else "L") + "%g,%g" % p for i, p in enumerate(points))
    return '<path d="%s" class="ar%s" marker-end="url(#ah)"/>' % (path, " dash" if dashed else "")


def write(name, content):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(content, encoding="utf-8", newline="\n")
    print("wrote", OUT / name)
L = {
    "en": {"you": ("You", ["goal and authorization"]),
           "planner": ("Planner", ["writes task.md", "dispatches the task"]),
           "executor": ("Executor", ["implements and tests", "writes RESULT.md + HANDOFF.md"]),
           "monitor": ("monitor_task.py", ["polls status every 300 s", "checks files, task ID, nonce",
                                           "reports a stop once, keeps watching"]),
           "ledger": ("SQLite ledger", ["records exactly one", "inbox event"]),
           "reviewer": ("Reviewer", ["reads the raw evidence", "accept / repair / reject"]),
           "once": "sent once", "next": "next or repair task", "continue": "dashed: continue once after a stop",
           "legend_agent": "AI agent (any harness)", "legend_tool": "script in this repository",
           "overview": "Workflow overview"},
    "zh": {"you": ("你", ["目标与授权"]),
           "planner": ("Planner 规划者", ["编写 task.md", "派发任务"]),
           "executor": ("Executor 执行者", ["实现并测试", "写入 RESULT.md 和 HANDOFF.md"]),
           "monitor": ("monitor_task.py", ["每 300 秒查询一次状态", "核对文件、任务 ID、nonce",
                                           "中途停止只报告一次，继续观察"]),
           "ledger": ("SQLite 台账", ["只记录一条", "收件箱事件"]),
           "reviewer": ("Reviewer 审核者", ["阅读原始证据", "accept / repair / reject"]),
           "once": "只发送一次", "next": "下一任务或返工任务", "continue": "虚线：中途停止后继续一次",
           "legend_agent": "AI Agent（任意客户端）", "legend_tool": "本仓库中的脚本",
           "overview": "工作流概览"},
}


def legend(y, lang):
    t = L[lang]
    return ('<rect x="30" y="%d" width="14" height="14" rx="3" class="agent"/>' % (y - 11) +
            text(52, y, t["legend_agent"], 12.5, "m", anchor="start") +
            '<rect x="250" y="%d" width="14" height="14" rx="3" class="tool"/>' % (y - 11) +
            text(272, y, t["legend_tool"], 12.5, "m", anchor="start"))


def overview(lang):
    t = L[lang]
    body = [box(30, 40, 170, 90, *t["you"], cls="chip"),
            box(260, 40, 260, 90, *t["planner"], cls="agent"),
            box(600, 40, 290, 90, *t["executor"], cls="agent"),
            box(600, 190, 290, 104, *t["monitor"], cls="tool", mono_title=True),
            box(320, 190, 220, 104, *t["ledger"], cls="tool"),
            box(30, 190, 230, 104, *t["reviewer"], cls="agent"),
            arrow([(200, 70), (258, 70)]), arrow([(520, 70), (598, 70)]),
            text(560, 60, t["once"], 11.5, "m"),
            # A stopped Executor gets at most a bounded continuation of the same task, never a resend.
            arrow([(520, 108), (598, 108)], dashed=True),
            text(560, 150, t["continue"], 11.5, "m"),
            arrow([(745, 130), (745, 188)]), arrow([(600, 242), (542, 242)]), arrow([(320, 242), (262, 242)]),
            arrow([(200, 190), (200, 166), (390, 166), (390, 132)], dashed=True),
            text(295, 160, t["next"], 11.5, "m"),
            legend(328, lang)]
    return svg(920, 346, t["overview"], body)
def terminal(demo_line, status_lines):
    # Real captured output; a terminal stays dark in both page themes.
    lines = [("$ ", "python -B scripts/demo_scheduler.py --directory runtime/first-run")]
    lines.append(("", demo_line))
    lines.append(("$ ", "python -B scripts/workflow_control.py --state runtime/first-run/state status --summary --format text"))
    lines += [("", line) for line in status_lines]
    width, step, top = 1000, 19, 52
    height = top + step * len(lines) + 18
    body = ['<rect x="0" y="0" width="%d" height="%d" rx="10" fill="#0d1117"/>' % (width, height),
            '<rect x="0" y="0" width="%d" height="30" rx="10" fill="#21262d"/>' % width,
            '<rect x="0" y="20" width="%d" height="10" fill="#21262d"/>' % width]
    for i, color in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        body.append('<circle cx="%d" cy="15" r="5.5" fill="%s"/>' % (20 + i * 18, color))
    for i, (prompt, line) in enumerate(lines):
        y = top + i * step
        color = "#e6edf3"
        if prompt: color = "#79c0ff"
        elif "OFFLINE_DEMO_PASS" in line or "| accepted |" in line: color = "#7ee787"
        elif line.startswith("TASK |"): color = "#d2a8ff"
        elif line.startswith("  "): color = "#9198a1"
        span = ('<tspan fill="#7ee787">%s</tspan>' % escape(prompt)) if prompt else ""
        body.append('<text x="18" y="%d" font-size="12.5" class="mono" fill="%s" xml:space="preserve">%s%s</text>'
                    % (y, color, span, escape(line)))
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" role="img">'
            '<title>Offline demo output</title>%s%s</svg>\n' % (width, height, width, height, STYLE, "".join(body)))


D = {"en": {"title": "Demo task graph", "a": ("A", ["worker: executor"]), "b": ("B", ["worker: executor-b"]),
            "join": ("JOIN", ["worker: executor", "needs A and B"]),
            "p1": "pass 1: A and B dispatched together", "p2": "pass 2: JOIN becomes eligible",
            "gate": "only after both reviews = accept", "acc": "accepted"},
     "zh": {"title": "演示任务图", "a": ("A", ["Worker：executor"]), "b": ("B", ["Worker：executor-b"]),
            "join": ("JOIN", ["Worker：executor", "依赖 A 和 B"]),
            "p1": "第 1 轮：A 与 B 同时派发", "p2": "第 2 轮：JOIN 才可派发",
            "gate": "仅当两份审核均为 accept", "acc": "accepted"}}


def dag(lang):
    t = D[lang]
    body = [text(150, 34, t["p1"], 12.5, "m"), text(620, 34, t["p2"], 12.5, "m"),
            box(60, 50, 180, 70, *t["a"], cls="agent"), box(60, 160, 180, 70, *t["b"], cls="agent"),
            box(530, 95, 190, 90, *t["join"], cls="agent"),
            arrow([(240, 85), (380, 85), (380, 125), (528, 125)]), arrow([(240, 195), (380, 195), (380, 155), (528, 155)]),
            '<rect x="275" y="128" width="210" height="24" rx="12" class="warn"/>', text(380, 145, t["gate"], 11.5),
            ]
    for x, y in ((150, 138), (150, 248), (625, 208)):
        body.append('<rect x="%d" y="%d" width="80" height="20" rx="10" class="tool"/>' % (x - 40, y - 12))
        body.append(text(x, y, t["acc"], 11.5, mono=True))
    return svg(780, 262, t["title"], body)
S = {"en": {"title": "Task lifecycle",
            "steps": [("ready", ["dependencies met"]), ("execution_unconfirmed", ["sent once, never resent"]),
                      ("executing", ["start confirmed", "by reconcile"]), ("awaiting_review", ["review_ready:", "files + nonce valid"]),
                      ("accepted", ["review recorded", "dependents unlocked"])],
            "e1": ("needs_inspection", ["stop, prompt or outage:", "watched; continue or close"]),
            "e2": ("review_blocked", ["reject or repair:", "plan a new scoped task"]),
            "e3": ("evidence_changed", ["accepted files changed:", "dependents stay blocked"]),
            "dispatch": "dispatch", "reconcile": "reconcile", "monitor": "monitor", "review": "review"},
     "zh": {"title": "任务生命周期",
            "steps": [("ready", ["依赖已满足"]), ("execution_unconfirmed", ["只发送一次，不重发"]),
                      ("executing", ["reconcile 确认", "已开始执行"]), ("awaiting_review", ["review_ready：", "文件与 nonce 有效"]),
                      ("accepted", ["审核已记录", "解锁下游任务"])],
            "e1": ("needs_inspection", ["停止、提示或中断：", "继续观察；继续一次或关闭"]),
            "e2": ("review_blocked", ["reject 或 repair：", "新建限定范围任务"]),
            "e3": ("evidence_changed", ["已通过的文件被改动：", "下游保持阻塞"]),
            "dispatch": "dispatch", "reconcile": "reconcile", "monitor": "monitor", "review": "review"}}


def lifecycle(lang):
    t = S[lang]
    w, gap, x0, y0 = 170, 34, 24, 60
    body = []
    labels = [t["dispatch"], t["reconcile"], t["monitor"], t["review"]]
    for i, (name, lines) in enumerate(t["steps"]):
        x = x0 + i * (w + gap)
        cls = "tool" if name == "accepted" else ("warn" if name == "execution_unconfirmed" else "agent")
        body.append(box(x, y0, w, 84, name, lines, cls=cls, mono_title=True, size=13.5))
        if i < 4:
            body.append(arrow([(x + w, y0 + 42), (x + w + gap - 2, y0 + 42)]))
            body.append(text(x + w + gap / 2, y0 - 8, labels[i], 11, "m", mono=True))
    def down(i, entry, cls):
        x = x0 + i * (w + gap)
        return (arrow([(x + w / 2, y0 + 84), (x + w / 2, 196)], dashed=True) +
                box(x - 6, 198, w + 12, 74, *entry, cls=cls, mono_title=True, size=13))
    body += [down(2, t["e1"], "warn"), down(3, t["e2"], "bad"), down(4, t["e3"], "bad")]
    return svg(x0 * 2 + 5 * w + 4 * gap, 292, t["title"], body)


def main():
    demo_line = Path(sys.argv[1]).read_text(encoding="utf-8-sig").strip()
    status_lines = Path(sys.argv[2]).read_text(encoding="utf-8-sig").rstrip().splitlines()
    # Shorten the absolute local state path; everything else is printed verbatim.
    demo_line = demo_line.replace(demo_line.split('"state": "')[1].split('"')[0], "runtime/first-run/state")
    write("demo-terminal.svg", terminal(demo_line, status_lines))
    for lang, suffix in (("en", ""), ("zh", ".zh-CN")):
        write("workflow-overview" + suffix + ".svg", overview(lang))
        write("demo-dag" + suffix + ".svg", dag(lang))
        write("task-lifecycle" + suffix + ".svg", lifecycle(lang))


if __name__ == "__main__":
    main()
