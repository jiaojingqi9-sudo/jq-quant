    const report = JSON.parse(document.getElementById("report-data").textContent);
    const unionEvents = [];
    const seenEventIds = new Set();
    ["top_events", "negative_risks", "positive_catalysts", "watchlist"].forEach(function (key) {
      (report[key] || []).forEach(function (event) {
        if (!seenEventIds.has(event.cluster_id)) {
          seenEventIds.add(event.cluster_id);
          unionEvents.push(event);
        }
      });
    });

    const eventByCluster = new Map(unionEvents.map(function (event) {
      return [event.cluster_id, event];
    }));

    const techBlock = report.tech_block || { summary: {}, signals: [], themes: [], asset_ladder: [] };
    const techSignals = Array.isArray(techBlock.signals) ? techBlock.signals : [];

    const state = {
      view: "core",
      direction: "all",
      query: "",
      selectedClusterId: (report.alerts && report.alerts[0] && report.alerts[0].cluster_id)
        || (unionEvents[0] && unionEvents[0].cluster_id)
        || null,
      selectedTechClusterId: (techSignals[0] && techSignals[0].cluster_id) || null
    };

    const directionLabels = {
      all: "全部",
      negative: "利空",
      positive: "利好",
      neutral: "中性"
    };

    const runtimeStatusLabels = {
      ok: "正常",
      idle: "空闲",
      degraded: "降级",
      stale: "过期",
      error: "错误",
      missing: "未启动",
      unknown: "未知"
    };

    const viewLabels = {
      core: "我们最开始之前的那套",
      tech: "港A股消息"
    };

    function escapeHtml(value) {
      return String(value || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }

    function score(value) {
      const number = Number(value || 0);
      return String(Math.round(number * 100) / 100);
    }

    function tokensForEvent(event) {
      return [
        event.headline,
        event.summary,
        (event.themes || []).join(" "),
        (event.entities || []).join(" "),
        (event.sectors || []).join(" "),
        (event.regions || []).join(" "),
        (event.top_instruments || []).map(function (item) { return item.symbol + " " + item.name; }).join(" ")
      ].join(" ").toLowerCase();
    }

    function matchesDirection(event) {
      return state.direction === "all" || event.direction === state.direction;
    }

    function matchesQuery(text) {
      if (!state.query) {
        return true;
      }
      return String(text || "").toLowerCase().indexOf(state.query) !== -1;
    }

    function renderViewSwitch() {
      const host = document.getElementById("viewSwitch");
      const views = ["core", "tech"];
      host.innerHTML = views.map(function (view) {
        const active = state.view === view ? " active" : "";
        return '<button class="view-button' + active + '" type="button" data-view="' + view + '">'
          + escapeHtml(viewLabels[view] || view)
          + "</button>";
      }).join("");
      host.querySelectorAll("[data-view]").forEach(function (button) {
        button.addEventListener("click", function () {
          state.view = button.getAttribute("data-view") || "core";
          render();
        });
      });
    }

    function renderWorkspaces() {
      document.querySelectorAll(".workspace").forEach(function (node) {
        node.classList.toggle("active", node.getAttribute("data-view") === state.view);
      });
    }

    function filteredEvents() {
      return unionEvents.filter(function (event) {
        return matchesDirection(event) && matchesQuery(tokensForEvent(event));
      });
    }

    function filteredInstruments() {
      return (report.top_instruments || []).filter(function (instrument) {
        const text = [
          instrument.symbol,
          instrument.name,
          instrument.headline,
          (instrument.reasons || []).join(" ")
        ].join(" ").toLowerCase();
        const event = eventByCluster.get(instrument.cluster_id);
        return matchesQuery(text) && (!event || matchesDirection(event));
      });
    }

    function filteredFeed() {
      return (report.latest_feed || []).filter(function (item) {
        const text = [
          item.title,
          item.summary,
          item.source_id,
          (item.themes || []).join(" "),
          (item.entities || []).join(" ")
        ].join(" ").toLowerCase();
        return matchesQuery(text);
      });
    }

    function tokensForTechSignal(signal) {
      return [
        signal.headline,
        (signal.trigger_tags || []).join(" "),
        (signal.rationale || []).join(" "),
        (signal.matched_terms || []).map(function (item) {
          return item.term + " " + (item.matched_terms || []).join(" ");
        }).join(" "),
        (signal.candidate_assets || []).map(function (item) {
          return item.symbol + " " + item.name;
        }).join(" "),
        (signal.activated_themes || []).map(function (item) {
          return item.label + " " + (item.drivers || []).join(" ");
        }).join(" ")
      ].join(" ").toLowerCase();
    }

    function filteredTechSignals() {
      return techSignals.filter(function (signal) {
        const directionOk = state.direction === "all" || signal.direction === state.direction;
        return directionOk && matchesQuery(tokensForTechSignal(signal));
      });
    }

    function filteredTechAssets() {
      return (techBlock.asset_ladder || []).filter(function (asset) {
        const text = [
          asset.symbol,
          asset.name,
          (asset.drivers || []).join(" ")
        ].join(" ").toLowerCase();
        const directionOk = state.direction === "all" || asset.direction === state.direction;
        return directionOk && matchesQuery(text);
      });
    }

    function selectedTechSignal() {
      const current = techSignals.find(function (signal) {
        return signal.cluster_id === state.selectedTechClusterId;
      });
      if (current) {
        return current;
      }
      const first = filteredTechSignals()[0] || techSignals[0] || null;
      if (first) {
        state.selectedTechClusterId = first.cluster_id;
      }
      return first;
    }

    function selectedEvent() {
      const current = eventByCluster.get(state.selectedClusterId);
      if (current) {
        return current;
      }
      const first = filteredEvents()[0] || unionEvents[0] || null;
      if (first) {
        state.selectedClusterId = first.cluster_id;
      }
      return first;
    }

    function rightColumn() {
      return document.querySelector(".right-column");
    }

    function detailView() {
      return document.getElementById("detailView");
    }

    function renderHero() {
      const counts = report.counts || {};
      const runtime = report.runtime_status || {};
      const runtimeLabel = runtimeStatusLabels[runtime.overall_status] || runtime.overall_status || "未知";
      document.getElementById("heroMeta").textContent =
        "更新于 " + report.created_at + "，来源 " + report.source + "。全局新闻映射到 A 股、港股、美股候选标的，当前运行状态 " + runtimeLabel + "。";

      const metrics = [
        { label: "事件", value: counts.ranked_events || 0, meta: "当前排序后的事件数" },
        { label: "候选标的", value: counts.ranked_instruments || 0, meta: "港美 A 股相关标的" },
        { label: "高优先级提醒", value: (report.alert_counts || {}).high || 0, meta: "当前 high 级提醒" },
        { label: "紧急提醒", value: (report.alert_counts || {}).critical || 0, meta: "当前 critical 级提醒" }
      ];
      document.getElementById("metricGrid").innerHTML = metrics.map(function (metric) {
        return '<div class="metric-card">'
          + '<div class="label">' + escapeHtml(metric.label) + '</div>'
          + '<div class="value">' + escapeHtml(metric.value) + '</div>'
          + '<div class="meta">' + escapeHtml(metric.meta) + '</div>'
          + '</div>';
      }).join("");
    }

    function renderRuntimeStatus() {
      const runtime = report.runtime_status || { overall_status: "unknown", lines: [] };
      const lines = runtime.lines || [];
      document.getElementById("runtimeOverallLabel").textContent =
        "总览 " + (runtimeStatusLabels[runtime.overall_status] || runtime.overall_status || "未知");

      const host = document.getElementById("runtimeStatusGrid");
      host.innerHTML = lines.map(function (line) {
        const status = String(line.status || "unknown");
        const ageText = line.age_seconds === null || line.age_seconds === undefined
          ? "age n/a"
          : "age " + line.age_seconds + "s";
        const updateText = line.last_update ? String(line.last_update) : "n/a";
        const modules = Array.isArray(line.modules) ? line.modules : [];
        return '<div class="status-card ' + escapeHtml(status) + '">'
          + '<div class="status-topline">'
          + '<div class="status-name">' + escapeHtml(line.name || "line") + "</div>"
          + '<div class="status-state">' + escapeHtml(runtimeStatusLabels[status] || status) + "</div>"
          + "</div>"
          + '<p class="status-note">' + escapeHtml(line.detail || "暂无说明") + "</p>"
          + '<div class="status-meta">'
          + '<span>last update: ' + escapeHtml(updateText) + "</span>"
          + '<span>' + escapeHtml(ageText) + "</span>"
          + '<span>source: ' + escapeHtml(line.source_status || "n/a") + "</span>"
          + "</div>"
          + (modules.length
              ? '<div class="status-module-row">'
                + modules.map(function (module) {
                    const moduleStatus = String(module.status || "unknown");
                    const counter = module.count || module.signal_count || module.event_count || module.alert_count || "";
                    return '<span class="status-module ' + escapeHtml(moduleStatus) + '">'
                      + escapeHtml(module.name || "module")
                      + (counter !== "" ? ' · ' + escapeHtml(counter) : "")
                      + "</span>";
                  }).join("")
                + "</div>"
              : "")
          + "</div>";
      }).join("");

      if (!lines.length) {
        host.innerHTML = '<div class="empty">还没有可展示的运行状态。</div>';
      }
    }

    function renderTechBlock() {
      const tech = techBlock;
      const summary = tech.summary || {};
      const signals = filteredTechSignals();
      const themes = Array.isArray(tech.themes) ? tech.themes : [];
      const assets = filteredTechAssets();
      const currentSignal = selectedTechSignal();

      document.getElementById("techSummaryLabel").textContent =
        "信号 " + String(summary.signal_count || 0) + " · 主题 " + String(summary.hot_theme_count || 0)
        + " · 词库 " + String(summary.lexicon_version || "unversioned");
      document.getElementById("techSignalCountLabel").textContent =
        currentSignal ? "关注分 " + score(currentSignal.trading_attention_score) : "暂无信号";
      document.getElementById("techThemeCountLabel").textContent = themes.length + " 个";
      document.getElementById("techAssetCountLabel").textContent = assets.length + " 个";

      const signalHost = document.getElementById("techSignalList");
      const themeHost = document.getElementById("techThemeList");
      const assetHost = document.getElementById("techAssetList");
      const detailHost = document.getElementById("techDetailView");

      if (!signals.length) {
        signalHost.innerHTML = '<div class="empty">当前没有科技专题信号。</div>';
      } else {
        signalHost.innerHTML = signals.map(function (signal) {
          const active = signal.cluster_id === state.selectedTechClusterId ? " active" : "";
          const assetsText = (signal.candidate_assets || []).slice(0, 3).map(function (item) {
            return item.symbol;
          }).join(", ") || "n/a";
          return '<div class="tech-card' + active + '">'
            + '<button type="button" data-tech-cluster="' + escapeHtml(signal.cluster_id) + '">'
            + '<div class="card-topline">'
            + '<span class="badge dir-' + escapeHtml(signal.direction) + '">' + escapeHtml(signal.direction) + '</span>'
            + '<span class="badge level-medium">' + escapeHtml(String(signal.attention_tier || "watch").toUpperCase()) + '</span>'
            + '<span>attention ' + escapeHtml(score(signal.trading_attention_score)) + "</span>"
            + "</div>"
            + '<div class="headline">' + escapeHtml(signal.headline) + "</div>"
            + '<p class="summary">' + escapeHtml((signal.rationale || []).slice(0, 2).join("；") || "暂无专题解释") + "</p>"
            + '<div class="chip-row">'
            + (signal.trigger_tags || []).slice(0, 4).map(function (tag) {
                return '<span class="mini-tag">' + escapeHtml(tag) + "</span>";
              }).join("")
            + "</div>"
            + '<div class="tech-score-row">'
            + '<span class="mini-score">炒作度 ' + escapeHtml(score(signal.spec_score)) + "</span>"
            + '<span class="mini-score">热度 ' + escapeHtml(score(signal.heat_score)) + "</span>"
            + '<span class="mini-score">标的 ' + escapeHtml(assetsText) + "</span>"
            + "</div>"
            + "</button></div>";
        }).join("");
        signalHost.querySelectorAll("[data-tech-cluster]").forEach(function (button) {
          button.addEventListener("click", function () {
            state.selectedClusterId = button.getAttribute("data-tech-cluster");
            state.selectedTechClusterId = state.selectedClusterId;
            renderTechBlock();
            renderDetail();
          });
        });
      }

      if (!themes.length) {
        themeHost.innerHTML = '<div class="empty">当前没有科技热主题。</div>';
      } else {
        themeHost.innerHTML = themes.map(function (theme) {
          return '<div class="theme-card">'
            + '<div class="card-topline"><span class="badge type-company">theme</span><span>score ' + escapeHtml(score(theme.score)) + "</span></div>"
            + '<div class="headline">' + escapeHtml(theme.label) + "</div>"
            + '<p class="summary">' + escapeHtml((theme.drivers || []).join("，") || "暂无驱动词") + "</p>"
            + "</div>";
        }).join("");
      }

      if (!assets.length) {
        assetHost.innerHTML = '<div class="empty">当前没有专题候选标的。</div>';
      } else {
        assetHost.innerHTML = assets.map(function (asset) {
          return '<div class="asset-card">'
            + '<button type="button" data-tech-symbol="' + escapeHtml(asset.symbol) + '">'
            + '<div class="card-topline">'
            + '<span class="badge dir-' + escapeHtml(asset.direction) + '">' + escapeHtml(asset.direction) + '</span>'
            + '<span>' + escapeHtml(asset.market) + "</span>"
            + '<span>score ' + escapeHtml(score(asset.score)) + "</span>"
            + "</div>"
            + '<div class="headline">' + escapeHtml(asset.symbol + " · " + asset.name) + "</div>"
            + '<p class="summary">' + escapeHtml((asset.drivers || []).slice(0, 2).join("；") || "暂无专题解释") + "</p>"
            + "</button></div>";
        }).join("");
        assetHost.querySelectorAll("[data-tech-symbol]").forEach(function (button) {
          button.addEventListener("click", function () {
            const symbol = button.getAttribute("data-tech-symbol");
            const candidate = techSignals.find(function (signal) {
              return (signal.candidate_assets || []).some(function (item) {
                return item.symbol === symbol;
              });
            });
            if (candidate) {
              state.selectedClusterId = candidate.cluster_id;
              state.selectedTechClusterId = candidate.cluster_id;
              renderTechBlock();
              renderDetail();
            }
          });
        });
      }

      if (!currentSignal) {
        detailHost.innerHTML = '<div class="empty">当前没有科技专题详情可看。</div>';
        return;
      }

      const linkedEvent = eventByCluster.get(currentSignal.cluster_id);
      const matchedTerms = (currentSignal.matched_terms || []).map(function (item) {
        const matched = Array.isArray(item.matched_terms) ? item.matched_terms.slice(0, 4).join(", ") : "n/a";
        return '<li>' + escapeHtml(item.term || "term")
          + ' · ' + escapeHtml(item.term_type || "unknown")
          + ' · ' + escapeHtml(matched)
          + "</li>";
      }).join("") || "<li>暂无触发词。</li>";
      const themeCards = (currentSignal.activated_themes || []).map(function (theme) {
        return '<div class="instrument-card">'
          + '<div class="card-topline">'
          + '<span class="badge type-company">theme</span>'
          + '<span>score ' + escapeHtml(score(theme.score)) + "</span>"
          + "</div>"
          + '<div class="headline">' + escapeHtml(theme.label || theme.theme || "theme") + "</div>"
          + '<p class="summary">' + escapeHtml(theme.path || (theme.drivers || []).join("，") || "暂无链路说明") + "</p>"
          + "</div>";
      }).join("") || '<div class="empty">当前没有主题扩散链。</div>';
      const candidateCards = (currentSignal.candidate_assets || []).map(function (asset) {
        return '<div class="instrument-card">'
          + '<div class="card-topline">'
          + '<span class="badge dir-' + escapeHtml(asset.direction) + '">' + escapeHtml(asset.direction) + '</span>'
          + '<span>' + escapeHtml(asset.market) + "</span>"
          + '<span>score ' + escapeHtml(score(asset.score)) + "</span>"
          + "</div>"
          + '<div class="headline">' + escapeHtml(asset.symbol + " · " + asset.name) + "</div>"
          + '<p class="instrument-note">' + escapeHtml((asset.reasons || []).join("；") || "暂无候选说明") + "</p>"
          + "</div>";
      }).join("") || '<div class="empty">当前没有候选标的。</div>';
      const linkedDocs = linkedEvent && linkedEvent.related_documents
        ? linkedEvent.related_documents.map(function (doc) {
            const link = doc.url
              ? '<a href="' + escapeHtml(doc.url) + '" target="_blank" rel="noreferrer">打开原文</a>'
              : '<span class="tiny">暂无原文链接</span>';
            return '<li class="doc-card">'
              + '<div class="card-meta">' + escapeHtml(doc.published_at) + " · " + escapeHtml(doc.source_id) + "</div>"
              + '<div class="headline">' + escapeHtml(doc.title) + "</div>"
              + '<p>' + escapeHtml(doc.summary || "无摘要") + "</p>"
              + '<div class="tag-row">' + link + "</div>"
              + "</li>";
          }).join("")
        : "";
      const rationale = (currentSignal.rationale || []).map(function (item) {
        return "<li>" + escapeHtml(item) + "</li>";
      }).join("") || "<li>暂无科技专题逻辑。</li>";

      detailHost.innerHTML = '<div class="tech-detail-grid">'
        + '<div class="detail-block">'
        + '<div class="detail-section-title"><strong>专题摘要</strong><span>港A科技催化</span></div>'
        + '<div class="card-topline">'
        + '<span class="badge dir-' + escapeHtml(currentSignal.direction) + '">' + escapeHtml(currentSignal.direction) + '</span>'
        + '<span class="badge level-medium">' + escapeHtml(String(currentSignal.attention_tier || "watch").toUpperCase()) + '</span>'
        + '<span>docs ' + escapeHtml(currentSignal.doc_count) + "</span>"
        + "</div>"
        + '<h3 class="headline-serif">' + escapeHtml(currentSignal.headline) + "</h3>"
        + '<p class="summary">' + escapeHtml((currentSignal.trigger_tags || []).join("，") || "暂无触发词标签") + "</p>"
        + '<div class="score-strip">'
        + '<div class="score-box"><div class="label">Attention</div><div class="value">' + escapeHtml(score(currentSignal.trading_attention_score)) + "</div></div>"
        + '<div class="score-box"><div class="label">Spec</div><div class="value">' + escapeHtml(score(currentSignal.spec_score)) + "</div></div>"
        + '<div class="score-box"><div class="label">Heat</div><div class="value">' + escapeHtml(score(currentSignal.heat_score)) + "</div></div>"
        + "</div>"
        + "</div>"
        + '<div class="detail-block"><div class="detail-section-title"><strong>触发词</strong><span>命中的炒作因子</span></div><ul class="rationale-list">' + matchedTerms + "</ul></div>"
        + '<div class="detail-block"><div class="detail-section-title"><strong>影响链</strong><span>主题扩散路径</span></div><div class="stack">' + themeCards + "</div></div>"
        + '<div class="detail-block"><div class="detail-section-title"><strong>候选标的</strong><span>港A科技映射</span></div><div class="stack">' + candidateCards + "</div></div>"
        + '<div class="detail-block"><div class="detail-section-title"><strong>专题逻辑</strong><span>为什么值得看</span></div><ul class="rationale-list">' + rationale + "</ul></div>"
        + '<div class="detail-block"><div class="detail-section-title"><strong>相关新闻</strong><span>回到原文</span></div><ul class="doc-list">' + (linkedDocs || '<div class="empty">当前没有联动原文。</div>') + "</ul></div>"
        + "</div>";
    }

    function renderFilters() {
      const filterHost = document.getElementById("filterChips");
      const directions = ["all", "negative", "positive", "neutral"];
      filterHost.innerHTML = directions.map(function (key) {
        const active = state.direction === key ? " active" : "";
        return '<button class="chip' + active + '" type="button" data-direction="' + key + '">'
          + escapeHtml(directionLabels[key])
          + "</button>";
      }).join("");
      filterHost.querySelectorAll("[data-direction]").forEach(function (button) {
        button.addEventListener("click", function () {
          state.direction = button.getAttribute("data-direction") || "all";
          render();
        });
      });
    }

    function renderAlerts() {
      const alerts = (report.alerts || []).filter(function (alert) {
        const text = [alert.headline, alert.reason, (alert.symbols || []).join(" ")].join(" ").toLowerCase();
        return (state.direction === "all" || alert.direction === state.direction) && matchesQuery(text);
      });
      document.getElementById("alertCountLabel").textContent = alerts.length + " 条";

      const host = document.getElementById("alertsList");
      if (!alerts.length) {
        host.innerHTML = '<div class="empty">当前筛选条件下没有提醒。</div>';
        return;
      }

      host.innerHTML = alerts.map(function (alert) {
        const active = alert.cluster_id === state.selectedClusterId ? " active" : "";
        const prefix = alert.is_new ? '<span class="badge level-medium">NEW</span>' : "";
        return '<div class="alert-card' + active + '">'
          + '<button class="card-button" type="button" data-cluster="' + escapeHtml(alert.cluster_id) + '">'
          + '<div class="card-topline">'
          + '<span class="badge level-' + escapeHtml(alert.level) + '">' + escapeHtml(alert.level.toUpperCase()) + '</span>'
          + '<span class="badge dir-' + escapeHtml(alert.direction) + '">' + escapeHtml(alert.direction) + '</span>'
          + prefix
          + "</div>"
          + '<div class="headline">' + escapeHtml(alert.headline) + "</div>"
          + '<div class="card-meta">分数 ' + escapeHtml(score(alert.final_score)) + " · "
          + escapeHtml((alert.symbols || []).join(", ") || "n/a") + "</div>"
          + '<p class="summary">' + escapeHtml(alert.reason) + "</p>"
          + "</button></div>";
      }).join("");

      host.querySelectorAll("[data-cluster]").forEach(function (button) {
        button.addEventListener("click", function () {
          state.selectedClusterId = button.getAttribute("data-cluster");
          render();
        });
      });
    }

    function renderEvents() {
      const events = filteredEvents();
      document.getElementById("eventCountLabel").textContent = events.length + " 个";

      const host = document.getElementById("eventsGrid");
      if (!events.length) {
        host.innerHTML = '<div class="empty">没有匹配的事件，试试更宽松的搜索词。</div>';
        return;
      }

      host.innerHTML = events.map(function (event) {
        const active = event.cluster_id === state.selectedClusterId ? " active" : "";
        const topSymbols = (event.top_instruments || []).map(function (item) { return item.symbol; }).slice(0, 4).join(", ") || "n/a";
        return '<div class="event-card' + active + '">'
          + '<button class="card-button" type="button" data-cluster="' + escapeHtml(event.cluster_id) + '">'
          + '<div class="card-topline">'
          + '<span class="badge dir-' + escapeHtml(event.direction) + '">' + escapeHtml(event.direction) + '</span>'
          + '<span class="badge type-' + escapeHtml(event.event_type) + '">' + escapeHtml(event.event_type) + '</span>'
          + '<span>score ' + escapeHtml(score(event.final_score)) + "</span>"
          + '<span>docs ' + escapeHtml(event.doc_count) + "</span>"
          + "</div>"
          + '<div class="headline">' + escapeHtml(event.headline) + "</div>"
          + '<p class="summary">' + escapeHtml(event.summary || "暂无事件摘要，先看右侧原文列表。") + "</p>"
          + '<div class="tag-row">'
          + (event.themes || []).slice(0, 4).map(function (theme) {
              return '<span class="mini-tag">' + escapeHtml(theme) + "</span>";
            }).join("")
          + "</div>"
          + '<div class="card-meta">候选标的: ' + escapeHtml(topSymbols) + "</div>"
          + "</button></div>";
      }).join("");

      host.querySelectorAll("[data-cluster]").forEach(function (button) {
        button.addEventListener("click", function () {
          state.selectedClusterId = button.getAttribute("data-cluster");
          render();
        });
      });
    }

    function renderDetail() {
      const event = selectedEvent();
      const host = detailView();
      if (!event) {
        host.innerHTML = '<div class="empty">暂无事件可展示。</div>';
        host.scrollTop = 0;
        const column = rightColumn();
        if (column) {
          column.scrollTop = 0;
        }
        return;
      }

      const instruments = (event.top_instruments || []).map(function (item) {
        return '<div class="instrument-card">'
          + '<div class="card-topline">'
          + '<span class="badge dir-' + escapeHtml(item.direction) + '">' + escapeHtml(item.direction) + '</span>'
          + '<span>' + escapeHtml(item.market) + "</span>"
          + '<span>score ' + escapeHtml(score(item.final_score)) + "</span>"
          + "</div>"
          + '<div class="headline">' + escapeHtml(item.symbol + " · " + item.name) + "</div>"
          + '<p class="instrument-note">' + escapeHtml((item.reasons || []).join("；") || "暂无说明") + "</p>"
          + "</div>";
      }).join("") || '<div class="empty">当前事件还没有映射到候选标的。</div>';

      const docs = (event.related_documents || []).map(function (doc) {
        const summary = doc.summary || "无摘要";
        const link = doc.url
          ? '<a href="' + escapeHtml(doc.url) + '" target="_blank" rel="noreferrer">打开原文</a>'
          : '<span class="tiny">暂无原文链接</span>';
        return '<li class="doc-card">'
          + '<div class="card-meta">' + escapeHtml(doc.published_at) + " · " + escapeHtml(doc.source_id) + "</div>"
          + '<div class="headline">' + escapeHtml(doc.title) + "</div>"
          + '<p>' + escapeHtml(summary) + "</p>"
          + '<div class="tag-row">'
          + (doc.themes || []).slice(0, 4).map(function (theme) {
              return '<span class="mini-tag">' + escapeHtml(theme) + "</span>";
            }).join("")
          + "</div>"
          + '<div class="tag-row">' + link + "</div>"
          + "</li>";
      }).join("") || '<div class="empty">当前事件没有展开到原文。</div>';

      const rationale = (event.rationale || []).map(function (item) {
        return "<li>" + escapeHtml(item) + "</li>";
      }).join("") || "<li>暂无解释。</li>";

      host.innerHTML = '<div class="detail-grid">'
        + '<div class="detail-block">'
        + '<div class="detail-section-title"><strong>事件摘要</strong><span>核心信息</span></div>'
        + '<div class="card-topline">'
        + '<span class="badge dir-' + escapeHtml(event.direction) + '">' + escapeHtml(event.direction) + '</span>'
        + '<span class="badge type-' + escapeHtml(event.event_type) + '">' + escapeHtml(event.event_type) + '</span>'
        + '<span>docs ' + escapeHtml(event.doc_count) + "</span>"
        + '<span>' + escapeHtml((event.source_ids || []).join(", ") || "source n/a") + "</span>"
        + "</div>"
        + '<h3 class="headline-serif">' + escapeHtml(event.headline) + "</h3>"
        + '<p class="summary">' + escapeHtml(event.summary || "暂无摘要。") + "</p>"
        + '<div class="chip-row">'
        + (event.entities || []).slice(0, 6).map(function (entity) {
            return '<span class="mini-tag">' + escapeHtml(entity) + "</span>";
          }).join("")
        + (event.markets || []).map(function (market) {
            return '<span class="mini-tag">' + escapeHtml(market) + "</span>";
          }).join("")
        + "</div>"
        + '<div class="detail-times">'
        + '<span>首次出现: ' + escapeHtml(event.first_seen_at || "n/a") + "</span>"
        + '<span>最近更新: ' + escapeHtml(event.last_seen_at || "n/a") + "</span>"
        + "</div>"
        + "</div>"
        + '<div class="detail-block">'
        + '<div class="detail-section-title"><strong>评分拆解</strong><span>为什么它排在这里</span></div>'
        + '<div class="score-strip">'
        + '<div class="score-box"><div class="label">Final</div><div class="value">' + escapeHtml(score(event.final_score)) + "</div></div>"
        + '<div class="score-box"><div class="label">Heat</div><div class="value">' + escapeHtml(score(event.heat_score)) + "</div></div>"
        + '<div class="score-box"><div class="label">Importance</div><div class="value">' + escapeHtml(score(event.importance_score)) + "</div></div>"
        + "</div>"
        + "</div>"
        + '<div class="detail-block"><div class="detail-section-title"><strong>影响逻辑</strong><span>打分依据</span></div><ul class="rationale-list">' + rationale + "</ul></div>"
        + '<div class="detail-block"><div class="detail-section-title"><strong>候选标的</strong><span>可能受影响的交易对象</span></div><div class="stack">' + instruments + "</div></div>"
        + '<div class="detail-block"><div class="detail-section-title"><strong>相关新闻原文</strong><span>点击可跳转</span></div><ul class="doc-list">' + docs + "</ul></div>"
        + "</div>";
      host.scrollTop = 0;
      const column = rightColumn();
      if (column) {
        column.scrollTop = 0;
      }
    }

    function renderInstruments() {
      const instruments = filteredInstruments();
      document.getElementById("instrumentCountLabel").textContent = instruments.length + " 个";
      const host = document.getElementById("instrumentList");
      if (!instruments.length) {
        host.innerHTML = '<div class="empty">当前筛选下没有候选标的。</div>';
        return;
      }

      host.innerHTML = instruments.map(function (item) {
        const active = item.cluster_id === state.selectedClusterId ? " active" : "";
        return '<div class="instrument-card' + active + '">'
          + '<button class="card-button" type="button" data-cluster="' + escapeHtml(item.cluster_id) + '">'
          + '<div class="card-topline">'
          + '<span class="badge dir-' + escapeHtml(item.direction) + '">' + escapeHtml(item.direction) + '</span>'
          + '<span>' + escapeHtml(item.market) + "</span>"
          + '<span>score ' + escapeHtml(score(item.final_score)) + "</span>"
          + "</div>"
          + '<div class="headline">' + escapeHtml(item.symbol + " · " + item.name) + "</div>"
          + '<p class="summary">' + escapeHtml(item.headline) + "</p>"
          + "</button></div>";
      }).join("");

      host.querySelectorAll("[data-cluster]").forEach(function (button) {
        button.addEventListener("click", function () {
          state.selectedClusterId = button.getAttribute("data-cluster");
          render();
        });
      });
    }

    function renderFeed() {
      const items = filteredFeed();
      document.getElementById("feedCountLabel").textContent = items.length + " 条";
      const host = document.getElementById("feedList");
      if (!items.length) {
        host.innerHTML = '<div class="empty">当前筛选下没有消息流内容。</div>';
        return;
      }

      host.innerHTML = items.map(function (item) {
        const link = item.url
          ? '<a href="' + escapeHtml(item.url) + '" target="_blank" rel="noreferrer">打开原文</a>'
          : '<span class="tiny">暂无原文链接</span>';
        return '<div class="feed-card">'
          + '<div class="card-meta">' + escapeHtml(item.published_at) + " · " + escapeHtml(item.source_id) + "</div>"
          + '<div class="headline">' + escapeHtml(item.title) + "</div>"
          + '<p>' + escapeHtml(item.summary || "无摘要") + "</p>"
          + '<div class="tag-row">'
          + (item.themes || []).slice(0, 4).map(function (theme) {
              return '<span class="mini-tag">' + escapeHtml(theme) + "</span>";
            }).join("")
          + (item.entities || []).slice(0, 4).map(function (entity) {
              return '<span class="mini-tag">' + escapeHtml(entity) + "</span>";
            }).join("")
          + '</div><div class="tag-row">' + link + "</div></div>";
      }).join("");
    }

    function render() {
      renderViewSwitch();
      renderWorkspaces();
      renderHero();
      renderRuntimeStatus();
      renderTechBlock();
      renderFilters();
      renderAlerts();
      renderEvents();
      renderDetail();
      renderInstruments();
      renderFeed();
    }

    document.getElementById("searchInput").addEventListener("input", function (event) {
      state.query = String(event.target.value || "").trim().toLowerCase();
      render();
    });

    document.getElementById("resetButton").addEventListener("click", function () {
      state.direction = "all";
      state.query = "";
      document.getElementById("searchInput").value = "";
      render();
    });

    render();

    let countdown = 60;
    function refreshPage() {
      const url = new URL(window.location.href);
      url.searchParams.set("ts", String(Date.now()));
      window.location.replace(url.toString());
    }
    function renderRefreshText() {
      document.getElementById("refreshText").textContent =
        "页面每 60 秒自动刷新一次，距下次刷新 " + countdown + " 秒";
    }
    renderRefreshText();
    setInterval(function () {
      countdown -= 1;
      if (countdown <= 0) {
        refreshPage();
        return;
      }
      renderRefreshText();
    }, 1000);