"""ansipeek —— 把终端输出里的 ANSI 转义序列拆开来看。

设计原则：
1. 先还原，再评价：颜色码影响内容时，内容优先。
2. 每个转义序列都说明含义，不只做删除。
3. 输出能进管道，退出码区分「没找到」与「解析失败」。

仅依赖 Python 标准库。

用法:
    python ansipeek.py --help
    python ansipeek.py --input sample.txt
    cat sample.txt | python ansipeek.py
    python ansipeek.py --input sample.txt --plain
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

# ---------------------------------------------------------------------------
# 序列识别
# ---------------------------------------------------------------------------

# CSI 序列：ESC [ 参数 中间符 最终字节
CSI_RE = re.compile(r"\x1b\[([0-9;?]*)([ -/]*)([@-~])")

# OSC 序列：ESC ] ... BEL 或 ESC ] ... ESC \
OSC_RE = re.compile(r"\x1b\](.*?)(?:\x07|\x1b\\)", re.DOTALL)

# 其它两字符转义：ESC + 单字符
ESC_RE = re.compile(r"\x1b([@-Z\\-_])")

SGR_NAMES = {
    "0": "重置全部属性",
    "1": "加粗",
    "2": "变暗",
    "3": "斜体",
    "4": "下划线",
    "5": "闪烁",
    "7": "反显",
    "8": "隐藏",
    "9": "删除线",
    "21": "关闭加粗或下划线",
    "22": "正常字重",
    "23": "关闭斜体",
    "24": "关闭下划线",
    "25": "关闭闪烁",
    "27": "关闭反显",
    "28": "关闭隐藏",
    "29": "关闭删除线",
    "30": "前景 黑",
    "31": "前景 红",
    "32": "前景 绿",
    "33": "前景 黄",
    "34": "前景 蓝",
    "35": "前景 洋红",
    "36": "前景 青",
    "37": "前景 白",
    "39": "前景 默认",
    "40": "背景 黑",
    "41": "背景 红",
    "42": "背景 绿",
    "43": "背景 黄",
    "44": "背景 蓝",
    "45": "背景 洋红",
    "46": "背景 青",
    "47": "背景 白",
    "49": "背景 默认",
    "90": "前景 亮黑",
    "91": "前景 亮红",
    "92": "前景 亮绿",
    "93": "前景 亮黄",
    "94": "前景 亮蓝",
    "95": "前景 亮洋红",
    "96": "前景 亮青",
    "97": "前景 亮白",
}

FINAL_NAMES = {
    "A": "光标上移", "B": "光标下移", "C": "光标右移", "D": "光标左移",
    "H": "光标定位", "J": "清除屏幕", "K": "清除行",
    "m": "设置图形属性（SGR）", "s": "保存光标位置", "u": "恢复光标位置",
}


def describe_sgr(params: str) -> str:
    """把 SGR 参数串翻译成可读说明。"""
    codes = [p for p in params.split(";")] if params else ["0"]
    out = []
    for code in codes:
        code = code or "0"
        if code in SGR_NAMES:
            out.append(SGR_NAMES[code])
        elif code.isdigit() and 40 <= int(code) <= 47:
            out.append(f"背景色 {int(code) - 40}")
        elif code.isdigit() and 100 <= int(code) <= 107:
            out.append(f"背景亮色 {int(code) - 100}")
        else:
            out.append(f"属性 {code}")
    return "、".join(out)


def describe_csi(params: str, intermediate: str, final: str) -> str:
    """把一个 CSI 序列翻译成可读说明。"""
    base = FINAL_NAMES.get(final, f"控制序列 {final}")
    extra = []
    if final == "m":
        extra.append(describe_sgr(params))
    elif params:
        extra.append(f"参数 {params}")
    if intermediate:
        extra.append(f"中间字节 {intermediate!r}")
    return "；".join([base] + [e for e in extra if e])


def parse_sequences(text: str) -> tuple[list[dict], str]:
    """把文本拆成 (序列列表, 纯文本)。

    序列列表里每项记录：原始串、类型、说明、它在纯文本中的位置。
    """
    sequences: list[dict] = []
    out: list[str] = []
    pos = 0

    def consume(match: re.Match, kind: str, desc: str) -> int:
        nonlocal pos
        start, end = match.start(), match.end()
        out.append(text[pos:start])
        sequences.append({
            "raw": match.group(0),
            "kind": kind,
            "description": desc,
            "text_offset": len("".join(out)),
        })
        pos = end
        return end

    i = 0
    while i < len(text):
        idx = text.find("\x1b", i)
        if idx == -1:
            break
        # 先看是不是 OSC
        osc = OSC_RE.match(text, idx)
        if osc:
            body = osc.group(1)
            desc = "设置终端标题或 OSC 扩展"
            if body.startswith("0;") or body.startswith("2;"):
                desc = f"设置窗口标题：{body.split(';', 1)[1][:60]}"
            consume(osc, "OSC", desc)
            i = osc.end()
            continue
        csi = CSI_RE.match(text, idx)
        if csi:
            consume(csi, "CSI", describe_csi(csi.group(1), csi.group(2), csi.group(3)))
            i = csi.end()
            continue
        esc = ESC_RE.match(text, idx)
        if esc:
            consume(esc, "ESC", f"单字符转义 ESC {esc.group(1)!r}")
            i = esc.end()
            continue
        i = idx + 1

    out.append(text[pos:])
    return sequences, "".join(out)


# ---------------------------------------------------------------------------
# 颜色统计
# ---------------------------------------------------------------------------

COLOR_RE = re.compile(r"\x1b\[([0-9;]*)m")


def color_usage(text: str) -> list[dict]:
    """按出现顺序统计 SGR 颜色码的使用情况。"""
    counter: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    for index, match in enumerate(COLOR_RE.finditer(text)):
        params = match.group(1) or "0"
        for code in params.split(";"):
            code = code or "0"
            counter[code] += 1
            if code not in first_seen:
                first_seen[code] = index
    rows = []
    for code, count in counter.most_common():
        rows.append({
            "code": code,
            "name": SGR_NAMES.get(code, f"属性 {code}"),
            "count": count,
            "first_seen": first_seen[code],
        })
    return rows


def visible_invisible_counts(text: str) -> dict:
    """统计原文与纯文本在长度上的差别。"""
    sequences, plain = parse_sequences(text)
    return {
        "raw_length": len(text),
        "plain_length": len(plain),
        "sequence_count": len(sequences),
        "sequence_bytes": len(text) - len(plain),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def analyze(text: str) -> dict:
    sequences, plain = parse_sequences(text)
    colors = color_usage(text)
    return {
        "summary": visible_invisible_counts(text),
        "sequences": sequences,
        "colors": colors,
        "plain": plain,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ansipeek",
        description="解析终端输出里的 ANSI 转义序列，并还原成纯文本（仅依赖标准库）",
        epilog=(
            "示例:\n"
            "  python ansipeek.py --input sample.txt\n"
            "  cat sample.txt | python ansipeek.py --plain\n"
            "  python ansipeek.py --input sample.txt --json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--input", "-i", help="输入文件；不给则从标准输入读")
    ap.add_argument("--plain", action="store_true", help="只输出还原后的纯文本")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    if args.input:
        path = Path(args.input)
        if not path.is_file():
            print(f"找不到文件：{path}", file=sys.stderr)
            return 2
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"读取失败：{e}", file=sys.stderr)
            return 2
    else:
        if sys.stdin.isatty():
            print("没有输入。请用 --input 指定文件，或通过管道传入内容。",
                  file=sys.stderr)
            return 2
        text = sys.stdin.read()

    if not text:
        print("输入为空，没有可分析的序列。", file=sys.stderr)
        return 1

    result = analyze(text)

    if args.plain:
        sys.stdout.write(result["plain"])
        return 0

    if args.json:
        payload = dict(result)
        payload.pop("plain", None)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    summary = result["summary"]
    print(f"原始长度: {summary['raw_length']} 字符")
    print(f"纯文本长度: {summary['plain_length']} 字符")
    print(f"转义序列: {summary['sequence_count']} 个，占 {summary['sequence_bytes']} 字符")
    print()

    if result["sequences"]:
        print("序列明细：")
        for i, s in enumerate(result["sequences"][:50], 1):
            raw = s["raw"].replace("\x1b", "\\e")
            print(f"  {i:>3}. [{s['kind']}] {raw}  →  {s['description']}")
        if len(result["sequences"]) > 50:
            print(f"  ...（还有 {len(result['sequences']) - 50} 条，用 --json 看全部）")
        print()

    if result["colors"]:
        print("颜色使用（按出现次数）：")
        for c in result["colors"]:
            print(f"  {c['name']:<24} 出现 {c['count']} 次（代码 {c['code']}）")
        print()

    print("还原后的纯文本：")
    print("-" * 40)
    sys.stdout.write(result["plain"])
    if not result["plain"].endswith("\n"):
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
