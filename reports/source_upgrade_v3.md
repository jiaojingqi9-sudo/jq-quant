# 信息源全面升级 & 美→中科技前沿词库方案（v3）

> 目标：提高官方信源占比、降低社交噪音、引入美→中科技差距追踪、强化短线突破信号。
> 所有改动必须满足 **与现有功能块低耦合** 的原则：只新增 Collector / 新增配置文件 / 在 tech_block 内部读取，不改 domain 层。

---

## §1 信息源分层体系

### 1.1 新分层定义

| 层级 | 定位 | 信源 | source_trust | 在 tech_block 中的角色 |
|------|------|------|-------------|----------------------|
| T1 官方权威 | 信号来源主力 | 财联社(CLS), 东方财富7×24, 东方财富焦点, 新华社, 国务院/工信部/科技部/发改委, 证监会, 交易所公告 | 0.82–1.00 | 完整参与打分 |
| T2 专业媒体 | 补充深度 | 36kr, 虎嗅, 钛媒体, 格隆汇, 凤凰科技 | 0.65–0.78 | 完整参与打分, 稍低权重 |
| T3 海外财经 | 前沿追踪 | Reuters RSS, SEC filings, HKEX | 0.80–0.98 | 驱动 tech_frontier_map 匹配 |
| T4 社交讨论 | **仅热度标记** | 微博, 雪球, 股吧 | 0.38–0.62 | **不产生独立信号**, 仅通过 discussion_count 驱动 social_signal 和 burst_freshness |

### 1.2 T4 社交降级机制

在 `tech_block.py` 的 `_build_signal` 中增加一条规则：

```python
# 如果 cluster 所有文档都来自 T4 源且无任何 T1/T2 交叉验证，跳过信号生成
t4_sources = {"weibo", "xueqiu", "guba"}
if all(sid in t4_sources for sid in cluster.source_ids):
    # 不产生交易信号——但仍然计算 discussion_count 供其他信号的热度加权使用
    return None
```

这样微博/雪球的帖子只有在至少一条 T1/T2 新闻也在报同一件事时，才会被聚类进去放大热度；如果只有社交在讨论、官媒没有报，直接过滤掉。

### 1.3 SOURCE_CREDIBILITY_MULTIPLIER 更新

在 `tech_block.py` 更新：

```python
SOURCE_CREDIBILITY_MULTIPLIER = {
    # --- T1 官方 ---
    "cls":              1.20,
    "eastmoney-724":    1.12,   # 新增：7×24 实时快讯
    "eastmoney-focus":  1.18,   # 新增：焦点精选
    "eastmoney-ann":    1.15,
    "eastmoney-news":   1.05,
    "xinhua-finance":   1.20,
    "xinhua-tech":      1.20,
    "csrc_home":        1.10,
    "gov-miit":         1.15,   # 新增：工信部
    "gov-most":         1.12,   # 新增：科技部
    "gov-ndrc":         1.10,   # 新增：发改委
    # --- T2 专业 ---
    "36kr":             0.92,
    "huxiu":            0.90,
    "tmtpost":          0.88,
    "gelonghui":        1.00,
    "ifeng-tech":       0.90,
    # --- T3 海外 ---
    "reuters-tech":     1.05,   # 新增
    "sec_press":        1.00,
    "hkex_news":        1.00,
    # --- T4 社交 (被 T4 过滤拦住, 但如果合并进 T1 cluster 仍需乘数) ---
    "weibo":            0.72,
    "xueqiu":           0.78,
}
```

---

## §2 新增采集器

### 2.1 东方财富焦点栏目 (EastmoneyFocusCollector)

**原理**：现有 `EastmoneyCollector` 的 `news` endpoint 已经在采集 7×24 全球直播（column 102）。"焦点"栏目对应 column 350，API 格式相同，只换 column ID。

**改动方式**（二选一，推荐 A）：

**方案 A — 扩展现有 EastmoneyCollector**：在 `NEWS_ENDPOINTS` 字典中新增一条：

