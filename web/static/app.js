/* Hawaiʻi District Profiles: chat client.
   No framework, no build step. All answer text is rendered with DOM APIs (never innerHTML),
   so model output can't inject markup. */
(function () {
  "use strict";

  var CHAMBER_NAMES = { house: "State House", senate: "State Senate" };
  // Friendlier phrasing for the picker; falls back to the data label.
  var TOPIC_PHRASES = {
    total_population: "total population",
    median_age: "median age",
    median_household_income: "median household income",
    poverty_rate: "poverty rate",
    snap_households: "number of households receiving SNAP",
    snap_household_rate: "share of households receiving SNAP",
    renter_cost_burden_rate: "share of renters paying 30% or more of income on rent",
    veterans: "number of veterans",
    veteran_rate: "veteran share of the adult population",
    limited_english_household_rate: "share of limited-English households",
    armed_forces: "number of people in the Armed Forces",
    armed_forces_rate: "share of people 16 and over in the Armed Forces"
  };
  var TOOL_LABELS = {};
  var MAX_HISTORY_MESSAGES = 18;   // server allows 20 incl. the new question
  var MAX_HISTORY_CHARS = 16000;   // server allows 20,000 total
  var REQUEST_TIMEOUT_MS = 120000;

  var history = [];      // [{role, content}] of completed exchanges
  var busy = false;
  var answerCount = 0;
  var meta = null;

  var $ = function (id) { return document.getElementById(id); };
  var log, statusEl, form, textarea, sendBtn, formError, emptyState, errorArea;

  // ---------------------------------------------------------------- helpers
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (k === "class") node.className = attrs[k];
        else if (k === "text") node.textContent = attrs[k];
        else if (attrs[k] !== null && attrs[k] !== undefined) node.setAttribute(k, attrs[k]);
      });
    }
    (children || []).forEach(function (c) {
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }

  var statusTimer = null;
  function setStatus(text) {
    // Clear first so repeating the same message is announced again. Cancel any pending
    // update so an older message can't overwrite a newer one.
    if (statusTimer) window.clearTimeout(statusTimer);
    statusEl.textContent = "";
    statusTimer = window.setTimeout(function () { statusEl.textContent = text; statusTimer = null; }, 50);
  }

  function escapeRegExp(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

  function chamberName(c) { return CHAMBER_NAMES[c] || c; }

  function metricLabel(key) {
    if (meta) {
      for (var i = 0; i < meta.metrics.length; i++) {
        if (meta.metrics[i].key === key) return TOPIC_PHRASES[key] || meta.metrics[i].label;
      }
    }
    return TOPIC_PHRASES[key] || String(key).replace(/_/g, " ");
  }

  // Fallback only; mirrors DISPLAY_DECIMALS in agent/tools.py.
  var DECIMALS = { percent: 2, dollars: 0, people: 0, households: 0, years: 1 };
  function formatValue(v, unit) {
    if (v === null || v === undefined) return "n/a";
    var d = unit in DECIMALS ? DECIMALS[unit] : 2;
    var s = Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
    if (unit === "percent") return s + "%";
    if (unit === "dollars") return "$" + s;
    return s;
  }

  // ------------------------------------------------- markdown-lite renderer
  // Supports paragraphs, **bold**, `code`, https links, bullet/numbered lists,
  // headings and pipe tables. Low-reliability numbers are wrapped in <mark>.
  function makeMarker(lowrel) {
    var tokens = [];
    (lowrel || []).forEach(function (item) {
      (item.matched_text || []).forEach(function (t) { if (tokens.indexOf(t) === -1) tokens.push(t); });
    });
    if (!tokens.length) return null;
    tokens.sort(function (a, b) { return b.length - a.length; });
    // Include a trailing % so the marker icon follows the whole value ("4.19%⚠").
    return new RegExp("(?<![\\d.,$])(?:" + tokens.map(escapeRegExp).join("|") + ")(?![\\d])%?", "g");
  }

  function appendText(parent, text, marker) {
    if (!marker) { parent.appendChild(document.createTextNode(text)); return; }
    marker.lastIndex = 0;
    var last = 0, m;
    while ((m = marker.exec(text)) !== null) {
      if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      var mark = el("mark", { class: "lowrel" }, [m[0]]);
      mark.appendChild(el("span", { class: "lowrel-icon", "aria-hidden": "true", text: "⚠︎" }));
      mark.appendChild(el("span", { class: "visually-hidden", text: " (low reliability: see note below)" }));
      parent.appendChild(mark);
      last = m.index + m[0].length;
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }

  var INLINE_RE = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\(https:\/\/[^\s)]+\))/;

  function renderInline(parent, text, marker) {
    text.split(INLINE_RE).forEach(function (part) {
      if (!part) return;
      if (/^\*\*[^*]+\*\*$/.test(part)) {
        var strong = el("strong");
        appendText(strong, part.slice(2, -2), marker);
        parent.appendChild(strong);
      } else if (/^`[^`]+`$/.test(part)) {
        parent.appendChild(el("code", { text: part.slice(1, -1) }));
      } else if (/^\[[^\]]+\]\(https:\/\/[^\s)]+\)$/.test(part)) {
        var mm = part.match(/^\[([^\]]+)\]\((https:\/\/[^\s)]+)\)$/);
        parent.appendChild(el("a", { href: mm[2], rel: "noopener noreferrer", target: "_blank" },
          [mm[1], el("span", { class: "visually-hidden", text: " (opens in a new tab)" })]));
      } else {
        appendText(parent, part.replace(/\*([^*\s][^*]*)\*/g, "$1"), marker);
      }
    });
  }

  function splitRow(line) {
    var s = line.trim();
    if (s.charAt(0) === "|") s = s.slice(1);
    if (s.charAt(s.length - 1) === "|") s = s.slice(0, -1);
    return s.split("|").map(function (c) { return c.trim(); });
  }

  function renderTable(lines, marker) {
    var rows = lines.map(splitRow);
    var hasHeader = rows.length > 1 && rows[1].every(function (c) { return /^:?-{2,}:?$/.test(c); });
    var table = el("table");
    var start = 0;
    if (hasHeader) {
      var thead = el("thead"), tr = el("tr");
      rows[0].forEach(function (c) { var th = el("th", { scope: "col" }); renderInline(th, c, marker); tr.appendChild(th); });
      thead.appendChild(tr); table.appendChild(thead);
      start = 2;
    }
    var tbody = el("tbody");
    rows.slice(start).forEach(function (r) {
      var tr = el("tr");
      r.forEach(function (c) { var td = el("td"); renderInline(td, c, marker); tr.appendChild(td); });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    // Scrollable region must be focusable so keyboard users can scroll it.
    return el("div", { class: "table-wrap", tabindex: "0", role: "region", "aria-label": "Data table" }, [table]);
  }

  function renderMarkdown(text, marker) {
    var frag = document.createDocumentFragment();
    var lines = String(text).replace(/\r\n/g, "\n").split("\n");
    var i = 0, para = [];

    function flushPara() {
      if (!para.length) return;
      var p = el("p");
      renderInline(p, para.join(" "), marker);
      frag.appendChild(p);
      para = [];
    }

    while (i < lines.length) {
      var line = lines[i];
      if (!line.trim()) { flushPara(); i++; continue; }
      if (/^\s*\|/.test(line)) {
        flushPara();
        var tl = [];
        while (i < lines.length && /^\s*\|/.test(lines[i])) tl.push(lines[i++]);
        frag.appendChild(renderTable(tl, marker));
        continue;
      }
      var h = line.match(/^\s*#{1,6}\s+(.*)$/);
      if (h) {
        flushPara();
        var hd = el("h4"); renderInline(hd, h[1], marker); frag.appendChild(hd);
        i++; continue;
      }
      if (/^\s*([-*•])\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
        flushPara();
        var ordered = /^\s*\d+[.)]\s+/.test(line);
        var list = el(ordered ? "ol" : "ul");
        var itemRe = ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-*•]\s+/;
        while (i < lines.length && itemRe.test(lines[i])) {
          var li = el("li"); renderInline(li, lines[i].replace(itemRe, ""), marker);
          list.appendChild(li); i++;
        }
        frag.appendChild(list);
        continue;
      }
      para.push(line.trim());
      i++;
    }
    flushPara();
    return frag;
  }

  // ------------------------------------------------------- answer pieces
  function describeTool(t) {
    var a = t.input || {};
    var ch = chamberName(a.chamber);
    switch (t.tool) {
      case "get_district_profile":
        return "District profile: " + ch + " District " + a.district;
      case "compare_districts":
        return "Comparison of " + metricLabel(a.metric) + ": " + ch + " Districts " + (a.districts || []).join(", ");
      case "rank_districts":
        return "Ranking of " + ch + " districts by " + metricLabel(a.metric) + " (" +
          (a.order === "asc" ? "lowest " : "highest ") + a.top_n + ")";
      case "statewide_context":
        return "Range of " + metricLabel(a.metric) + " across " +
          (a.chamber === "both" ? "State House and State Senate" : ch) + " districts";
      case "find_programs":
        return "Assistance programs" + (a.need && a.need !== "any" ? " for " + String(a.need).replace(/_/g, " ") : "");
      case "district_program_context":
        return "Programs and related measures: " + ch + " District " + a.district;
      default:
        return TOOL_LABELS[t.tool] || t.tool;
    }
  }

  function buildLowRelPanel(items) {
    var panel = el("div", { class: "lowrel-panel" });
    panel.appendChild(el("h4", { text: "⚠︎ Low-reliability values in this answer" }));
    var ul = el("ul");
    items.forEach(function (it) {
      var where = it.district ? chamberName(it.chamber) + " District " + it.district : "";
      var li = el("li");
      // Prefer the server's display strings so decimals match everywhere.
      var val = it.value_display || formatValue(it.value, it.unit);
      var moe = it.moe_display || (it.moe !== null && it.moe !== undefined ? "± " + formatValue(it.moe, it.unit) : "");
      li.appendChild(el("strong", {
        text: it.label + (where ? " (" + where + ")" : "") + ": " + val + (moe ? " " + moe : "")
      }));
      li.appendChild(document.createTextNode(". " + (it.caveat || "This estimate has a large margin of error.")));
      ul.appendChild(li);
    });
    panel.appendChild(ul);
    return panel;
  }

  function buildSources(data) {
    var citations = data.citations || [];
    var tools = data.tool_results || [];
    var details = el("details", { class: "sources" });
    var count = citations.length;
    details.appendChild(el("summary", {
      text: "Sources (" + count + " citation" + (count === 1 ? "" : "s") + ", " +
        tools.length + " data lookup" + (tools.length === 1 ? "" : "s") + ")"
    }));
    var body = el("div", { class: "sources-body" });
    body.appendChild(el("h4", { text: "Citations" }));
    if (citations.length) {
      var ul = el("ul");
      citations.forEach(function (c) { ul.appendChild(el("li", { text: c })); });
      body.appendChild(ul);
    } else {
      body.appendChild(el("p", { text: "No Census data was used in this answer." }));
    }
    if (tools.length) {
      body.appendChild(el("h4", { text: "Data lookups" }));
      var ol = el("ol");
      tools.forEach(function (t) {
        var li = el("li", { text: describeTool(t) });
        if (t.is_error) {
          li.appendChild(el("span", { class: "tool-error", text: " — not found: " + ((t.output && t.output.error) || "error") }));
        }
        var census = (t.source_urls || []).filter(function (u) { return u.indexOf("https://api.census.gov/") === 0; });
        (t.source_urls || []).forEach(function (u) {
          var isReport = u.indexOf("https://elections.hawaii.gov/") === 0;
          var isCensus = u.indexOf("https://api.census.gov/") === 0;
          var label = isReport ? "Reapportionment Commission final report (PDF)"
            : isCensus ? "Census API query" + (census.length > 1 ? " " + (census.indexOf(u) + 1) : "")
            : "Program source: " + u.replace(/^https:\/\/(www\.)?/, "").split("/")[0];
          li.appendChild(document.createTextNode(" — "));
          li.appendChild(el("a", { href: u, rel: "noopener noreferrer", target: "_blank" }, [
            label,
            el("span", { class: "visually-hidden", text: " for " + describeTool(t) + " (opens in a new tab)" })
          ]));
        });
        ol.appendChild(li);
      });
      body.appendChild(ol);
    }
    details.appendChild(body);
    return details;
  }

  // One-page profile links for every district profile this answer looked up. The page is
  // built on the server from tool data only, never from the model's text.
  function buildExports(data) {
    var seen = {}, items = [];
    (data.tool_results || []).forEach(function (t) {
      if (t.tool !== "get_district_profile" || t.is_error || !t.input) return;
      var key = t.input.chamber + "-" + t.input.district;
      if (seen[key]) return;
      seen[key] = true;
      items.push(t.input);
    });
    if (!items.length) return null;
    var box = el("div", { class: "exports" });
    items.forEach(function (a) {
      var base = "/district/" + encodeURIComponent(a.chamber) + "/" + encodeURIComponent(a.district) + "/profile";
      var name = chamberName(a.chamber) + " District " + a.district;
      box.appendChild(el("p", {}, [
        el("a", { class: "btn btn-secondary", href: base + "?download=1", download: "" },
          ["Download district profile", el("span", { class: "visually-hidden", text: ": " + name })]),
        " ",
        el("a", { href: base, target: "_blank", rel: "noopener" },
          ["Open print view of " + name, el("span", { class: "visually-hidden", text: " (opens in a new tab)" })])
      ]));
    });
    return box;
  }

  function addUserMessage(text) {
    if (emptyState) { emptyState.remove(); emptyState = null; }
    var art = el("article", { class: "msg msg-user", "aria-label": "Your question" }, [
      el("h3", { class: "msg-who", text: "You asked" }),
      el("p", { text: text })
    ]);
    log.appendChild(art);
    return art;
  }

  function addAnswer(data) {
    answerCount += 1;
    var marker = makeMarker(data.low_reliability);
    var body = el("div", { class: "body" });
    body.appendChild(renderMarkdown(data.answer || "No answer was returned.", marker));
    var art = el("article", { class: "msg msg-agent", "aria-label": "Answer " + answerCount }, [
      el("h3", { class: "msg-who", text: "Answer" }),
      body
    ]);
    if (data.low_reliability && data.low_reliability.length) art.appendChild(buildLowRelPanel(data.low_reliability));
    if (data.hit_tool_limit) {
      art.appendChild(el("p", { class: "hint", text: "This answer reached the lookup limit for one question, so it may be incomplete. Try asking a narrower question." }));
    }
    var exp = buildExports(data);
    if (exp) art.appendChild(exp);
    art.appendChild(buildSources(data));
    log.appendChild(art);
  }

  function showLoading() {
    var node = el("div", { class: "loading", id: "loading", "aria-hidden": "true" }, [
      el("span", { class: "spinner" }),
      el("span", { text: "Looking up Census data and writing an answer…" })
    ]);
    log.appendChild(node);
    setStatus("Looking up Census data. This can take up to a minute.");
  }

  function hideLoading() {
    var n = $("loading");
    if (n) n.remove();
  }

  function clearError() {
    errorArea.textContent = "";
    errorArea.hidden = true;
  }

  function showError(message, retry) {
    clearError();
    var box = el("div", { class: "error-box" }, [el("p", { text: message })]);
    if (retry) {
      var btn = el("button", { type: "button", class: "btn", text: "Try again" });
      btn.addEventListener("click", function () { clearError(); retry(); });
      box.appendChild(btn);
    }
    errorArea.appendChild(box);
    errorArea.hidden = false;
  }

  function setBusy(on) {
    busy = on;
    log.setAttribute("aria-busy", on ? "true" : "false");
    sendBtn.setAttribute("aria-disabled", on ? "true" : "false");
    sendBtn.textContent = on ? "Working…" : "Ask";
  }

  function trimmedHistory() {
    var h = history.slice(-MAX_HISTORY_MESSAGES);
    var total = function () { return h.reduce(function (n, m) { return n + m.content.length; }, 0); };
    while (h.length && total() > MAX_HISTORY_CHARS) h = h.slice(2);
    return h;
  }

  // ------------------------------------------------------------- network
  function send(question, userNode) {
    setBusy(true);
    clearError();
    showLoading();
    var ctrl = new AbortController();
    var timer = window.setTimeout(function () { ctrl.abort(); }, REQUEST_TIMEOUT_MS);
    var messages = trimmedHistory().concat([{ role: "user", content: question }]);

    fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: messages }),
      signal: ctrl.signal
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        return { ok: res.ok, status: res.status, data: data };
      });
    }).then(function (r) {
      window.clearTimeout(timer);
      hideLoading();
      setBusy(false);
      if (!r.ok) {
        var msg = (r.data && r.data.error) || ("The request failed (status " + r.status + ").");
        showError(msg, function () { send(question, userNode); });
        setStatus("Error: " + msg);
        return;
      }
      history.push({ role: "user", content: question });
      history.push({ role: "assistant", content: r.data.answer || "" });
      addAnswer(r.data);
      setStatus("Answer received.");
    }).catch(function (err) {
      window.clearTimeout(timer);
      hideLoading();
      setBusy(false);
      var msg = err && err.name === "AbortError"
        ? "The answer took too long. Please try again."
        : "Couldn't connect to the server. Check your connection and try again.";
      showError(msg, function () { send(question, userNode); });
      setStatus("Error: " + msg);
    });
  }

  function ask(question) {
    question = (question || "").trim();
    if (busy) { setStatus("Still working on the last question."); return; }
    if (!question) {
      formError.textContent = "Type a question first.";
      formError.hidden = false;
      textarea.setAttribute("aria-invalid", "true");
      textarea.focus();
      return;
    }
    formError.hidden = true;
    textarea.removeAttribute("aria-invalid");
    textarea.value = "";
    var node = addUserMessage(question);
    send(question, node);
  }

  // ---------------------------------------------------------------- picker
  function fillDistricts() {
    var sel = $("pick-district");
    var chamber = $("pick-chamber").value;
    sel.textContent = "";
    var list = (meta && meta.chambers[chamber]) || [];
    list.forEach(function (d) { sel.appendChild(el("option", { value: String(d), text: "District " + d })); });
  }

  function fillTopics() {
    var sel = $("pick-topic");
    (meta.metrics || []).forEach(function (m) {
      var phrase = TOPIC_PHRASES[m.key] || m.label;
      sel.appendChild(el("option", { value: m.key, text: phrase.charAt(0).toUpperCase() + phrase.slice(1) }));
    });
  }

  function pickerQuestion() {
    var chamber = $("pick-chamber").value;
    var district = $("pick-district").value;
    var topic = $("pick-topic").value;
    var where = chamberName(chamber) + " District " + district;
    if (topic === "profile") return "Give me a profile of " + where + ".";
    return "What is the " + metricLabel(topic) + " in " + where + "?";
  }

  function loadMeta() {
    fetch("/api/meta").then(function (r) {
      if (!r.ok) throw new Error("meta " + r.status);
      return r.json();
    }).then(function (m) {
      meta = m;
      fillDistricts();
      fillTopics();
      var vintage = String(m.boundary_vintage || "").replace(/districts?$/, "district boundaries");
      $("data-note").textContent = "Data: U.S. Census Bureau, " + m.dataset + " estimates; " + vintage + ".";
    }).catch(function () {
      var sel = $("pick-district");
      sel.textContent = "";
      sel.appendChild(el("option", { value: "", text: "Unavailable" }));
      $("picker").querySelector("button").setAttribute("aria-disabled", "true");
      setStatus("The district list couldn't be loaded. You can still type a question.");
    });
  }

  // ------------------------------------------------------------------ init
  function init() {
    log = $("log"); statusEl = $("status"); form = $("chat-form"); textarea = $("question");
    sendBtn = $("send"); formError = $("form-error"); emptyState = $("empty-state");
    errorArea = el("div", { id: "error-area", role: "alert", hidden: "" });
    log.parentNode.insertBefore(errorArea, statusEl.nextSibling);

    form.addEventListener("submit", function (e) { e.preventDefault(); ask(textarea.value); });
    textarea.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(textarea.value); }
    });
    textarea.addEventListener("input", function () {
      if (!formError.hidden && textarea.value.trim()) { formError.hidden = true; textarea.removeAttribute("aria-invalid"); }
    });

    Array.prototype.forEach.call(document.querySelectorAll(".chip"), function (chip) {
      chip.addEventListener("click", function () { ask(chip.getAttribute("data-question")); });
    });

    $("pick-chamber").addEventListener("change", fillDistricts);
    $("picker").addEventListener("submit", function (e) {
      e.preventDefault();
      if (!meta || !$("pick-district").value) return;
      textarea.value = pickerQuestion();
      textarea.focus();
      setStatus("Question filled in. Press Enter or the Ask button to send it.");
    });

    $("new-chat").addEventListener("click", function () {
      if (busy) { setStatus("Wait for the current answer before starting over."); return; }
      history = []; answerCount = 0;
      log.textContent = "";
      emptyState = el("p", { class: "empty-state", id: "empty-state",
        text: "No questions yet. Pick an example, build a question above, or type your own below." });
      log.appendChild(emptyState);
      clearError();
      textarea.value = "";
      textarea.focus();
      setStatus("Started a new conversation.");
    });

    loadMeta();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
