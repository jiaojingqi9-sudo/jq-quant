from taa_futu.control_panel import (
    SRC_ROOT,
    build_env,
    detect_tcl_tk_paths,
    ensure_tcl_tk_env,
    is_port_open,
)


def test_build_env_includes_src_root() -> None:
    env = build_env()
    assert str(SRC_ROOT) in env["PYTHONPATH"]


def test_is_port_open_returns_false_for_unused_port() -> None:
    assert is_port_open("127.0.0.1", 65534, timeout=0.01) is False


def test_detect_tcl_tk_paths_returns_existing_dirs() -> None:
    tcl_dir, tk_dir = detect_tcl_tk_paths()
    assert tcl_dir is not None
    assert tk_dir is not None
    assert tcl_dir.exists()
    assert tk_dir.exists()


def test_ensure_tcl_tk_env_sets_env(monkeypatch) -> None:
    monkeypatch.delenv("TCL_LIBRARY", raising=False)
    monkeypatch.delenv("TK_LIBRARY", raising=False)
    tcl_dir, tk_dir = ensure_tcl_tk_env()
    assert tcl_dir is not None
    assert tk_dir is not None
