#!/usr/bin/env python
"""UserPromptSubmit hook: force the paper-translator skill to be used.

Skill discovery is model judgment over the description field, so an
unusually-worded request can slip past it. This hook reads the prompt and,
when it is unambiguously a paper-translation request, injects an
instruction that does not depend on that judgment.

Requires BOTH an action word and a target word, so "翻译这段代码注释" and
"这篇 paper 讲了什么" stay untouched.

Reads hook JSON on stdin, writes hook JSON on stdout. Always exits 0 - a
crashing prompt hook must never block the user's turn.
"""

import json
import re
import sys

ACTION = re.compile(r"翻译|译成|译为|翻成|中译|translat", re.I)
# Boundaries are ASCII-letter lookarounds, not \b: CJK characters count as
# \w in Python, so "\bpdf\b" never matches inside "把这个PDF翻译成中文".
TARGET = re.compile(
    r"文献|论文|期刊|全文|摘要"
    r"|(?<![a-z])(?:pdf|paper|article|literature|manuscript)(?![a-z])",
    re.I)

REMINDER = (
    "用户请求翻译文献。必须调用 paper-translator skill（Skill 工具，"
    "skill='paper-translator'），不要直接开始翻译。\n"
    "该 skill 的核心约束：PDF 里的图（独立整页图/正文内嵌图/矢量图表）不会出现在"
    "文本层里，只提取文本会交付一份看起来完整、实际漏图的译文；必须用 "
    "extract_paper.py 提取并通过图数量交叉校验，最后同时产出 Markdown 和 PDF。"
)


def main():
    # Read/write bytes explicitly: on Windows the default stdio encoding is
    # the ANSI codepage (cp936), which mangles the UTF-8 payload and makes
    # every Chinese prompt miss.
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    try:
        prompt = json.loads(raw).get("prompt", "")
    except Exception:
        prompt = raw  # malformed payload: fall back to scanning it whole

    if ACTION.search(prompt) and TARGET.search(prompt):
        out = json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": REMINDER,
            }
        }, ensure_ascii=False)
        sys.stdout.buffer.write(out.encode("utf-8"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
