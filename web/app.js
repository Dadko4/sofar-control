"use strict";

const modeNames = {
  "0": "Self-use",
  "1": "Time-of-Use — nabíjanie do cieľového SOC",
  "2": "Timing — časový plán",
  "3": "Passive — vzdialené riadenie výkonu"
};
const dailyEnergyFields = [
  ["daily-generation", "generation"],
  ["daily-consumption", "consumption"],
  ["daily-grid-import", "grid_import"],
  ["daily-grid-export", "grid_export"],
  ["daily-battery-charge", "battery_charge"],
  ["daily-battery-discharge", "battery_discharge"]
];
const eventNames = {
  mode_change: "Zmena režimu",
  mode_confirmed: "Režim overený",
  optimization_calculated: "Výpočet plánu",
  plan_scheduled: "Plán uložený"
};
const SETTINGS_KEY = "sofar-opti-optimization-settings";
const TABS = ["overview", "plan", "history", "control"];

let controlReady = false;
let currentSoc = null;
let dischargeReady = false;
let batteryCapacityKwh = 5;
let maxDischargePowerW = 2500;
let optimizationPlan = null;
let latestStatus = null;
let schedulerState = null;
let continuousState = null;
let tariffHours = [];
let latestPlans = [];
let readOnly = false;

const $ = id => document.getElementById(id);

/* ---------- Feedback: toasts and confirmation dialog ---------- */

let toastTimer = null;
function notify(message, kind = "info", timeout = kind === "error" ? 12_000 : 6_000) {
  const toast = $("toast");
  toast.textContent = message;
  toast.className = `toast ${kind}`;
  toast.hidden = false;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => { toast.hidden = true; }, timeout);
}
$("toast").addEventListener("click", () => { $("toast").hidden = true; });

function showError(error) {
  notify(error.message || String(error), "error");
}

function confirmAction({ title, lines, confirmLabel = "Potvrdiť" }) {
  const dialog = $("confirm-dialog");
  if (typeof dialog.showModal !== "function") {
    return Promise.resolve(window.confirm(`${title}\n\n${lines.join("\n")}`));
  }
  $("confirm-title").textContent = title;
  const body = $("confirm-body");
  body.replaceChildren(...lines.map(line => {
    const paragraph = document.createElement("p");
    paragraph.textContent = line;
    return paragraph;
  }));
  $("confirm-ok").textContent = confirmLabel;
  dialog.returnValue = "cancel";
  dialog.showModal();
  return new Promise(resolve => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
  });
}

/* ---------- Tabs ---------- */