```python
NEWS_ENDPOINTS = {
    "news":   "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_{page_size}_1_.html",
    "focus":  "https://newsapi.eastmoney.com/kuaixun/v1/getlist_350_ajaxResult_{page_size}_1_.html",  # 焦点
    "global": "https://globalnewsapi.eastmoney.com/api/News/GetNewsList?...",
}
```

在 `_collect_news` 中给 `focus` endpoint 设 `source_id = "eastmoney-focus"`，`source_trust = 0.88`。

**配置 `live_sources.json`**：

```json
"eastmoney": {
    "enabled": true,
    "endpoints": ["ann-a", "ann-h", "news", "focus", "global"],
    "page_size": 50,
    "global_page_size": 30,
    "max_records_per_endpoint": 25
}
```

### 2.2 政府部委政策收集器 (GovPolicyCollector)

用 HTML list+detail 模式即可，与现有 `csrc_home` 同类型。

**目标站点**：

| source_id | 站点 | URL | include_title_patterns |
|-----------|------|-----|----------------------|
| `gov-miit` | 工信部 | `https://www.miit.gov.cn/zwgk/zcwj/` | `人工智能\|芯片\|半导体\|机器人\|新材料\|软件\|集成电路\|通信\|5G\|6G\|制造\|数字\|算力\|储能\|新能源\|航空\|航天\|核电\|工业互联网` |
| `gov-most` | 科技部 | `https://www.most.gov.cn/xxgk/xinxifenlei/fdzdgknr/` | `科技\|创新\|人工智能\|量子\|集成电路\|生物\|航天\|新材料\|新能源\|数字` |
| `gov-ndrc` | 发改委 | `https://www.ndrc.gov.cn/xxgk/zcfb/` | `高技术\|战略性新兴\|数字经济\|新质生产力\|储能\|氢能\|核电\|半导体\|算力` |

**实现方式**：在 `live_sources.json` 的 `html_sources` 数组中新增 3 个配置块。与现有 `csrc_home` 同结构，`source_trust` 设 0.95–0.98。**不需要新代码**，复用现有的 `HtmlListDetailCollector`。

**注意**：政府网站的 HTML 结构各不相同，`body_container_patterns` 需要实测。初始设为：

```json
"body_container_patterns": [
    "<div[^>]+class=\\\"[^\\\"]*(?:TRS_Editor|article|content|xxgk_content)[^\\\"]*\\\"[^>]*>.*?</div>",
    "<article.*?</article>"
]
```

### 2.3 Reuters 科技 RSS (已有 RSS 采集器可复用)

在 `live_sources.json` 的 `rss.feeds` 数组中新增：

```json
{
    "url": "https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best",
    "name": "reuters-tech",
    "source_id": "reuters-tech",
    "source_trust": 0.88,
    "language": "en",
    "regions": ["US", "GLOBAL"],
    "include_title_patterns": [
        "AI|chip|semiconductor|quantum|robot|EUV|HBM|TSMC|ASML|Nvidia|data center|battery|nuclear|carbon fiber"
    ]
}
```

---

## §3 美→中科技前沿差距追踪词库 (tech_frontier_map)

这是本次升级的核心创新。

### 3.1 设计思路

建立一个结构化的"技术差距表"：每个条目描述一个美国领先的技术节点，对应的中国追赶状态，以及一旦中国出现突破新闻时应该激活哪些图节点和关注哪些股票。

**核心逻辑**：
- 美国科技新闻中提到某技术 → 更新该条目的"前沿状态"描述
- 中国新闻中出现该条目的 `cn_breakthrough_keywords` → 触发 `breakthrough_signal` → 加权到 tech_block 的 spec_score
- 这个匹配在 `tech_block.py` 的 `_build_signal` 中进行，与 lexicon 匹配同级并列

### 3.2 配置文件 `config/tech_frontier_map.json`

