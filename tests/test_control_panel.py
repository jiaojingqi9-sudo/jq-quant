from taa_futu.control_panel import SRC_ROOT, build_env, is_port_open


def test_build_env_includes_src_root() -> None:
    env = build_env()
    assert str(SRC_ROOT) in env["PYTHONPATH"]


def test_is_port_open_returns_false_for_unused_port() -> None:
    assert is_port_open("127.0.0.1", 65534, timeout=0.01) is False