function setTab(name, updateHash = true) {
  if (!TABS.includes(name)) name = "overview";
  for (const tab of TABS) {
    $(`tab-${tab}`).hidden = tab !== name;
    $(`tab-button-${tab}`).setAttribute("aria-selected", String(tab === name));
  }
  if (updateHash && location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
}
for (const button of document.querySelectorAll("[data-tab]")) {
  button.addEventListener("click", () => setTab(button.dataset.tab));
}
for (const button of document.querySelectorAll("[data-goto]")) {
  button.addEventListener("click", () => setTab(button.dataset.goto));
}
document.querySelector(".tabs").addEventListener("keydown", event => {
  if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
  const current = TABS.findIndex(tab => $(`tab-button-${tab}`).getAttribute("aria-selected") === "true");
  const next = TABS[(current + (event.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length];
  setTab(next);
  $(`tab-button-${next}`).focus();
});
window.addEventListener("hashchange", () => setTab(location.hash.slice(1), false));

/* ---------- Helpers ---------- */

async function fetchJson(url, options) {
  const response = await fetch(url, { cache: "no-store", ...options });
  const data = await response.json();
  if (!response.ok) {
    const details = [
      data.stage,
      data.api_code ? `API kód ${data.api_code}` : null,
      data.http_status ? `HTTP ${data.http_status}` : null
    ].filter(Boolean).join(", ");
    throw new Error(`${data.error || `Chyba HTTP ${response.status}`}${details ? ` (${details})` : ""}`);
  }
  return data;
}

function postJson(url, body) {
  return fetchJson(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
}

function setPill(id, text, level) {
  const pill = $(id);
  pill.textContent = text;
  pill.className = `pill${level ? ` ${level}` : ""}`;
}

function numberOrNull(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function energyAid() {
  if (continuousState?.enabled && typeof continuousState.settings?.energy_aid === "boolean") {
    return continuousState.settings.energy_aid;
  }
  return $("optimization-aid").checked;
}

function windowLabel(window, now = new Date()) {
  const start = new Date(window.charge_start);
  const end = new Date(window.charge_end);
  return `${formatRelativeDay(start, now)}–${formatClock(end)}`;
}

function renderWindowChips(container, windows, currentIndex = 0, now = new Date()) {
  container.replaceChildren();
  windows.forEach((window, index) => {
    const start = Date.parse(window.charge_start);
    const end = Date.parse(window.charge_end);
    const chip = document.createElement("div");
    chip.className = "window-chip";
    if (end <= now.getTime() || index < currentIndex) chip.classList.add("done");
    else if (start <= now.getTime()) chip.classList.add("active");
    const title = document.createElement("strong");
    title.textContent = `⚡ ${windowLabel(window, now)}`;
    const detail = document.createElement("span");
    detail.className = "muted";
    detail.textContent = [
      `${window.charge_power_w || 2000} W`,
      `do ${window.recommended_target_soc} %`,
      window.recommended_grid_charge_kwh !== undefined ? historyNumber(window.recommended_grid_charge_kwh, "kWh") : null,
      window.average_price_eur_per_kwh !== undefined ? formatPrice(window.average_price_eur_per_kwh) : null
    ].filter(Boolean).join(" · ");
    chip.append(title, detail);
    container.append(chip);
  });
}

/* ---------- Live status and the "now" card ---------- */

async function refresh() {
  const button = $("refresh");
  button.disabled = true;
  try {
    const data = await fetchJson("/api/status");
    latestStatus = data;
    $("station").textContent = data.station_name;
    $("model").textContent = data.model;
    const metrics = data.metrics || {};
    currentSoc = numberOrNull(metrics.battery_soc);
    $("soc").textContent = currentSoc === null ? "—" : `${Math.round(currentSoc)} %`;
    $("soc-fill").style.width = `${Math.max(0, Math.min(100, currentSoc ?? 0))}%`;
    $("battery-capacity").textContent = formatEnergyValue(data.battery_capacity);
    $("generation").textContent = formatValue(metrics.generation_power, "kW");
    $("consumption").textContent = formatValue(metrics.use_power, "kW");

    const charge = Math.abs(numberOrNull(metrics.charge_power) ?? 0);
    const discharge = Math.abs(numberOrNull(metrics.discharge_power) ?? 0);
    $("battery-flow-label").textContent = charge > 0.01 ? "▮ Batéria · nabíja" : discharge > 0.01 ? "▮ Batéria · vybíja" : "▮ Batéria";
    $("battery-flow").textContent = formatValue(charge > 0.01 ? charge : discharge, "kW");

    const grid = numberOrNull(metrics.grid_power);
    $("grid-flow-label").textContent = grid === null || Math.abs(grid) < 0.01
      ? "⚡ Sieť"
      : grid > 0 ? "⚡ Sieť · dodávka" : "⚡ Sieť · odber";
    $("grid").textContent = formatValue(grid, "kW", true);

    for (const [elementId, key] of dailyEnergyFields) {
      $(elementId).textContent = formatEnergyValue(data.daily_energy?.[key]);
    }

    const measuredAt = Number(data.data_time) * 1000;
    if (Number.isFinite(measuredAt) && measuredAt > 0) {
      const ageMinutes = Math.max(0, Math.round((Date.now() - measuredAt) / 60_000));
      setPill(
        "health-data",
        `Dáta ${formatClock(new Date(measuredAt))} · pred ${ageMinutes} min`,
        ageMinutes <= 15 ? "ok" : ageMinutes <= 60 ? "warn" : "bad"
      );
    } else {
      setPill("health-data", "Čas merania neznámy", "warn");
    }
    updateDischargeEstimate();
    updateNowCard();
  } catch (error) {
    setPill("health-data", "SofarCloud nedostupný", "bad");
    $("now-headline").textContent = "Aktuálne údaje sa nedajú načítať";
    notify(`Údaje z meniča: ${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
}

function headline(metrics, now) {
  const generation = numberOrNull(metrics.generation_power) ?? 0;
  const charge = Math.abs(numberOrNull(metrics.charge_power) ?? 0);
  const discharge = Math.abs(numberOrNull(metrics.discharge_power) ?? 0);
  const grid = numberOrNull(metrics.grid_power) ?? 0;
  const soc = currentSoc ?? 0;
  if (schedulerState?.status === "charging") {
    const window = (schedulerState.windows_queue || [])[schedulerState.current_window_index || 0];
    return window
      ? `Nabíja sa zo siete podľa plánu do ${formatClock(new Date(window.charge_end))}`
      : "Nabíja sa zo siete podľa plánu";
  }
  if (discharge > 0.05) return "Batéria napája dom";
  if (charge > 0.05 && generation > 0.05) return "Prebytok z FVE nabíja batériu";
  if (charge > 0.05) return "Batéria sa nabíja";
  if (grid > 0.05) return soc >= 94 ? "Batéria je plná, prebytok ide do siete" : "Prebytok z FVE ide do siete";
  if (grid < -0.05) return soc <= 31 ? "Batéria je na minime, dom berie zo siete" : "Dom berie zo siete";
  return now.getHours() >= 22 || now.getHours() < 6 ? "Noc — systém je v pokoji" : "Systém je v pokoji";
}

function tariffLine(now) {
  const aid = energyAid();
  const key = aid ? "with_aid_eur_per_kwh" : "without_aid_eur_per_kwh";
  const index = tariffHours.findIndex(hour => {
    const start = Date.parse(hour.time);
    return start <= now.getTime() && now.getTime() < start + 3_600_000;
  });
  if (index < 0) return "Tarifa: —";
  const current = tariffHours[index];
  const next = tariffHours.slice(index + 1).find(hour => hour.band !== current.band);
  const nextText = next
    ? ` · od ${formatClock(new Date(next.time))} ${BAND_NAMES[next.band].toLowerCase()} ${formatPrice(next[key])}`
    : "";
  return `Teraz ${BAND_NAMES[current.band].toLowerCase()} ${formatPrice(current[key])}${nextText}`;
}

function nextActionLine(now) {
  const windows = schedulerState?.windows_queue || [];
  const index = schedulerState?.current_window_index || 0;
  const status = schedulerState?.status;
  if (status === "charging" && windows[index]) {
    const window = windows[index];
    return `Teraz: nabíjanie ${window.charge_power_w || 2000} W do ${window.recommended_target_soc} %, koniec ${formatClock(new Date(window.charge_end))}`;
  }
  const upcoming = ["pending", "transitioning"].includes(status)
    ? windows.slice(index).find(window => Date.parse(window.charge_end) > now.getTime())
    : null;
  if (upcoming) {
    return `Ďalej: nabíjanie ${windowLabel(upcoming, now)} · ${upcoming.charge_power_w || 2000} W · do ${upcoming.recommended_target_soc} %`;
  }
  if (status === "failed") return `Plán zlyhal: ${schedulerState.error || "skontroluj menič"}`;
  if (continuousState?.enabled) {
    const nextRun = continuousState.next_run_at ? formatClock(new Date(continuousState.next_run_at)) : "—";
    return `Ďalej: nabíjanie zo siete sa teraz neoplatí · ďalší prepočet ${nextRun}`;
  }
  return "Automatika je vypnutá — zapni ju v záložke Plán a automatika.";
}

function updateNowCard() {
  const now = new Date();
  if (latestStatus) $("now-headline").textContent = headline(latestStatus.metrics || {}, now);
  $("now-tariff").textContent = tariffLine(now);
  $("now-next").textContent = nextActionLine(now);
}

async function refreshTariff() {
  try {
    tariffHours = (await fetchJson("/api/tariff")).hours || [];
    updateNowCard();
  } catch (error) {
    console.warn("Tariff could not be loaded.", error);
  }
}

/* ---------- Active plan on the overview ---------- */

function activePlanForecast() {
  if (!schedulerState?.plan_id) return null;
  const record = latestPlans.find(plan => plan.plan_id === schedulerState.plan_id);
  return record?.forecast || null;
}

function renderOverviewPlan() {
  const chart = $("plan-chart");
  const meta = $("plan-chart-meta");
  const forecast = activePlanForecast();
  const windows = schedulerState?.windows_queue || [];
  if (!forecast) {
    meta.textContent = "";
    emptyState(chart, continuousState?.enabled
      ? "Plán sa pripravuje — zobrazí sa po najbližšom prepočte automatiky."
      : "Žiadny aktívny plán. Zapni automatiku alebo vypočítaj plán v záložke Plán a automatika.");
    renderWindowChips($("plan-windows"), windows, schedulerState?.current_window_index || 0);
    return;
  }
  const generated = forecast.generated_at ? formatRelativeDay(new Date(forecast.generated_at)) : "—";
  const savings = Number.isFinite(forecast.estimated_savings_eur) ? ` · odhad úspory ${formatEuro(forecast.estimated_savings_eur)}` : "";
  meta.textContent = `Prepočítané ${generated}${savings}`;
  renderPlanChart(chart, { ...forecast, windows });
  if (windows.length === 0) {
    const none = document.createElement("p");
    none.className = "muted small";
    none.textContent = "V tomto pláne sa nabíjanie zo siete neoplatí — batériu nabije FVE alebo vystačí.";
    $("plan-windows").replaceChildren(none);
  } else {
    renderWindowChips($("plan-windows"), windows, schedulerState.current_window_index || 0);
  }
}

/* ---------- Inverter controls ---------- */

async function loadControls() {
  const hint = $("control-hint");
  const button = $("apply-mode");
  button.disabled = true;
  $("current-mode").textContent = "Overujem…";
  try {
    const [capabilities, mode, discharge] = await Promise.all([
      fetchJson("/api/capabilities"),
      fetchJson("/api/mode"),
      fetchJson("/api/discharge-status")
    ]);
    const supported = new Set(capabilities.modes.map(item => String(item.value)));
    const fieldsAvailable = new Set(capabilities.fields);
    const canCharge = [
      "touEnable", "touChargeStopSoc", "touChargePower",
      "touStartChargeTimestamp", "touEndChargeTimestamp",
      "touStartTimestamp", "touEndTimestamp", "touEnableWeek"
    ].every(field => fieldsAvailable.has(field));
    const canDischarge = ["passiveManualBup", "passiveManualBlo"].every(field => fieldsAvailable.has(field));
    readOnly = capabilities.read_only === true;
    $("health-readonly").hidden = !readOnly;
    if (readOnly) $("continuous-toggle").disabled = true;
    controlReady = !readOnly && supported.has("0") && supported.has("1") && canCharge;
    dischargeReady = capabilities.passive_discharge_enabled === true && canDischarge;
    batteryCapacityKwh = Number(capabilities.battery_capacity_kwh);
    maxDischargePowerW = Number(capabilities.max_discharge_power_w);
    if (!Number.isFinite(batteryCapacityKwh) || batteryCapacityKwh <= 0 ||
        !Number.isFinite(maxDischargePowerW) || maxDischargePowerW < 100) {
      throw new Error("Konfigurácia kapacity batérie alebo maximálneho výkonu vybíjania je neplatná.");
    }
    const dischargePower = $("discharge-power");
    dischargePower.max = String(maxDischargePowerW);
    if (Number(dischargePower.value) > maxDischargePowerW) dischargePower.value = String(maxDischargePowerW);
    document.querySelector('#requested-mode option[value="grid_discharge"]').disabled = !dischargeReady;
    const modeValue = String(mode.mode ?? "");
    $("current-mode").textContent = modeNames[modeValue] || `Neznámy (${modeValue || "—"})`;
    showDischargeStatus(discharge);
    const passive = capabilities.passive_discharge_enabled
      ? "povolené"
      : capabilities.passive_discharge_supported
        ? "vypnuté nastavením SOFAR_ENABLE_PASSIVE_DISCHARGE"
        : "nepodporované týmto meničom";
    hint.textContent = controlReady
      ? `Ručné nabíjanie prepíše pravidlá Time-of-Use, začne okamžite a platí do 23:59 s cieľom 95 % a limitom najviac 2 kW. Vybíjanie do siete (${passive}) používa záporný výkonový povel a serverový dohľad s cieľom 30 %; hardvérový limit SOC treba overiť priamo v meniči. Aplikácia aj SofarCloud musia zostať dostupné a telemetria môže meškať.`
      : "Menič nehlási všetky polia potrebné na bezpečné ovládanie. Príkazy sú vypnuté.";
    button.disabled = !controlReady;
    updateDischargeEstimate();
  } catch (error) {
    controlReady = false;
    $("current-mode").textContent = "Neznámy (neoverený)";
    hint.textContent = `Ovládanie vypnuté: ${error.message}`;
  }
}

async function applyMode() {
  const mode = $("requested-mode").value;
  const powerW = Number($(mode === "grid_discharge" ? "discharge-power" : "charge-power").value);
  const charge = mode === "grid_charge";
  const discharge = mode === "grid_discharge";
  if (!controlReady) return;
  if (charge && (!Number.isFinite(currentSoc) || currentSoc >= 95)) {
    showError(new Error("Nabíjanie nie je povolené: SOC nie je známe alebo je už aspoň 95 %."));
    return;
  }
  if (discharge && (!dischargeReady || !Number.isFinite(currentSoc) || currentSoc <= 30)) {
    showError(new Error("Vybíjanie nie je povolené: vyžaduje Passive režim, jeho povolenie a SOC nad 30 %."));
    return;
  }
  const energy = discharge ? batteryCapacityKwh * (currentSoc - 30) / 100 : 0;
  const minutes = discharge ? energy / (powerW / 1000) * 60 : 0;
  const lines = charge
    ? [`Nabíjanie od teraz do 23:59, cieľ 95 %, najviac ${powerW} W.`, "Existujúce pravidlá Time-of-Use sa prepíšu."]
    : discharge
      ? [`Vybíjanie do siete ${powerW} W, server sleduje SOC do 30 %.`, `Odhad: ${energy.toFixed(2)} kWh za ${minutes.toFixed(0)} min.`, "Menič nemá potvrdený hardvérový limit SOC."]
      : ["Menič sa vráti do režimu Self-use.", continuousState?.enabled ? "Nepretržitá optimalizácia sa tým vypne." : null].filter(Boolean);
  const confirmed = await confirmAction({
    title: charge ? "Spustiť nabíjanie zo siete?" : discharge ? "Spustiť vybíjanie do siete?" : "Prepnúť na Self-use?",
    lines: [...lines, "Príkaz sa odošle do meniča cez SofarCloud."],
    confirmLabel: "Odoslať príkaz"
  });
  if (!confirmed) return;

  const button = $("apply-mode");
  button.disabled = true;
  notify("Odosielam príkaz a čakám na potvrdenie meniča…", "info", 60_000);
  try {
    const result = await postJson("/api/mode", { mode, power_w: powerW, confirmed: true });
    await Promise.all([refresh(), loadControls(), refreshContinuousOptimization()]);
    const message = !result.changed
      ? "Menič už je v režime Self-use; príkaz sa neodoslal."
      : result.mode === "1"
        ? "Menič potvrdil nabíjací plán."
        : result.mode === "3"
          ? `Menič potvrdil vybíjanie. Server sleduje SOC ${result.start_soc} % → ${result.target_soc} %, bezpečnostný limit ${result.deadline}.`
          : "Menič potvrdil zmenu režimu.";
    notify(`${message}${result.history_warning ? ` ${result.history_warning}` : ""}`, "success");
    await refreshHistory();
  } catch (error) {
    showError(error);
    await Promise.all([refresh(), loadControls()]);
  } finally {
    button.disabled = !controlReady;
  }
}

function updateDischargeEstimate() {
  const element = $("discharge-estimate");
  const power = Number($("discharge-power").value);
  if (!Number.isFinite(currentSoc) || currentSoc <= 30 || !Number.isFinite(power) || power <= 0) {
    element.textContent = "Odhad bude dostupný pri známom SOC nad 30 %.";
    return;
  }
  const energy = batteryCapacityKwh * (currentSoc - 30) / 100;
  const minutes = energy / (power / 1000) * 60;
  element.textContent = `Odhad do 30 %: ${energy.toLocaleString("sk-SK", { maximumFractionDigits: 2 })} kWh, približne ${minutes.toLocaleString("sk-SK", { maximumFractionDigits: 0 })} min pri ${power} W. Softvérový odhad, nie hardvérová ochrana.`;
}

function showDischargeStatus(discharge) {
  const element = $("discharge-watchdog-status");
  element.hidden = !discharge.active;
  element.classList.remove("error");
  if (discharge.active) {
    element.textContent = `Aktívny dohľad vybíjania: SOC ${formatValue(discharge.last_soc, "%")}, cieľ ${discharge.target_soc} %, ${discharge.power_w} W. ${discharge.message}`;
  }
}

async function refreshDischargeStatus() {
  const element = $("discharge-watchdog-status");
  try {
    showDischargeStatus(await fetchJson("/api/discharge-status"));
  } catch (error) {
    element.hidden = false;
    element.textContent = `Stav dohľadu vybíjania sa nedá načítať: ${error.message}`;
    element.classList.add("error");
  }
}

/* ---------- Settings ---------- */

function readOptimizationSettings() {
  return {
    location: $("optimization-location").value.trim(),
    pv_capacity_kwp: Number($("optimization-pv").value),
    summer_consumption_kwh: Number($("optimization-summer").value),
    winter_consumption_kwh: Number($("optimization-winter").value),
    energy_aid: $("optimization-aid").checked,
    planning_horizon_hours: Number($("optimization-horizon").value)
  };
}

function saveOptimizationSettings(settings) {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  } catch (error) {
    console.warn("Optimization settings could not be saved in this browser.", error);
  }
}

function applySettingsToForm(saved, includeHorizon = true) {
  if (!saved || typeof saved !== "object") return;
  if (typeof saved.location === "string") $("optimization-location").value = saved.location;
  for (const [key, id] of [
    ["pv_capacity_kwp", "optimization-pv"],
    ["summer_consumption_kwh", "optimization-summer"],
    ["winter_consumption_kwh", "optimization-winter"]
  ]) {
    if (Number.isFinite(Number(saved[key]))) $(id).value = String(saved[key]);
  }
  if (typeof saved.energy_aid === "boolean") $("optimization-aid").checked = saved.energy_aid;
  if (includeHorizon && [12, 24, 36].includes(Number(saved.planning_horizon_hours))) {
    $("optimization-horizon").value = String(saved.planning_horizon_hours);
  }
}

function loadOptimizationSettings() {
  try {
    applySettingsToForm(JSON.parse(localStorage.getItem(SETTINGS_KEY) || "null"));
  } catch (error) {
    console.warn("Saved optimization settings could not be read.", error);
  }
}

function setSettingsLocked(locked) {
  for (const id of [
    "optimization-location", "optimization-pv", "optimization-summer",
    "optimization-winter", "optimization-aid", "optimization-horizon",
    "calculate-optimization", "apply-optimization"
  ]) {
    $(id).disabled = locked;
  }
  $("settings-lock-note").textContent = locked ? "🔒 Automatika beží s týmito nastaveniami — zmeníš ich po jej vypnutí." : "";
}

/* ---------- Automation ---------- */

async function refreshContinuousOptimization() {
  const button = $("continuous-toggle");
  const status = $("continuous-status");
  try {
    const state = await fetchJson("/api/continuous-optimization");
    continuousState = state;
    button.dataset.enabled = String(state.enabled);
    button.setAttribute("aria-checked", String(state.enabled));
    $("continuous-toggle-label").textContent = state.enabled ? "Zapnutá" : "Vypnutá";
    button.disabled = readOnly;
    if (state.enabled) applySettingsToForm(state.settings, false);
    setSettingsLocked(state.enabled);
    status.classList.toggle("error", Boolean(state.last_error));
    const lastRun = state.last_run_at ? formatRelativeDay(new Date(state.last_run_at)) : "zatiaľ neprebehol";
    const nextRun = state.next_run_at ? formatClock(new Date(state.next_run_at)) : "—";
    status.textContent = state.enabled
      ? `Posledný prepočet ${lastRun}, ďalší o ${nextRun}.${state.last_error ? ` Chyba: ${state.last_error}` : ""}`
      : "Automatika je vypnutá. Menič riadi batériu v Self-use, kým neuložíš plán alebo automatiku nezapneš.";
    setPill(
      "health-automation",
      state.enabled ? (state.last_error ? "Automatika: chyba" : "Automatika zapnutá") : "Automatika vypnutá",
      state.enabled ? (state.last_error ? "bad" : "ok") : ""
    );
  } catch (error) {
    status.textContent = `Stav automatiky sa nedá načítať: ${error.message}`;
    status.classList.add("error");
    setPill("health-automation", "Automatika: neznámy stav", "warn");
    button.disabled = true;
  }
  updateNowCard();
  renderOverviewPlan();
  await refreshContinuousResults();
}

async function toggleContinuousOptimization() {
  const button = $("continuous-toggle");
  const enabled = button.dataset.enabled !== "true";
  const settings = readOptimizationSettings();
  const confirmed = await confirmAction(enabled
    ? {
        title: "Zapnúť nepretržitú optimalizáciu?",
        lines: [
          "Appka bude každú hodinu prepočítavať 24-hodinový plán a podľa neho sama meniť režim meniča.",
          `Použijú sa nastavenia: ${settings.location}, ${historyNumber(settings.pv_capacity_kwp, "kWp")}, ${settings.energy_aid ? "s energopomocou" : "bez energopomoci"}.`,
          "Server aj spojenie so SofarCloud musia zostať dostupné."
        ],
        confirmLabel: "Zapnúť"
      }
    : {
        title: "Vypnúť automatiku?",
        lines: ["Aktuálny plán sa zruší a appka sa pokúsi vrátiť menič do Self-use."],
        confirmLabel: "Vypnúť"
      });
  if (!confirmed) return;
  if (enabled) saveOptimizationSettings(settings);
  button.disabled = true;
  try {
    await postJson("/api/continuous-optimization", {
      enabled,
      settings: enabled ? settings : undefined,
      confirmed: enabled
    });
    notify(enabled ? "Automatika je zapnutá. Prvý plán sa počíta hneď." : "Automatika je vypnutá.", "success");
    await refreshContinuousOptimization();
    window.setTimeout(refreshScheduledPlan, 4_000);
  } catch (error) {
    showError(error);
    button.disabled = false;
  }
}

async function refreshContinuousResults() {
  const days = Number($("continuous-days").value);
  const status = $("continuous-results-status");
  try {
    const results = await fetchJson(`/api/continuous-results?days=${days}`);
    $("continuous-savings").textContent = historyNumber(results.savings_eur, "€");
    $("continuous-actual-cost").textContent = historyNumber(results.actual_cost_eur, "€");
    $("continuous-self-use-cost").textContent = historyNumber(results.self_use_cost_eur, "€");
    renderSavingsChart($("continuous-savings-chart"), results.daily || []);
    status.classList.remove("error");
    const period = days === 1 ? "posledný deň" : `posledných ${days} dní`;
    status.textContent = results.sessions
      ? `${period[0].toUpperCase()}${period.slice(1)} · automatika aktívna ${historyNumber(results.active_hours, "h")} · pokrytie telemetriou ${historyNumber(results.coverage_percent, "%")}. Úspora je odhad oproti simulovanému Self-use.`
      : "Za toto obdobie zatiaľ nie sú vyhodnotené merania.";
    $("overview-savings").textContent = results.sessions ? formatEuro(results.savings_eur) : "—";
    $("overview-savings-note").textContent = results.sessions
      ? `za ${period} · automatika aktívna ${historyNumber(results.active_hours, "h")} · odhad oproti Self-use`
      : "Výsledky sa objavia po zapnutí automatiky a nazbieraní telemetrie.";
  } catch (error) {
    status.textContent = `Výsledky sa nedajú načítať: ${error.message}`;
    status.classList.add("error");
  }
}

/* ---------- Scheduler state ---------- */

async function refreshScheduledPlan() {
  const element = $("scheduled-plan-status");
  try {
    const state = await fetchJson("/api/optimization-plan");
    const changedPlan = schedulerState?.plan_id !== state.plan_id;
    schedulerState = state;
    element.classList.remove("error");
    const windows = state.windows_queue || [];
    if (state.plan) {
      const plan = state.plan;
      const start = formatRelativeDay(new Date(plan.charge_start));
      const end = formatRelativeDay(new Date(plan.charge_end));
      const statusText = {
        pending: `Naplánované nabíjanie ${start} – ${end}.`,
        charging: `Prebieha plánované nabíjanie do ${end}.`,
        returning_to_self_use: "Nabíjanie skončilo; appka vracia menič do Self-use.",
        completed: `Plán dokončený (${start} – ${end}).`,
        cancelled: "Plán bol zrušený ručným prepnutím do Self-use.",
        failed: `Plán zlyhal: ${state.error || "skontroluj menič."}`,
        expired: `Plán sa nestihol spustiť: ${state.error || "časové okno uplynulo"}`,
        idle: "Nie je uložený žiadny aktívny plán."
      };
      element.textContent = `${statusText[state.status] || `Stav plánu: ${state.status}`}${state.history_warning ? ` ${state.history_warning}` : ""}`;
      element.classList.toggle("error", ["failed", "expired"].includes(state.status));
    } else if (windows.length > 0 || state.plan_id) {
      const statusText = {
        pending: windows.length ? `Aktívny plán: ${windows.length === 1 ? "1 nabíjacie okno" : `${windows.length} nabíjacie okná`} v horizonte.` : "Aktívny plán: nabíjanie zo siete sa v horizonte neoplatí.",
        charging: `Prebieha nabíjanie v okne ${(state.current_window_index || 0) + 1} z ${windows.length}.`,
        transitioning: "Prechod medzi oknami; appka vracia menič do Self-use.",
        evaluating: `Nabíjanie skončilo; výsledok sa vyhodnocuje do ${formatRelativeDay(new Date(state.horizon_end))}.`,
        completed: "Plán skončil a je vyhodnotený.",
        cancelled: "Plán bol zrušený ručným prepnutím do Self-use.",
        failed: `Plán zlyhal: ${state.error || "skontroluj menič."}`,
        idle: "Nie je uložený žiadny aktívny plán."
      };
      element.textContent = statusText[state.status] || `Stav plánu: ${state.status}`;
      element.classList.toggle("error", state.status === "failed");
    } else {
      element.textContent = "Nie je uložený žiadny aktívny plán.";
    }
    if (state.status === "charging" || state.status === "transitioning") await refreshLiveMetrics();
    else $("live-metrics").textContent = "";
    if (changedPlan && state.plan_id && !latestPlans.some(plan => plan.plan_id === state.plan_id)) {
      await refreshHistory();
    }
    updateNowCard();
    renderOverviewPlan();
  } catch (error) {
    element.textContent = `Stav plánu sa nedá načítať: ${error.message}`;
    element.classList.add("error");
  }
}

async function refreshLiveMetrics() {
  const element = $("live-metrics");
  try {
    const metrics = await fetchJson("/api/live-metrics");
    if (!metrics.is_active) {
      element.textContent = "";
      return;
    }
    element.textContent = `Toto okno: ${historyNumber(metrics.grid_import_kwh, "kWh")} zo siete za ${formatEuro(metrics.plan_cost_eur)} (Self-use ${formatEuro(metrics.self_use_cost_eur)}, úspora ${formatEuro(metrics.savings_eur)}) · ${historyNumber(metrics.time_in_window_percent, "%")} okna`;
  } catch (error) {
    element.textContent = `Priebeh okna sa nedá načítať: ${error.message}`;
  }
}

/* ---------- On-demand plan ---------- */

function appendOptimizationLine(container, text, strong = false) {
  const line = document.createElement("p");
  line.className = "estimate";
  line.textContent = text;
  if (strong) line.style.fontWeight = "700";
  container.append(line);
}

function statTile(label, value, good = false) {
  const tile = document.createElement("div");
  tile.className = `tile${good ? " good" : ""}`;
  const name = document.createElement("span");
  name.className = "muted small";
  name.textContent = label;
  const number = document.createElement("strong");
  number.textContent = value;
  tile.append(name, number);
  return tile;
}

function showOptimizationPlan(plan) {
  $("optimization-card").hidden = false;
  const result = $("optimization-result");
  result.replaceChildren();
  result.classList.remove("error");
  const windows = plan.windows || [];
  $("optimization-meta").textContent = `${formatRelativeDay(new Date(plan.horizon_start))} – ${formatRelativeDay(new Date(plan.horizon_end))}`;
  const totalCharge = windows.reduce((sum, window) => sum + (Number(window.recommended_grid_charge_kwh) || 0), 0);
  const estimatedSavings = Number.isFinite(plan.estimated_savings_eur) ? plan.estimated_savings_eur : plan.cash_savings_eur;
  const tiles = document.createElement("div");
  tiles.className = "stat-tiles";
  tiles.append(
    statTile("Odhadovaná úspora", formatEuro(estimatedSavings), estimatedSavings > 0),
    statTile("Náklad s plánom", formatEuro(plan.cost_with_plan_eur)),
    statTile("Náklad v Self-use", formatEuro(plan.cost_self_use_eur)),
    statTile("Nabíjanie zo siete", historyNumber(totalCharge, "kWh"))
  );
  result.append(tiles);
  if (plan.consumption_history_sync_warning) appendOptimizationLine(result, plan.consumption_history_sync_warning);
  if (windows.length === 0) {
    appendOptimizationLine(result, "V tomto horizonte sa nabíjanie zo siete neoplatí — ceny alebo stav batérie ho neodôvodňujú.", true);
  }
  appendOptimizationLine(result, "Úspora zahŕňa hodnotu energie, ktorá ostane v batérii na konci horizontu, a opotrebenie batérie.");
  renderPlanChart($("optimization-chart"), plan);
  renderWindowChips($("optimization-windows"), windows);
  showHourlyPlan(plan);
  const applyButton = $("apply-optimization");
  applyButton.hidden = windows.length === 0;
  applyButton.disabled = windows.length === 0 || Boolean(continuousState?.enabled);
}

function showHourlyPlan(plan) {
  const container = $("hourly-plan");
  const hours = plan.hourly_plan || [];
  container.hidden = hours.length === 0;
  appendHistoryTable(
    container,
    ["Hodina", "Tarifa €/kWh", "FVE kWh", "Spotreba kWh", "Nabíjanie zo siete kWh", "SOC na konci", "Odber zo siete kWh", "Náklad €"],
    hours.map(hour => [
      formatRelativeDay(new Date(hour.time)),
      Number(hour.tariff_eur_per_kwh).toLocaleString("sk-SK", { minimumFractionDigits: 3, maximumFractionDigits: 3 }),
      historyNumber(hour.solar_kwh),
      historyNumber(hour.consumption_kwh),
      historyNumber(hour.grid_charge_kwh),
      historyNumber(hour.soc_end, "%"),
      historyNumber(hour.grid_import_kwh),
      Number(hour.cost_eur).toLocaleString("sk-SK", { minimumFractionDigits: 2, maximumFractionDigits: 3 })
    ])
  );
}

function invalidateOptimizationPlan() {
  optimizationPlan = null;
  $("apply-optimization").disabled = true;
  $("apply-optimization").hidden = true;
}

async function calculateOptimization() {
  const button = $("calculate-optimization");
  button.disabled = true;
  $("apply-optimization").hidden = true;
  $("optimization-card").hidden = false;
  $("optimization-result").textContent = "Načítavam predpoveď počasia a aktuálny stav batérie…";
  const settings = readOptimizationSettings();
  saveOptimizationSettings(settings);
  try {
    const plan = await postJson("/api/optimization", settings);
    optimizationPlan = plan;
    showOptimizationPlan(plan);
    if (plan.history_warning) appendOptimizationLine($("optimization-result"), plan.history_warning);
  } catch (error) {
    optimizationPlan = null;
    $("optimization-result").textContent = error.message;
    $("optimization-result").classList.add("error");
  } finally {
    button.disabled = Boolean(continuousState?.enabled);
  }
}

async function applyOptimization() {
  if (!optimizationPlan || !controlReady) {
    if (!controlReady) showError(new Error("Ovládanie meniča nie je overené; plán sa nedá uložiť."));
    return;
  }
  const windows = optimizationPlan.windows || [];
  if (windows.length === 0) return;
  if (Date.now() - Date.parse(optimizationPlan.generated_at) > 10 * 60 * 1000) {
    invalidateOptimizationPlan();
    showError(new Error("Plán je starší ako 10 minút. Vypočítaj nový podľa aktuálneho SOC a predpovede."));
    return;
  }
  const confirmed = await confirmAction({
    title: `Naplánovať ${windows.length === 1 ? "1 nabíjacie okno" : `${windows.length} nabíjacie okná`}?`,
    lines: [
      ...windows.map(window => `⚡ ${windowLabel(window)} · ${window.charge_power_w || 2000} W · do ${window.recommended_target_soc} %`),
      "Po každom okne appka vráti menič do Self-use. Server musí počas plánu bežať."
    ],
    confirmLabel: "Naplánovať"
  });
  if (!confirmed) return;
  const button = $("apply-optimization");
  button.disabled = true;
  try {
    const result = await postJson("/api/optimization-plan", {
      windows,
      plan_date: optimizationPlan.plan_date,
      generated_at: optimizationPlan.generated_at,
      plan_snapshot: optimizationPlan,
      confirmed: true
    });
    notify(`Plán je uložený. Appka zapne nabíjanie v určených časoch.${result.history_warning ? ` ${result.history_warning}` : ""}`, "success");
    button.hidden = true;
    await refreshHistory();
    await refreshScheduledPlan();
  } catch (error) {
    showError(error);
    button.disabled = false;
  }
}

/* ---------- History ---------- */

function appendHistoryTable(container, headings, rows) {
  container.replaceChildren();
  if (rows.length === 0) {
    container.textContent = "Zatiaľ nie sú zaznamenané žiadne údaje.";
    return;
  }
  const table = document.createElement("table");
  const head = document.createElement("thead");
  const headerRow = document.createElement("tr");
  for (const heading of headings) {
    const cell = document.createElement("th");
    cell.textContent = heading;
    headerRow.append(cell);
  }
  head.append(headerRow);
  table.append(head);
  const body = document.createElement("tbody");
  for (const values of rows) {
    const row = document.createElement("tr");
    for (const value of values) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    }
    body.append(row);
  }
  table.append(body);
  container.append(table);
}

function appendCostMetric(container, label, value, unit = "€") {
  const row = document.createElement("div");
  row.className = "cost-comparison-row";
  const name = document.createElement("span");
  name.textContent = label;
  const amount = document.createElement("strong");
  amount.textContent = historyNumber(value, unit);
  row.append(name, amount);
  container.append(row);
}

function renderPlanCostComparison(container, forecast, actual, planStatus) {
  const grid = document.createElement("div");
  grid.className = "cost-comparison-grid";

  const forecastCard = document.createElement("section");
  forecastCard.className = "cost-comparison-card";
  const forecastHeading = document.createElement("h4");
  forecastHeading.textContent = forecast.continuous_optimization ? "Najnovší plán automatiky" : "Predikcia pre celý horizont";
  forecastCard.append(forecastHeading);
  const predictedSavings = Number.isFinite(forecast.cash_savings_eur)
    ? forecast.cash_savings_eur
    : Number.isFinite(forecast.cost_self_use_eur) && Number.isFinite(forecast.cost_with_plan_eur)
      ? forecast.cost_self_use_eur - forecast.cost_with_plan_eur
      : null;
  appendCostMetric(forecastCard, "S plánom", forecast.cost_with_plan_eur);
  appendCostMetric(forecastCard, "V Self-use", forecast.cost_self_use_eur);
  appendCostMetric(forecastCard, "Rozdiel nákladov", predictedSavings);
  const forecastNote = document.createElement("p");
  forecastNote.className = "cost-comparison-note";
  forecastNote.textContent = "Rozdiel je Self-use mínus plán, bez hodnoty energie, ktorá ostane v batérii.";
  forecastCard.append(forecastNote);

  const actualCard = document.createElement("section");
  actualCard.className = "cost-comparison-card";
  const actualHeading = document.createElement("h4");
  actualHeading.textContent = forecast.continuous_optimization ? "Výsledok celého behu" : "Skutočnosť a Self-use model";
  actualCard.append(actualHeading);
  if (actual) {
    const hasMatchedSelfUseCost = Number.isFinite(actual.self_use_cost_on_simulation_coverage_eur);
    const matchedSelfUseCost = hasMatchedSelfUseCost
      ? actual.self_use_cost_on_simulation_coverage_eur
      : actual.self_use_counterfactual_cost_eur;
    const comparisonCoverage = Number.isFinite(actual.cost_comparison_coverage_percent)
      ? actual.cost_comparison_coverage_percent
      : actual.self_use_simulation_coverage_percent;
    appendCostMetric(actualCard, "Plán (porovnateľné intervaly)", actual.actual_cost_on_simulation_coverage_eur);
    appendCostMetric(actualCard, hasMatchedSelfUseCost ? "Self-use (rovnaké intervaly)" : "Self-use (dostupný rozsah)", matchedSelfUseCost);
    appendCostMetric(actualCard, hasMatchedSelfUseCost ? "Rozdiel" : "Rozdiel (starší odhad)", actual.estimated_savings_vs_self_use_eur);
    appendCostMetric(actualCard, "Nameraný náklad za horizont", actual.actual_cost_with_aid_eur);
    const actualNote = document.createElement("p");
    actualNote.className = "cost-comparison-note";
    actualNote.textContent = `Pokrytie porovnania ${historyNumber(comparisonCoverage, "%")}, telemetria ${historyNumber(actual.telemetry_coverage_percent, "%")}. Self-use je simulácia, nie samostatné meranie.`;
    actualCard.append(actualNote);
  } else {
    appendCostMetric(actualCard, "Skutočný náklad plánu", null);
    appendCostMetric(actualCard, "Self-use podľa skutočných dát", null);
    const pendingNote = document.createElement("p");
    pendingNote.className = "cost-comparison-note";
    pendingNote.textContent = forecast.continuous_optimization
      ? "Aktualizuje sa pri každom prepočte; finálne vyhodnotenie sa uloží po vypnutí automatiky."
      : planStatus === "evaluating"
        ? "Okná sa skončili; výsledok sa doplní po skončení horizontu."
        : "Doplní sa po vyhodnotení celého horizontu.";
    actualCard.append(pendingNote);
  }
  grid.append(forecastCard, actualCard);
  container.append(grid);
}

function renderContinuousSessionEvaluation(container, actual) {
  const hourly = actual.hourly || [];
  if (hourly.length === 0) return;
  const chartPanel = document.createElement("section");
  chartPanel.className = "chart-panel";
  const chart = document.createElement("div");
  renderLineChart(
    chart,
    [
      { name: "Náklad s automatikou", color: "#f6c85f", points: hourly.map(hour => ({ time: hour.time, value: hour.simulation_cost_eur })) },
      { name: "Self-use · model", color: "#5dade2", points: hourly.map(hour => ({ time: hour.time, value: hour.self_use_cost_eur })) }
    ],
    "Hodinový náklad počas behu",
    "€"
  );
  chartPanel.append(chart);
  container.append(chartPanel);

  const tableContainer = document.createElement("div");
  tableContainer.className = "table-wrap";
  appendHistoryTable(
    tableContainer,
    ["Hodina", "S automatikou", "Self-use model", "Rozdiel", "Pokrytie"],
    hourly.map(hour => [
      formatRelativeDay(new Date(hour.time)),
      historyNumber(hour.simulation_cost_eur, "€"),
      historyNumber(hour.self_use_cost_eur, "€"),
      historyNumber(hour.estimated_savings_eur, "€"),
      historyNumber(hour.coverage_percent, "%")
    ])
  );
  const tableDetails = document.createElement("details");
  tableDetails.className = "plan-result";
  const tableSummary = document.createElement("summary");
  tableSummary.textContent = "Hodinové vyhodnotenie behu";
  tableDetails.append(tableSummary, tableContainer);
  container.append(tableDetails);
}

function renderOptimizationResults(plans) {
  const container = $("plan-results");
  container.replaceChildren();
  const visiblePlans = plans.filter(plan => !(plan.plan_status === "replaced" && plan.forecast?.continuous_optimization));
  if (visiblePlans.length === 0) {
    container.textContent = "Zatiaľ nie sú uložené žiadne plány.";
    return;
  }
  const statusNames = {
    pending: "čaká", charging: "nabíja", evaluating: "vyhodnocuje sa", completed: "vyhodnotený",
    continuous: "automatika beží", cancelled: "zrušený", replaced: "nahradený", failed: "zlyhal"
  };
  for (const plan of visiblePlans) {
    const forecast = plan.forecast || {};
    const actual = plan.actual;
    const details = document.createElement("details");
    details.className = "plan-result";
    const summary = document.createElement("summary");
    const startTime = actual?.continuous_session_start || plan.horizon_start;
    const endTime = actual?.continuous_session_end || plan.horizon_end;
    const start = startTime ? formatRelativeDay(new Date(startTime)) : "—";
    const end = endTime ? formatRelativeDay(new Date(endTime)) : "—";
    const savings = Number.isFinite(actual?.estimated_savings_vs_self_use_eur)
      ? ` · ${formatEuro(actual.estimated_savings_vs_self_use_eur)}`
      : "";
    summary.textContent = `${forecast.continuous_optimization ? "Automatika" : "Plán"} · ${statusNames[plan.plan_status] || plan.plan_status} · ${start} – ${end}${savings}`;
    details.append(summary);
    renderPlanCostComparison(details, forecast, actual, plan.plan_status);
    if (actual) {
      appendOptimizationLine(
        details,
        `Namerané: výroba ${historyNumber(actual.actual_generation_kwh, "kWh")}, spotreba ${historyNumber(actual.actual_consumption_kwh, "kWh")}, odber ${historyNumber(actual.actual_grid_import_kwh, "kWh")} · ${actual.telemetry_sample_count || 0} meraní, pokrytie ${historyNumber(actual.telemetry_coverage_percent, "%")}.`
      );
      if (forecast.continuous_optimization) {
        renderContinuousSessionEvaluation(details, actual);
      } else {
        const actualByTime = new Map((actual.hourly || []).map(hour => [hour.time, hour]));
        renderPlanComparisonCharts(details, forecast, actual);
        const tableContainer = document.createElement("div");
        tableContainer.className = "table-wrap";
        appendHistoryTable(
          tableContainer,
          ["Čas", "FVE plán / skut. kWh", "Spotreba plán / skut. kWh", "Odber plán / skut. kWh", "Náklad plán / skut. €", "SOC plán / skut."],
          (forecast.hourly_plan || []).map(hour => {
            const measured = actualByTime.get(hour.time) || {};
            return [
              formatRelativeDay(new Date(hour.time)),
              `${historyNumber(hour.solar_kwh)} / ${historyNumber(measured.solar_kwh)}`,
              `${historyNumber(hour.consumption_kwh)} / ${historyNumber(measured.consumption_kwh)}`,
              `${historyNumber(hour.grid_import_kwh)} / ${historyNumber(measured.grid_import_kwh)}`,
              `${historyNumber(hour.cost_eur)} / ${historyNumber(measured.cost_eur)}`,
              `${historyNumber(hour.soc_end, "%")} / ${historyNumber(measured.soc_end, "%")}`
            ];
          })
        );
        const tableDetails = document.createElement("details");
        tableDetails.className = "plan-result";
        const tableSummary = document.createElement("summary");
        tableSummary.textContent = "Hodinová tabuľka";
        tableDetails.append(tableSummary, tableContainer);
        details.append(tableDetails);
      }
    }
    container.append(details);
  }
}

async function refreshHistory() {
  const status = $("history-status");
  status.textContent = "Načítavam…";
  try {
    const [history, planResult] = await Promise.all([
      fetchJson("/api/history"),
      fetchJson("/api/optimization-results")
    ]);
    latestPlans = planResult.plans || [];
    renderOptimizationResults(latestPlans);
    renderOverviewPlan();

    appendHistoryTable(
      $("event-history"),
      ["Čas", "Typ", "Udalosť", "Detaily"],
      history.events.map(event => {
        const details = event.details || {};
        const values = [];
        if (details.recommended_grid_charge_kwh !== undefined) values.push(`${historyNumber(details.recommended_grid_charge_kwh, "kWh")} nabíjanie`);
        if (details.estimated_savings_eur !== undefined) values.push(`${historyNumber(details.estimated_savings_eur, "€")} modelovaný rozdiel`);
        if (details.actual_grid_import_kwh !== undefined) values.push(`${historyNumber(details.actual_grid_import_kwh, "kWh")} skutočný odber`);
        if (details.mode) values.push(`režim: ${details.mode}`);
        return [
          new Date(event.created_at).toLocaleString("sk-SK"),
          eventNames[event.event_type] || event.event_type,
          event.summary,
          values.join(" · ")
        ];
      })
    );
    appendHistoryTable(
      $("telemetry-history"),
      ["Čas", "SOC", "FVE", "Dom", "Sieť", "Denný odber", "Odber za interval", "Cena (pomoc / bez)"],
      history.telemetry.map(sample => [
        new Date(sample.sampled_at).toLocaleString("sk-SK"),
        historyNumber(sample.battery_soc, "%"),
        historyNumber(sample.generation_kw, "kW"),
        historyNumber(sample.consumption_kw, "kW"),
        historyNumber(sample.grid_kw, "kW"),
        historyNumber(sample.daily_grid_import_kwh, "kWh"),
        historyNumber(sample.observed_grid_import_delta_kwh, "kWh"),
        `${historyNumber(sample.observed_grid_import_cost_with_aid_eur, "€")} / ${historyNumber(sample.observed_grid_import_cost_without_aid_eur, "€")}`
      ])
    );
    status.textContent = `${history.events.length} udalostí · ${history.telemetry.length} meraní`;
    status.classList.remove("error");
  } catch (error) {
    status.textContent = `História sa nedá načítať: ${error.message}`;
    status.classList.add("error");
  }
}

let sofarHistory = null;

function sofarDayLabel(isoDate) {
  const label = new Date(`${isoDate}T12:00:00`).toLocaleDateString("sk-SK", { weekday: "long", day: "numeric", month: "long" });
  const today = new Date();
  const isToday = isoDate === `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
  return isToday ? `${label} (dnes)` : label;
}

function renderSofarHistory() {
  const container = $("sofar-history-days");
  container.replaceChildren();
  const aid = energyAid();
  for (const day of [...(sofarHistory?.days || [])].reverse()) {
    const totals = day.totals;
    const article = document.createElement("article");
    article.className = "history-day";
    const heading = document.createElement("h3");
    heading.textContent = sofarDayLabel(day.date);
    const summary = document.createElement("p");
    summary.className = "muted small";
    summary.textContent = `FVE ${historyNumber(totals.pv_kwh, "kWh")} · spotreba ${historyNumber(totals.consumption_kwh, "kWh")} · zo siete ${historyNumber(totals.grid_import_kwh, "kWh")} (${formatEuro(aid ? totals.cost_with_aid_eur : totals.cost_without_aid_eur)}), z toho nabíjanie batérie ${historyNumber(totals.grid_charge_kwh, "kWh")} · do siete ${historyNumber(totals.grid_export_kwh, "kWh")} · batéria +${historyNumber(totals.battery_charge_kwh, "kWh")} / −${historyNumber(totals.battery_discharge_kwh, "kWh")}`;
    const chart = document.createElement("div");
    chart.className = "chart-box";
    article.append(heading, summary, chart);
    container.append(article);
    renderHistoryDayChart(chart, day, aid);
  }
  if (!sofarHistory?.days?.length) emptyState(container, "Zatiaľ nie sú dáta za posledných 7 dní.");
}

async function refreshSofarHistory() {
  const status = $("sofar-history-status");
  try {
    sofarHistory = await fetchJson("/api/sofar-history?days=7");
    renderSofarHistory();
    status.classList.remove("error");
    status.textContent = `SofarCloud · aktualizované ${formatClock(new Date(sofarHistory.generated_at))} · SOC len z lokálnych meraní`;
  } catch (error) {
    status.textContent = `Históriu sa nepodarilo načítať: ${error.message}`;
    status.classList.add("error");
  }
}

/* ---------- Wiring ---------- */

$("requested-mode").addEventListener("change", event => {
  $("charge-power-row").hidden = event.target.value !== "grid_charge";
  const discharge = event.target.value === "grid_discharge";
  $("discharge-power-row").hidden = !discharge;
  $("discharge-estimate").hidden = !discharge;
  updateDischargeEstimate();
});
$("charge-power").addEventListener("input", event => { $("charge-power-value").textContent = event.target.value; });
$("discharge-power").addEventListener("input", event => {
  $("discharge-power-value").textContent = event.target.value;
  updateDischargeEstimate();
});
$("refresh").addEventListener("click", () => {
  refresh();
  refreshScheduledPlan();
  refreshContinuousOptimization();
  refreshSofarHistory();
  refreshHistory();
});
$("refresh-history").addEventListener("click", () => Promise.all([refreshHistory(), refreshSofarHistory()]));
$("apply-mode").addEventListener("click", applyMode);
$("calculate-optimization").addEventListener("click", calculateOptimization);
$("apply-optimization").addEventListener("click", applyOptimization);
$("continuous-toggle").addEventListener("click", toggleContinuousOptimization);
$("continuous-days").addEventListener("change", refreshContinuousResults);
for (const id of ["optimization-location", "optimization-pv", "optimization-summer", "optimization-winter", "optimization-aid", "optimization-horizon"]) {
  $(id).addEventListener("input", invalidateOptimizationPlan);
  $(id).addEventListener("change", invalidateOptimizationPlan);
}
$("optimization-aid").addEventListener("change", () => {
  updateNowCard();
  renderSofarHistory();
});

setTab(location.hash.slice(1), false);
loadOptimizationSettings();
refresh();
loadControls();
refreshTariff();
refreshScheduledPlan();
refreshContinuousOptimization();
refreshHistory();
refreshSofarHistory();
window.setInterval(refresh, 60_000);
window.setInterval(refreshScheduledPlan, 10_000);
window.setInterval(refreshDischargeStatus, 10_000);
window.setInterval(refreshContinuousOptimization, 60_000);
window.setInterval(refreshHistory, 60_000);
window.setInterval(refreshSofarHistory, 900_000);
window.setInterval(refreshTariff, 900_000);
window.setInterval(updateNowCard, 30_000);