```json
[
    {
        "frontier_id": "euv-lithography",
        "cn_label": "EUV光刻",
        "us_label": "EUV Lithography",
        "gap_level": "large",
        "us_keywords": ["euv", "high-na", "asml", "pellicle"],
        "cn_breakthrough_keywords": [
            "极紫外", "EUV光源", "光刻机突破", "国产光刻", "光刻胶国产化",
            "掩模版", "上海微电子", "科益虹源"
        ],
        "impact_themes": ["semicap-equipment", "domestic-substitution", "advanced-process"],
        "related_symbols": ["002371.SZ", "688012.SH"],
        "breakthrough_bonus": 0.35,
        "note": "中国目前在 28nm DUV，EUV 完全依赖进口"
    },
    {
        "frontier_id": "hbm-memory",
        "cn_label": "HBM高带宽内存",
        "us_label": "HBM (High Bandwidth Memory)",
        "gap_level": "large",
        "us_keywords": ["hbm", "hbm3e", "hbm4", "sk hynix", "micron hbm"],
        "cn_breakthrough_keywords": [
            "国产HBM", "HBM量产", "长鑫存储", "长存", "CXMT",
            "高带宽内存突破", "自主HBM"
        ],
        "impact_themes": ["ai-compute", "chip-design", "advanced-packaging", "domestic-substitution"],
        "related_symbols": [],
        "breakthrough_bonus": 0.30,
        "note": "长鑫存储在追赶 HBM2E，与 SK Hynix HBM3E 差 2 代"
    },
    {
        "frontier_id": "advanced-node-sub5nm",
        "cn_label": "5nm以下制程",
        "us_label": "Sub-5nm Process Node",
        "gap_level": "large",
        "us_keywords": ["3nm", "2nm", "gaa", "gate-all-around", "nanosheet", "tsmc n2", "intel 18a"],
        "cn_breakthrough_keywords": [
            "3纳米", "2纳米", "GAA工艺", "先进制程突破", "中芯国际N+2",
            "SMIC量产", "国产3nm"
        ],
        "impact_themes": ["advanced-process", "chip-manufacturing", "semicap-equipment", "domestic-substitution"],
        "related_symbols": ["688981.SH", "00981.HK"],
        "breakthrough_bonus": 0.40,
        "note": "SMIC 最先进量产 7nm（N+1），3nm 及以下需要 EUV"
    },
    {
        "frontier_id": "solid-state-battery",
        "cn_label": "全固态电池",
        "us_label": "Solid-State Battery",
        "gap_level": "parallel",
        "us_keywords": ["solid-state battery", "quantumscape", "solid power", "samsung sdi solid"],
        "cn_breakthrough_keywords": [
            "全固态量产", "固态电池装车", "固态电解质突破", "宁德时代固态",
            "比亚迪固态", "卫蓝新能源", "清陶能源", "赣锋锂电固态"
        ],
        "impact_themes": ["energy-storage", "new-materials", "advanced-manufacturing", "order-momentum"],
        "related_symbols": ["300750.SZ", "002460.SZ"],
        "breakthrough_bonus": 0.25,
        "note": "中美日韩基本同步竞争，中国在半固态略微领先"
    },
    {
        "frontier_id": "humanoid-robot",
        "cn_label": "人形机器人",
        "us_label": "Humanoid Robot",
        "gap_level": "catching-up",
        "us_keywords": ["optimus", "figure", "1x", "humanoid", "boston dynamics atlas"],
        "cn_breakthrough_keywords": [
            "人形机器人量产", "宇树量产", "小米铁大", "智元机器人", "傅利叶",
            "人形机器人进工厂", "人形机器人订单", "机器人国标"
        ],
        "impact_themes": ["robotics", "ai-hardware", "advanced-manufacturing", "order-momentum"],
        "related_symbols": ["300124.SZ", "688256.SH"],
        "breakthrough_bonus": 0.22,
        "note": "宇树、智元等追赶速度极快，硬件差距在缩小"
    },
    {
        "frontier_id": "ai-chip-training",
        "cn_label": "AI训练芯片",
        "us_label": "AI Training GPU/Accelerator",
        "gap_level": "large",
        "us_keywords": ["b200", "gb200", "blackwell", "h100", "nvidia gpu", "amd mi300"],
        "cn_breakthrough_keywords": [
            "国产GPU突破", "海光新一代", "寒武纪新品", "昇腾突破",
            "壁仞科技", "摩尔线程", "国产AI芯片量产", "算力芯片流片"
        ],
        "impact_themes": ["chip-design", "ai-compute", "domestic-substitution"],
        "related_symbols": ["688041.SH", "688256.SH"],
        "breakthrough_bonus": 0.35,
        "note": "华为昇腾910B 对标 A100，差 Nvidia 2 代"
    },
    {
        "frontier_id": "quantum-computing",
        "cn_label": "量子计算",
        "us_label": "Quantum Computing",
        "gap_level": "parallel",
        "us_keywords": ["quantum supremacy", "logical qubit", "error correction", "ibm quantum", "google willow"],
        "cn_breakthrough_keywords": [
            "量子比特数突破", "九章", "祖冲之号", "量子纠错突破",
            "国盾量子", "本源量子", "量子计算云平台"
        ],
        "impact_themes": ["quantum-computing", "chip-design", "domestic-substitution"],
        "related_symbols": ["688027.SH"],
        "breakthrough_bonus": 0.20,
        "note": "中美在不同路线各有领先"
    },
    {
        "frontier_id": "carbon-fiber-t1200",
        "cn_label": "T1200级碳纤维",
        "us_label": "Ultra-High-Modulus Carbon Fiber",
        "gap_level": "catching-up",
        "us_keywords": ["toray t1100", "t1200", "hexcel", "sgl carbon"],
        "cn_breakthrough_keywords": [
            "T1100国产", "T1200国产", "高模碳纤维突破", "光威复材",
            "中复神鹰", "碳纤维自主可控", "国产碳纤维量产"
        ],
        "impact_themes": ["new-materials", "aerospace-defense", "domestic-substitution", "order-momentum"],
        "related_symbols": ["300699.SZ", "688295.SH"],
        "breakthrough_bonus": 0.22,
        "note": "T800 已量产，T1000/T1100 在追赶"
    },
    {
        "frontier_id": "aircraft-engine",
        "cn_label": "航空发动机",
        "us_label": "Aero Engine / Turbofan",
        "gap_level": "large",
        "us_keywords": ["leap", "pw1000g", "ge9x", "cfm rise", "pratt whitney"],
        "cn_breakthrough_keywords": [
            "CJ-1000A", "长江发动机", "国产大飞机发动机", "涡扇发动机突破",
            "航发动力", "航发控制", "高温合金叶片", "单晶叶片国产"
        ],
        "impact_themes": ["aerospace-defense", "new-materials", "advanced-manufacturing", "domestic-substitution"],
        "related_symbols": ["600893.SH", "000738.SZ"],
        "breakthrough_bonus": 0.30,
        "note": "C919 暂用 LEAP-1C，国产 CJ-1000A 还在试飞"
    },
    {
        "frontier_id": "commercial-aircraft-parts",
        "cn_label": "大飞机零部件国产化",
        "us_label": "Commercial Aircraft Supply Chain",
        "gap_level": "catching-up",
        "us_keywords": ["boeing 737", "airbus a320", "aircraft supply chain", "safran", "honeywell aero"],
        "cn_breakthrough_keywords": [
            "C919国产化率", "大飞机零部件", "机载系统国产", "航电国产",
            "起落架国产", "APU国产", "C929", "国产大飞机供应商",
            "中航电子", "中航光电", "航天电器", "博云新材"
        ],
        "impact_themes": ["aerospace-defense", "advanced-manufacturing", "domestic-substitution", "order-momentum"],
        "related_symbols": ["600372.SH", "002179.SZ", "600879.SH"],
        "breakthrough_bonus": 0.25,
        "note": "C919 国产化率约 60%，核心零部件仍大量进口"
    },
    {
        "frontier_id": "photoresist-chemicals",
        "cn_label": "光刻胶/电子特气",
        "us_label": "Advanced Photoresist & Electronic Chemicals",
        "gap_level": "large",
        "us_keywords": ["arf photoresist", "euv resist", "shin-etsu", "tok", "jsr"],
        "cn_breakthrough_keywords": [
            "光刻胶国产", "ArF光刻胶突破", "EUV光刻胶", "电子特气国产化",
            "彤程新材", "晶瑞电材", "南大光电", "华特气体", "雅克科技"
        ],
        "impact_themes": ["semicap-equipment", "chip-manufacturing", "domestic-substitution", "new-materials"],
        "related_symbols": ["603650.SH", "300655.SZ", "688076.SH"],
        "breakthrough_bonus": 0.28,
        "note": "ArF 国产化率 < 5%，EUV 光刻胶基本空白"
    },
    {
        "frontier_id": "sic-substrate",
        "cn_label": "碳化硅衬底",
        "us_label": "SiC Substrate & Epitaxy",
        "gap_level": "catching-up",
        "us_keywords": ["sic wafer", "wolfspeed", "coherent sic", "8-inch sic"],
        "cn_breakthrough_keywords": [
            "8英寸SiC", "碳化硅衬底量产", "三安光电SiC", "天科合达",
            "天岳先进", "山东天瑞重工", "SiC国产化"
        ],
        "impact_themes": ["new-materials", "chip-design", "energy-storage", "advanced-manufacturing"],
        "related_symbols": ["600703.SH", "688234.SH"],
        "breakthrough_bonus": 0.22,
        "note": "6 英寸已量产，8 英寸在突破中"
    },
    {
        "frontier_id": "nuclear-smr",
        "cn_label": "小型模块化核反应堆",
        "us_label": "Small Modular Reactor (SMR)",
        "gap_level": "leading",
        "us_keywords": ["nuscale", "smr", "x-energy", "terrapower"],
        "cn_breakthrough_keywords": [
            "玲龙一号", "ACP100", "高温气冷堆商用", "海南昌江",
            "小型堆出口", "浮动核电站", "核电审批"
        ],
        "impact_themes": ["nuclear-power", "ai-energy", "order-momentum", "advanced-manufacturing"],
        "related_symbols": ["601985.SH", "000777.SZ"],
        "breakthrough_bonus": 0.20,
        "note": "玲龙一号是全球首个陆上 SMR，中国在此领域领先"
    },
    {
        "frontier_id": "evtol-lowaltitude",
        "cn_label": "eVTOL/低空经济",
        "us_label": "eVTOL / Urban Air Mobility",
        "gap_level": "parallel",
        "us_keywords": ["joby aviation", "lilium", "archer", "evtol faa", "urban air mobility"],
        "cn_breakthrough_keywords": [
            "eVTOL适航", "低空经济政策", "亿航智能", "峰飞航空",
            "小鹏汇天", "低空空域开放", "无人机适航证"
        ],
        "impact_themes": ["aerospace-defense", "advanced-manufacturing", "chip-design", "order-momentum"],
        "related_symbols": ["EH", "002097.SZ"],
        "breakthrough_bonus": 0.20,
        "note": "亿航 EH216-S 全球首获适航证，政策层面中国推进更快"
    },
    {
        "frontier_id": "ai-inference-chip",
        "cn_label": "AI推理芯片",
        "us_label": "AI Inference Accelerator",
        "gap_level": "catching-up",
        "us_keywords": ["nvidia l40", "groq", "cerebras", "tenstorrent", "inference chip"],
        "cn_breakthrough_keywords": [
            "国产推理卡", "推理芯片量产", "燧原科技", "壁仞科技推理",
            "海光DCU推理", "寒武纪推理", "推理算力自主"
        ],
        "impact_themes": ["chip-design", "ai-compute", "domestic-substitution", "model-applications"],
        "related_symbols": ["688041.SH", "688256.SH"],
        "breakthrough_bonus": 0.28,
        "note": "推理卡差距小于训练卡，海光 DCU 已在大规模部署"
    }
]
```

