#!/usr/bin/env python3
"""githubcheck - 检查本机的 GitHub 相关配置，只读，绝不回传任何密钥内容。

只报告"有没有"，不读取私钥、token 或密码。
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home()


def _run(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"


def which(name):
    found = shutil.which(name)
    if found:
        return found
    for base in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
        p = Path(base) / name
        if p.exists():
            return str(p)
    return ""


def main():
    out = {"kind": "githubcheck"}

    # git 本身
    gitbin = which("git")
    out["git"] = {"path": gitbin}
    if gitbin:
        _, so, _ = _run([gitbin, "--version"])
        out["git"]["version"] = so
        _, name, _ = _run([gitbin, "config", "--global", "user.name"])
        _, email, _ = _run([gitbin, "config", "--global", "user.email"])
        out["git"]["global_user_name"] = name or "(未设置)"
        out["git"]["global_user_email"] = email or "(未设置)"

    # gh CLI（有的话建仓库最省事）
    ghbin = which("gh")
    out["gh_cli"] = {"installed": bool(ghbin), "path": ghbin}
    if ghbin:
        rc, so, se = _run([ghbin, "auth", "status"], timeout=25)
        out["gh_cli"]["authenticated"] = rc == 0
        # 只保留是否登录的结论，不回传 token
        out["gh_cli"]["summary"] = ("已登录" if rc == 0 else "未登录")

    # SSH 密钥（只看文件是否存在，不读内容）
    ssh_dir = HOME / ".ssh"
    keys = []
    if ssh_dir.exists():
        for pub in sorted(ssh_dir.glob("*.pub")):
            keys.append(pub.name)
    out["ssh"] = {
        "dir_exists": ssh_dir.exists(),
        "public_keys": keys,
        "has_config": (ssh_dir / "config").exists(),
    }
    if keys:
        rc, so, se = _run(["ssh", "-o", "StrictHostKeyChecking=no",
                           "-o", "ConnectTimeout=10", "-T", "git@github.com"], timeout=25)
        blob = (so + " " + se).lower()
        out["ssh"]["github_test"] = (
            "已认证" if "successfully authenticated" in blob
            else ("能连上但未认证" if "permission denied" in blob else "连不上或未配置")
        )

    # 凭证助手（https 方式用）
    if gitbin:
        _, helper, _ = _run([gitbin, "config", "--global", "credential.helper"])
        out["credential_helper"] = helper or "(未设置)"

    # 现有仓库的远程
    out["repos"] = {}
    for name in ("news collector", "trade"):
        repo = HOME / "All here" / name
        if (repo / ".git").exists():
            _, so, _ = _run([gitbin, "-C", str(repo), "remote", "-v"])
            out["repos"][name] = so or "(无远程)"

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
