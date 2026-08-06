"""界面上给出的命令必须能在当前这台机器上直接粘贴运行。

2026-08-06 在一台 Windows 机器上照 README 部署时踩到：各处写死
``.venv/bin/taa-futu``，而 Windows 的 venv 可执行文件在 ``.venv\\Scripts\\``，
照着敲报「系统找不到指定的路径」。
"""
from taa_futu.cli_hint import venv_command


def test_posix_venv(monkeypatch, tmp_path) -> None:
    binv = tmp_path / ".venv" / "bin"
    binv.mkdir(parents=True)
    (binv / "taa-futu").write_text("")
    monkeypatch.chdir(tmp_path)
    assert venv_command("stock-learning-build", exe=str(binv / "python"), on_windows=False) \
        == ".venv/bin/taa-futu stock-learning-build"


def test_windows_uses_scripts_dir_without_exe_suffix(monkeypatch, tmp_path) -> None:
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "taa-futu.exe").write_text("")
    monkeypatch.chdir(tmp_path)
    cmd = venv_command("stock-learning-build", exe=str(scripts / "python.exe"), on_windows=True)
    # POSIX 上跑测试时 Path 仍是 PosixPath，分隔符不是反斜杠，所以只断言实质
    assert "Scripts" in cmd and ".exe" not in cmd
    assert cmd.endswith("taa-futu stock-learning-build")


def test_falls_back_per_platform(tmp_path) -> None:
    assert venv_command("x", exe=str(tmp_path / "python.exe"), on_windows=True) == ".venv\\Scripts\\taa-futu x"
    assert venv_command("x", exe=str(tmp_path / "python"), on_windows=False) == ".venv/bin/taa-futu x"