### 3.3 tech_block.py 中的集成方式

在 `AHShareTechFeatureBlock.__init__` 中新增 `frontier_map: list[dict]` 参数。

在 `_build_signal` 中，**紧跟现有 lexicon 匹配循环之后**，新增 frontier 匹配循环：

```python
# --- Frontier breakthrough detection ---
frontier_hits: list[dict[str, Any]] = []
for entry in self.frontier_map:
    hit_terms = [kw for kw in entry["cn_breakthrough_keywords"] if self._contains(text, kw)]
    if not hit_terms:
        continue
    bonus = float(entry.get("breakthrough_bonus", 0.2))
    for theme in entry.get("impact_themes", []):
        theme_scores[theme] += bonus
        theme_drivers[theme].append(f"frontier-{entry['frontier_id']}")
    frontier_hits.append({
        "frontier_id": entry["frontier_id"],
        "cn_label": entry["cn_label"],
        "gap_level": entry["gap_level"],
        "matched_keywords": hit_terms[:4],
        "bonus": bonus,
    })
    # 突破消息天然是正向催化
    positive_bias += bonus * 0.8
    spec_raw += bonus * 1.2
    importance_raw += bonus * 1.0
```

输出 dict 中新增：
```python
"frontier_hits": frontier_hits,
```

