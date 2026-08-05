"""下单练习台的界面。

一屏三块：左边价格阶梯（看盘口），中间下单，右边进度和提醒。
每按一次「下一回合」，市场往前走一段时间，你挂的单可能成交也可能没成交。

界面上刻意不显示「真实价值」和影子市场——那两样是评分用的，练的时候看得到
就等于开了外挂。收工评分之后才摊开给你看。
"""
from __future__ import annotations

import streamlit as st

from .book import BUY, SELL
from .market import MarketConfig, load_config, profile_path
from .session import Session, TaskSpec

STATE = "jq_exec_trainer_session"


_START_TIMES = {
    "09:30 开盘": 0,
    "10:30 上午": 3600,
    "12:30 午盘（最淡）": 3 * 3600,
    "15:00 收盘前": 5.5 * 3600,
}

# NVDA 盘中每秒成交多少股（5 个交易日的 1 分钟 K 线中位数）。
# 有成交量档案时用档案里的值，这里只是兜底。
_BASE_VPS = 3027.0


def _base_vps() -> float:
    import json
    p = profile_path().parent / "US_NVDA_volume_profile.json"
    if p.exists():
        try:
            return float(json.loads(p.read_text(encoding="utf-8"))["volume_per_sec_median"])
        except Exception:
            pass
    return _BASE_VPS


# ── 小工具 ───────────────────────────────────────────────────────────────

