// LoRa APRS Live Map frontend. No build toolchain, no framework.
(function () {
  "use strict";

  // ---- Map init -----------------------------------------------------------
  var map = L.map("map").setView([-7.70776, 110.41006], 10); // YG2UFH-10 iGate, Yogyakarta

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);

  // state: Map<callsign, {marker, polyline, visible, lastData}>
  var stations = new Map();
  var mapCentered = false;
  var focusedCallsign = null; // currently searched/focused callsign

  // ---- Symbol icon lookup (minimal, generic fallback for unknown pairs) ---
  var ICON_TABLE = {
    "/>": { glyph: "\uD83D\uDE97", color: "#2a7de1" },
    "/-": { glyph: "\uD83C\uDFE0", color: "#2a7de1" },
    "/j": { glyph: "\uD83D\uDE99", color: "#2a7de1" },
    "/k": { glyph: "\uD83D\uDE9A", color: "#2a7de1" },
    "/b": { glyph: "\uD83D\uDEB2", color: "#2a7de1" },
    "/_": { glyph: "\u26C5", color: "#f0a500" },
    "/O": { glyph: "\uD83C\uDF88", color: "#f0a500" },
    "/#": { glyph: "\uD83D\uDCE1", color: "#8a2be2" },
    "/r": { glyph: "\uD83D\uDCE1", color: "#8a2be2" },
    "/[": { glyph: "\uD83D\uDEB6", color: "#2a7de1" },
    "/Y": { glyph: "\u26F5", color: "#2a7de1" },
    "\\_": { glyph: "\u26C5", color: "#1e6fd9" },
    "L_": { overlay: "L", color: "#1e6fd9" },
  };
  var FALLBACK_ICON = { glyph: null, color: "#2a7de1" };
  var OVERLAY_COLORS = { "_": "#1e6fd9", "&": "#2a2a2a", "#": "#8a2be2" };

  function overlayIconDef(symbol) {
    if (!symbol || symbol.length !== 2 || !/^[0-9A-Z]$/.test(symbol.charAt(0))) return null;
    return { overlay: symbol.charAt(0), color: OVERLAY_COLORS[symbol.charAt(1)] || "#2a7de1" };
  }

  function iconFor(symbol) {
    var def = (symbol && ICON_TABLE[symbol]) || overlayIconDef(symbol) || FALLBACK_ICON;
    if (def.overlay) {
      var el = document.createElement("div");
      el.className = "aprs-overlay-icon";
      el.style.background = def.color;
      el.textContent = def.overlay;
      return L.divIcon({ className: "aprs-marker", html: el, iconSize: [24, 24], iconAnchor: [12, 12] });
    }
    var inner = def.glyph
      ? '<div style="background:' + def.color + ';width:20px;height:20px;border-radius:50%;border:2px solid white;box-shadow:0 0 2px rgba(0,0,0,0.6);display:flex;align-items:center;justify-content:center;font-size:12px;line-height:1;">' + def.glyph + "</div>"
      : '<div style="background:' + def.color + ';width:14px;height:14px;border-radius:50%;border:2px solid white;box-shadow:0 0 2px rgba(0,0,0,0.6);"></div>';
    return L.divIcon({
      className: "aprs-marker",
      html: inner,
      iconSize: def.glyph ? [24, 24] : [18, 18],
      iconAnchor: def.glyph ? [12, 12] : [9, 9],
    });
  }

  // ---- Telemetry rendering --------------------------------------------------
  function renderTelemetry(telemetry) {
    if (telemetry === null || telemetry === undefined) {
      return "<div>&mdash;</div>";
    }
    if (Object.prototype.hasOwnProperty.call(telemetry, "raw_vals")) {
      var raw = telemetry.raw_vals.join(", ");
      return '<div class="popup-telemetry-row">raw telemetry (awaiting calibration): [' + raw + "]</div>";
    }
    var rows = [];
    for (var key in telemetry) {
      if (!Object.prototype.hasOwnProperty.call(telemetry, key)) continue;
      var entry = telemetry[key];
      rows.push(
        '<div class="popup-telemetry-row"><span>' +
          escapeHtml(key) +
          "</span><span>" +
          escapeHtml(String(entry.value)) +
          " " +
          escapeHtml(String(entry.unit)) +
          "</span></div>"
      );
    }
    return rows.join("");
  }

  // ---- Weather rendering ----------------------------------------------------
  var WEATHER_FIELDS = [
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

  function formatWeatherValue(value, digits) {
    return typeof value === "number" ? value.toFixed(digits) : String(value);
  }

  function buildWeatherSection(weather, callsign) {
    var section = document.createElement("div");
    section.className = "popup-weather";

    var title = document.createElement("div");
    title.className = "popup-section-title";
    title.textContent = "Weather";
    section.appendChild(title);

    var parts = [];
    var known = {};
    WEATHER_FIELDS.forEach(function (field) {
      known[field.key] = true;
      if (!Object.prototype.hasOwnProperty.call(weather, field.key)) return;
      var value = weather[field.key];
      if (value === null || value === undefined) return;
      parts.push(field.label + " " + formatWeatherValue(value, field.digits) + " " + field.unit);
    });
    Object.keys(weather).forEach(function (key) {
      if (known[key] || weather[key] === null || weather[key] === undefined) return;
      parts.push(key + ": " + String(weather[key]));
    });

    var line = document.createElement("div");
    line.textContent = parts.length > 0 ? parts.join(" \u00b7 ") : "\u2014";
    section.appendChild(line);

    var link = document.createElement("a");
    link.className = "popup-weather-link";
    link.href = "/weather/a/" + encodeURIComponent(callsign);
    link.textContent = "Show weather charts";
    section.appendChild(link);
    return section;
  }

  function escapeHtml(str) {
    var div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  // ---- Popup content ---------------------------------------------------
  function buildPopupContent(data) {
    var container = document.createElement("div");

    var header = document.createElement("div");
    header.innerHTML = "<strong>" + escapeHtml(data.callsign) + "</strong>";
    container.appendChild(header);

    if (data.comment) {
      var commentDiv = document.createElement("div");
      commentDiv.className = "popup-comment";
      commentDiv.textContent = data.comment;
      container.appendChild(commentDiv);
    }

    var lastHeard = document.createElement("div");
    lastHeard.textContent = "Last heard: " + new Date(data.received_at).toLocaleString();
    container.appendChild(lastHeard);

    if (data.latitude !== null && data.longitude !== null) {
      var coords = document.createElement("div");
      coords.textContent =
        "Lat/Lon: " + data.latitude.toFixed(5) + ", " + data.longitude.toFixed(5);
      container.appendChild(coords);
    }

    var extras = [];
    if (data.course !== null && data.course !== undefined) extras.push("Course: " + data.course + "\u00b0");
    if (data.speed !== null && data.speed !== undefined) extras.push("Speed: " + data.speed + " km/h");
    if (data.altitude !== null && data.altitude !== undefined) extras.push("Altitude: " + data.altitude + " m");
    if (extras.length > 0) {
      var extrasDiv = document.createElement("div");
      extrasDiv.textContent = extras.join(" | ");
      container.appendChild(extrasDiv);
    }

    if (data.weather) {
      container.appendChild(buildWeatherSection(data.weather, data.callsign));
    }

    var telemetryDiv = document.createElement("div");
    telemetryDiv.innerHTML = renderTelemetry(data.telemetry);
    container.appendChild(telemetryDiv);

    var toggleLabel = document.createElement("label");
    var toggleCheckbox = document.createElement("input");
    toggleCheckbox.type = "checkbox";
    var entry = stations.get(data.callsign);
    toggleCheckbox.checked = entry ? entry.visible : false;
    toggleCheckbox.addEventListener("change", function () {
      toggleTrack(data.callsign, toggleCheckbox.checked);
    });
    toggleLabel.appendChild(toggleCheckbox);
    toggleLabel.appendChild(document.createTextNode(" Show track"));
    container.appendChild(toggleLabel);

    var pre = document.createElement("pre");
    pre.className = "popup-raw-packet";
    pre.textContent = data.raw_packet || "";
    container.appendChild(pre);

    return container;
  }

  // ---- Time-range filter ------------------------------------------------
  function getTimeRangeMinutes() {
    var sel = document.getElementById("time-range");
    return sel ? parseInt(sel.value, 10) : 0;
  }

  function isStationInTimeRange(entry) {
    var minutes = getTimeRangeMinutes();
    if (minutes === 0) return true; // "All" — no filtering
    if (!entry.lastData || !entry.lastData.received_at) return false;
    var receivedTime = new Date(entry.lastData.received_at).getTime();
    var cutoff = Date.now() - minutes * 60 * 1000;
    return receivedTime >= cutoff;
  }

  function applyTimeRangeFilter() {
    stations.forEach(function (entry) {
      if (!entry.marker) return;
      if (isStationInTimeRange(entry)) {
        if (!map.hasLayer(entry.marker)) {
          entry.marker.addTo(map);
        }
      } else {
        if (map.hasLayer(entry.marker)) {
          map.removeLayer(entry.marker);
        }
      }
    });
    renderSidebar();
  }

  // ---- Marker / station state management --------------------------------
  function upsertStation(data) {
    var entry = stations.get(data.callsign);
    if (!entry) {
      entry = { marker: null, polyline: null, visible: false, lastData: null };
      stations.set(data.callsign, entry);
    }
    entry.lastData = data;

    if (data.latitude !== null && data.longitude !== null && data.latitude !== undefined && data.longitude !== undefined) {
      var latlng = [data.latitude, data.longitude];
      if (!entry.marker) {
        entry.marker = L.marker(latlng, { icon: iconFor(data.symbol) });
        // Only add to map if it passes the time range filter
        if (isStationInTimeRange(entry)) {
          entry.marker.addTo(map);
        }
      } else {
        entry.marker.setLatLng(latlng);
      }
      entry.marker.bindPopup(function () {
        return buildPopupContent(entry.lastData);
      });

      if (!mapCentered) {
        map.setView(latlng, 10);
        mapCentered = true;
      }
    }

    renderSidebar();
  }

  function toggleTrack(callsign, show) {
    var entry = stations.get(callsign);
    if (!entry) return;
    entry.visible = show;

    if (show) {
      fetchHistory(callsign).then(function (points) {
        var latlngs = points
          .filter(function (p) { return p.latitude !== null && p.longitude !== null; })
          .map(function (p) { return [p.latitude, p.longitude]; });
        entry.polyline = L.polyline(latlngs, { color: "#2a7de1" }).addTo(map);
      });
    } else if (entry.polyline) {
      entry.polyline.remove();
      entry.polyline = null;
    }
    renderSidebar();
  }

  function fetchHistory(callsign) {
    return fetch("/api/stations/" + encodeURIComponent(callsign) + "/history")
      .then(function (res) { return res.json(); });
  }

  // ---- Sidebar: weather link helper -------------------------------------
  function updateWeatherLink() {
    var linkEl = document.getElementById("link-weather");
    if (!linkEl) return;
    if (focusedCallsign && stations.has(focusedCallsign)) {
      linkEl.href = "/weather/a/" + encodeURIComponent(focusedCallsign);
      linkEl.classList.remove("sidebar-link-disabled");
      linkEl.textContent = "Weather charts — " + focusedCallsign;
    } else {
      linkEl.href = "#";
      linkEl.classList.add("sidebar-link-disabled");
      linkEl.textContent = "Weather charts";
    }
  }

  // ---- Sidebar: station list --------------------------------------------
  function renderSidebar() {
    var filterValue = document.getElementById("station-filter").value.trim().toLowerCase();
    var list = document.getElementById("station-list");
    list.innerHTML = "";

    var callsigns = Array.from(stations.keys()).sort();
    var visibleCount = 0;
    var rangeMinutes = getTimeRangeMinutes();

    callsigns.forEach(function (callsign) {
      if (filterValue && callsign.toLowerCase().indexOf(filterValue) === -1) return;
      var entry = stations.get(callsign);
      // Respect time range filter in the list too
      if (rangeMinutes > 0 && !isStationInTimeRange(entry)) return;
      visibleCount++;

      var li = document.createElement("li");

      var left = document.createElement("div");
      var callsignSpan = document.createElement("span");
      callsignSpan.className = "callsign";
      callsignSpan.textContent = callsign;
      left.appendChild(callsignSpan);

      var lastHeardSpan = document.createElement("div");
      lastHeardSpan.className = "last-heard";
      if (entry.lastData && entry.lastData.received_at) {
        lastHeardSpan.textContent = new Date(entry.lastData.received_at).toLocaleString();
      }
      left.appendChild(lastHeardSpan);
      li.appendChild(left);

      var label = document.createElement("label");
      var checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = entry.visible;
      checkbox.addEventListener("change", function () {
        toggleTrack(callsign, checkbox.checked);
      });
      label.appendChild(checkbox);
      label.appendChild(document.createTextNode(" track"));
      li.appendChild(label);

      li.addEventListener("click", function (evt) {
        if (evt.target === checkbox) return;
        if (entry.marker) {
          map.setView(entry.marker.getLatLng(), map.getZoom());
          entry.marker.openPopup();
          focusedCallsign = callsign;
          updateWeatherLink();
        }
      });

      list.appendChild(li);
    });

    if (visibleCount === 0) {
      var emptyLi = document.createElement("li");
      emptyLi.className = "empty-row";
      emptyLi.textContent = callsigns.length === 0 ? "no stations heard yet" : "no matches";
      list.appendChild(emptyLi);
    }
  }

  document.getElementById("station-filter").addEventListener("input", renderSidebar);

  // ---- Time-range change handler ----------------------------------------
  document.getElementById("time-range").addEventListener("change", applyTimeRangeFilter);

  // ---- Search callsign --------------------------------------------------
  function doCallsignSearch() {
    var input = document.getElementById("callsign-search");
    var msgEl = document.getElementById("callsign-search-msg");
    var query = input.value.trim().toUpperCase();
    msgEl.textContent = "";

    if (!query) return;

    // Look for an exact match first, then try a prefix/substring match
    var entry = stations.get(query);
    if (!entry) {
      stations.forEach(function (e, key) {
        if (!entry && key.toUpperCase().indexOf(query) !== -1) {
          entry = e;
          query = key; // use the actual key for focusing
        }
      });
    }

    if (entry && entry.marker) {
      map.setView(entry.marker.getLatLng(), 13);
      entry.marker.openPopup();
      focusedCallsign = query;
      updateWeatherLink();
    } else {
      msgEl.textContent = "Callsign not found in loaded stations.";
    }
  }

  document.getElementById("callsign-search-btn").addEventListener("click", doCallsignSearch);
  document.getElementById("callsign-search").addEventListener("keydown", function (evt) {
    if (evt.key === "Enter") {
      evt.preventDefault();
      doCallsignSearch();
    }
  });

  // ---- WebSocket lifecycle -------------------------------------------------
  var ws = null;
  var buffering = true;
  var messageBuffer = [];
  var backoffMs = 1000;
  var MAX_BACKOFF_MS = 30000;

  function wsUrl() {
    var proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    return proto + "//" + window.location.host + "/ws/live";
  }

  function handleMessage(payload) {
    if (payload.type === "position") {
      upsertStation(payload);
    }
  }

  function connect() {
    buffering = true;
    messageBuffer = [];

    ws = new WebSocket(wsUrl());

    ws.addEventListener("message", function (event) {
      var payload = JSON.parse(event.data);
      if (buffering) {
        messageBuffer.push(payload);
      } else {
        handleMessage(payload);
      }
    });

    ws.addEventListener("open", function () {
      backoffMs = 1000;
      fetch("/api/stations")
        .then(function (res) { return res.json(); })
        .then(function (initialStations) {
          initialStations.forEach(function (station) {
            upsertStation(
              Object.assign({ type: "position" }, station)
            );
          });

          var buffered = messageBuffer;
          messageBuffer = [];
          buffering = false;
          buffered.forEach(handleMessage);
        })
        .catch(function (err) {
          console.error("initial /api/stations fetch failed", err);
          buffering = false;
        });
    });

    ws.addEventListener("close", function () {
      scheduleReconnect();
    });

    ws.addEventListener("error", function () {
      // "close" fires after "error" for a WebSocket; reconnection is
      // handled there to avoid double-scheduling.
    });
  }

  function scheduleReconnect() {
    setTimeout(function () {
      connect();
    }, backoffMs);
    backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF_MS);
  }

  connect();
  renderSidebar();
})();