这样 dashboard 可以直接显示"本信号触发了 EUV 光刻技术差距追踪 → 突破信号"。

### 3.4 `from_files` 加载

```python
@classmethod
def from_files(cls, ..., frontier_map_path: Path | None = None, ...) -> "AHShareTechFeatureBlock":
    ...
    frontier_map = []
    if frontier_map_path is not None and frontier_map_path.exists():
        frontier_map = json.loads(frontier_map_path.read_text(encoding="utf-8"))
    return cls(..., frontier_map=frontier_map, ...)
```

---

## §4 供应链国产化率突破词库

### 4.1 新增 lexicon 条目类型: `localization_catalyst`

在 `tech_lexicon.json` 中新增以下条目。这些不是具体技术，而是"国产化率提升"事件本身作为催化剂：

```json
{
    "canonical_text": "国产化率提升",
    "term_type": "localization_catalyst",
    "synonyms": [
        "国产化率", "国产替代率", "自主化比例", "进口替代进展",
        "供应链安全", "卡脖子突破", "断供替代", "零部件国产"
    ],
    "regexes": [],
    "impact_vector": {
        "domestic-substitution": 1.0,
        "order-momentum": 0.72,
        "advanced-manufacturing": 0.55
    },
    "trigger_tags": ["国产化催化", "供应链安全"],
    "base_confidence": 0.82,
    "spec_weight": 0.92,
    "heat_weight": 0.65,
    "importance_weight": 0.90,
    "direction_hint": "positive"
}
```