def _est_pov(cfg, start_sec: float, horizon_sec: float, shares: int) -> float:
    """估这一局的参与率：任务量 ÷ 这段时间市场会成交多少股。

    参与率是做大单唯一真正的约束。一样是 10 万股，开盘那半小时做掉毫不费力，
    午盘那一格就要吃掉市场三成的成交量——这个数必须在开局前就摆出来。
    """
    prof = cfg.intraday_profile
    n = max(1, int(horizon_sec // 60))
    mult = 0.0
    for i in range(n):
        t = start_sec + i * 60
        mult += prof[min(max(int(t // 1800), 0), len(prof) - 1)]
    mult /= n
    return shares / max(_base_vps() * mult * horizon_sec, 1.0)


def _pov_note(pov: float, shares: int) -> str:
    if pov < 0.02:
        tag, why = "🟢 轻松", "占市场成交不到 2%，慢慢做基本不会留下痕迹"
    elif pov < 0.06:
        tag, why = "🟡 有点分量", "占市场成交 2–6%，急了就会看到成本"
    elif pov < 0.12:
        tag, why = "🟠 难", "占市场成交 6–12%，机构真实大单的量级，必须耐心"
    else:
        tag, why = "🔴 很难", "占市场成交超过 12%，怎么做都会推动价格"
    return (f"**预计参与率 {pov*100:.1f}%　{tag}**　—— {why}。\n\n"
            f"参考：NVDA 一天成交约 7080 万股，这笔单是全天的 {shares/70_835_705*100:.2f}%。")




def _fmt_secs(s: float) -> str:
    m, sec = divmod(int(max(0, s)), 60)
    return f"{m:02d}:{sec:02d}"


def _ladder_html(board: dict, levels: int) -> str:
    """价格阶梯。自己的挂单单独标出来，一眼看得到排在哪一档。"""
    rows = []
    for px, qty, mine, others in reversed(board["ask"][:levels]):
        rows.append(("ask", px, qty, mine, others))
    rows.append(("spread", None, None, None, None))
    for px, qty, mine, others in board["bid"][:levels]:
        rows.append(("bid", px, qty, mine, others))

    maxq = max([r[2] for r in rows if r[2]] or [1])
    out = ['<table class="jq-ladder">']
    for kind, px, qty, mine, others in rows:
        if kind == "spread":
            sp = board["spread_ticks"]
            mid = board["mid"]
            out.append(
                f'<tr class="mid"><td colspan="3">中间价 {mid:.2f}　价差 {sp} tick</td></tr>'
                if mid else '<tr class="mid"><td colspan="3">盘口空了</td></tr>')
            continue
        w = int(100 * others / maxq)
        tag = f'<span class="mine">我 {mine:,}</span>' if mine else ""
        out.append(
            f'<tr class="{kind}">'
            f'<td class="px">{px:.2f}</td>'
            f'<td class="bar"><div style="width:{w}%"></div><span>{qty:,}</span></td>'
            f'<td class="me">{tag}</td></tr>')
    out.append("</table>")
    return "".join(out)


_CSS = """
<style>
.jq-ladder{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:13px}
.jq-ladder td{padding:2px 6px;border-bottom:1px solid rgba(128,128,128,.15)}
.jq-ladder .px{width:64px;font-weight:600}
.jq-ladder .bar{position:relative;width:auto}
.jq-ladder .bar div{position:absolute;left:0;top:2px;bottom:2px;border-radius:2px;opacity:.28}
.jq-ladder .bar span{position:relative;padding-left:4px}
.jq-ladder tr.ask .px{color:#e05252}
.jq-ladder tr.ask .bar div{background:#e05252}
.jq-ladder tr.bid .px{color:#2e9e5b}
.jq-ladder tr.bid .bar div{background:#2e9e5b}
.jq-ladder tr.mid td{text-align:center;font-size:12px;opacity:.75;padding:6px 0;
  border-top:1px solid rgba(128,128,128,.45);border-bottom:1px solid rgba(128,128,128,.45)}
.jq-ladder .me{width:88px;text-align:right}
.jq-ladder .mine{background:#f5b12e;color:#111;border-radius:3px;padding:1px 5px;font-size:11px;font-weight:700}
.jq-ladder tr.hdr td{font-size:11px;opacity:.6;font-weight:600}
.jq-ladder .q-bad{color:#e05252;font-weight:700}
.jq-ladder .q-ok{color:#2e9e5b;font-weight:600}
</style>
"""


# ── 主界面 ───────────────────────────────────────────────────────────────

def render_exec_trainer(settings=None) -> None:
    st.markdown(_CSS, unsafe_allow_html=True)
    sess: Session | None = st.session_state.get(STATE)

    if sess is None:
        _render_setup()
        return

    if sess.finished and st.session_state.get(STATE + "_scored"):
        _render_report(sess)
        return

    _render_trading(sess)


def _render_setup() -> None:
    st.subheader("下单练习台")
    st.caption(
        "给你一个大单任务，你自己决定怎么切、什么时候挂单什么时候吃单。"
        "收工按「你不在场的那个市场」的成交均价打分——行情涨跌两边一样，"
        "差出来的就是你留下的脚印。")
    c1, c2, c3 = st.columns(3)
    side = c1.radio("方向", ["买入", "卖出"], horizontal=True, key="et_side")
    total = c2.number_input("任务量（股）", 1_000, 5_000_000, 1_000_000, step=50_000, key="et_total")
    mins = c3.number_input("限时（分钟）", 5, 240, 60, step=5, key="et_mins")
    c4, c5, c6 = st.columns(3)
    turn = c4.select_slider("一回合多少秒", [15, 30, 60, 120, 300], value=60, key="et_turn")
    start_label = c5.selectbox("什么时候开始（美东）", list(_START_TIMES), index=1, key="et_start")
    seed = c6.number_input("行情编号（换一个就是另一天）", 1, 100, 7, key="et_seed")

    start_sec = _START_TIMES[start_label]
    cfg0 = load_config()
    pov = _est_pov(cfg0, start_sec, mins * 60, int(total))
    st.caption(f"限时 {mins} 分钟、一回合 {turn} 秒 → 一共 {int(mins*60//turn)} 个回合")
    st.markdown(_pov_note(pov, int(total)))
    if not profile_path().exists():
        st.warning("没找到标定档案，用的是代码里写死的默认值（同一批数据的中位数）。"
                   "要重新标定：先跑 calibrate.py 和 calibrate_volume.py，再跑各自的 merge。",
                   icon="⚠️")
    if st.button("开始", type="primary", use_container_width=True):
        task = TaskSpec(side=BUY if side == "买入" else SELL,
                        total_shares=int(total),
                        horizon_sec=float(mins) * 60,
                        turn_sec=float(turn),
                        start_sec_after_open=float(start_sec))
        # 上一局留下的输入框数值要清掉：新任务量比上次小的时候，
        # 残留的旧数值会超出输入框的上限，Streamlit 会直接报错。
        for k in ("et_qty", "et_depth", "et_fast", "et_show"):
            st.session_state.pop(k, None)
        st.session_state[STATE] = Session(task, load_config(seed=int(seed)))
        st.session_state[STATE + "_scored"] = False
        st.rerun()


def _render_trading(sess: Session) -> None:
    b = sess.board()
    task = sess.task
    buying = task.side == BUY
    word = "买" if buying else "卖"

    done_pct = sess.done_shares / task.total_shares
    # 计划线：按时间均匀推进的话现在该做完多少。落后太多收工要被罚。
    plan = min(1.0, sess.elapsed / task.horizon_sec) if task.horizon_sec else 1.0

    top = st.columns([1.6, 1, 1, 1, 1, 1])
    top[0].markdown(f"**{word}入 {task.total_shares:,} 股 {task.symbol}**")
    top[1].metric("已成交", f"{sess.done_shares:,}", f"{done_pct*100:.0f}%")
    top[2].metric("剩余时间", _fmt_secs(sess.remaining_sec))
    # 时钟和活跃度：什么时候做单跟做多少一样重要。
    # 开盘半小时占全天成交 17%，午后那一格只占 3.9%——差四倍。
    fm = b["flow_mult"]
    top[3].metric("现在（美东）", b["clock"], f"活跃度 {fm:.2f}x", delta_color="off")
    avg = sess.avg_price()
    top[4].metric("我的均价", "—" if avg != avg else f"{avg:.3f}")
    top[5].metric("到达价", f"{sess.arrival:.3f}")
    st.progress(done_pct, text=f"进度 {done_pct*100:.0f}%　计划线 {plan*100:.0f}%")
    if done_pct < plan - 0.12:
        st.warning(f"落后计划线 {(plan-done_pct)*100:.0f} 个百分点。收工没做完的部分要按 "
                   f"{task.unfilled_penalty_bp:.0f}bp 计罚。", icon="⏳")

    left, mid, right = st.columns([1.1, 1, 1])

    with left:
        st.markdown("**盘口**")
        st.markdown(_ladder_html(b, 8), unsafe_allow_html=True)
        if fm >= 1.3:
            st.success(f"这个时段市场很忙（{fm:.1f} 倍），做单的好时机", icon="🔥")
        elif fm <= 0.75:
            st.info(f"这个时段很淡（{fm:.1f} 倍），同样的量推动价格更狠", icon="🌙")

    with mid:
        st.markdown("**下单**")
        if not b["bid"] or not b["ask"]:
            st.error("盘口暂时空了，先走一回合")
        else:
            near = b["bid"][0][0] if buying else b["ask"][0][0]     # 本方最优价
            far = b["ask"][0][0] if buying else b["bid"][0][0]      # 对手价
            qty = st.number_input("数量（股）", 100, int(task.total_shares),
                                  min(5000, max(100, sess.left_shares)), step=100, key="et_qty")
            cA, cB = st.columns(2)
            depth = cA.selectbox("挂在本方第几档", [1, 2, 3, 4, 5], key="et_depth")
            # 冰山单：盘口上只露一部分。露得少别人看不出你要买一大笔，
            # 就不会抢在你前面挂；代价是排队照全额排，不会成交得更快。
            show = cB.number_input("盘口只显示（冰山）", 100, int(task.total_shares),
                                   min(200, max(100, int(qty))), step=100, key="et_show")
            step = -(depth - 1) if buying else (depth - 1)
            px = round(near + step * sess.market.cfg.tick, 4)
            st.caption(f"挂单价 {px:.2f}　对手价 {far:.2f}")
            norm = sess.market.cfg.depth_profile[0]
            if int(show) > norm * sess.market.cfg.mm_frontrun_ratio:
                st.caption(f"⚠️ 露出 {int(show):,} 股，超过这档平常厚度（{norm} 股）的两倍——"
                           f"别人会抢在你前面挂。藏起来的部分不会被看见。")
            if st.button(f"挂 {int(qty):,} 股 @ {px:.2f}（露 {min(int(show), int(qty)):,}）",
                         use_container_width=True):
                sess.send_limit(px, int(qty), display=int(show))
                st.rerun()
            if st.button(f"直接吃单 {int(qty):,} 股", use_container_width=True):
                got = sess.send_market(int(qty))
                st.toast(f"吃到 {got:,} 股")
                st.rerun()
            working = sess._working_shares()
            if st.button(f"撤销全部挂单（{working:,} 股）", use_container_width=True,
                         disabled=working == 0):
                sess.cancel_all()
                st.rerun()

        # 排队位置。这是这一版最该盯的数字：盘口显示的量只是冰山一角，
        # 前面通常还压着五六倍看不见的量。看着「才一百多股，马上轮到我」
        # 一直等下去，是做大单最常踩的坑。
        mine = b["my_orders"]
        if mine:
            st.markdown("**我的挂单**")
            rows = "".join(
                f"<tr><td>{o['price']:.2f}</td><td>{o['left']:,}</td>"
                f"<td>{o['shown']:,}</td><td>{o['level_others']:,}</td>"
                f"<td class=\"{'q-bad' if o['queue_ahead'] > 400 else 'q-ok'}\">{o['queue_ahead']:,}</td></tr>"
                for o in mine)
            st.markdown(
                '<table class="jq-ladder"><tr class="hdr"><td>价格</td><td>我还剩</td>'
                '<td>我露出</td><td>别人显示</td><td>排我前面</td></tr>' + rows + "</table>",
                unsafe_allow_html=True)
            worst = max(o["queue_ahead"] for o in mine)
            if worst > 400:
                st.caption(f"前面压着 {worst:,} 股才轮到你——盘口上看不见，"
                           f"那些是别人的隐藏单。等不起就得去吃单。")

        st.divider()
        c1, c2 = st.columns(2)
        if c1.button("下一回合 ▶", type="primary", use_container_width=True):
            sess.advance()
            st.rerun()
        fast = c2.selectbox("快进", [1, 3, 5, 10], index=0, label_visibility="collapsed", key="et_fast")
        if c2.button(f"快进 {fast} 回合 ⏩", use_container_width=True):
            for _ in range(int(fast)):
                if sess.finished:
                    break
                sess.advance()
            st.rerun()
        if st.button("收工并评分", use_container_width=True):
            sess.close_out()
            st.session_state[STATE + "_scored"] = True
            st.rerun()

    with right:
        st.markdown("**这单干得怎么样**")
        trades = sess.market_trades()
        mkt = sum(t.qty for t in trades)
        part = sess.done_shares / mkt if mkt else 0.0
        passive = sum(f.qty for f in sess.fills if f.passive)
        prate = passive / sess.done_shares if sess.done_shares else 0.0
        m1, m2 = st.columns(2)
        m1.metric("参与率", f"{part*100:.0f}%", help="自己的成交占同期市场总成交的比例，一般别超过 20%")
        m2.metric("被动成交", f"{prate*100:.0f}%", help="挂单等来的占比。全靠吃单的话价差白付")
        if part > 0.25:
            st.warning("砸得太急了，冲击成本会很难看", icon="⚠️")
        if sess._working_shares() > sess.market.cfg.depth_profile[0] * 2:
            st.warning("挂出去的量远超这档平常的厚度——别人看得见，会抢在你前面挂。", icon="👀")

        st.markdown("**最近成交**")
        if sess.fills:
            rows = "".join(
                f"<tr><td>{_fmt_secs(f.ts - sess.t0)}</td><td>{f.price:.2f}</td>"
                f"<td>{f.qty:,}</td><td>{'挂单' if f.passive else '吃单'}</td></tr>"
                for f in reversed(sess.fills[-12:]))
            st.markdown(f'<table class="jq-ladder">{rows}</table>', unsafe_allow_html=True)
        else:
            st.caption("还没成交")

    if sess.finished and not st.session_state.get(STATE + "_scored"):
        st.info("时间到了。按「收工并评分」看成绩。")


def _render_report(sess: Session) -> None:
    r = sess.report()
    color = {"优秀": "🟢", "良好": "🔵", "及格": "🟡", "不及格": "🔴", "未完成": "🔴"}[r.grade]
    st.subheader(f"{color} {r.grade}")

    c = st.columns(4)
    c[0].metric("对影子 VWAP", f"{r.slip_vs_vwap_bp:+.1f} bp",
                help="正数＝比「你不在场的那个市场」的成交均价差。及格线 7bp，1bp 以内算优秀")
    c[1].metric("对到达价", f"{r.slip_vs_arrival_bp:+.1f} bp",
                help="真正的盈亏，但一小时里行情自己就能走上百 bp，别拿这个数评价自己")
    c[2].metric("成交率", f"{r.filled/r.target*100:.0f}%")
    c[3].metric("我的均价", f"{r.avg_price:.3f}")

    c = st.columns(4)
    c[0].metric("影子 VWAP", f"{r.market_vwap:.3f}")
    c[1].metric("到达价", f"{r.arrival_price:.3f}")
    c[2].metric("被动成交", f"{r.passive_rate*100:.0f}%")
    c[3].metric("参与率", f"{r.participation_rate*100:.0f}%")

    for n in r.notes:
        st.warning(n)

    st.caption(
        "分数线是拿几种基准打法各跑 6 个随机日量出来的（买 100 万股 / 1 小时 / 10:30 开始，"
        "约 9% 参与率）：每回合等额打市价 9.9bp；等额挂单不追 7.2bp；"
        "只露小块 + 补市价 9.9bp；挂单为主 + 落后补市价 2.4bp。"
        "任务量小的时候这些数会全线下移——参与率才是难度的真正来源。")

    if st.button("再来一局", type="primary"):
        for k in (STATE, STATE + "_scored", "et_qty", "et_depth", "et_fast", "et_show"):
            st.session_state.pop(k, None)
        st.rerun()
