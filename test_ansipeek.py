"""ansipeek 的自检测试。"""

from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ansipeek import analyze, color_usage, parse_sequences  # noqa: E402

ENTRY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ansipeek.py")


def test_plain_text_has_no_sequences() -> None:
    seqs, plain = parse_sequences("hello world")
    assert seqs == [], seqs
    assert plain == "hello world"


def test_sgr_color_is_detected() -> None:
    seqs, plain = parse_sequences("\x1b[31mred\x1b[0m")
    assert len(seqs) == 2, seqs
    assert seqs[0]["kind"] == "CSI", seqs[0]
    assert plain == "red", repr(plain)


def test_sgr_description_is_readable() -> None:
    seqs, _ = parse_sequences("\x1b[1;32mbold green\x1b[0m")
    desc = seqs[0]["description"]
    assert "加粗" in desc, desc
    assert "绿" in desc, desc


def test_cursor_movement_is_recognized() -> None:
    seqs, plain = parse_sequences("a\x1b[2Kb")
    assert len(seqs) == 1
    assert "清除行" in seqs[0]["description"], seqs[0]
    assert plain == "ab"


def test_osc_title_is_recognized() -> None:
    seqs, plain = parse_sequences("\x1b]0;my title\x07text")
    assert seqs and seqs[0]["kind"] == "OSC", seqs
    assert "my title" in seqs[0]["description"], seqs[0]
    assert plain == "text"


def test_osc_with_st_terminator() -> None:
    seqs, plain = parse_sequences("\x1b]0;title\x1b\\body")
    assert seqs and seqs[0]["kind"] == "OSC", seqs
    assert plain == "body"


def test_color_usage_counts_appearances() -> None:
    text = "\x1b[31ma\x1b[0m\x1b[31mb\x1b[0m\x1b[32mc\x1b[0m"
    rows = color_usage(text)
    counts = {r["code"]: r["count"] for r in rows}
    assert counts.get("31") == 2, counts
    assert counts.get("32") == 1, counts


def test_color_usage_records_first_position() -> None:
    text = "\x1b[32mgreen\x1b[0m\x1b[31mred\x1b[0m"
    rows = {r["code"]: r for r in color_usage(text)}
    assert rows["32"]["first_seen"] < rows["31"]["first_seen"], rows


def test_analyze_summary_counts_bytes() -> None:
    result = analyze("\x1b[31mred\x1b[0m")
    s = result["summary"]
    assert s["sequence_count"] == 2, s
    assert s["raw_length"] - s["plain_length"] == s["sequence_bytes"], s


def test_analyze_plain_roundtrip_preserves_words() -> None:
    result = analyze("\x1b[1mERROR\x1b[0m: something failed\n")
    assert "ERROR" in result["plain"]
    assert "something failed" in result["plain"]


def test_ansi_splitting_makes_text_searchable() -> None:
    result = analyze("\x1b[32mOK\x1b[0m")
    assert "OK" in result["plain"]
    # 原文里 "OK" 前后粘着转义码，按空白切词时拿不到干净的 "OK"
    raw_tokens = "\x1b[32mOK\x1b[0m".split()
    assert "OK" not in raw_tokens, raw_tokens
    assert "OK" in result["plain"].split(), result["plain"]


def test_cli_help_exits_zero() -> None:
    r = subprocess.run([sys.executable, ENTRY, "--help"], capture_output=True,
                       text=True, timeout=30, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    assert "ansipeek" in r.stdout


def test_cli_plain_output() -> None:
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as f:
        f.write("\x1b[31mred\x1b[0m")
        path = f.name
    try:
        r = subprocess.run([sys.executable, ENTRY, "--input", path, "--plain"],
                           capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace")
        assert r.returncode == 0, r.stderr
        assert r.stdout == "red", repr(r.stdout)
    finally:
        os.unlink(path)


def test_cli_json_output() -> None:
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as f:
        f.write("\x1b[31mred\x1b[0m")
        path = f.name
    try:
        r = subprocess.run([sys.executable, ENTRY, "--input", path, "--json"],
                           capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace")
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        assert data["summary"]["sequence_count"] == 2, data
    finally:
        os.unlink(path)


def test_cli_missing_file_exits_two() -> None:
    r = subprocess.run([sys.executable, ENTRY, "--input", "nope.txt"],
                       capture_output=True, text=True, timeout=30,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert "找不到文件" in r.stderr, r.stderr


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  [OK  ] {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n共 {len(tests)} 项，失败 {failed} 项")
    raise SystemExit(1 if failed else 0)
