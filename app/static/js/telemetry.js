// Per-station telemetry charts page (/telemetry/a/<callsign>), modelled on the
// weather charts page. One line chart per analog channel that has data in the
// selected range (the Raspberry Pi's CPUTemp is the headline channel, alongside
// Vin, CPULoad, MemUsed, Uptime). Channel names/units come from the station's
// EQNS/UNIT/PARM metadata, with generic Analog1..Analog5 fallback when a station
// has not sent calibration yet. Packet-derived text is rendered with textContent.
(function () {
  "use strict";

  // Distinct colours per channel index (0-4), reused across range reloads.
  var CHANNEL_COLORS = ["#d9480f", "#1971c2", "#2b8a3e", "#5f3dc4", "#e8590c"];

  var RANGE_LABELS = { 24: "24 hours", 48: "48 hours", 168: "7 days", 720: "30 days" };
  // Tick spacing per range, in hours (aligned to local time).
  var TICK_STEP_HOURS = { 24: 3, 48: 6, 168: 24, 720: 120 };

  var callsign = callsignFromPath();
  var hours = 48;
  var channels = defaultChannels(); // [{name, unit}] x5, from the API metadata
  var points = []; // telemetry points for the loaded range, oldest first
  var station = null; // entry from GET /api/stations, if known
  var charts = [];
  var loading = false;
  var liveBuffer = []; // live points that arrive while a range is loading
  var requestSeq = 0;

  function defaultChannels() {
    return [
      { name: "Analog1", unit: "" },
      { name: "Analog2", unit: "" },
      { name: "Analog3", unit: "" },
      { name: "Analog4", unit: "" },
      { name: "Analog5", unit: "" },
    ];
  }

  function callsignFromPath() {
    var m = window.location.pathname.match(/^\/telemetry\/a\/([^\/]+)\/?$/);
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

  function channelLabel(ch) {
    return ch.unit ? ch.name + " (" + ch.unit + ")" : ch.name;
  }

  function formatValue(ch, value) {
    // Telemetry channels have no fixed precision; show up to 2 decimals,
    // trimming trailing zeros, then append the unit when present.
    var text = Math.abs(value) >= 1000 ? value.toFixed(0) : trimZeros(value.toFixed(2));
    return ch.unit ? text + " " + ch.unit : text;
  }

  function trimZeros(s) {
    return s.indexOf(".") === -1 ? s : s.replace(/\.?0+$/, "");
  }

  function timeOf(p) {
    return Date.parse(p.received_at);
  }

  function setStatus(text) {
    document.getElementById("tlm-status").textContent = text;
  }

  // Convert a live WebSocket telemetry object into {name: value|null} keyed by
  // the current channel names. Handles both the named shape
  // ({name: {value, unit}}) and the raw fallback ({raw_seq, raw_vals}).
  function telemetryToChannels(telemetry) {
    var out = {};
    channels.forEach(function (ch) { out[ch.name] = null; });
    if (!telemetry || typeof telemetry !== "object") return out;

    if (Object.prototype.hasOwnProperty.call(telemetry, "raw_vals")) {
      var vals = telemetry.raw_vals;
      if (Array.isArray(vals)) {
        channels.forEach(function (ch, i) {
          out[ch.name] = isNumber(vals[i]) ? vals[i] : null;
        });
      }
      return out;
    }
    channels.forEach(function (ch) {
      var entry = telemetry[ch.name];
      if (entry && typeof entry === "object" && isNumber(entry.value)) {
        out[ch.name] = entry.value;
      }
    });
    return out;
  }

  // ---- Header + latest readings -------------------------------------------
  function latestPoint() {
    if (points.length > 0) return points[points.length - 1];
    return null;
  }

  function renderHeader() {
    document.getElementById("tlm-callsign").textContent = callsign ? callsign + " telemetry" : "Telemetry";
    document.title = (callsign ? callsign + " " : "") + "telemetry charts";
    document.getElementById("tlm-comment").textContent = (station && station.comment) || "";

    var last = latestPoint();
    var lastEl = document.getElementById("tlm-last-report");
    if (last) {
      lastEl.textContent = "Last telemetry: " + new Date(last.received_at).toLocaleString();
    } else if (station) {
      lastEl.textContent = "Last heard: " + new Date(station.received_at).toLocaleString();
    } else {
      lastEl.textContent = "";
    }
  }

  function renderCurrent() {
    var container = document.getElementById("tlm-current");
    container.textContent = "";
    var last = latestPoint();
    var dl = document.createElement("dl");
    dl.className = "wx-current-grid";

    if (last) {
      channels.forEach(function (ch) {
        var v = last.channels[ch.name];
        if (!isNumber(v)) return;
        var item = document.createElement("div");
        var dt = document.createElement("dt");
        dt.textContent = ch.name;
        var dd = document.createElement("dd");
        dd.textContent = formatValue(ch, v);
        item.appendChild(dt);
        item.appendChild(dd);
        dl.appendChild(item);
      });
    }

    if (dl.childNodes.length === 0) {
      var p = document.createElement("p");
      p.textContent = "No telemetry received from this station yet.";
      container.appendChild(p);
    } else {
      container.appendChild(dl);
    }
  }

  // ---- Min / max / latest table -------------------------------------------
  function renderStats(visible) {
    var table = document.getElementById("tlm-stats");
    var tbody = table.querySelector("tbody");
    tbody.textContent = "";

    channels.forEach(function (ch) {
      var values = visible.map(function (p) { return p.channels[ch.name]; }).filter(isNumber);
      if (values.length === 0) return;
      var row = document.createElement("tr");
      var cells = [
        channelLabel(ch),
        formatValue(ch, Math.min.apply(null, values)),
        formatValue(ch, Math.max.apply(null, values)),
        formatValue(ch, values[values.length - 1]),
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
  // so no Chart.js date adapter is needed (same approach as weather.js).
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

  function renderCharts(visible, xMin, xMax) {
    charts.forEach(function (c) { c.destroy(); });
    charts = [];
    var container = document.getElementById("tlm-charts");
    container.textContent = "";
    if (typeof Chart === "undefined") {
      if (visible.length > 0) setStatus("Charts could not be loaded (Chart.js unavailable).");
      return;
    }

    channels.forEach(function (ch, idx) {
      var data = visible
        .filter(function (p) { return isNumber(p.channels[ch.name]); })
        .map(function (p) { return { x: timeOf(p), y: p.channels[ch.name] }; });
      if (data.length === 0) return;

      var color = CHANNEL_COLORS[idx % CHANNEL_COLORS.length];
      var card = document.createElement("section");
      card.className = "wx-card";
      var h2 = document.createElement("h2");
      h2.textContent = channelLabel(ch);
      card.appendChild(h2);
      var box = document.createElement("div");
      box.className = "wx-chart-box";
      var canvas = document.createElement("canvas");
      canvas.setAttribute("role", "img");
      canvas.setAttribute(
        "aria-label",
        channelLabel(ch) + " for " + callsign + ", last " + RANGE_LABELS[hours] + "; see the min/max/latest table for values"
      );
      box.appendChild(canvas);
      card.appendChild(box);
      container.appendChild(card);

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
          title: { display: !!ch.unit, text: ch.unit },
        },
      };

      charts.push(
        new Chart(canvas, {
          type: "line",
          data: {
            datasets: [
              {
                label: channelLabel(ch),
                data: data,
                borderColor: color,
                backgroundColor: color,
                borderWidth: 1.5,
                showLine: true,
                pointRadius: data.length < 50 ? 3 : 1,
                tension: 0,
              },
            ],
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            interaction: { mode: "nearest", axis: "x", intersect: false },
            scales: scales,
            plugins: {
              legend: { display: false },
              tooltip: {
                callbacks: {
                  title: function (items) {
                    return items.length ? new Date(items[0].parsed.x).toLocaleString() : "";
                  },
                  label: function (ctx) {
                    return channelLabel(ch) + ": " + formatValue(ch, ctx.parsed.y);
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

    var hasAny = visible.some(function (p) {
      return channels.some(function (ch) { return isNumber(p.channels[ch.name]); });
    });

    if (!callsign) {
      setStatus("No callsign in the URL. Use /telemetry/a/<callsign>.");
    } else if (!hasAny) {
      setStatus("No telemetry in this range (last " + RANGE_LABELS[hours] + ").");
    } else {
      setStatus(visible.length + " telemetry sample" + (visible.length === 1 ? "" : "s") + " in the last " + RANGE_LABELS[hours] + ".");
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

    fetch("/api/stations/" + encodeURIComponent(callsign) + "/telemetry?hours=" + h)
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (data) {
        if (seq !== requestSeq) return; // a newer range request superseded this one
        if (data && Array.isArray(data.channels) && data.channels.length === 5) {
          channels = data.channels;
        } else {
          channels = defaultChannels();
        }
        points = (data && data.points) || [];
        loading = false;
        var buffered = liveBuffer;
        liveBuffer = [];
        buffered.forEach(addPoint);
        render();
      })
      .catch(function (err) {
        if (seq !== requestSeq) return;
        loading = false;
        console.error("telemetry fetch failed", err);
        setStatus("Could not load telemetry data. Retrying when the live connection reconnects.");
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
      telemetry: payload.telemetry,
    });
    if (!payload.telemetry) {
      if (!loading) renderHeader();
      return;
    }
    var raw = payload.telemetry.raw_vals;
    var point = {
      received_at: payload.received_at,
      channels: telemetryToChannels(payload.telemetry),
      raw_vals: Array.isArray(raw) ? raw : null,
    };
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
      // After a reconnect, refetch so samples sent during the outage appear.
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
