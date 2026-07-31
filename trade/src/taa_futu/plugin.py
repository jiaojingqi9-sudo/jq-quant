"""JQ Quant 插件契约与注册表。

设计目标：核心稳定，功能可插拔——一个功能模块「插上就能用」，
拔掉也不影响其他功能。

三条硬规矩：

1. **核心不认识任何具体功能。** 本文件不 import 股票、加密、新闻等任何模块。
   功能反过来向核心登记自己。这样加一个功能不需要改核心代码，删一个功能也
   不会在核心留下悬空引用。

2. **一个功能坏掉，不能拖垮整个应用。** 发现阶段的导入错误、渲染阶段的异常，
   都被隔离在该功能内部，其余功能照常工作。

3. **同一份代码支持两种形态。** 统一版把所有功能装进一个外壳，独立版只跑一个
   功能，两者调用的是同一个 ``render``，不存在两套实现互相偏离的问题。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib
import logging
import pkgutil
from typing import Any, Callable, Iterable


_log = logging.getLogger(__name__)


def _render_identity(fn: Any) -> tuple:
    """认出「同一个渲染函数」，用来判断重复登记是否值得警告。

    不能用 ``is`` 比较：dashboard_app 把 stock 与 stock_history 的 render 写成
    main() 内部的闭包，而 dashboard_app.py 是 Streamlit 直接执行的脚本，每次
    交互都重跑一遍 main()，每次都是新的函数对象。按身份比对会次次判定"实现
    不同"，日志每次交互刷两行，真正的重名冲突反倒被淹没。

    按「定义在哪个模块的哪个函数」比对，重跑同一份代码就认得出是同一个。
    """
    return (getattr(fn, "__module__", None), getattr(fn, "__qualname__", None))


@dataclass(frozen=True)
class Availability:
    """功能当前是否可用，以及为什么不可用。"""

    ok: bool
    detail: str = ""


@dataclass
class Feature:
    """一个可插拔的功能模块。

    只有 ``id``、``label``、``render`` 是必填的——把一个现成的
    ``render_x(settings)`` 函数挂上来就够了，其余都有合理默认值。
    """

    id: str
    label: str
    render: Callable[[Any], None]

    icon: str = "▪"
    # 侧边栏与首页的排序，小的在前
    order: int = 100
    # 一句话说明这个功能是干什么的，显示在首页卡片上
    summary: str = ""
    # 在首页怎么摆：
    #   "card"  大卡片（带说明），给主力子系统
    #   "quick" 底部快捷链接，给辅助功能
    #   "none"  首页不出现，只能从侧边栏进
    # 有 home_block 的功能会额外整行展示内容，与本字段独立。
    placement: str = "card"
    # 首页按钮的 key。默认 enter_<id>，可覆盖以保持既有约定不变
    # （端到端测试按 key 点击，改名会让它们静默找不到目标）。
    home_button_key: str = ""
    # 首页要展示的紧凑区块（比如新闻的当日告警）。返回 None 表示不在首页露出。
    home_block: Callable[[], None] | None = None
    # 运行前的可用性检查，比如依赖的服务没起来。返回 Availability。
    check: Callable[[], Availability] | None = None
    # 独立版窗口标题，缺省用 label
    standalone_title: str = ""
    # 这个功能是否需要 settings（少数功能不需要，独立版可据此跳过昂贵的初始化）
    needs_settings: bool = True
    # 附加信息，供外壳自定义展示
    meta: dict[str, Any] = field(default_factory=dict)

    def title(self) -> str:
        return self.standalone_title or self.label

    def button_key(self) -> str:
        return self.home_button_key or f"enter_{self.id}"

    def short_label(self) -> str:
        """中文短名，用于按钮与卡片标题（去掉 " / English" 后缀）。"""
        return self.label.split(" / ")[0]

    def availability(self) -> Availability:
        if self.check is None:
            return Availability(True)
        try:
            return self.check()
        except Exception as exc:  # 检查本身出错不应该让功能消失
            _log.warning("feature %s 可用性检查失败: %s", self.id, exc)
            return Availability(True, f"可用性检查异常：{exc}")


class FeatureRegistry:
    """功能登记处。核心通过它认识功能，而不是通过 import。"""

    def __init__(self) -> None:
        self._features: dict[str, Feature] = {}
        self._discovered = False
        self._errors: list[tuple[str, str]] = []

    # ── 登记 ──────────────────────────────────────────────────────────────
    def register(self, feature: Feature) -> Feature:
        """登记一个功能。同一个 id 重复登记时后者覆盖前者。

        只有「同 id 但换了实现」才值得警告。宿主每次脚本重跑都会重新登记
        stock 与 stock_history（dashboard_app 是 Streamlit 直接执行的脚本，
        main() 每次 rerun 都跑一遍），那是正常行为——之前不加区分地警告，
        导致每一次页面交互都往日志里刷两行，真正的重名冲突反而被淹没。
        """
        existing = self._features.get(feature.id)
        if existing is not None and _render_identity(existing.render) != _render_identity(feature.render):
            _log.warning("功能 %s 重复登记且实现不同，后者覆盖前者", feature.id)
        self._features[feature.id] = feature
        return feature

    def unregister(self, feature_id: str) -> None:
        self._features.pop(feature_id, None)

    # ── 发现 ──────────────────────────────────────────────────────────────
    def discover(self, package: str = "taa_futu.features", force: bool = False) -> None:
        """导入功能包下的所有模块，让它们各自登记。

        某个功能导入失败只记录错误，不影响其他功能——这是「一个坏了不拖垮全部」
        的第一道防线。
        """
        if self._discovered and not force:
            return
        self._discovered = True
        try:
            pkg = importlib.import_module(package)
        except Exception as exc:
            self._errors.append((package, f"功能包导入失败：{exc}"))
            _log.error("无法导入功能包 %s: %s", package, exc)
            return

        for mod in pkgutil.iter_modules(pkg.__path__):
            if mod.name.startswith("_"):
                continue
            full = f"{package}.{mod.name}"
            try:
                importlib.import_module(full)
            except Exception as exc:
                self._errors.append((full, str(exc)))
                _log.error("功能模块 %s 导入失败: %s", full, exc)

    # ── 查询 ──────────────────────────────────────────────────────────────
    def all(self) -> list[Feature]:
        return sorted(self._features.values(), key=lambda f: (f.order, f.label))

    def get(self, feature_id: str) -> Feature | None:
        return self._features.get(feature_id)

    def ids(self) -> list[str]:
        return [f.id for f in self.all()]

    @property
    def errors(self) -> list[tuple[str, str]]:
        """发现阶段出问题的模块，外壳可以显示出来而不是静默吞掉。"""
        return list(self._errors)

    # ── 渲染 ──────────────────────────────────────────────────────────────
    def render(self, feature_id: str, settings: Any = None) -> bool:
        """渲染指定功能。返回 True 表示这个功能存在且已处理。

        渲染中的异常被就地捕获并显示，不向上冒泡——否则一个功能的 bug 会让
        整个应用白屏。
        """
        feature = self.get(feature_id)
        if feature is None:
            return False

        import streamlit as st

        status = feature.availability()
        if not status.ok:
            st.warning(f"{feature.label} 当前不可用：{status.detail}")
            return True

        try:
            feature.render(settings) if feature.needs_settings else feature.render(None)
        except Exception as exc:  # noqa: BLE001 - 故意兜住所有异常
            import traceback
            st.error(f"「{feature.label}」渲染出错：{exc}")
            with st.expander("错误详情"):
                st.code(traceback.format_exc(), language="text")
        return True


# 进程内共享的注册表
registry = FeatureRegistry()


def feature(**kwargs: Any) -> Callable[[Callable[[Any], None]], Callable[[Any], None]]:
    """装饰器写法，把一个 render 函数直接登记成功能。

        @feature(id="news", label="市场新闻", icon="📰", order=60)
        def render_news(settings): ...

    返回原函数，所以被装饰的函数仍可被直接调用与单独测试。
    """

    def wrap(fn: Callable[[Any], None]) -> Callable[[Any], None]:
        registry.register(Feature(render=fn, **kwargs))
        return fn

    return wrap
