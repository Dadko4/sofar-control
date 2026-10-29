"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
const BAND_COLORS = {
  night: "#1d4d3c",
  late_morning: "#2a4458",
  afternoon: "#5a4a24",
  peak: "#5c2b2b"
};
const BAND_NAMES = {
  night: "Noc",
  late_morning: "Dopoludnie",
  afternoon: "Popoludnie",
  peak: "Špička"
};

function formatValue(value, unit, absolute = false) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  const displayNumber = absolute ? Math.abs(number) : number;
  const text = Number.isFinite(displayNumber)
    ? displayNumber.toLocaleString("sk-SK", { maximumFractionDigits: 2 })
    : String(value);
  return `${text} ${unit}`;
}

function formatEnergyValue(energy) {
  if (!energy || energy.value === null || energy.value === undefined || energy.value === "") return "—";
  return formatValue(energy.value, energy.unit || "kWh");
}

function historyNumber(value, unit = "") {
  if (value === null || value === undefined || value === "") return "—";
  return `${Number(value).toLocaleString("sk-SK", { maximumFractionDigits: 2 })}${unit ? ` ${unit}` : ""}`;
}

function formatEuro(value, digits = 2) {
  if (!Number.isFinite(Number(value))) return "—";
  return `${Number(value).toLocaleString("sk-SK", { minimumFractionDigits: digits, maximumFractionDigits: digits })} €`;
}

function formatPrice(value) {
  return Number.isFinite(Number(value)) ? `${formatEuro(value, 3)}/kWh` : "—";
}

function formatClock(date) {
  return date.toLocaleTimeString("sk-SK", { hour: "2-digit", minute: "2-digit" });
}

function formatRelativeDay(date, now = new Date()) {
  const day = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const difference = Math.round((day - today) / 86_400_000);
  const clock = formatClock(date);
  if (difference === 0) return `dnes ${clock}`;
  if (difference === 1) return `zajtra ${clock}`;
  if (difference === -1) return `včera ${clock}`;
  return `${date.toLocaleDateString("sk-SK", { weekday: "short", day: "numeric", month: "numeric" })} ${clock}`;
}

function bandForPrice(price, minPrice, maxPrice) {
  const span = maxPrice - minPrice;
  if (!(span > 1e-6)) return "night";
  const position = (price - minPrice) / span;
  if (position < 0.08) return "night";
  if (position < 0.3) return "late_morning";
  if (position < 0.7) return "afternoon";
  return "peak";
}

function svgElement(name, attributes = {}) {
  const element = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attributes)) {
    element.setAttribute(key, value);
  }
  return element;
}

function svgText(svg, x, y, text, anchor = "middle") {
  const label = svgElement("text", { x, y, "text-anchor": anchor });
  label.textContent = text;
  svg.append(label);
  return label;
}

function appendLegend(container, items) {
  const legend = document.createElement("div");
  legend.className = "chart-legend";
  for (const item of items) {
    const entry = document.createElement("span");
    entry.className = "chart-legend-item";
    const swatch = document.createElement("span");
    swatch.className = `chart-legend-swatch${item.box ? " box" : ""}`;
    swatch.style.backgroundColor = item.color;
    if (item.dash) {
      swatch.style.backgroundColor = "transparent";
      swatch.style.borderTop = `2px dashed ${item.color}`;
    }
    entry.append(swatch, document.createTextNode(item.name));
    legend.append(entry);
  }
  container.append(legend);
}

function emptyState(container, text) {
  container.replaceChildren();
  const box = document.createElement("div");
  box.className = "empty";
  box.textContent = text;
  container.append(box);
}

