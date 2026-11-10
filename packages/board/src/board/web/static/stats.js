"use strict";

(() => {
  const SUMMARY_URL = "/stats/data";
  const PAGE_PREFIXES = { posts_per_principal_per_day: "posts", ticket_cycle_time_days: "tickets", files_live_bytes_per_uploader: "files" };
  const SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"];
  const OTHER_COLOR = "#898781";
  const TOKENS = getComputedStyle(document.documentElement);
  const SURFACE = TOKENS.getPropertyValue("--paper").trim();
  const INK = TOKENS.getPropertyValue("--muted").trim();
  const GRID = TOKENS.getPropertyValue("--line").trim();
  const BAR_THICKNESS = 24;
  const BYTE_UNITS = ["B", "kB", "MB", "GB", "TB"];

  const COUNT = { name: "Count", precision: 0, format: (value) => value.toLocaleString() };
  const DAY_SECONDS = 86400;
  const DURATION_UNITS = [
    { suffix: "s", seconds: 1 },
    { suffix: "min", seconds: 60 },
    { suffix: "h", seconds: 3600 },
    { suffix: "d", seconds: DAY_SECONDS },
  ];
  const BYTES = { name: "Bytes", precision: 0, format: formatBytes };

  const SPECS = {
    posts_per_principal_per_day: postsPerPrincipal,
    tickets_per_column: (counts) => singleSeries(Object.keys(counts), Object.values(counts), COUNT),
    ticket_cycle_time_days: (done) => ({
      ...durationSeries(
        done.map((ticket) => `#${ticket.ticket_id}`),
        done.map((ticket) => ticket.days * DAY_SECONDS),
        "Cycle time",
      ),
      titles: done.map((ticket) => `#${ticket.ticket_id} ${ticket.title}`),
    }),
    wake_latency_seconds: (latency) => durationSeries(["p50", "p95"], [latency.p50, latency.p95], "Latency"),
    top_mentioned: (ranked) => ({
      ...singleSeries(ranked.map((entry) => entry.principal), ranked.map((entry) => entry.mentions), COUNT),
      horizontal: true,
    }),
    files_live_bytes_per_uploader: (ranked) => ({
      ...singleSeries(ranked.map((entry) => entry.uploader), ranked.map((entry) => entry.bytes), BYTES),
      horizontal: true,
    }),
  };

  function singleSeries(labels, values, unit) {
    return { labels, unit, series: [{ label: unit.name, values, color: SERIES_COLORS[0] }] };
  }

  function durationSeries(labels, seconds, name) {
    const axis = durationUnit(Math.max(0, ...seconds.filter((value) => value !== null)));
    const values = seconds.map((value) => (value === null ? null : value / axis.seconds));
    return singleSeries(labels, values, {
      name,
      tick: (value) => `${threeSignificant(value)} ${axis.suffix}`,
      format: (value) => formatDuration(value * axis.seconds),
    });
  }

  function durationUnit(seconds) {
    return DURATION_UNITS.findLast((unit) => seconds >= unit.seconds) ?? DURATION_UNITS[0];
  }

  function formatDuration(seconds) {
    const unit = durationUnit(seconds);
    return `${threeSignificant(seconds / unit.seconds)} ${unit.suffix}`;
  }

  function threeSignificant(value) {
    return value.toLocaleString(undefined, { maximumSignificantDigits: 3 });
  }

  function postsPerPrincipal(byDay, page) {
    const days = Object.keys(byDay);
    const totals = new Map();
    for (const counts of Object.values(byDay)) {
      for (const [principal, posts] of Object.entries(counts)) {
        totals.set(principal, (totals.get(principal) ?? 0) + posts);
      }
    }
    const ranked = [...totals.keys()].sort((a, b) => totals.get(b) - totals.get(a));
    const keptCount = ranked.length > SERIES_COLORS.length ? SERIES_COLORS.length - 1 : ranked.length;
    const groups = ranked
      .slice(0, keptCount)
      .sort()
      .map((principal, slot) => ({ label: principal, members: [principal], color: SERIES_COLORS[slot] }));
    if (ranked.length > keptCount) {
      groups.push({ label: page.total > page.shown ? "Other on this page" : "Other", members: ranked.slice(keptCount), color: OTHER_COLOR });
    }
    const series = groups.map(({ label, members, color }) => ({
      label,
      color,
      values: days.map((day) => members.reduce((sum, member) => sum + (byDay[day][member] ?? 0), 0)),
    }));
    return { labels: days, unit: COUNT, series, stacked: true, shortLabel: (day) => day.slice(5) };
  }

  function oneDecimal(value) {
    return value.toLocaleString(undefined, { maximumFractionDigits: 1 });
  }

  function formatBytes(bytes) {
    let value = bytes;
    let unit = 0;
    while (value >= 1000 && unit < BYTE_UNITS.length - 1) {
      value /= 1000;
      unit += 1;
    }
    return `${oneDecimal(value)} ${BYTE_UNITS[unit]}`;
  }

  function hasData(spec) {
    return spec.series.some((series) => series.values.some((value) => value !== null && value > 0));
  }

  function chartConfig(spec) {
    const stacked = Boolean(spec.stacked);
    const categoryAxis = spec.horizontal ? "y" : "x";
    const valueAxis = spec.horizontal ? "x" : "y";
    const categoryTicks = spec.shortLabel
      ? {
          callback(value) {
            return spec.shortLabel(this.getLabelForValue(value));
          },
        }
      : {};
    return {
      type: "bar",
      data: {
        labels: spec.labels,
        datasets: spec.series.map((series) => ({
          label: series.label,
          data: series.values,
          backgroundColor: series.color,
          maxBarThickness: BAR_THICKNESS,
          ...(stacked ? { borderColor: SURFACE, borderWidth: { top: 2 } } : { borderRadius: 4 }),
        })),
      },
      options: {
        indexAxis: categoryAxis,
        plugins: {
          legend: { display: stacked, position: "bottom" },
          tooltip: {
            callbacks: {
              title: (items) => spec.titles?.[items[0].dataIndex] ?? items[0].label,
              label: (item) => {
                const value = spec.unit.format(item.raw);
                return stacked ? `${item.dataset.label}: ${value}` : value;
              },
            },
          },
        },
        scales: {
          [categoryAxis]: { stacked, grid: { display: false }, ticks: categoryTicks },
          [valueAxis]: {
            stacked,
            beginAtZero: true,
            ticks: {
              precision: spec.unit.precision,
              callback: (value) => (spec.unit.tick ?? spec.unit.format)(value),
            },
          },
        },
      },
    };
  }

  function dataTable(spec) {
    const table = document.createElement("table");
    table.className = "listing";
    const head = table.createTHead().insertRow();
    for (const heading of ["", ...spec.series.map((series) => series.label)]) {
      head.appendChild(document.createElement("th")).textContent = heading;
    }
    const body = table.createTBody();
    spec.labels.forEach((label, index) => {
      const row = body.insertRow();
      row.insertCell().textContent = spec.titles?.[index] ?? label;
      for (const series of spec.series) {
        const value = series.values[index];
        row.insertCell().textContent = value === null ? "none" : spec.unit.format(value);
      }
    });
    return table;
  }

  function emptyNote(text) {
    const note = document.createElement("p");
    note.className = "empty";
    note.textContent = text;
    return note;
  }

  function pageNavigation(figure, prefix, page) {
    const note = document.createElement("p");
    note.textContent = `Showing ${page.shown} of ${page.total}. This chart and table cover this page only.`;
    figure.appendChild(note);
    const nav = document.createElement("nav");
    nav.setAttribute("aria-label", `${prefix} chart pages`);
    for (const [label, value, key] of [
      ["Previous", page.previous, prefix === "tickets" ? "after" : "before"],
      ["Next", page.next, prefix === "tickets" ? "before" : "after"],
      ["Latest", null, null],
    ]) {
      if (label !== "Latest" && value === null) continue;
      const params = new URLSearchParams(window.location.search);
      params.delete(`${prefix}_before`);
      params.delete(`${prefix}_after`);
      if (key !== null) params.set(`${prefix}_${key}`, value);
      const link = document.createElement("a");
      link.textContent = label;
      link.href = `/stats${params.size ? `?${params}` : ""}`;
      nav.appendChild(link);
      nav.appendChild(document.createTextNode(" "));
    }
    figure.appendChild(nav);
  }

  function render(figure, summary) {
    const canvas = figure.querySelector("canvas[data-chart]");
    const key = canvas.dataset.chart;
    figure.dataset.chart = key;
    const spec = SPECS[key](summary[key], summary.pages[PAGE_PREFIXES[key]]);
    const prefix = PAGE_PREFIXES[key];
    if (prefix) pageNavigation(figure, prefix, summary.pages[prefix]);
    if (!hasData(spec)) {
      canvas.parentElement.replaceWith(emptyNote("No data yet."));
      return;
    }
    new Chart(canvas, chartConfig(spec));
    const details = document.createElement("details");
    details.appendChild(document.createElement("summary")).textContent = "Data table";
    const scroll = document.createElement("div");
    scroll.className = "table-scroll";
    scroll.appendChild(dataTable(spec));
    details.appendChild(scroll);
    figure.appendChild(details);
  }

  async function load() {
    const response = await fetch(SUMMARY_URL + window.location.search, { credentials: "same-origin", headers: { Accept: "application/json", "HX-Request": "true" } });
    if (response.status === 401) {
      window.location.assign("/login");
      return;
    }
    if (!response.ok) {
      throw new Error(`the summary request answered ${response.status}`);
    }
    const summary = await response.json();
    for (const figure of document.querySelectorAll("[data-figure]")) {
      figure.textContent = summary[figure.dataset.figure].toLocaleString();
    }
    for (const figure of document.querySelectorAll("figure.chart")) {
      render(figure, summary);
    }
  }

  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.color = INK;
  Chart.defaults.borderColor = GRID;
  Chart.defaults.maintainAspectRatio = false;

  load().catch((error) => {
    const status = document.getElementById("stats-status");
    status.textContent = `Could not load the statistics: ${error.message}`;
    status.hidden = false;
    for (const figure of document.querySelectorAll("[data-figure]")) {
      figure.textContent = "\u2013";
    }
    for (const frame of document.querySelectorAll("figure.chart .frame")) {
      frame.replaceWith(emptyNote("Unavailable."));
    }
  });
})();
