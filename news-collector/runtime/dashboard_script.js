    (function ensureFreshUrl() {
      const url = new URL(window.location.href);
      if (!url.searchParams.get("ts")) {
        url.searchParams.set("ts", String(Date.now()));
        window.location.replace(url.toString());
      }
    })();

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
    const lexiconDiscovery = report.lexicon_discovery || { summary: {}, candidates: [], accepted_terms: [] };
    const techSignals = Array.isArray(techBlock.signals) ? techBlock.signals : [];
    const reviewApiBase = "http://127.0.0.1:8765";
    const dashboardStateKey = "marketNewsDashboardState.v4";
    const interactionGraceMs = 3 * 60 * 1000;
    const lexiconTypeOptions = [
      { value: "theme", label: "主题" },
      { value: "tech", label: "技术词" },
      { value: "catalyst", label: "催化词" },
      { value: "policy", label: "政策词" },
      { value: "risk", label: "风险词" },
      { value: "company", label: "公司词" }
    ];
    let reviewApiOnline = false;
    let lexiconReviewMessage = "";
    let pendingReviewRequest = false;
    let lastInteractionAt = Date.now();
    let restoreScrollPending = true;
    let persistStateTimer = null;

    const state = {
      view: "core",
      direction: "all",
      query: "",
      lexiconCatalogQuery: "",
      selectedFrontierId: null,
      selectedFrontierClusterId: null,
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

    const runtimeLineLabels = {
      collect: "COLLECT",
      delivery: "DELIVERY",
      review_api: "REVIEW API",
      health: "HEALTH",
      cookies: "COOKIES"
    };

    const viewLabels = {
      core: "全市场消息",
      tech: "港A股消息",
      frontier: "科技前沿"
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

    function schedulePersistState() {
      if (persistStateTimer) {
        clearTimeout(persistStateTimer);
      }
      persistStateTimer = setTimeout(persistDashboardState, 120);
    }

    function collectScrollPositions() {
      const positions = {};
      document.querySelectorAll("[data-scroll-key]").forEach(function (node) {
        const key = node.getAttribute("data-scroll-key");
        if (!key) {
          return;
        }
        positions[key] = node.scrollTop || 0;
      });
      return positions;
    }

    function persistDashboardState() {
      try {
        const payload = {
          view: state.view,
          direction: state.direction,
          query: state.query,
          lexiconCatalogQuery: state.lexiconCatalogQuery,
          selectedFrontierId: state.selectedFrontierId,
          selectedFrontierClusterId: state.selectedFrontierClusterId,
          selectedClusterId: state.selectedClusterId,
          selectedTechClusterId: state.selectedTechClusterId,
          lastInteractionAt: lastInteractionAt,
          scrollPositions: collectScrollPositions()
        };
        window.localStorage.setItem(dashboardStateKey, JSON.stringify(payload));
      } catch (error) {
        return;
      }
    }

    function restoreDashboardState() {
      try {
        const raw = window.localStorage.getItem(dashboardStateKey);
        if (!raw) {
          return;
        }
        const payload = JSON.parse(raw);
        if (payload && typeof payload === "object") {
          if (payload.view === "core" || payload.view === "tech" || payload.view === "frontier") {
            state.view = payload.view;
          }
          if (payload.direction === "all" || payload.direction === "negative" || payload.direction === "positive" || payload.direction === "neutral") {
            state.direction = payload.direction;
          }
          if (typeof payload.query === "string") {
            state.query = payload.query;
          }
          if (typeof payload.lexiconCatalogQuery === "string") {
            state.lexiconCatalogQuery = payload.lexiconCatalogQuery;
          }
          if (typeof payload.selectedFrontierId === "string") {
            state.selectedFrontierId = payload.selectedFrontierId;
          }
          if (typeof payload.selectedFrontierClusterId === "string") {
            state.selectedFrontierClusterId = payload.selectedFrontierClusterId;
          }
          if (typeof payload.selectedClusterId === "string") {
            state.selectedClusterId = payload.selectedClusterId;
          }
          if (typeof payload.selectedTechClusterId === "string") {
            state.selectedTechClusterId = payload.selectedTechClusterId;
          }
          if (typeof payload.lastInteractionAt === "number") {
            lastInteractionAt = payload.lastInteractionAt;
          }
        }
      } catch (error) {
        return;
      }
    }

    function restoreScrollPositions() {
      try {
        const raw = window.localStorage.getItem(dashboardStateKey);
        if (!raw) {
          return;
        }
        const payload = JSON.parse(raw);
        const positions = payload && typeof payload === "object" ? payload.scrollPositions : null;
        if (!positions || typeof positions !== "object") {
          return;
        }
        document.querySelectorAll("[data-scroll-key]").forEach(function (node) {
          const key = node.getAttribute("data-scroll-key");
          if (!key || typeof positions[key] !== "number") {
            return;
          }
          node.scrollTop = positions[key];
        });
      } catch (error) {
        return;
      }
    }

    function markInteraction() {
      lastInteractionAt = Date.now();
      schedulePersistState();
    }

    function hasRecentInteraction() {
      return pendingReviewRequest || (Date.now() - lastInteractionAt < interactionGraceMs);
    }

    function reviewStatusText() {
      if (reviewApiOnline) {
        return lexiconReviewMessage || "网页里可以直接收录、忽略待审核新词，也可以删除已收录词。";
      }
      return "审核服务还没连上，所以这里只显示队列。启动总控后刷新页面即可。";
    }

    async function fetchReviewApi(path, options) {
      const response = await fetch(reviewApiBase + path, Object.assign({
        headers: { "Content-Type": "application/json" }
      }, options || {}));
      let payload = {};
      try {
        payload = await response.json();
      } catch (error) {
        payload = {};
      }
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.error || ("request failed: " + response.status));
      }
      return payload;
    }

    function applyDiscoveryPayload(payload) {
      lexiconDiscovery.summary = payload.summary || {};
      lexiconDiscovery.candidates = payload.candidates || [];
      lexiconDiscovery.accepted_terms = payload.accepted_terms || [];
      if (payload.message) {
        lexiconReviewMessage = payload.message;
      }
    }

    async function refreshDiscoveryFromApi() {
      try {
        const payload = await fetchReviewApi("/api/lexicon/pending");
        reviewApiOnline = true;
        applyDiscoveryPayload(payload);
      } catch (error) {
        reviewApiOnline = false;
      }
      renderTechBlock();
    }

    async function submitDiscoveryAction(action, term, termType, triggerButton) {
      markInteraction();
      pendingReviewRequest = true;
      const payload = action === "add"
        ? { term: term, term_type: termType }
        : { term: term };
      const buttons = triggerButton && triggerButton.closest(".theme-card")
        ? Array.from(triggerButton.closest(".theme-card").querySelectorAll(".review-action"))
        : [];
      buttons.forEach(function (button) { button.disabled = true; });
      try {
        const result = await fetchReviewApi(
          action === "add" ? "/api/lexicon/add" : "/api/lexicon/reject",
          {
            method: "POST",
            body: JSON.stringify(payload)
          }
        );
        reviewApiOnline = true;
        applyDiscoveryPayload(result);
      } catch (error) {
        reviewApiOnline = false;
        lexiconReviewMessage = "审核动作失败：" + String(error.message || error);
      } finally {
        pendingReviewRequest = false;
        markInteraction();
      }
      renderTechBlock();
    }

    async function submitLexiconRemove(term, triggerButton) {
      markInteraction();
      pendingReviewRequest = true;
      const buttons = triggerButton && triggerButton.closest(".theme-card")
        ? Array.from(triggerButton.closest(".theme-card").querySelectorAll(".review-action"))
        : [];
      buttons.forEach(function (button) { button.disabled = true; });
      try {
        const result = await fetchReviewApi(
          "/api/lexicon/remove",
          {
            method: "POST",
            body: JSON.stringify({ term: term })
          }
        );
        reviewApiOnline = true;
        applyDiscoveryPayload(result);
      } catch (error) {
        reviewApiOnline = false;
        lexiconReviewMessage = "删除词条失败：" + String(error.message || error);
      } finally {
        pendingReviewRequest = false;
        markInteraction();
      }
      renderTechBlock();
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
      const views = ["core", "tech", "frontier"];
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

    function filteredAcceptedTerms() {
      const items = Array.isArray(lexiconDiscovery.accepted_terms) ? lexiconDiscovery.accepted_terms : [];
      const query = state.lexiconCatalogQuery.trim().toLowerCase();
      if (!query) {
        return items;
      }
      return items.filter(function (item) {
        const text = String(item.text || "").toLowerCase();
        const termType = String(item.term_type || "").toLowerCase();
        const synonyms = Array.isArray(item.synonyms) ? item.synonyms.join(" ").toLowerCase() : "";
        const tags = Array.isArray(item.trigger_tags) ? item.trigger_tags.join(" ").toLowerCase() : "";
        return text.indexOf(query) >= 0
          || termType.indexOf(query) >= 0
          || synonyms.indexOf(query) >= 0
          || tags.indexOf(query) >= 0;
      });
    }

    function frontierTrackerItems(baseSignals) {
      const tracker = new Map();
      (Array.isArray(baseSignals) ? baseSignals : techSignals).forEach(function (signal) {
        (signal.frontier_hits || []).forEach(function (hit) {
          const frontierId = String(hit.frontier_id || "").trim();
          if (!frontierId) {
            return;
          }
          const current = tracker.get(frontierId) || {
            frontier_id: frontierId,
            cn_label: hit.cn_label || frontierId,
            gap_level: hit.gap_level || "unknown",
            score: 0,
            hit_count: 0,
            bonuses: [],
            matched_keywords: [],
            cluster_ids: [],
            headlines: [],
          };
          current.score += Number(signal.trading_attention_score || 0) * Math.max(Number(hit.bonus || 0), 0.1);
          current.hit_count += 1;
          current.bonuses.push(Number(hit.bonus || 0));
          current.matched_keywords = Array.from(new Set(current.matched_keywords.concat(hit.matched_keywords || []))).slice(0, 6);
          current.cluster_ids = Array.from(new Set(current.cluster_ids.concat([signal.cluster_id]))).slice(0, 6);
          current.headlines = Array.from(new Set(current.headlines.concat([signal.headline]))).slice(0, 3);
          tracker.set(frontierId, current);
        });
      });
      return Array.from(tracker.values())
        .map(function (item) {
          item.score = Math.round(item.score * 100) / 100;
          item.top_bonus = Math.max.apply(null, item.bonuses.length ? item.bonuses : [0]);
          return item;
        })
        .sort(function (left, right) {
          return right.score - left.score;
        });
    }

    function frontierSignalsBase() {
      return techSignals.filter(function (signal) {
        if (!Array.isArray(signal.frontier_hits) || !signal.frontier_hits.length) {
          return false;
        }
        const directionOk = state.direction === "all" || signal.direction === state.direction;
        const frontierText = (signal.frontier_hits || []).map(function (item) {
          return [
            item.frontier_id,
            item.cn_label,
            item.gap_level,
            (item.matched_keywords || []).join(" ")
          ].join(" ");
        }).join(" ");
        return directionOk && matchesQuery(tokensForTechSignal(signal) + " " + frontierText);
      });
    }

    function filteredFrontierSignals() {
      const signals = frontierSignalsBase();
      if (!state.selectedFrontierId) {
        return signals;
      }
      const narrowed = signals.filter(function (signal) {
        return (signal.frontier_hits || []).some(function (hit) {
          return hit.frontier_id === state.selectedFrontierId;
        });
      });
      return narrowed.length ? narrowed : signals;
    }

    function selectedFrontierSignal() {
      const signals = filteredFrontierSignals();
      const current = signals.find(function (signal) {
        return signal.cluster_id === state.selectedFrontierClusterId;
      });
      if (current) {
        return current;
      }
      const first = signals[0] || frontierSignalsBase()[0] || null;
      if (first) {
        state.selectedFrontierClusterId = first.cluster_id;
      }
      return first;
    }

    function renderTechSignalDetail(detailHost, currentSignal, summaryLabel, countLabelId) {
      if (!currentSignal) {
        detailHost.innerHTML = '<div class="empty">当前没有科技专题详情可看。</div>';
        if (countLabelId) {
          document.getElementById(countLabelId).textContent = "暂无信号";
        }
        return;
      }

      if (countLabelId) {
        document.getElementById(countLabelId).textContent = "关注分 " + score(currentSignal.trading_attention_score);
      }

      const linkedEvent = eventByCluster.get(currentSignal.cluster_id);
      const matchedTerms = (currentSignal.matched_terms || []).map(function (item) {
        const matched = Array.isArray(item.matched_terms) ? item.matched_terms.slice(0, 4).join(", ") : "n/a";
        return '<li>' + escapeHtml(item.term || "term")
          + ' · ' + escapeHtml(item.term_type || "unknown")
          + ' · ' + escapeHtml(matched)
          + "</li>";
      }).join("") || "<li>暂无触发词。</li>";
      const frontierCards = (currentSignal.frontier_hits || []).map(function (item) {
        return '<div class="instrument-card">'
          + '<div class="card-topline">'
          + '<span class="badge type-company">frontier</span>'
          + '<span>' + escapeHtml(item.gap_level || "unknown") + "</span>"
          + '<span>bonus ' + escapeHtml(score(item.bonus)) + "</span>"
          + "</div>"
          + '<div class="headline">' + escapeHtml(item.cn_label || item.frontier_id || "frontier") + "</div>"
          + '<p class="summary">' + escapeHtml((item.matched_keywords || []).join("，") || "暂无命中词") + "</p>"
          + "</div>";
      }).join("") || '<div class="empty">当前没有命中的前沿突破。</div>';
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
        ? linkedEvent.related_documents.filter(function (doc) {
            const evidenceSources = Array.isArray(currentSignal.evidence_source_ids) ? currentSignal.evidence_source_ids : [];
            return !evidenceSources.length || evidenceSources.indexOf(doc.source_id) >= 0;
          }).map(function (doc) {
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
        + '<div class="detail-section-title"><strong>专题摘要</strong><span>' + escapeHtml(summaryLabel || "港A科技催化") + "</span></div>'
        + '<div class="card-topline">'
        + '<span class="badge dir-' + escapeHtml(currentSignal.direction) + '">' + escapeHtml(currentSignal.direction) + '</span>'
        + '<span class="badge level-medium">' + escapeHtml(String(currentSignal.attention_tier || "watch").toUpperCase()) + '</span>'
        + '<span>docs ' + escapeHtml(currentSignal.doc_count) + "</span>"
        + '<span>' + escapeHtml(currentSignal.source_quality || "n/a") + "</span>"
        + "</div>"
        + '<h3 class="headline-serif">' + escapeHtml(currentSignal.headline) + "</h3>"
        + '<p class="summary">' + escapeHtml((currentSignal.trigger_tags || []).join("，") || "暂无触发词标签") + "</p>"
        + '<div class="chip-row">'
        + '<span class="mini-tag">主证据：' + escapeHtml((currentSignal.evidence_source_ids || []).join(", ") || "n/a") + "</span>"
        + '<span class="mini-tag">热度：' + escapeHtml((currentSignal.social_source_ids || []).join(", ") || "无") + "</span>"
        + "</div>"
        + '<div class="score-strip">'
        + '<div class="score-box"><div class="label">Attention</div><div class="value">' + escapeHtml(score(currentSignal.trading_attention_score)) + "</div></div>"
        + '<div class="score-box"><div class="label">Spec</div><div class="value">' + escapeHtml(score(currentSignal.spec_score)) + "</div></div>"
        + '<div class="score-box"><div class="label">Heat</div><div class="value">' + escapeHtml(score(currentSignal.heat_score)) + "</div></div>"
        + "</div>"
        + "</div>"
        + '<div class="detail-block"><div class="detail-section-title"><strong>触发词</strong><span>命中的炒作因子</span></div><ul class="rationale-list">' + matchedTerms + "</ul></div>"
        + '<div class="detail-block"><div class="detail-section-title"><strong>前沿命中</strong><span>美强中追追踪</span></div><div class="stack">' + frontierCards + "</div></div>"
        + '<div class="detail-block"><div class="detail-section-title"><strong>影响链</strong><span>主题扩散路径</span></div><div class="stack">' + themeCards + "</div></div>"
        + '<div class="detail-block"><div class="detail-section-title"><strong>候选标的</strong><span>港A科技映射</span></div><div class="stack">' + candidateCards + "</div></div>"
        + '<div class="detail-block"><div class="detail-section-title"><strong>专题逻辑</strong><span>为什么值得看</span></div><ul class="rationale-list">' + rationale + "</ul></div>"
        + '<div class="detail-block"><div class="detail-section-title"><strong>主证据原文</strong><span>只展示干净来源；微博/雪球只做热度加权</span></div><ul class="doc-list">' + (linkedDocs || '<div class="empty">当前没有联动原文。</div>') + "</ul></div>"
        + "</div>";
      detailHost.scrollTop = 0;
      const column = detailHost.closest(".right-column");
      if (column) {
        column.scrollTop = 0;
      }
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
        const displayStatus = line.name === "cookies" && status === "missing"
          ? "未配置"
          : (runtimeStatusLabels[status] || status);
        const ageText = line.age_seconds === null || line.age_seconds === undefined
          ? "age n/a"
          : "age " + line.age_seconds + "s";
        const updateText = line.last_update ? String(line.last_update) : "n/a";
        const modules = Array.isArray(line.modules) ? line.modules : [];
        return '<div class="status-card ' + escapeHtml(status) + '">'
          + '<div class="status-topline">'
          + '<div class="status-name">' + escapeHtml(runtimeLineLabels[line.name] || line.name || "line") + "</div>"
          + '<div class="status-state">' + escapeHtml(displayStatus) + "</div>"
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
                    const extra = module.reason ? String(module.reason) : (counter !== "" ? String(counter) : "");
                    return '<span class="status-module ' + escapeHtml(moduleStatus) + '">'
                      + escapeHtml(module.name || "module")
                      + (extra !== "" ? ' · ' + escapeHtml(extra) : "")
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
      const discoveryCandidates = Array.isArray(lexiconDiscovery.candidates) ? lexiconDiscovery.candidates : [];
      const acceptedTerms = filteredAcceptedTerms();
      const frontierItems = frontierTrackerItems(signals);
      const currentSignal = selectedTechSignal();

      document.getElementById("techSummaryLabel").textContent =
        "信号 " + String(summary.signal_count || 0) + " · 主题 " + String(summary.hot_theme_count || 0)
        + " · 词库 " + String(summary.lexicon_version || "unversioned");
      document.getElementById("techSignalCountLabel").textContent =
        currentSignal ? "关注分 " + score(currentSignal.trading_attention_score) : "暂无信号";
      document.getElementById("techThemeCountLabel").textContent = themes.length + " 个";
      document.getElementById("techFrontierCountLabel").textContent = frontierItems.length + " 个";
      document.getElementById("techAssetCountLabel").textContent = assets.length + " 个";
      document.getElementById("lexiconDiscoveryCountLabel").textContent = discoveryCandidates.length + " 个";
      document.getElementById("lexiconCatalogCountLabel").textContent =
        acceptedTerms.length + " / " + String((lexiconDiscovery.summary || {}).accepted_count || 0) + " 个";

      const signalHost = document.getElementById("techSignalList");
      const themeHost = document.getElementById("techThemeList");
      const frontierHost = document.getElementById("techFrontierList");
      const assetHost = document.getElementById("techAssetList");
      const discoveryHost = document.getElementById("lexiconDiscoveryList");
      const catalogHost = document.getElementById("lexiconCatalogList");
      const reviewNoteHost = document.getElementById("lexiconReviewNote");
      const detailHost = document.getElementById("techDetailView");

      reviewNoteHost.textContent = reviewStatusText();

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
            + '<span class="mini-score">证据 ' + escapeHtml((signal.evidence_source_ids || []).join(", ") || "n/a") + "</span>"
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

      if (!frontierItems.length) {
        frontierHost.innerHTML = '<div class="empty">当前没有命中的前沿突破信号。</div>';
      } else {
        frontierHost.innerHTML = frontierItems.map(function (item) {
          const linkedClusterId = item.cluster_ids[0] || "";
          const keywords = item.matched_keywords.join("，") || "暂无命中词";
          const leadingHeadline = item.headlines[0] || "暂无联动信号";
          return '<div class="theme-card">'
            + '<button type="button" data-tech-frontier-cluster="' + escapeHtml(linkedClusterId) + '">'
            + '<div class="card-topline">'
            + '<span class="badge type-company">frontier</span>'
            + '<span>' + escapeHtml(item.gap_level || "unknown") + "</span>"
            + '<span>score ' + escapeHtml(score(item.score)) + "</span>"
            + "</div>"
            + '<div class="headline">' + escapeHtml(item.cn_label || item.frontier_id) + "</div>"
            + '<p class="summary">' + escapeHtml(keywords) + "</p>"
            + '<p class="instrument-note">' + escapeHtml(leadingHeadline) + "</p>"
            + "</button></div>";
        }).join("");
        frontierHost.querySelectorAll("[data-tech-frontier-cluster]").forEach(function (button) {
          button.addEventListener("click", function () {
            const clusterId = button.getAttribute("data-tech-frontier-cluster");
            if (!clusterId) {
              return;
            }
            state.selectedClusterId = clusterId;
            state.selectedTechClusterId = clusterId;
            renderTechBlock();
            renderDetail();
          });
        });
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

      if (!discoveryCandidates.length) {
        discoveryHost.innerHTML = '<div class="empty">当前没有待审核新词。</div>';
      } else {
        discoveryHost.innerHTML = discoveryCandidates.map(function (candidate) {
          const impacts = Object.entries(candidate.inferred_impact || {}).slice(0, 4).map(function (entry) {
            return entry[0] + ":" + score(entry[1]);
          }).join(", ") || "n/a";
          const snippets = Array.isArray(candidate.example_snippets) ? candidate.example_snippets.slice(0, 2) : [];
          return '<div class="theme-card">'
            + '<div class="card-topline">'
            + '<span class="badge type-company">pending</span>'
            + '<span>freq ' + escapeHtml(candidate.raw_freq || 0) + '</span>'
            + '<span>score ' + escapeHtml(score(candidate.discovery_score)) + "</span>"
            + "</div>"
            + '<div class="headline">' + escapeHtml(candidate.text || "term") + "</div>"
            + '<p class="summary">' + escapeHtml(impacts) + "</p>"
            + (snippets.length
                ? '<div class="stack">'
                  + snippets.map(function (snippet) {
                      return '<p class="instrument-note">' + escapeHtml(snippet) + "</p>";
                    }).join("")
                  + "</div>"
                : "")
            + '<div class="review-row">'
            + '<select class="review-select" data-lexicon-type>'
            + lexiconTypeOptions.map(function (option) {
                return '<option value="' + escapeHtml(option.value) + '">' + escapeHtml(option.label) + "</option>";
              }).join("")
            + "</select>"
            + '<button type="button" class="review-action approve" data-lexicon-add="' + escapeHtml(candidate.text || "") + '">收录</button>'
            + '<button type="button" class="review-action reject" data-lexicon-reject="' + escapeHtml(candidate.text || "") + '">忽略</button>'
            + "</div>"
            + "</div>";
        }).join("");
        discoveryHost.querySelectorAll("[data-lexicon-add]").forEach(function (button) {
          button.addEventListener("click", function () {
            const card = button.closest(".theme-card");
            const select = card ? card.querySelector("[data-lexicon-type]") : null;
            const termType = select ? select.value : "theme";
            submitDiscoveryAction("add", button.getAttribute("data-lexicon-add"), termType, button);
          });
        });
        discoveryHost.querySelectorAll("[data-lexicon-reject]").forEach(function (button) {
          button.addEventListener("click", function () {
            submitDiscoveryAction("reject", button.getAttribute("data-lexicon-reject"), "theme", button);
          });
        });
      }

      if (!acceptedTerms.length) {
        catalogHost.innerHTML = '<div class="empty">当前没有可展示的正式词库词条。</div>';
      } else {
        catalogHost.innerHTML = acceptedTerms.map(function (item) {
          const synonyms = Array.isArray(item.synonyms) ? item.synonyms.slice(0, 5).join("，") : "";
          const tags = Array.isArray(item.trigger_tags) ? item.trigger_tags.slice(0, 4) : [];
          return '<div class="theme-card">'
            + '<div class="card-topline">'
            + '<span class="badge type-company">' + escapeHtml(item.term_type || "theme") + '</span>'
            + '<span>正式词库</span>'
            + '<span>同义词 ' + escapeHtml(item.synonym_count || 0) + '</span>'
            + "</div>"
            + '<div class="headline">' + escapeHtml(item.text || "term") + "</div>"
            + '<p class="summary">' + escapeHtml(synonyms || "暂无同义词") + "</p>"
            + (tags.length
                ? '<div class="chip-row">' + tags.map(function (tag) {
                    return '<span class="mini-tag">' + escapeHtml(tag) + "</span>";
                  }).join("") + "</div>"
                : "")
            + '<div class="review-row">'
            + '<button type="button" class="review-action reject" data-lexicon-remove="' + escapeHtml(item.text || "") + '">删除</button>'
            + "</div>"
            + "</div>";
        }).join("");
        catalogHost.querySelectorAll("[data-lexicon-remove]").forEach(function (button) {
          button.addEventListener("click", function () {
            submitLexiconRemove(button.getAttribute("data-lexicon-remove"), button);
          });
        });
      }

      if (!currentSignal) {
        detailHost.innerHTML = '<div class="empty">当前没有科技专题详情可看。</div>';
        return;
      }
      renderTechSignalDetail(detailHost, currentSignal, "港A科技催化", "techSignalCountLabel");
    }

    function renderFrontierWorkspace() {
      const trackerItems = frontierTrackerItems(frontierSignalsBase());
      const signalHost = document.getElementById("frontierSignalList");
      const trackerHost = document.getElementById("frontierTrackerList");
      const detailHost = document.getElementById("frontierDetailView");

      const hasSelectedFrontier = trackerItems.some(function (item) {
        return item.frontier_id === state.selectedFrontierId;
      });
      if (!trackerItems.length) {
        state.selectedFrontierId = null;
        state.selectedFrontierClusterId = null;
      } else if (!state.selectedFrontierId || !hasSelectedFrontier) {
        state.selectedFrontierId = trackerItems[0].frontier_id;
      }

      const signals = filteredFrontierSignals();
      const currentSignal = selectedFrontierSignal();

      document.getElementById("frontierTrackerCountLabel").textContent = trackerItems.length + " 个";
      document.getElementById("frontierSignalCountLabel").textContent = signals.length + " 条";

      if (!trackerItems.length) {
        trackerHost.innerHTML = '<div class="empty">当前没有命中的前沿突破主题。</div>';
      } else {
        trackerHost.innerHTML = trackerItems.map(function (item) {
          const active = item.frontier_id === state.selectedFrontierId ? " active" : "";
          const keywords = item.matched_keywords.join("，") || "暂无命中词";
          const headline = item.headlines[0] || "暂无联动信号";
          return '<div class="theme-card' + active + '">'
            + '<button type="button" data-frontier-id="' + escapeHtml(item.frontier_id) + '" data-frontier-cluster="' + escapeHtml(item.cluster_ids[0] || "") + '">'
            + '<div class="card-topline">'
            + '<span class="badge type-company">frontier</span>'
            + '<span>' + escapeHtml(item.gap_level || "unknown") + "</span>"
            + '<span>score ' + escapeHtml(score(item.score)) + "</span>"
            + "</div>"
            + '<div class="headline">' + escapeHtml(item.cn_label || item.frontier_id) + "</div>"
            + '<p class="summary">' + escapeHtml(keywords) + "</p>"
            + '<p class="instrument-note">' + escapeHtml(headline) + "</p>"
            + "</button></div>";
        }).join("");
        trackerHost.querySelectorAll("[data-frontier-id]").forEach(function (button) {
          button.addEventListener("click", function () {
            const frontierId = button.getAttribute("data-frontier-id");
            const clusterId = button.getAttribute("data-frontier-cluster");
            state.selectedFrontierId = frontierId || null;
            state.selectedFrontierClusterId = clusterId || null;
            if (clusterId) {
              state.selectedTechClusterId = clusterId;
              state.selectedClusterId = clusterId;
            }
            renderFrontierWorkspace();
          });
        });
      }

      if (!signals.length) {
        signalHost.innerHTML = '<div class="empty">当前没有前沿信号。</div>';
      } else {
        signalHost.innerHTML = signals.map(function (signal) {
          const active = signal.cluster_id === state.selectedFrontierClusterId ? " active" : "";
          const frontierSummary = (signal.frontier_hits || []).map(function (item) {
            return item.cn_label || item.frontier_id;
          }).slice(0, 3).join("，") || "暂无前沿命中";
          return '<div class="tech-card' + active + '">'
            + '<button type="button" data-frontier-signal="' + escapeHtml(signal.cluster_id) + '">'
            + '<div class="card-topline">'
            + '<span class="badge dir-' + escapeHtml(signal.direction) + '">' + escapeHtml(signal.direction) + '</span>'
            + '<span class="badge level-medium">' + escapeHtml(String(signal.attention_tier || "watch").toUpperCase()) + '</span>'
            + '<span>attention ' + escapeHtml(score(signal.trading_attention_score)) + "</span>"
            + "</div>"
            + '<div class="headline">' + escapeHtml(signal.headline) + "</div>"
            + '<p class="summary">' + escapeHtml(frontierSummary) + "</p>"
            + '<div class="tech-score-row">'
            + '<span class="mini-score">前沿 ' + escapeHtml((signal.frontier_hits || []).length) + "</span>"
            + '<span class="mini-score">炒作度 ' + escapeHtml(score(signal.spec_score)) + "</span>"
            + '<span class="mini-score">热度 ' + escapeHtml(score(signal.heat_score)) + "</span>"
            + "</div>"
            + "</button></div>";
        }).join("");
        signalHost.querySelectorAll("[data-frontier-signal]").forEach(function (button) {
          button.addEventListener("click", function () {
            const clusterId = button.getAttribute("data-frontier-signal");
            const signal = signals.find(function (item) {
              return item.cluster_id === clusterId;
            });
            state.selectedFrontierClusterId = clusterId || null;
            if (signal && signal.frontier_hits && signal.frontier_hits.length) {
              state.selectedFrontierId = signal.frontier_hits[0].frontier_id;
            }
            if (clusterId) {
              state.selectedTechClusterId = clusterId;
              state.selectedClusterId = clusterId;
            }
            renderFrontierWorkspace();
          });
        });
      }

      renderTechSignalDetail(detailHost, currentSignal, "科技前沿突破", "frontierDetailCountLabel");
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

    function renderPanelError(hostId, title, detail) {
      const host = document.getElementById(hostId);
      if (!host) {
        return;
      }
      host.innerHTML = '<div class="empty"><strong>' + escapeHtml(title)
        + '</strong><br>' + escapeHtml(detail || "当前模块渲染失败，请稍后刷新重试。")
        + "</div>";
    }

    function safeRender(label, hostIds, fn) {
      try {
        fn();
      } catch (error) {
        console.error("dashboard render failed:", label, error);
        hostIds.forEach(function (hostId) {
          renderPanelError(hostId, label + " 模块暂时不可用", "这块内容渲染出错了，但其他功能块会继续工作。");
        });
      }
    }

    function render() {
      safeRender("视图切换", ["viewSwitch"], renderViewSwitch);
      safeRender("工作区", ["coreWorkspace", "techWorkspace", "frontierWorkspace"], renderWorkspaces);
      safeRender("头部摘要", ["heroMeta", "metricGrid"], renderHero);
      safeRender("运行状态", ["runtimeStatusGrid"], renderRuntimeStatus);
      safeRender("筛选器", ["filterChips"], renderFilters);

      if (state.view === "tech") {
        safeRender(
          "港A科技专题",
          ["techSignalList", "techThemeList", "techFrontierList", "techAssetList", "lexiconDiscoveryList", "lexiconReviewNote", "techDetailView"],
          renderTechBlock
        );
        schedulePersistState();
        return;
      }

      if (state.view === "frontier") {
        safeRender(
          "科技前沿",
          ["frontierTrackerList", "frontierSignalList", "frontierDetailView"],
          renderFrontierWorkspace
        );
        schedulePersistState();
        return;
      }

      safeRender("提醒列表", ["alertsList"], renderAlerts);
      safeRender("事件列表", ["eventsGrid"], renderEvents);
      safeRender("事件详情", ["detailView"], renderDetail);
      safeRender("候选标的", ["instrumentList"], renderInstruments);
      safeRender("原始消息流", ["feedList"], renderFeed);
      schedulePersistState();
    }

    function renderAndRestoreIfNeeded() {
      render();
      if (restoreScrollPending) {
        restoreScrollPositions();
        restoreScrollPending = false;
      }
    }

    document.getElementById("searchInput").addEventListener("input", function (event) {
      markInteraction();
      state.query = String(event.target.value || "").trim().toLowerCase();
      render();
    });

    document.getElementById("lexiconCatalogQuery").addEventListener("input", function (event) {
      markInteraction();
      state.lexiconCatalogQuery = String(event.target.value || "").trim().toLowerCase();
      renderTechBlock();
      schedulePersistState();
    });

    document.getElementById("resetButton").addEventListener("click", function () {
      markInteraction();
      state.direction = "all";
      state.query = "";
      document.getElementById("searchInput").value = "";
      render();
    });

    restoreDashboardState();
    document.getElementById("searchInput").value = state.query;
    document.getElementById("lexiconCatalogQuery").value = state.lexiconCatalogQuery;
    renderAndRestoreIfNeeded();
    refreshDiscoveryFromApi();

    document.addEventListener("click", function () {
      markInteraction();
    }, true);
    document.addEventListener("keydown", function () {
      markInteraction();
    }, true);
    document.addEventListener("change", function () {
      markInteraction();
    }, true);
    document.querySelectorAll("[data-scroll-key]").forEach(function (node) {
      node.addEventListener("scroll", function () {
        markInteraction();
      }, { passive: true });
    });
    window.addEventListener("beforeunload", function () {
      persistDashboardState();
    });

    let countdown = 60;
    function refreshPage() {
      persistDashboardState();
      const url = new URL(window.location.href);
      url.searchParams.set("ts", String(Date.now()));
      window.location.replace(url.toString());
    }
    function renderRefreshText() {
      document.getElementById("refreshText").textContent = hasRecentInteraction()
        ? "检测到你最近在操作，自动刷新已延后，停止操作后会再等 60 秒刷新。"
        : ("页面每 60 秒自动刷新一次，距下次刷新 " + countdown + " 秒");
    }
    renderRefreshText();
    setInterval(function () {
      if (hasRecentInteraction()) {
        countdown = 60;
        renderRefreshText();
        return;
      }
      countdown -= 1;
      if (countdown <= 0) {
        refreshPage();
        return;
      }
      renderRefreshText();
    }, 1000);