以及多个细分领域的国产化条目：

```json
{
    "canonical_text": "光刻胶/材料国产化",
    "term_type": "localization_catalyst",
    "synonyms": [
        "光刻胶国产", "电子特气国产", "CMP材料国产", "靶材国产",
        "湿电子化学品", "高纯试剂国产"
    ],
    "regexes": [],
    "impact_vector": {
        "semicap-equipment": 0.85,
        "domestic-substitution": 1.0,
        "chip-manufacturing": 0.65,
        "new-materials": 0.55
    },
    "trigger_tags": ["半导体材料", "国产化催化"],
    "base_confidence": 0.80,
    "spec_weight": 0.90,
    "heat_weight": 0.62,
    "importance_weight": 0.88,
    "direction_hint": "positive"
},
{
    "canonical_text": "大飞机零部件国产化",
    "term_type": "localization_catalyst",
    "synonyms": [
        "C919国产化", "机载设备国产", "航电系统国产", "飞控国产",
        "起落架国产", "APU国产", "大飞机供应商", "C929"
    ],
    "regexes": [],
    "impact_vector": {
        "aerospace-defense": 1.0,
        "domestic-substitution": 0.85,
        "advanced-manufacturing": 0.72,
        "order-momentum": 0.68
    },
    "trigger_tags": ["大飞机催化", "国产化"],
    "base_confidence": 0.82,
    "spec_weight": 0.88,
    "heat_weight": 0.75,
    "importance_weight": 0.85,
    "direction_hint": "positive"
},
{
    "canonical_text": "工业软件国产化",
    "term_type": "localization_catalyst",
    "synonyms": [
        "EDA国产", "CAD国产", "CAE国产", "仿真软件国产",
        "华大九天", "广立微", "思尔芯", "中望软件", "工业软件自主"
    ],
    "regexes": [],
    "impact_vector": {
        "domestic-substitution": 1.0,
        "cloud-software": 0.78,
        "chip-design": 0.55,
        "order-momentum": 0.50
    },
    "trigger_tags": ["EDA催化", "工业软件"],
    "base_confidence": 0.80,
    "spec_weight": 0.88,
    "heat_weight": 0.60,
    "importance_weight": 0.85,
    "direction_hint": "positive"
}
```

