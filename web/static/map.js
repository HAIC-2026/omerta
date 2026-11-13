/* Hawaiʻi district choropleth. All values come from /api/map (tool data). Color is never the
   only channel: every district has a text label (tooltip + aria-label), low-reliability
   districts are hatched, and the same data is in a sortable table. */
(function () {
  "use strict";
  var SVGNS = "http://www.w3.org/2000/svg";
  var CHAMBER = { house: "State House", senate: "State Senate" };
  var PHRASES = {
    total_population: "total population", median_age: "median age",
    median_household_income: "median household income", poverty_rate: "poverty rate",
    snap_households: "households receiving SNAP", snap_household_rate: "share of households receiving SNAP",
    renter_cost_burden_rate: "share of renters paying 30%+ of income on rent", veterans: "veterans",
    veteran_rate: "veteran share of adults", armed_forces: "people in the Armed Forces",
    armed_forces_rate: "Armed Forces share of people 16+", limited_english_household_rate: "limited-English households"
  };
  var CLASSES = 5;
  var state = { data: null, sort: { key: "district", dir: "ascending" } };
  var $ = function (id) { return document.getElementById(id); };

  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") n.textContent = attrs[k]; else if (k === "class") n.className = attrs[k]; else n.setAttribute(k, attrs[k]);
    });
    (kids || []).forEach(function (c) { n.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return n;
  }
  function svg(tag, attrs) {
    var n = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    return n;
  }
  function phrase(key) { return PHRASES[key] || key.replace(/_/g, " "); }
  function districtName(ch, d) { return CHAMBER[ch] + " District " + d; }
  function question(ch, d, metric) {
    return "Tell me about " + districtName(ch, d) + ", including its " + phrase(metric) + ".";
  }
  // The question travels in the URL fragment, which browsers never send to the server, so it
  // can't end up in access logs. The chat page prefills it; the user sends it.
  function askUrl(ch, d, metric) { return "/#ask=" + encodeURIComponent(question(ch, d, metric)); }
  function status(t) { $("map-status").textContent = t; }

  // Equal-count (quantile) classes over districts with a value.
  function classify(rows) {
    var withVal = rows.filter(function (r) { return r.value !== null && r.value !== undefined; })
      .slice().sort(function (a, b) { return a.value - b.value; });
    var cls = {}, groups = [];
    for (var i = 0; i < CLASSES; i++) groups.push([]);
    withVal.forEach(function (r, i) {
      var c = Math.min(CLASSES - 1, Math.floor(i * CLASSES / withVal.length));
      // keep ties in the same class
      if (i > 0 && r.value === withVal[i - 1].value) c = cls[withVal[i - 1].district];
      cls[r.district] = c;
      groups[c].push(r);
    });
    return { cls: cls, groups: groups };
  }

  function rowLabel(r, data) {
    var v = r.value_display ? r.value_display + " " + (r.moe_display || "") : "no estimate";
    return districtName(data.chamber, r.district) + ": " + phrase(data.metric) + " " + v +
      (r.low_reliability ? ". Low reliability" : "") + ".";
  }

  function zoomBox(data, group) {
    var vb = data.view_box;
    if (group === "all") return vb;
    var b = null;
    data.districts.forEach(function (r) {
      if (r.island_group !== group || !r.bbox) return;
      b = b ? [Math.min(b[0], r.bbox[0]), Math.min(b[1], r.bbox[1]), Math.max(b[2], r.bbox[2]), Math.max(b[3], r.bbox[3])] : r.bbox.slice();
    });
    if (!b) return vb;
    var pad = Math.max(b[2] - b[0], b[3] - b[1]) * 0.06;
    return [b[0] - pad, b[1] - pad, b[2] - b[0] + 2 * pad, b[3] - b[1] + 2 * pad];
  }

  function showTip(r, data, evt, target) {
    var tip = $("map-tip");
    tip.textContent = "";
    tip.appendChild(el("strong", { text: districtName(data.chamber, r.district) }));
    tip.appendChild(document.createTextNode(phrase(data.metric) + ": " + (r.value_display || "no estimate") +
      (r.moe_display ? " " + r.moe_display : "")));
    if (r.low_reliability) tip.appendChild(el("div", { class: "lr-label", text: "⚠︎ Low reliability (MOE " + r.moe_percent_of_estimate + "% of estimate)" }));
    tip.appendChild(el("div", { class: "hint", text: "Click or press Enter to ask about this district." }));
    tip.hidden = false;
    var wrap = tip.parentNode.getBoundingClientRect();
    var x, y;
    if (evt && evt.clientX) { x = evt.clientX - wrap.left; y = evt.clientY - wrap.top; }
    else { var bb = target.getBoundingClientRect(); x = bb.left - wrap.left + bb.width / 2; y = bb.top - wrap.top + bb.height / 2; }
    var w = tip.offsetWidth;
    tip.style.left = Math.max(4, Math.min(wrap.width - w - 4, x + 12)) + "px";
    tip.style.top = Math.max(4, y + 12) + "px";
  }
  function hideTip() { $("map-tip").hidden = true; $("map-highlight").setAttribute("d", ""); }
  function highlight(r) { $("map-highlight").setAttribute("d", r.path); }

  function render() {
    var data = state.data;
    var group = $("map-island").value;
    var k = classify(data.districts);
    var gD = $("map-districts"), gH = $("map-hatches");
    gD.textContent = ""; gH.textContent = "";
    var vb = zoomBox(data, group);
    $("map-svg").setAttribute("viewBox", vb.join(" "));
    // Keep the hatch the same on-screen size at any zoom: ~7px spacing, ~1.5px lines.
    var px = vb[2] / Math.max(1, $("map-svg").clientWidth || 800);
    var pat = $("hatch"), line = $("hatch-line"), size = 7 * px;
    pat.setAttribute("width", size); pat.setAttribute("height", size);
    line.setAttribute("y2", size); line.style.strokeWidth = (1.5 * px) + "px";
    var shown = 0;
    data.districts.forEach(function (r) {
      if (!r.path) return;
      var inView = group === "all" || r.island_group === group;
      var c = r.district in k.cls ? "c" + (k.cls[r.district] + 1) : "cnone";
      var p = svg("path", { d: r.path, class: "district " + c, "data-district": r.district,
        tabindex: inView ? "0" : "-1", role: "button", "aria-label": rowLabel(r, data) + " Press Enter to ask about it." });
      if (!inView) p.setAttribute("aria-hidden", "true"); else shown++;
      p.addEventListener("mousemove", function (e) { showTip(r, data, e, p); highlight(r); });
      p.addEventListener("mouseleave", hideTip);
      p.addEventListener("focus", function () { showTip(r, data, null, p); highlight(r); });
      p.addEventListener("blur", hideTip);
      p.addEventListener("click", function () { location.href = askUrl(data.chamber, r.district, data.metric); });
      p.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); location.href = askUrl(data.chamber, r.district, data.metric); }
      });
      gD.appendChild(p);
      if (r.low_reliability) gH.appendChild(svg("path", { d: r.path, class: "hatch" }));
    });

    // Caption, legend, notes
    var label = phrase(data.metric);
    $("map-caption").textContent = label.charAt(0).toUpperCase() + label.slice(1) + " by " + CHAMBER[data.chamber] +
      " district (" + data.citation + ")";
    var legend = $("map-legend");
    legend.textContent = "";
    k.groups.forEach(function (g, i) {
      if (!g.length) return;
      var lo = g[0].value_display, hi = g[g.length - 1].value_display;
      legend.appendChild(el("span", { class: "legend-item" }, [
        el("span", { class: "swatch", "aria-hidden": "true" }, [legendSwatch("c" + (i + 1), false)]),
        (lo === hi ? lo : lo + " to " + hi) + " (" + g.length + ")"
      ]));
    });
    var nLow = data.districts.filter(function (r) { return r.low_reliability; }).length;
    legend.appendChild(el("span", { class: "legend-item" }, [
      el("span", { class: "swatch", "aria-hidden": "true" }, [legendSwatch("c1", true)]),
      "Hatched: low reliability, " + data.low_reliability_rule + " (" + nLow + ")"
    ]));
    var nd = (data.not_drawn.districts[data.chamber] || []);
    $("map-notes").textContent = "Darker shades mean higher values; classes hold about equal numbers of districts. " +
      (nd.length ? data.not_drawn.description + " (" + districtName(data.chamber, nd[0]) + " includes them.) " : "") +
      "Boundaries: " + data.boundaries.name + ", " + data.boundaries.vintage + ".";
    status("Showing " + label + " for " + data.districts.length + " " + CHAMBER[data.chamber] + " districts" +
      (group === "all" ? "" : " (" + shown + " in view)") + ".");
    renderTable();
  }

  function legendSwatch(cls, hatched) {
    var s = svg("svg", { viewBox: "0 0 20 12", "aria-hidden": "true" });
    s.appendChild(svg("rect", { x: 0, y: 0, width: 20, height: 12, class: cls }));
    if (hatched) s.appendChild(svg("rect", { x: 0, y: 0, width: 20, height: 12, fill: "url(#hatch-legend)" }));
    return s;
  }

  function renderTable() {
    var data = state.data, s = state.sort;
    var rows = data.districts.slice().sort(function (a, b) {
      var x = s.key === "district" ? a.district : a.value, y = s.key === "district" ? b.district : b.value;
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      return (x - y) * (s.dir === "ascending" ? 1 : -1) || a.district - b.district;
    });
    var tb = $("map-table").querySelector("tbody");
    tb.textContent = "";
    rows.forEach(function (r) {
      var tr = el("tr", r.low_reliability ? { class: "flag" } : {}, [
        el("td", { text: districtName(data.chamber, r.district) }),
        el("td", { text: r.value_display || "no estimate" }),
        el("td", { text: r.moe_display || "n/a" }),
        el("td", {}, [r.low_reliability
          ? el("span", { class: "lr-label", text: "⚠︎ Low (MOE " + r.moe_percent_of_estimate + "% of estimate)" })
          : document.createTextNode(r.moe_percent_of_estimate !== null ? "MOE " + r.moe_percent_of_estimate + "% of estimate" : "n/a")]),
        el("td", {}, [el("a", { href: askUrl(data.chamber, r.district, data.metric) },
          ["Ask", el("span", { class: "visually-hidden", text: " about " + districtName(data.chamber, r.district) })])])
      ]);
      tb.appendChild(tr);
    });
    $("map-table-caption").textContent = phrase(data.metric) + " by " + CHAMBER[data.chamber] + " district, sorted by " +
      (s.key === "district" ? "district number" : "estimate") + " (" + s.dir + ")";
    Array.prototype.forEach.call($("map-table").querySelectorAll("th[aria-sort]"), function (th) {
      var key = th.querySelector("button").getAttribute("data-sort");
      th.setAttribute("aria-sort", key === s.key ? s.dir : "none");
    });
    $("map-source").textContent = "Source: " + data.citation + ".";
  }

  function load() {
    var ch = $("map-chamber").value, metric = $("map-metric").value || "poverty_rate";
    status("Loading map…");
    fetch("/api/map?chamber=" + encodeURIComponent(ch) + "&metric=" + encodeURIComponent(metric))
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok) { status("The map couldn't be loaded: " + (res.j.error || "error") + "."); return; }
        state.data = res.j;
        render();
      })
      .catch(function () { status("The map couldn't be loaded. Check your connection and try again."); });
  }

  function init() {
    fetch("/api/meta").then(function (r) { return r.json(); }).then(function (m) {
      var sel = $("map-metric");
      m.metrics.forEach(function (x) {
        var p = phrase(x.key);
        sel.appendChild(el("option", { value: x.key, text: p.charAt(0).toUpperCase() + p.slice(1) }));
      });
      sel.value = "poverty_rate";
      load();
    }).catch(function () { status("The list of measures couldn't be loaded."); });
    ["map-chamber", "map-metric"].forEach(function (id) { $(id).addEventListener("change", load); });
    $("map-island").addEventListener("change", function () { if (state.data) render(); });
    $("map-controls").addEventListener("submit", function (e) { e.preventDefault(); });
    Array.prototype.forEach.call(document.querySelectorAll("#map-table th button"), function (b) {
      b.addEventListener("click", function () {
        var key = b.getAttribute("data-sort");
        state.sort = { key: key, dir: state.sort.key === key && state.sort.dir === "ascending" ? "descending" : "ascending" };
        if (state.data) renderTable();
      });
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
