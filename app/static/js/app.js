// LoRa APRS Live Map frontend. No build toolchain, no framework.
(function () {
  "use strict";

  // ---- Map init -----------------------------------------------------------
  var map = L.map("map").setView([-2.5, 118], 5); // fallback center: Indonesia

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);

  // state: Map<callsign, {marker, polyline, visible, lastData}>
  var stations = new Map();
  var mapCentered = false;

  // ---- Symbol icon lookup (minimal, generic fallback for unknown pairs) ---
  // symbol is a 2-char string: table char + code char (table char first, per
  // "Symbol handling" in design.md). No bundled icon image set in v1 -- known
  // (table, code) pairs map to a small built-in set of distinguishable
  // divIcon glyphs/colors; any pair not in this table falls back to the
  // generic colored dot. Covers the symbols most relevant to LoRa
  // APRS/iGate deployments (mobile trackers, fixed stations, digipeaters,
  // weather, balloons); not an exhaustive APRS symbol table.
  var ICON_TABLE = {
    "/>": { glyph: "\uD83D\uDE97", color: "#2a7de1" }, // car
    "/-": { glyph: "\uD83C\uDFE0", color: "#2a7de1" }, // house (fixed station)
    "/j": { glyph: "\uD83D\uDE99", color: "#2a7de1" }, // jeep
    "/k": { glyph: "\uD83D\uDE9A", color: "#2a7de1" }, // truck
    "/b": { glyph: "\uD83D\uDEB2", color: "#2a7de1" }, // bike
    "/_": { glyph: "\u26C5", color: "#f0a500" }, // weather station
    "/O": { glyph: "\uD83C\uDF88", color: "#f0a500" }, // balloon
    "/#": { glyph: "\uD83D\uDCE1", color: "#8a2be2" }, // digipeater
    "/r": { glyph: "\uD83D\uDCE1", color: "#8a2be2" }, // repeater
    "/[": { glyph: "\uD83D\uDEB6", color: "#2a7de1" }, // person
    "/Y": { glyph: "\u26F5", color: "#2a7de1" }, // yacht/boat
  };
  var FALLBACK_ICON = { glyph: null, color: "#2a7de1" };

  function iconFor(symbol) {
    var def = (symbol && ICON_TABLE[symbol]) || FALLBACK_ICON;
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

    if (data.comment) {
      var commentDiv = document.createElement("div");
      commentDiv.textContent = "Comment: " + data.comment;
      container.appendChild(commentDiv);
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
        entry.marker = L.marker(latlng, { icon: iconFor(data.symbol) }).addTo(map);
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

  // ---- Sidebar ------------------------------------------------------------
  function renderSidebar() {
    var filterValue = document.getElementById("station-filter").value.trim().toLowerCase();
    var list = document.getElementById("station-list");
    list.innerHTML = "";

    var callsigns = Array.from(stations.keys()).sort();
    var visibleCount = 0;

    callsigns.forEach(function (callsign) {
      if (filterValue && callsign.toLowerCase().indexOf(filterValue) === -1) return;
      visibleCount++;

      var entry = stations.get(callsign);
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

  // ---- WebSocket lifecycle -------------------------------------------------
  // Exact sequence: open WS first -> buffer messages until the initial
  // GET /api/stations resolves -> apply REST snapshot -> flush buffer in
  // order -> switch to immediate live updates. On every reconnect (not just
  // the first), re-run this whole sequence so an outage longer than the
  // backoff window cannot leave stale data with no recovery path.
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
    // Future message types (e.g. "station_removed") can be added here
    // without a breaking contract change.
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
      backoffMs = 1000; // reset backoff on a successful connection
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