---

## §5 tech_impact_graph.json 补充边

| source | target | weight | relation | rationale |
|--------|--------|--------|----------|-----------|
| `xinchuang` → `industrial-ai` | 0.52 | demand-chain | 信创推动国产工业软件替代，工业 AI 是落地场景 |
| `nuclear-power` → `domestic-substitution` | 0.68 | policy-spillover | 核电核心设备国产化要求极高 |
| `ai-healthcare` → `order-momentum` | 0.55 | demand-chain | 医疗器械注册证/集采中标直接带来订单 |
| `quantum-computing` → `cybersecurity` | 0.48 | adjacent-theme | 量子通信/量子密钥是网络安全升级路径 |
| `energy-storage` → `new-materials` | 0.52 | supply-chain | 固态电解质、钠电正极等需要新材料突破 |

---

## §6 live_sources.json 总更新清单

### 6.1 eastmoney 配置

```json
"eastmoney": {
    "enabled": true,
    "endpoints": ["ann-a", "ann-h", "news", "focus", "global"],
    "page_size": 50,
    "global_page_size": 30,
    "max_records_per_endpoint": 25
}
```

### 6.2 新增 html_sources 条目（3 个政府站）

添加到 `html_sources` 数组末尾：

```json
{
    "enabled": true,
    "type": "html_list_detail",
    "name": "miit-policy",
    "source_id": "gov-miit",
    "url": "https://www.miit.gov.cn/zwgk/zcwj/wjfb/tz/index.html",
    "source_trust": 0.96,
    "timeout": 15,
    "language": "zh",
    "regions": ["CN"],
    "themes": ["technology", "policy"],
    "item_limit": 10,
    "detail_fetch_limit": 4,
    "include_link_patterns": ["/zwgk/.+\\.html"],
    "exclude_link_patterns": ["index\\.html$", "javascript:", "#"],
    "include_title_patterns": [
        "人工智能|芯片|半导体|机器人|新材料|软件|集成电路|通信|5G|6G|制造|数字|算力|储能|新能源|航空|航天|核电|工业互联网|工业母机|智能制造"
    ],
    "body_container_patterns": [
        "<div[^>]+class=\\\"[^\\\"]*(?:TRS_Editor|article|xxgk_content|content)[^\\\"]*\\\"[^>]*>.*?</div>",
        "<article.*?</article>"
    ],
    "metadata": {
        "source_category": "official-policy",
        "source_tier": "T1"
    }
}
```

