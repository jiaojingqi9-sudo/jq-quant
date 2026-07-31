#!/usr/bin/env python3
"""deadcode_cut - 按函数名精确切除无引用的函数，并当场验证。

不加 --apply 时只报告将要删什么、各多少行。

为什么按 AST 而不按行号：行号会随任何一次编辑漂移，删错一行就是几百行代码
被截断。这里用 ast 拿到每个函数的真实起止（含装饰器与前置注释），删完立刻
重新解析确认文件仍然合法，并全仓搜一遍确认没有残留调用。
"""
import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path

TRADE = Path.home() / "All here" / "trade"

# 文件 → 要删的顶层函数名
TARGETS = {
    "src/taa_futu/dashboard_app.py": [
        "_candlestick_chart",
        "_render_terminal_watchlist",
        "_render_terminal_quote_panel",
        "_render_symbol_detail",
        "_runtime_health_label",
        "_strategy_runtime_display",
    ],
    "src/taa_futu/dashboard_extras.py": [
        "render_home",      # 插件架构之前的手写首页，唯一调用方 render_view 也已死
        "render_view",
        "maybe_render",
    ],
    "src/taa_futu/control_panel.py": [
        "_manual_strategy_label",
    ],
    "src/taa_futu/plugin.py": [
        "register_all",
    ],
}

# 整个删掉的文件
DELETE_FILES = [
    "src/taa_futu/unified_app.py",      # 依赖未安装的 pywebview，无任何调用方
    "src/taa_futu/audit_log.py",        # 模块与两个公开函数均 0 引用
    "_removed_features_backup/history.py",
    "_removed_features_backup/stock.py",
]

# 要删的顶层赋值（旧导航的常量）
DELETE_ASSIGNS = {
    "src/taa_futu/dashboard_extras.py": [
        "SIDEBAR_OPTIONS", "EXTRA_PAGE_OPTIONS", "PAGE_RENDERERS",
    ],
}


def span_of(node, lines, src_lines):
    """函数/赋值在文件中的行范围（1-based，含）。往上吃掉紧邻的注释与空行。"""
    start = min([d.lineno for d in getattr(node, "decorator_list", [])] or [node.lineno])
    i = start - 2
    while i >= 0 and (src_lines[i].strip().startswith("#") or not src_lines[i].strip()):
        i -= 1
    start = i + 2
    return start, node.end_lineno


def cut(path: Path, func_names, assign_names, dry):
    src = path.read_text(encoding="utf-8")
    src_lines = src.splitlines()
    tree = ast.parse(src)

    spans, found = [], {}
    for node in tree.body:
        name = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in func_names:
            name = node.name
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in assign_names:
                    name = t.id
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id in assign_names:
                name = node.target.id
        if name:
            s, e = span_of(node, src_lines, src_lines)
            spans.append((s, e))
            found[name] = e - s + 1

    missing = [n for n in list(func_names) + list(assign_names) if n not in found]
    if dry:
        return {"found": found, "missing": missing, "lines_removed": sum(found.values())}

    keep = []
    drop = set()
    for s, e in spans:
        drop.update(range(s, e + 1))
    for i, line in enumerate(src_lines, 1):
        if i not in drop:
            keep.append(line)
    new = "\n".join(keep).rstrip() + "\n"
    ast.parse(new)              # 删完必须仍是合法 Python
    path.write_text(new, encoding="utf-8")
    return {"found": found, "missing": missing, "lines_removed": sum(found.values())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args, _ = ap.parse_known_args()

    out = {"kind": "deadcode_cut", "applied": args.apply, "files": {}}
    total = 0
    for rel, names in TARGETS.items():
        p = TRADE / rel
        if not p.exists():
            out["files"][rel] = {"error": "文件不存在"}
            continue
        try:
            r = cut(p, names, DELETE_ASSIGNS.get(rel, []), dry=not args.apply)
        except SyntaxError as exc:
            out["files"][rel] = {"error": f"删后语法错误，已放弃：{exc}"}
            continue
        out["files"][rel] = r
        total += r["lines_removed"]

    # 只删常量、不删函数的文件
    for rel, names in DELETE_ASSIGNS.items():
        if rel in TARGETS:
            continue
        p = TRADE / rel
        if p.exists():
            r = cut(p, [], names, dry=not args.apply)
            out["files"][rel] = r
            total += r["lines_removed"]

    out["func_lines_removed"] = total

    file_lines = 0
    removed_files = []
    for rel in DELETE_FILES:
        p = TRADE / rel
        if not p.exists():
            continue
        n = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
        file_lines += n
        removed_files.append({"path": rel, "lines": n})
        if args.apply:
            subprocess.run(["git", "-C", str(TRADE), "rm", "-q", "-f", rel],
                           capture_output=True, text=True)
            if p.exists():
                p.unlink()
    out["deleted_files"] = removed_files
    out["file_lines_removed"] = file_lines
    out["total_lines_removed"] = total + file_lines

    if args.apply:
        # 空目录收尾
        bak = TRADE / "_removed_features_backup"
        if bak.exists() and not any(bak.iterdir()):
            bak.rmdir()
            out["removed_empty_dir"] = str(bak)

        # 残留引用检查
        leftovers = {}
        allnames = [n for ns in TARGETS.values() for n in ns] \
                   + [n for ns in DELETE_ASSIGNS.values() for n in ns] \
                   + ["unified_app", "audit_log", "_removed_features_backup"]
        for n in allnames:
            r = subprocess.run(
                ["grep", "-rn", "--include=*.py", "--include=*.toml",
                 "--include=*.command", n, str(TRADE / "src"), str(TRADE / "tests"),
                 str(TRADE / "pyproject.toml")],
                capture_output=True, text=True)
            hits = [l for l in r.stdout.splitlines() if l.strip()]
            if hits:
                leftovers[n] = hits[:4]
        out["leftover_references"] = leftovers

        # 全部改过的文件重新编译
        bad = []
        for rel in list(TARGETS) + list(DELETE_ASSIGNS):
            p = TRADE / rel
            if p.exists():
                try:
                    ast.parse(p.read_text(encoding="utf-8"))
                except SyntaxError as exc:
                    bad.append(f"{rel}: {exc}")
        out["syntax_errors"] = bad

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
