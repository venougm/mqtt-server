// Per-station weather charts page (/weather/a/<callsign>), modelled on
// aprs.fi's weather page. Packet-derived text is rendered with textContent only.
(function () {
  "use strict";

  // aprslib already converts weather to metric (°C, mbar, m/s, mm).
  var FIELDS = [
    { key: "temperature", label: "Temperature", unit: "\u00b0C", digits: 1 },
    { key: "humidity", label: "Humidity", unit: "%", digits: 0 },
    { key: "pressure", label: "Pressure", unit: "mbar", digits: 1 },
    { key: "wind_direction", label: "Wind direction", unit: "\u00b0", digits: 0 },
    { key: "wind_speed", label: "Wind speed", unit: "m/s", digits: 1 },
    { key: "wind_gust", label: "Wind gust", unit: "m/s", digits: 1 },
    { key: "rain_1h", label: "Rain 1h", unit: "mm", digits: 1 },
    { key: "rain_24h", label: "Rain 24h", unit: "mm", digits: 1 },
    { key: "rain_since_midnight", label: "Rain since midnight", unit: "mm", digits: 1 },
    { key: "luminosity", label: "Luminosity", unit: "W/m\u00b2", digits: 0 },
  ];
  var FIELD_BY_KEY = {};
  FIELDS.forEach(function (f) { FIELD_BY_KEY[f.key] = f; });

  // One chart per entry; a chart is only drawn if at least one of its series
  // has a value in the selected range. `axis: "y1"` puts a series on a
  // secondary 0-360° axis, drawn as unconnected points.
  var CHARTS = [
    { title: "Temperature (\u00b0C)", unit: "\u00b0C", series: [{ key: "temperature", color: "#d9480f" }] },
    { title: "Humidity (%)", unit: "%", yMin: 0, yMax: 100, series: [{ key: "humidity", color: "#1971c2" }] },
    { title: "Pressure (mbar)", unit: "mbar", series: [{ key: "pressure", color: "#5f3dc4" }] },
    {
      title: "Wind (m/s, direction \u00b0)",
      unit: "m/s",
      yMin: 0,
      series: [
        { key: "wind_speed", color: "#2b8a3e" },
        { key: "wind_gust", color: "#e67700" },
        { key: "wind_direction", color: "#495057", axis: "y1" },
      ],
    },
    {
      title: "Rain (mm)",
      unit: "mm",
      yMin: 0,
      series: [
        { key: "rain_1h", color: "#1c7ed6" },
        { key: "rain_24h", color: "#0b7285" },
        { key: "rain_since_midnight", color: "#748ffc" },
      ],
    },
    { title: "Luminosity (W/m\u00b2)", unit: "W/m\u00b2", yMin: 0, series: [{ key: "luminosity", color: "#f59f00" }] },
  ];

  var RANGE_LABELS = { 24: "24 hours", 48: "48 hours", 168: "7 days", 720: "30 days" };
  // Tick spacing per range, in hours (aligned to local time).
  var TICK_STEP_HOURS = { 24: 3, 48: 6, 168: 24, 720: 120 };

  var callsign = callsignFromPath();
  var hours = 48;
  var points = []; // weather points for the loaded range, oldest first
  var station = null; // entry from GET /api/stations, if known
  var charts = [];
  var loading = false;
  var liveBuffer = []; // live points that arrive while a range is loading
  var requestSeq = 0;

  function callsignFromPath() {
    var m = window.location.pathname.match(/^\/weather\/a\/([^\/]+)\/?$/);
    if (!m) return "";
    try {
      return decodeURIComponent(m[1]);
    } catch (e) {
      return m[1];
    }
  }

  function isNumber(v) {
    return typeof v === "number" && isFinite(v);
  }

  function formatValue(key, value) {
    var f = FIELD_BY_KEY[key];
    return value.toFixed(f.digits) + " " + f.unit;
  }

  function toPoint(receivedAt, weather) {
    var p = { received_at: receivedAt };
    FIELDS.forEach(function (f) {
      p[f.key] = isNumber(weather[f.key]) ? weather[f.key] : null;
    });
    return p;
  }

  function timeOf(p) {
    return Date.parse(p.received_at);
  }

  function setStatus(text) {
    document.getElementById("wx-status").textContent = text;
  }

  // ---- Header + current conditions --------------------------------------
  function latestReport() {
    if (points.length > 0) {
      var last = points[points.length - 1];
      return { received_at: last.received_at, values: last };
    }
    if (station && station.weather) {
      return { received_at: station.received_at, values: toPoint(station.received_at, station.weather) };
    }
    return null;
  }

  function renderHeader() {
    document.getElementById("wx-callsign").textContent = callsign ? callsign + " weather" : "Weather";
    document.title = (callsign ? callsign + " " : "") + "weather charts";
    document.getElementById("wx-comment").textContent = (station && station.comment) || "";

    var report = latestReport();
    var lastEl = document.getElementById("wx-last-report");
    if (report) {
      lastEl.textContent = "Last weather report: " + new Date(report.received_at).toLocaleString();
    } else if (station) {
      lastEl.textContent = "Last heard: " + new Date(station.received_at).toLocaleString();
    } else {
      lastEl.textContent = "";
    }
  }

  function renderCurrent() {
    var container = document.getElementById("wx-current");
    container.textContent = "";
    var report = latestReport();
    var dl = document.createElement("dl");
    dl.className = "wx-current-grid";

    if (report) {
      FIELDS.forEach(function (f) {
        var v = report.values[f.key];
        if (v === null) return;
        var item = document.createElement("div");
        var dt = document.createElement("dt");
        dt.textContent = f.label;
        var dd = document.createElement("dd");
        dd.textContent = formatValue(f.key, v);
        item.appendChild(dt);
        item.appendChild(dd);
        dl.appendChild(item);
      });
    }

    if (dl.childNodes.length === 0) {
      var p = document.createElement("p");
      p.textContent = "No weather reports received from this station yet.";
      container.appendChild(p);
    } else {
      container.appendChild(dl);
    }
  }

  // ---- Min / max / latest table -------------------------------------------
  function renderStats(visible) {
    var table = document.getElementById("wx-stats");
    var tbody = table.querySelector("tbody");
    tbody.textContent = "";

    FIELDS.forEach(function (f) {
      var values = visible.map(function (p) { return p[f.key]; }).filter(isNumber);
      if (values.length === 0) return;
      var row = document.createElement("tr");
      var cells = [
        f.label,
        formatValue(f.key, Math.min.apply(null, values)),
        formatValue(f.key, Math.max.apply(null, values)),
        formatValue(f.key, values[values.length - 1]),
      ];
      cells.forEach(function (text, i) {
        var cell = document.createElement(i === 0 ? "th" : "td");
        if (i === 0) cell.scope = "row";
        cell.textContent = text;
        row.appendChild(cell);
      });
      tbody.appendChild(row);
    });

    table.hidden = tbody.childNodes.length === 0;
  }

  // ---- Charts ---------------------------------------------------------------
  // epoch-ms on a linear x axis; ticks are generated on local-time boundaries
  // so no Chart.js date adapter is needed.
  function localTicks(min, max, stepHours) {
    var ticks = [];
    var d = new Date(min);
    d.setMinutes(0, 0, 0);
    if (stepHours >= 24) {
      d.setHours(0);
      var stepDays = stepHours / 24;
      for (var i = 0; d.getTime() <= max && i < 400; i++) {
        if (d.getTime() >= min && i % stepDays === 0) ticks.push({ value: d.getTime() });
        d.setDate(d.getDate() + 1);
      }
    } else {
      for (var j = 0; d.getTime() <= max && j < 1000; j++) {
        if (d.getTime() >= min && d.getHours() % stepHours === 0) ticks.push({ value: d.getTime() });
        d.setHours(d.getHours() + 1);
      }
    }
    return ticks;
  }

  function formatTick(value) {
    var d = new Date(value);
    if (d.getHours() === 0 && d.getMinutes() === 0) {
      return d.toLocaleDateString([], { day: "numeric", month: "short" });
    }
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function buildDatasets(def, visible) {
    var datasets = [];
    def.series.forEach(function (s) {
      var data = visible
        .filter(function (p) { return p[s.key] !== null; })
        .map(function (p) { return { x: timeOf(p), y: p[s.key] }; });
      if (data.length === 0) return;
      var field = FIELD_BY_KEY[s.key];
      var isDirection = s.axis === "y1";
      datasets.push({
        label: field.label + " (" + field.unit + ")",
        fieldKey: s.key,
        data: data,
        yAxisID: isDirection ? "y1" : "y",
        borderColor: s.color,
        backgroundColor: s.color,
        borderWidth: 1.5,
        showLine: !isDirection,
        pointRadius: isDirection || data.length < 50 ? 3 : 1,
        tension: 0,
      });
    });
    return datasets;
  }

  function renderCharts(visible, xMin, xMax) {
    charts.forEach(function (c) { c.destroy(); });
    charts = [];
    var container = document.getElementById("wx-charts");
    container.textContent = "";
    if (typeof Chart === "undefined") {
      if (visible.length > 0) setStatus("Charts could not be loaded (Chart.js unavailable).");
      return;
    }

    CHARTS.forEach(function (def) {
      var datasets = buildDatasets(def, visible);
      if (datasets.length === 0) return;

      var card = document.createElement("section");
      card.className = "wx-card";
      var h2 = document.createElement("h2");
      h2.textContent = def.title;
      card.appendChild(h2);
      var box = document.createElement("div");
      box.className = "wx-chart-box";
      var canvas = document.createElement("canvas");
      canvas.setAttribute("role", "img");
      canvas.setAttribute(
        "aria-label",
        def.title + " for " + callsign + ", last " + RANGE_LABELS[hours] + "; see the min/max/latest table for values"
      );
      box.appendChild(canvas);
      card.appendChild(box);
      container.appendChild(card);

      var hasDirection = datasets.some(function (d) { return d.yAxisID === "y1"; });
      var scales = {
        x: {
          type: "linear",
          min: xMin,
          max: xMax,
          afterBuildTicks: function (axis) {
            axis.ticks = localTicks(axis.min, axis.max, TICK_STEP_HOURS[hours]);
          },
          ticks: { callback: formatTick, maxRotation: 0, autoSkip: true },
        },
        y: {
          min: def.yMin,
          max: def.yMax,
          title: { display: true, text: def.unit },
        },
      };
      if (hasDirection) {
        scales.y1 = {
          position: "right",
          min: 0,
          max: 360,
          ticks: { stepSize: 90 },
          grid: { drawOnChartArea: false },
          title: { display: true, text: "direction \u00b0" },
        };
      }

      charts.push(
        new Chart(canvas, {
          type: "line",
          data: { datasets: datasets },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            interaction: { mode: "nearest", axis: "x", intersect: false },
            scales: scales,
            plugins: {
              legend: { display: datasets.length > 1 },
              tooltip: {
                callbacks: {
                  title: function (items) {
                    return items.length ? new Date(items[0].parsed.x).toLocaleString() : "";
                  },
                  label: function (ctx) {
                    var field = FIELD_BY_KEY[ctx.dataset.fieldKey];
                    return field.label + ": " + formatValue(field.key, ctx.parsed.y);
                  },
                },
              },
            },
          },
        })
      );
    });
  }

  // ---- Render pipeline --------------------------------------------------------
  function render() {
    var xMax = Date.now();
    var xMin = xMax - hours * 3600 * 1000;
    var visible = points.filter(function (p) { return timeOf(p) >= xMin; });

    renderHeader();
    renderCurrent();
    renderStats(visible);

    if (!callsign) {
      setStatus("No callsign in the URL. Use /weather/a/<callsign>.");
    } else if (visible.length === 0) {
      setStatus("No weather reports in this range (last " + RANGE_LABELS[hours] + ").");
    } else {
      setStatus(visible.length + " weather report" + (visible.length === 1 ? "" : "s") + " in the last " + RANGE_LABELS[hours] + ".");
    }
    renderCharts(visible, xMin, xMax);
  }

  function addPoint(p) {
    var t = timeOf(p);
    for (var i = 0; i < points.length; i++) {
      if (points[i].received_at === p.received_at) return;
    }
    points.push(p);
    if (points.length > 1 && timeOf(points[points.length - 2]) > t) {
      points.sort(function (a, b) { return timeOf(a) - timeOf(b); });
    }
  }

  function updateRangeButtons() {
    var buttons = document.querySelectorAll(".wx-range button");
    Array.prototype.forEach.call(buttons, function (b) {
      b.setAttribute("aria-pressed", Number(b.getAttribute("data-hours")) === hours ? "true" : "false");
    });
  }

  function loadRange(h) {
    hours = h;
    updateRangeButtons();
    if (!callsign) {
      render();
      return;
    }

    var seq = ++requestSeq;
    loading = true;
    liveBuffer = [];
    setStatus("Loading\u2026");

    fetch("/api/stations/" + encodeURIComponent(callsign) + "/weather?hours=" + h)
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (data) {
        if (seq !== requestSeq) return; // a newer range request superseded this one
        points = data;
        loading = false;
        var buffered = liveBuffer;
        liveBuffer = [];
        buffered.forEach(addPoint);
        render();
      })
      .catch(function (err) {
        if (seq !== requestSeq) return;
        loading = false;
        console.error("weather fetch failed", err);
        setStatus("Could not load weather data. Retrying when the live connection reconnects.");
      });
  }

  function loadStation() {
    if (!callsign) return;
    fetch("/api/stations")
      .then(function (res) { return res.json(); })
      .then(function (list) {
        station = list.filter(function (s) { return s.callsign === callsign; })[0] || null;
        if (!loading) render();
        else renderHeader();
      })
      .catch(function (err) {
        console.error("station metadata fetch failed", err);
      });
  }

  // ---- Live updates (same reconnect/backoff approach as app.js) -------------
  var backoffMs = 1000;
  var MAX_BACKOFF_MS = 30000;
  var connectedOnce = false;

  function wsUrl() {
    var proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    return proto + "//" + window.location.host + "/ws/live";
  }

  function handleLive(payload) {
    if (payload.type !== "position" || payload.callsign !== callsign) return;
    station = Object.assign({}, station || {}, {
      callsign: payload.callsign,
      received_at: payload.received_at,
      comment: payload.comment,
      symbol: payload.symbol,
      weather: payload.weather,
    });
    if (!payload.weather) {
      if (!loading) renderHeader();
      return;
    }
    var point = toPoint(payload.received_at, payload.weather);
    if (loading) {
      liveBuffer.push(point);
      return;
    }
    addPoint(point);
    render();
  }

  function connect() {
    var ws = new WebSocket(wsUrl());

    ws.addEventListener("open", function () {
      backoffMs = 1000;
      // After a reconnect, refetch so reports sent during the outage appear.
      if (connectedOnce) {
        loadRange(hours);
        loadStation();
      }
      connectedOnce = true;
    });

    ws.addEventListener("message", function (event) {
      var payload;
      try {
        payload = JSON.parse(event.data);
      } catch (e) {
        return;
      }
      handleLive(payload);
    });

    ws.addEventListener("close", function () {
      scheduleReconnect();
    });
  }

  function scheduleReconnect() {
    setTimeout(connect, backoffMs);
    backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF_MS);
  }

  // ---- Init -------------------------------------------------------------------
  Array.prototype.forEach.call(document.querySelectorAll(".wx-range button"), function (b) {
    b.addEventListener("click", function () {
      loadRange(Number(b.getAttribute("data-hours")));
    });
  });

  renderHeader();
  loadRange(hours);
  loadStation();
  if (callsign) connect();
})();