function renderPlanChart(container, plan, now = new Date()) {
  container.replaceChildren();
  const hours = (plan?.hourly_plan || []).filter(hour => Number.isFinite(Date.parse(hour.time)));
  if (hours.length === 0) {
    emptyState(container, "Plán zatiaľ nemá hodinový rozpis.");
    return;
  }
  const width = 800;
  const height = 290;
  const margin = { top: 26, right: 46, bottom: 34, left: 46 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const start = Date.parse(hours[0].time);
  const end = Date.parse(hours[hours.length - 1].time) + 3_600_000;
  const span = Math.max(1, end - start);
  const x = time => margin.left + (time - start) / span * plotWidth;
  const energyMax = Math.max(0.5, ...hours.flatMap(hour => [Number(hour.solar_kwh) || 0, Number(hour.consumption_kwh) || 0])) * 1.15;
  const yEnergy = value => margin.top + (1 - value / energyMax) * plotHeight;
  const ySoc = value => margin.top + (1 - value / 100) * plotHeight;
  const prices = hours.map(hour => Number(hour.tariff_eur_per_kwh)).filter(Number.isFinite);
  const minPrice = Math.min(...prices);
  const maxPrice = Math.max(...prices);

  const svg = svgElement("svg", {
    class: "chart-svg",
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": "Plán: tarifa, výroba, spotreba, stav batérie a nabíjacie okná"
  });

  for (const hour of hours) {
    const time = Date.parse(hour.time);
    const band = bandForPrice(Number(hour.tariff_eur_per_kwh), minPrice, maxPrice);
    svg.append(svgElement("rect", {
      x: x(time), y: margin.top, width: Math.max(0, x(time + 3_600_000) - x(time)), height: plotHeight,
      fill: BAND_COLORS[band], opacity: 0.55
    }));
  }

  for (let tick = 0; tick <= 4; tick++) {
    const position = margin.top + tick / 4 * plotHeight;
    svg.append(svgElement("line", { x1: margin.left, x2: width - margin.right, y1: position, y2: position, stroke: "#263a4a", "stroke-width": 1 }));
    svgText(svg, margin.left - 6, position + 4, (energyMax * (1 - tick / 4)).toLocaleString("sk-SK", { maximumFractionDigits: 1 }), "end");
    svgText(svg, width - margin.right + 6, position + 4, `${100 - tick * 25} %`, "start");
  }
  svgText(svg, margin.left - 6, margin.top - 10, "kWh", "end");

  for (const window of plan.windows || []) {
    const windowStart = Date.parse(window.charge_start);
    const windowEnd = Date.parse(window.charge_end);
    if (!Number.isFinite(windowStart) || !Number.isFinite(windowEnd)) continue;
    const left = x(Math.max(start, windowStart));
    const right = x(Math.min(end, windowEnd));
    if (right <= left) continue;
    const rect = svgElement("rect", {
      x: left, y: margin.top - 14, width: right - left, height: 10, rx: 3, fill: "#4fd1c5"
    });
    const tip = svgElement("title");
    tip.textContent = `Nabíjanie ${formatClock(new Date(windowStart))}–${formatClock(new Date(windowEnd))} · ${window.charge_power_w || 2000} W · do ${window.recommended_target_soc} %`;
    rect.append(tip);
    svg.append(rect);
    svg.append(svgElement("rect", { x: left, y: margin.top, width: right - left, height: plotHeight, fill: "#4fd1c5", opacity: 0.12 }));
  }

  const slot = plotWidth / hours.length;
  const barWidth = Math.max(1.5, slot * 0.32);
  for (const hour of hours) {
    const center = x(Date.parse(hour.time)) + slot / 2;
    for (const [field, color, offset] of [["solar_kwh", "#f6c85f", -barWidth], ["consumption_kwh", "#ed6a5a", 0]]) {
      const value = Number(hour[field]) || 0;
      if (value <= 0) continue;
      svg.append(svgElement("rect", {
        x: center + offset, y: yEnergy(value), width: barWidth, height: Math.max(0.5, yEnergy(0) - yEnergy(value)),
        fill: color, opacity: 0.9, rx: 1
      }));
    }
  }

  let path = "";
  const firstSoc = Number(hours[0].soc_start);
  if (Number.isFinite(firstSoc)) path = `M${x(start).toFixed(1)},${ySoc(firstSoc).toFixed(1)} `;
  for (const hour of hours) {
    const value = Number(hour.soc_end);
    if (!Number.isFinite(value)) continue;
    path += `${path ? "L" : "M"}${x(Date.parse(hour.time) + 3_600_000).toFixed(1)},${ySoc(value).toFixed(1)} `;
  }
  if (path) {
    svg.append(svgElement("path", { d: path.trim(), fill: "none", stroke: "#b28dff", "stroke-width": 2.5, "stroke-linejoin": "round" }));
  }

  const nowTime = now.getTime();
  if (nowTime >= start && nowTime <= end) {
    svg.append(svgElement("line", { x1: x(nowTime), x2: x(nowTime), y1: margin.top, y2: margin.top + plotHeight, stroke: "#e9f1f7", "stroke-width": 1.5, "stroke-dasharray": "4 4" }));
    svgText(svg, x(nowTime), margin.top + plotHeight + 26, "teraz");
  }

  const labelStep = Math.max(1, Math.ceil(hours.length / 8));
  hours.forEach((hour, index) => {
    if (index % labelStep !== 0) return;
    svgText(svg, x(Date.parse(hour.time)), height - 18, formatClock(new Date(hour.time)));
  });

  for (const hour of hours) {
    const time = Date.parse(hour.time);
    const target = svgElement("rect", {
      x: x(time), y: margin.top, width: Math.max(0, x(time + 3_600_000) - x(time)), height: plotHeight, fill: "transparent"
    });
    const tip = svgElement("title");
    tip.textContent = [
      `${formatRelativeDay(new Date(time), now)}`,
      `tarifa ${formatPrice(hour.tariff_eur_per_kwh)}`,
      `FVE ${historyNumber(hour.solar_kwh, "kWh")} · spotreba ${historyNumber(hour.consumption_kwh, "kWh")}`,
      `nabíjanie zo siete ${historyNumber(hour.grid_charge_kwh, "kWh")}`,
      `SOC na konci ${historyNumber(hour.soc_end, "%")}`
    ].join("\n");
    target.append(tip);
    svg.append(target);
  }

  container.append(svg);
  appendLegend(container, [
    { name: "FVE (kWh)", color: "#f6c85f", box: true },
    { name: "Spotreba (kWh)", color: "#ed6a5a", box: true },
    { name: "Batéria SOC (%)", color: "#b28dff" },
    { name: "Nabíjanie zo siete", color: "#4fd1c5", box: true },
    ...Object.entries(BAND_NAMES).map(([band, name]) => ({ name, color: BAND_COLORS[band], box: true }))
  ]);
}

function renderLineChart(container, datasets, title, unit, options = {}) {
  container.replaceChildren();
  const usable = datasets
    .map(dataset => ({
      ...dataset,
      points: (dataset.points || []).filter(point => Number.isFinite(Date.parse(point.time)))
    }))
    .filter(dataset => dataset.points.some(point => Number.isFinite(point.value)));
  if (usable.length === 0) {
    emptyState(container, "Zatiaľ nie sú dostupné merania na zobrazenie.");
    return;
  }

  const width = 800;
  const height = 270;
  const margin = { top: 18, right: 18, bottom: 38, left: 62 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const allPoints = usable.flatMap(dataset => dataset.points.filter(point => Number.isFinite(point.value)));
  const times = allPoints.map(point => Date.parse(point.time));
  const xMin = Math.min(...times);
  const xMax = Math.max(...times);
  const xSpan = Math.max(1, xMax - xMin);
  const values = allPoints.map(point => point.value);
  const yMin = options.yMin ?? Math.min(0, ...values);
  const yMax = options.yMax ?? Math.max(0, ...values);
  const yPadding = yMax === yMin ? Math.max(1, Math.abs(yMax) * .1) : (yMax - yMin) * .08;
  const low = options.yMin ?? yMin - yPadding;
  const high = options.yMax ?? yMax + yPadding;
  const ySpan = Math.max(1e-9, high - low);
  const x = time => margin.left + (Date.parse(time) - xMin) / xSpan * plotWidth;
  const y = value => margin.top + (high - value) / ySpan * plotHeight;
  const svg = svgElement("svg", {
    class: "chart-svg",
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": `${title}, jednotka ${unit}`
  });
  const svgTitle = svgElement("title");
  svgTitle.textContent = `${title} (${unit})`;
  svg.append(svgTitle);

  for (let tick = 0; tick <= 4; tick++) {
    const value = high - tick / 4 * ySpan;
    const position = y(value);
    svg.append(svgElement("line", { x1: margin.left, x2: width - margin.right, y1: position, y2: position, stroke: "#263a4a", "stroke-width": 1 }));
    svgText(svg, margin.left - 8, position + 4, `${value.toLocaleString("sk-SK", { maximumFractionDigits: 1 })} ${unit}`, "end");
  }

  const labelCount = Math.min(5, Math.max(2, allPoints.length));
  for (let index = 0; index < labelCount; index++) {
    const time = new Date(xMin + index / (labelCount - 1) * xSpan);
    svgText(
      svg,
      margin.left + index / (labelCount - 1) * plotWidth,
      height - 10,
      options.timeLabel ? options.timeLabel(time) : formatClock(time),
      index === 0 ? "start" : index === labelCount - 1 ? "end" : "middle"
    );
  }

  for (const dataset of usable) {
    let path = "";
    let drawing = false;
    for (const point of dataset.points) {
      if (!Number.isFinite(point.value)) {
        drawing = false;
        continue;
      }
      path += `${drawing ? "L" : "M"}${x(point.time).toFixed(1)},${y(point.value).toFixed(1)} `;
      drawing = true;
    }
    const line = svgElement("path", {
      d: path.trim(), fill: "none", stroke: dataset.color, "stroke-width": 2.5,
      "stroke-linejoin": "round", "stroke-linecap": "round"
    });
    if (dataset.dash) line.setAttribute("stroke-dasharray", dataset.dash);
    const lineTitle = svgElement("title");
    lineTitle.textContent = dataset.name;
    line.append(lineTitle);
    svg.append(line);
  }
  container.append(svg);
  appendLegend(container, usable.map(dataset => ({ name: dataset.name, color: dataset.color, dash: dataset.dash })));
}

function renderSavingsChart(container, daily) {
  container.replaceChildren();
  container.hidden = false;
  const days = daily.filter(day => day && day.date);
  if (days.length === 0 || !days.some(day => Number.isFinite(day.savings_eur))) {
    emptyState(container, "Po nazbieraní telemetrie sa tu zobrazí denný odhad úspory.");
    return;
  }

  const width = 800;
  const height = 300;
  const margin = { top: 22, right: 62, bottom: 42, left: 62 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const dailyValues = days.map(day => day.savings_eur).filter(Number.isFinite);
  const cumulativeValues = days.map(day => day.cumulative_savings_eur).filter(Number.isFinite);
  const dailyExtent = Math.max(.01, ...dailyValues.map(Math.abs)) * 1.15;
  const cumulativeMin = Math.min(0, ...cumulativeValues);
  const cumulativeMax = Math.max(.01, ...cumulativeValues);
  const cumulativeSpan = Math.max(.01, cumulativeMax - cumulativeMin);
  const x = index => margin.left + (index + .5) / days.length * plotWidth;
  const yDaily = value => margin.top + (dailyExtent - value) / (dailyExtent * 2) * plotHeight;
  const yCumulative = value => margin.top + (cumulativeMax - value) / cumulativeSpan * plotHeight;
  const zeroY = yDaily(0);
  const svg = svgElement("svg", {
    class: "chart-svg",
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": "Denná a kumulatívna odhadovaná úspora"
  });

  for (let tick = 0; tick <= 4; tick++) {
    const fraction = tick / 4;
    const position = margin.top + fraction * plotHeight;
    svg.append(svgElement("line", { x1: margin.left, x2: width - margin.right, y1: position, y2: position, stroke: "#263a4a", "stroke-width": 1 }));
    svgText(svg, margin.left - 8, position + 4, (dailyExtent - fraction * dailyExtent * 2).toLocaleString("sk-SK", { maximumFractionDigits: 2 }), "end");
    svgText(svg, width - margin.right + 8, position + 4, (cumulativeMax - fraction * cumulativeSpan).toLocaleString("sk-SK", { maximumFractionDigits: 2 }), "start");
  }
  svg.append(svgElement("line", { x1: margin.left, x2: width - margin.right, y1: zeroY, y2: zeroY, stroke: "#7890a2", "stroke-width": 1.5 }));

  const cellWidth = plotWidth / days.length;
  days.forEach((day, index) => {
    const value = day.savings_eur;
    if (!Number.isFinite(value)) return;
    const barY = yDaily(Math.max(0, value));
    const barBottom = yDaily(Math.min(0, value));
    const bar = svgElement("rect", {
      x: x(index) - cellWidth * .28, y: Math.min(barY, barBottom),
      width: Math.max(2, cellWidth * .56), height: Math.max(1, Math.abs(barBottom - barY)),
      rx: 2, fill: value >= 0 ? "#55c596" : "#ed6a5a"
    });
    const tooltip = svgElement("title");
    tooltip.textContent = `${day.date}: ${historyNumber(value, "€")} · pokrytie ${historyNumber(day.coverage_percent, "%")}`;
    bar.append(tooltip);
    svg.append(bar);
  });

  let path = "";
  let hasSegment = false;
  days.forEach((day, index) => {
    if (!Number.isFinite(day.cumulative_savings_eur)) {
      hasSegment = false;
      return;
    }
    path += `${hasSegment ? "L" : "M"}${x(index).toFixed(1)},${yCumulative(day.cumulative_savings_eur).toFixed(1)} `;
    hasSegment = true;
    const point = svgElement("circle", { cx: x(index), cy: yCumulative(day.cumulative_savings_eur), r: 3.5, fill: "#b28dff" });
    const tooltip = svgElement("title");
    tooltip.textContent = `${day.date}: kumulatívne ${historyNumber(day.cumulative_savings_eur, "€")}`;
    point.append(tooltip);
    svg.append(point);
  });
  if (path) {
    svg.append(svgElement("path", { d: path.trim(), fill: "none", stroke: "#b28dff", "stroke-width": 2.5, "stroke-linejoin": "round", "stroke-linecap": "round" }));
  }

  const labelStep = Math.max(1, Math.ceil(days.length / 7));
  days.forEach((day, index) => {
    if (index % labelStep !== 0 && index !== days.length - 1) return;
    svgText(svg, x(index), height - 12, new Date(`${day.date}T12:00:00`).toLocaleDateString("sk-SK", { day: "2-digit", month: "2-digit" }));
  });
  container.append(svg);
  appendLegend(container, [
    { name: "Denná úspora (ľavá os, €)", color: "#55c596", box: true },
    { name: "Kumulatívna úspora (pravá os, €)", color: "#b28dff" }
  ]);
}

function renderPlanComparisonCharts(container, forecast, actual) {
  const actualByTime = new Map((actual.hourly || []).map(hour => [hour.time, hour]));
  const forecastHours = forecast.hourly_plan || [];
  const specs = [
    { title: "Výroba FVE a spotreba (kWh)", unit: "kWh", forecastField: ["solar_kwh", "consumption_kwh"], actualField: ["solar_kwh", "consumption_kwh"], names: ["FVE", "Spotreba"], colors: ["#f6c85f", "#ed6a5a"] },
    { title: "Odber zo siete (kWh)", unit: "kWh", forecastField: ["grid_import_kwh"], actualField: ["grid_import_kwh"], names: ["Odber zo siete"], colors: ["#5dade2"] },
    { title: "Náklad za hodinu (€)", unit: "€", forecastField: ["cost_eur"], actualField: [["simulation_cost_eur", "cost_eur"]], names: ["Náklad"], colors: ["#55c596"] },
    { title: "Stav batérie SOC (%)", unit: "%", forecastField: ["soc_end"], actualField: ["soc_end"], names: ["SOC"], colors: ["#b28dff"], yMin: 0, yMax: 100 }
  ];
  const grid = document.createElement("div");
  grid.className = "chart-grid";
  for (const spec of specs) {
    const panel = document.createElement("section");
    panel.className = "chart-panel";
    const heading = document.createElement("h3");
    heading.textContent = spec.title;
    const chart = document.createElement("div");
    panel.append(heading, chart);
    const datasets = [];
    spec.names.forEach((name, index) => {
      const actualField = spec.actualField[index];
      const forecastField = spec.forecastField[index];
      datasets.push({
        name: `${name} · predikcia`,
        color: spec.colors[index],
        dash: "7 5",
        points: forecastHours.map(hour => ({ time: hour.time, value: hour[forecastField] }))
      });
      datasets.push({
        name: `${name} · skutočnosť`,
        color: spec.colors[index],
        points: forecastHours.map(hour => {
          const measured = actualByTime.get(hour.time) || {};
          const value = Array.isArray(actualField)
            ? actualField.map(field => measured[field]).find(Number.isFinite)
            : measured[actualField];
          return { time: hour.time, value };
        })
      });
    });
    renderLineChart(chart, datasets, spec.title, spec.unit, { yMin: spec.yMin, yMax: spec.yMax });
    grid.append(panel);
  }
  container.append(grid);
}