同样结构复制给 `gov-most` 和 `gov-ndrc`，替换 URL 和 patterns。

### 6.3 新增 RSS 条目 (Reuters)

添加到 `rss.feeds` 数组：

```json
{
    "url": "https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best",
    "name": "reuters-tech",
    "source_id": "reuters-tech",
    "source_trust": 0.88,
    "language": "en",
    "regions": ["US", "GLOBAL"],
    "include_title_patterns": [
        "AI|chip|semiconductor|quantum|robot|EUV|HBM|TSMC|ASML|Nvidia|data center|battery|nuclear|carbon fiber|SpaceX"
    ]
}
```

---

## §7 实施任务清单 (Codex 按序执行)

### P0 — 核心 (必须先做)

- [ ] **T1**: 扩展 `EastmoneyCollector.NEWS_ENDPOINTS`，新增 `"focus"` endpoint（column 350），`source_id` = `"eastmoney-focus"`，`source_trust` = 0.88。更新 `live_sources.json`。
- [ ] **T2**: 在 `live_sources.json` 的 `html_sources` 中新增工信部、科技部、发改委 3 个配置块。实测并校准 `body_container_patterns` 和 `include_link_patterns`。
- [ ] **T3**: 创建 `config/tech_frontier_map.json`，写入 §3.2 的 15 个条目。
- [ ] **T4**: 在 `AHShareTechFeatureBlock.__init__` 中新增 `frontier_map` 参数；在 `from_files` 中新增 `frontier_map_path` 加载；在 `_build_signal` 中新增 §3.3 的 frontier 匹配循环。输出 dict 新增 `frontier_hits`。
- [ ] **T5**: 在 `_build_signal` 中加入 §1.2 的 T4 社交过滤规则（纯社交 cluster → return None）。
- [ ] **T6**: 更新 `SOURCE_CREDIBILITY_MULTIPLIER` 为 §1.3 版本。

### P1 — 词库扩展

- [ ] **T7**: 在 `tech_lexicon.json` 中新增 §4 的 4 个 `localization_catalyst` 条目。
- [ ] **T8**: 在 `tech_impact_graph.json` 中新增 §5 的 5 条边。
- [ ] **T9**: 在 `rss.feeds` 中新增 Reuters 科技 RSS。

### P2 — 验证与调优

- [ ] **T10**: 编写集成测试：模拟一条"国产光刻胶 ArF 突破量产"的新闻，验证 frontier_map 匹配 → impact_themes 正确 → 相关股票被召回 → 分数合理（trading_attention_score ≥ 65）。
- [ ] **T11**: 编写集成测试：模拟一条只有微博来源的 cluster，验证 T4 过滤生效（不产生信号但 discussion_count 正常累积）。
- [ ] **T12**: 运行一次完整 pipeline，检查新增信源的采集成功率、政府网站的 HTML 解析准确率，微调 `body_container_patterns`。

---

## §8 低耦合保证

| 改动位置 | 影响范围 |
|----------|---------|
| `EastmoneyCollector` 新增 focus endpoint | 仅 eastmoney.py，新增一个 dict entry |
| 政府站 html_sources | 仅 live_sources.json 配置，复用现有 HtmlListDetailCollector |
| `tech_frontier_map.json` | 新文件，仅被 tech_block.py 读取 |
| `_build_signal` frontier 匹配 | 在现有方法内部追加循环，不改返回类型 |
| T4 社交过滤 | 在 `_build_signal` 开头新增 3 行判断 |
| `SOURCE_CREDIBILITY_MULTIPLIER` | 同文件同 dict，新增 key |
| 新 lexicon 条目 | 追加到现有 JSON 数组 |
| 新 graph 边 | 追加到现有 edges 数组 |

**不碰的文件**：`domain/models.py`, `domain/ports.py`, `ranking.py`, `clustering.py`, `normalization.py`, `pipeline.py`, `notify.py`, `monitoring.py`, `sqlite_store.py`。
