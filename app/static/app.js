"use strict";

const $ = (id) => document.getElementById(id);
const token = new URLSearchParams(location.search).get("token") || "";
const STORAGE_KEY = "sephiria-solver-build-wiki-v1";
const GRID_COLS = 6;
const state = {
  catalog: { artifacts: [], tablets: [] },
  customTabletTypes: [],
  kind: "artifact",
  items: [],
  result: null,
  solveId: null,
  finishedSolveId: null,
  solveUsedGameSource: false,
  gameSource: null,
  serial: 1,
  pollTimer: null,
  composeNameDirty: false,
};

const criteriaLabels = {
  edge: "背包边缘启用", inner: "背包内侧启用", side_free: "左右留空启用",
  top: "最上行启用", bottom: "最下行启用", side_end: "左右两端启用",
  both_side_artifacts: "两侧均为神器启用",
};
const tierLabels = { common: "普通", advanced: "高级", rare: "稀有", legend: "传说", solid: "羁绊", special: "特殊", custom: "自定义" };
const specialConditionLabels = {
  nearby_levels: "优先周围神器等级",
  top_row_artifacts: "优先最上行神器",
  nearby_planets: "优先周围行星",
  matching_side_categories: "优先两侧共同组合",
  target_above: "优先指定上方神器",
};

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "X-Sephiria-Token": token, ...(options.headers || {}) } });
  let body = null;
  try { body = await response.json(); } catch { body = {}; }
  if (!response.ok) throw new Error(body?.error?.message || `请求失败 (${response.status})`);
  return body;
}

function findType(kind, typeId) {
  const source = kind === "artifact" ? state.catalog.artifacts : [...state.catalog.tablets, ...state.customTabletTypes];
  return source.find((entry) => entry.id === typeId);
}

function availableTabletTypes() {
  return [
    ...state.catalog.tablets,
    ...state.customTabletTypes.filter((type) => type.cellCount === gridCellCount()),
  ];
}

function alphabeticalLabel(index) {
  let value = index + 1; let label = "";
  while (value > 0) { value -= 1; label = String.fromCharCode(65 + (value % 26)) + label; value = Math.floor(value / 26); }
  return label;
}

function itemDisplayName(item, type) {
  if (item?.kind !== "tablet" || !type?.custom) return type?.name || "";
  const customItems = state.items.filter((candidate) => candidate.kind === "tablet" && findType("tablet", candidate.typeId)?.custom);
  const index = customItems.findIndex((candidate) => candidate.instanceId === item.instanceId);
  return index < 0 ? type.name : alphabeticalLabel(index);
}

function itemImageRotationDegrees(kind, rotation) {
  if (kind !== "tablet") return 0;
  return placementRotation({ rotation }) * 90;
}

function itemVisual(type, kind, rotation = 0, displayName = type.name) {
  const visual = document.createElement("span");
  visual.className = `item-visual ${kind === "tablet" ? "tablet" : ""}${type.custom ? " custom" : ""}`;
  if (type.image) {
    const img = document.createElement("img");
    img.src = type.image; img.alt = ""; img.loading = "lazy";
    img.style.setProperty("--item-rotation", `${itemImageRotationDegrees(kind, rotation)}deg`);
    img.addEventListener("error", () => { visual.textContent = displayName.slice(0, 1); });
    visual.append(img);
  } else {
    visual.textContent = displayName.slice(0, 1);
  }
  return visual;
}

function catalogSubtitle(type, kind) {
  if (kind === "artifact") {
    const details = [`上限 ${type.cap}`];
    if (type.categories?.length) details.push(type.categories.join(" / "));
    if (type.criteria?.length) details.push(type.criteria.map((c) => criteriaLabels[c] || c).join("、"));
    return details.join(" · ");
  }
  const details = [tierLabels[type.tier] || type.tier, type.rotatable ? "可旋转" : "固定方向"];
  if (type.constraint === "last_row") details.push("仅最后一行");
  if (type.constraint === "first_row") details.push("仅第一行");
  if (type.constraint === "first_or_last_col") details.push("仅左右边列");
  return details.join(" · ");
}

function renderCatalog() {
  const list = $("catalogList");
  list.replaceChildren();
  const source = state.kind === "artifact" ? state.catalog.artifacts : availableTabletTypes();
  const query = $("searchInput").value.trim().toLocaleLowerCase();
  const filtered = source.filter((type) => {
    const haystack = [type.name, ...(type.categories || []), ...(type.criteria || []).map((c) => criteriaLabels[c] || c), tierLabels[type.tier] || type.tier].join(" ".toLocaleLowerCase());
    return !query || haystack.toLocaleLowerCase().includes(query);
  });
  $("catalogCount").textContent = `${filtered.length} / ${source.length} 种`;
  const fragment = document.createDocumentFragment();
  filtered.forEach((type) => {
    const button = document.createElement("button");
    button.type = "button"; button.className = "catalog-item";
    button.title = `添加${state.kind === "artifact" ? "神器" : "石板"}：${type.name}`;
    button.append(itemVisual(type, state.kind));
    const copy = document.createElement("span"); copy.className = "item-copy";
    const name = document.createElement("strong"); name.textContent = type.name;
    const subtitle = document.createElement("span"); subtitle.textContent = catalogSubtitle(type, state.kind);
    copy.append(name, subtitle);
    const mark = document.createElement("span"); mark.className = "add-mark"; mark.textContent = "+"; mark.setAttribute("aria-hidden", "true");
    button.append(copy, mark);
    button.addEventListener("click", () => addItem(state.kind, type.id));
    fragment.append(button);
  });
  list.append(fragment);
}

function addItem(kind, typeId) {
  const capacity = gridCellCount();
  if (state.items.length >= capacity) return showToast("背包格数不足，无法继续添加。");
  const prefix = kind === "artifact" ? "a" : "t";
  state.items.push({ instanceId: `${prefix}-${Date.now().toString(36)}-${state.serial++}`, typeId, kind, weight: kind === "artifact" ? 5 : 1, baseLevel: 0, minLevel: null, exactLevel: null, fixedCell: null, fixedRotation: null, specialPriority: false, specialTargetInstanceId: null });
  state.result = null;
  persist(); renderOwned(); renderBoard(); updateStatusIdle();
}

function removeItem(instanceId) {
  state.items = state.items.filter((item) => item.instanceId !== instanceId);
  state.items.forEach((item) => { if (item.specialTargetInstanceId === instanceId) item.specialTargetInstanceId = null; });
  state.result = null; persist(); renderOwned(); renderBoard(); updateStatusIdle();
}

function constraintInput(label, value, options, onChange) {
  const wrapper = document.createElement("label"); wrapper.textContent = label;
  let input;
  if (options) {
    input = document.createElement("select");
    options.forEach(([optionValue, text]) => {
      const option = document.createElement("option"); option.value = optionValue; option.textContent = text; input.append(option);
    });
    input.value = value == null ? "" : String(value);
  } else {
    input = document.createElement("input"); input.type = "number"; input.value = value == null ? "" : String(value);
  }
  input.setAttribute("aria-label", label);
  input.addEventListener("input", () => onChange(input.value)); wrapper.append(input); return wrapper;
}

function cellOptions() {
  const options = [["", "自动"]];
  for (let i = 0; i < gridCellCount(); i += 1) options.push([String(i), `${i + 1}（${Math.floor(i / cols()) + 1}行${i % cols() + 1}列）`]);
  return options;
}

function specialPriorityInput(item, type) {
  const label = document.createElement("label"); label.className = "special-priority";
  const input = document.createElement("input"); input.type = "checkbox"; input.checked = Boolean(item.specialPriority);
  input.setAttribute("aria-label", specialConditionLabels[type.specialCondition]);
  const text = document.createElement("span"); text.textContent = specialConditionLabels[type.specialCondition];
  input.addEventListener("change", () => {
    item.specialPriority = input.checked; state.result = null; persist(); renderOwned(); renderBoard(); updateStatusIdle();
  });
  label.append(input, text); return label;
}

function artifactTargetOptions(sourceItem) {
  const options = [["", "请选择神器"]];
  state.items.forEach((item, index) => {
    if (item.kind !== "artifact" || item.instanceId === sourceItem.instanceId) return;
    const type = findType("artifact", item.typeId);
    if (type) options.push([item.instanceId, `${type.name} · 第 ${index + 1} 件`]);
  });
  return options;
}

function renderOwned() {
  const list = $("ownedList"); list.replaceChildren();
  const artifactCount = state.items.filter((item) => item.kind === "artifact").length;
  const tabletCount = state.items.length - artifactCount;
  $("ownedCount").textContent = `${state.items.length} 件`; $("artifactOwned").textContent = artifactCount; $("tabletOwned").textContent = tabletCount;
  $("emptyCells").textContent = Math.max(0, gridCellCount() - state.items.length);
  $("combineTabletBtn").disabled = tabletCount < 2;
  if (!state.items.length) { const empty = document.createElement("div"); empty.className = "empty-owned"; empty.textContent = "从物品目录添加本局已获取的神器和石板。"; list.append(empty); return; }
  state.items.forEach((item) => {
    const type = findType(item.kind, item.typeId); if (!type) return;
    const displayName = itemDisplayName(item, type);
    const row = document.createElement("article"); row.className = "owned-item";
    const head = document.createElement("div"); head.className = "owned-head"; head.append(itemVisual(type, item.kind, item.fixedRotation, displayName));
    const copy = document.createElement("div"); const name = document.createElement("strong"); name.textContent = displayName;
    const meta = document.createElement("span"); meta.textContent = catalogSubtitle(type, item.kind); copy.append(name, meta);
    const remove = document.createElement("button"); remove.type = "button"; remove.className = "remove-button"; remove.textContent = "×"; remove.title = `移除 ${displayName}`; remove.addEventListener("click", () => removeItem(item.instanceId));
    head.append(copy, remove); row.append(head);
    const constraints = document.createElement("div"); constraints.className = "constraints";
    constraints.append(constraintInput("固定位置", item.fixedCell, cellOptions(), (raw) => updateItem(item, "fixedCell", raw === "" ? null : Number(raw))));
    if (item.kind === "artifact") {
      constraints.append(constraintInput("附魔等级", item.baseLevel ?? 0, null, (raw) => updateItem(item, "baseLevel", raw === "" ? 0 : Number(raw))));
      constraints.lastChild.querySelector("input").min = "0"; constraints.lastChild.querySelector("input").max = "99";
      constraints.append(constraintInput("最低等级", item.minLevel, null, (raw) => updateItem(item, "minLevel", raw === "" ? null : Number(raw))));
      constraints.lastChild.querySelector("input").min = "-99"; constraints.lastChild.querySelector("input").max = String(type.cap);
      constraints.append(constraintInput("固定等级", item.exactLevel, null, (raw) => updateItem(item, "exactLevel", raw === "" ? null : Number(raw))));
      constraints.lastChild.querySelector("input").min = "-99"; constraints.lastChild.querySelector("input").max = String(type.cap);
      constraints.append(constraintInput("优先权重", item.weight, [["1", "1"], ["2", "2"], ["3", "3"], ["5", "5 默认"], ["8", "8"], ["10", "10 最高"]], (raw) => updateItem(item, "weight", Number(raw))));
      if (type.specialCondition) {
        constraints.append(specialPriorityInput(item, type));
        if (type.specialCondition === "target_above" && item.specialPriority) {
          constraints.append(constraintInput("上方目标", item.specialTargetInstanceId, artifactTargetOptions(item), (raw) => updateItem(item, "specialTargetInstanceId", raw || null)));
        }
      }
    } else {
      const rotations = [["", "自动"], ["0", "0°"]];
      if (type.rotatable) rotations.push(["1", "90°"], ["2", "180°"], ["3", "270°"]);
      constraints.append(constraintInput("固定旋转", item.fixedRotation, rotations, (raw) => updateItem(item, "fixedRotation", raw === "" ? null : Number(raw))));
    }
    row.append(constraints); list.append(row);
  });
}

function updateItem(item, key, value) { item[key] = Number.isNaN(value) ? null : value; state.result = null; persist(); if (key === "fixedRotation") renderOwned(); renderBoard(); updateStatusIdle(); }
function gridCellCount() { return Math.max(1, Math.min(60, Math.trunc(Number($("cellCountInput").value)) || 30)); }
function rows() { return Math.ceil(gridCellCount() / GRID_COLS); }
function cols() { return GRID_COLS; }
function placementRotation(placement) {
  const turns = Number(placement?.rotation);
  return Number.isInteger(turns) ? ((turns % 4) + 4) % 4 : 0;
}

function toggleTabletRange(board, placement, active) {
  (placement.rangeCells || []).forEach((cellIndex) => {
    board.querySelector(`.cell[data-cell="${cellIndex}"]`)?.classList.toggle("tablet-range", active);
  });
  board.querySelector(`.cell[data-cell="${placement.cell}"]`)?.classList.toggle("tablet-range-source", active);
}

function toggleArtifactSources(board, detail, active) {
  board.querySelector(`.cell[data-cell="${detail.cell}"]`)?.classList.toggle("artifact-effect-target", active);
  (detail.tabletEffects || []).forEach((effect) => {
    const cell = board.querySelector(`.cell[data-cell="${effect.cell}"]`);
    if (!cell) return;
    cell.classList.toggle("artifact-effect-source", active);
    cell.querySelector(".artifact-source-effect")?.remove();
    if (!active) return;
    const badge = document.createElement("span"); badge.className = `artifact-source-effect${effect.additive < 0 ? " negative" : ""}`;
    const values = [];
    if (effect.additive) values.push(`${effect.additive > 0 ? "+" : ""}${effect.additive}等级`);
    if (effect.multiplier) values.push(`倍率${effect.multiplier}`);
    badge.textContent = values.join(" · "); cell.append(badge);
  });
}

function renderBoard() {
  const board = $("board"); board.style.setProperty("--cols", String(cols())); board.replaceChildren();
  const placements = new Map((state.result?.placements || []).map((entry) => [entry.cell, entry]));
  const details = new Map((state.result?.artifacts || []).map((entry) => [entry.instanceId, entry]));
  const unlocked = new Set(state.result?.unlockedCells || []); const effects = state.result?.cellEffects || [];
  for (let cellIndex = 0; cellIndex < gridCellCount(); cellIndex += 1) {
    const cell = document.createElement("div"); cell.className = `cell${unlocked.has(cellIndex) ? " unlock" : ""}`;
    cell.dataset.cell = String(cellIndex);
    const index = document.createElement("span"); index.className = "cell-index"; index.textContent = String(cellIndex + 1); cell.append(index);
    if (effects[cellIndex]) { const effect = document.createElement("span"); effect.className = `cell-effect${effects[cellIndex] < 0 ? " negative" : ""}`; effect.textContent = effects[cellIndex] > 0 ? `+${effects[cellIndex]}` : String(effects[cellIndex]); cell.append(effect); }
    const placement = placements.get(cellIndex);
    if (placement) {
      const source = state.items.find((item) => item.instanceId === placement.instanceId); const type = source && findType(source.kind, source.typeId);
      if (source && type) {
        const displayName = itemDisplayName(source, type);
        cell.classList.add(source.kind); const content = document.createElement("div"); content.className = "cell-item";
        const rotation = placementRotation(placement);
        if (type.image) { const img = document.createElement("img"); img.src = type.image; img.alt = ""; img.style.setProperty("--item-rotation", `${itemImageRotationDegrees(source.kind, rotation)}deg`); content.append(img); }
        const label = document.createElement("strong"); label.textContent = displayName; content.append(label);
        const sub = document.createElement("small"); const detail = details.get(source.instanceId);
        if (source.kind === "artifact") {
          const level = detail?.level ?? 0;
          const levelState = level < 0 ? "negative" : level > type.cap ? "overcapped" : level === type.cap ? "capped" : "partial";
          sub.className = `artifact-level ${levelState}`;
          sub.textContent = `Lv.${level}/${type.cap}`;
          content.tabIndex = 0;
          content.addEventListener("mouseenter", () => toggleArtifactSources(board, detail, true));
          content.addEventListener("mouseleave", () => toggleArtifactSources(board, detail, false));
          content.addEventListener("focus", () => toggleArtifactSources(board, detail, true));
          content.addEventListener("blur", () => toggleArtifactSources(board, detail, false));
        } else {
          sub.textContent = `${rotation * 90}°`;
          content.title = `${displayName} · ${placement.rangeCells?.length || 0} 格作用范围`;
          content.addEventListener("mouseenter", () => toggleTabletRange(board, placement, true));
          content.addEventListener("mouseleave", () => toggleTabletRange(board, placement, false));
        }
        content.append(sub); cell.append(content);
      }
    }
    board.append(cell);
  }
}

function updateGrid() {
  $("cellCountInput").value = gridCellCount();
  if (state.items.length > gridCellCount()) { state.items = state.items.slice(0, gridCellCount()); showToast("网格缩小后，超出容量的末尾物品已移除。"); }
  let cleared = 0;
  state.items.forEach((item) => { if (item.fixedCell != null && item.fixedCell >= gridCellCount()) { item.fixedCell = null; cleared += 1; } });
  if (cleared) showToast(`已清除 ${cleared} 个越界固定位置。`);
  const incompatible = new Set(state.customTabletTypes
    .filter((type) => type.cellCount !== gridCellCount()).map((type) => type.id));
  const previousCount = state.items.length;
  state.items = state.items.filter((item) => item.kind !== "tablet" || !incompatible.has(item.typeId));
  if (state.items.length !== previousCount) showToast("背包格数已变化，不兼容的自定义石板已从构筑中移除。");
  state.result = null; persist(); renderCatalog(); renderOwned(); renderBoard(); updateStatusIdle();
}

function scheduleGridUpdate() {
  clearTimeout(scheduleGridUpdate.timer);
  if (!$("cellCountInput").value.trim()) return;
  scheduleGridUpdate.timer = setTimeout(updateGrid, 200);
}

function requestPayload() {
  const customIds = new Set(state.items.filter((item) => item.kind === "tablet").map((item) => item.typeId));
  const payload = {
    grid: { cellCount: gridCellCount() },
    artifacts: state.items.filter((i) => i.kind === "artifact").map((i) => ({ instanceId: i.instanceId, typeId: i.typeId, weight: i.weight, baseLevel: i.baseLevel ?? 0, minLevel: i.minLevel, exactLevel: i.exactLevel, fixedCell: i.fixedCell, specialPriority: Boolean(i.specialPriority), specialTargetInstanceId: i.specialTargetInstanceId || null })),
    tablets: state.items.filter((i) => i.kind === "tablet").map((i) => ({ instanceId: i.instanceId, typeId: i.typeId, fixedCell: i.fixedCell, fixedRotation: i.fixedRotation, preferredRotation: i.preferredRotation ?? null })),
    customTabletTypes: state.customTabletTypes.filter((type) => customIds.has(type.id)),
    options: { timeLimitMs: Number($("timeLimit").value), workerCount: Number($("workerCount").value) },
  };
  if (gameSourceMatchesItems()) payload.gameSource = state.gameSource;
  return payload;
}

function gameSourceMatchesItems() {
  if (!state.gameSource?.fingerprint || state.gameSource.complete === false ||
      Number(state.gameSource.cellCount) !== gridCellCount() || !Array.isArray(state.gameSource.items)) return false;
  const sourceIds = new Set(state.gameSource.items.map((item) => item.solverInstanceId));
  return sourceIds.size === state.items.length && state.items.every((item) => sourceIds.has(item.instanceId));
}

async function startSolve() {
  if (!state.items.length) return showToast("请先添加本局已获取的物品。");
  if (state.items.length > gridCellCount()) return showToast("物品数量超过背包容量。");
  const missingTarget = state.items.find((item) => item.kind === "artifact" && item.typeId === "artifact-unalloyed_gold_needle" && item.specialPriority && !item.specialTargetInstanceId);
  if (missingTarget) return showToast("请为北向的金色针选择上方目标神器。");
  state.result = null; state.finishedSolveId = null; state.solveUsedGameSource = gameSourceMatchesItems(); updateApplyButton(); renderBoard(); setSolving(true); setStatus("solving", "正在构建精确模型", "求解器将放入全部已录入物品");
  try {
    const response = await api("/api/solve", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(requestPayload()) });
    state.solveId = response.solveId; pollSolve();
  } catch (error) { setSolving(false); state.solveId = null; setStatus("error", "无法开始求解", error.message); }
}

async function pollSolve() {
  if (!state.solveId) return;
  try {
    const response = await api(`/api/solve/${state.solveId}`);
    if (response.jobStatus === "FINISHED" || response.jobStatus === "FAILED") {
      const finishedId = state.solveId; state.solveId = null; setSolving(false);
      if (response.error) { setStatus("error", "求解失败", response.error.message); return; }
      showResult(response.result, finishedId); persist(); return;
    }
    setStatus("solving", "正在搜索最优排布", `任务 ${state.solveId.slice(0, 8)} · 可随时停止并保留已找到的解`);
    state.pollTimer = setTimeout(pollSolve, 350);
  } catch (error) { state.solveId = null; setSolving(false); setStatus("error", "读取求解状态失败", error.message); }
}

async function stopSolve() {
  if (!state.solveId) return;
  $("stopBtn").disabled = true;
  try { await api(`/api/solve/${state.solveId}`, { method: "DELETE" }); setStatus("warning", "正在停止", "等待求解器返回当前结果"); }
  catch (error) { showToast(error.message); $("stopBtn").disabled = false; }
}

async function readGameInventory() {
  if (state.items.length && !confirm("用游戏当前背包替换已录入的构筑？")) return;
  const button = $("readGameBtn"); button.disabled = true;
  try {
    const inventory = await api("/api/game-inventory");
    $("cellCountInput").value = inventory.grid.cellCount;
    state.customTabletTypes = Array.isArray(inventory.customTabletTypes) ? inventory.customTabletTypes : [];
    state.gameSource = inventory.source?.fingerprint ? inventory.source : null;
    state.items = [
      ...inventory.artifacts.map((item) => ({ ...item, kind: "artifact", fixedRotation: null })),
      ...inventory.tablets.map((item) => ({ ...item, kind: "tablet", weight: 1, baseLevel: null, minLevel: null, exactLevel: null, specialPriority: false, specialTargetInstanceId: null })),
    ];
    state.serial = state.items.length + 1; state.result = null;
    persist(); renderCatalog(); renderOwned(); renderBoard(); updateStatusIdle();
    const skipped = inventory.unmapped?.length || 0;
  if (skipped) {
    const names = (inventory.unmapped || []).map((item) => item.name || `实体 ${item.entityId}`).join("、");
    setStatus("warning", `有 ${skipped} 件物品无法识别`, `${names}。求解后无法应用到游戏。`);
  }
    showToast(skipped ? `已读取 ${state.items.length} 件物品，另有 ${skipped} 件无法识别。` : `已读取游戏中的 ${state.items.length} 件物品。`);
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
  }
}

function updateApplyButton() {
  const available = Boolean(state.finishedSolveId && state.result?.placements?.length);
  $("applyBtn").hidden = !available;
  $("applyBtn").disabled = false;
}

async function applyArrangement() {
  if (!state.finishedSolveId) return;
  if (!confirm("将当前求解结果直接应用到游戏背包？插件会从游戏中的当前排布开始调整。")) return;
  const button = $("applyBtn"); button.disabled = true;
  setStatus("solving", "正在应用到游戏", "游戏插件正在交换物品、旋转石板并复核结果");
  try {
    const response = await api("/api/apply-arrangement", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ solveId: state.finishedSolveId }),
    });
    state.finishedSolveId = null; state.solveUsedGameSource = false; updateApplyButton();
    setStatus("success", "已应用到游戏", `完成 ${response.moves || 0} 次交换、${response.rotations || 0} 次旋转，并已验证排布`);
    showToast("游戏背包已按求解结果排布");
  } catch (error) {
    button.disabled = false;
    setStatus("error", "无法应用到游戏", error.message);
    showToast(error.message);
  }
}

function composeTabletItems() {
  return state.items.filter((item) => {
    if (item.kind !== "tablet") return false;
    const type = findType("tablet", item.typeId);
    return type && (!type.custom || type.cellCount === gridCellCount());
  });
}

function fillComposeSelect(select, items) {
  select.replaceChildren();
  items.forEach((item) => {
    const type = findType("tablet", item.typeId);
    const option = document.createElement("option");
    option.value = item.instanceId;
    option.textContent = `${itemDisplayName(item, type)} · #${state.items.indexOf(item) + 1}`;
    select.append(option);
  });
}

function fillRotationSelect(select, item) {
  const previous = Number(select.value) || 0;
  const type = item && findType("tablet", item.typeId);
  select.replaceChildren();
  const count = type?.rotatable ? 4 : 1;
  for (let rotation = 0; rotation < count; rotation += 1) {
    const option = document.createElement("option");
    option.value = String(rotation); option.textContent = `${rotation * 90}°`; select.append(option);
  }
  select.value = String(Math.min(previous, count - 1));
}

function refreshComposeRotations() {
  const items = new Map(composeTabletItems().map((item) => [item.instanceId, item]));
  fillRotationSelect($("composeFirstRotation"), items.get($("composeFirst").value));
  fillRotationSelect($("composeSecondRotation"), items.get($("composeSecond").value));
}

function updateComposeName() {
  if (state.composeNameDirty) return;
  const first = state.items.find((item) => item.instanceId === $("composeFirst").value);
  const second = state.items.find((item) => item.instanceId === $("composeSecond").value);
  const firstType = first && findType("tablet", first.typeId);
  const secondType = second && findType("tablet", second.typeId);
  if (firstType && secondType) $("composeName").value = `${itemDisplayName(first, firstType)} + ${itemDisplayName(second, secondType)}`.slice(0, 40);
}

function changeComposeSource() {
  refreshComposeRotations();
  updateComposeName();
}

function openComposeDialog() {
  const items = composeTabletItems();
  if (items.length < 2) return showToast("至少需要两块石板才能合成。");
  fillComposeSelect($("composeFirst"), items);
  fillComposeSelect($("composeSecond"), items);
  $("composeFirst").value = items[0].instanceId;
  $("composeSecond").value = items[1].instanceId;
  state.composeNameDirty = false;
  refreshComposeRotations();
  updateComposeName();
  $("composeDialog").showModal();
}

async function composeSelectedTablets(event) {
  event.preventDefault();
  const first = state.items.find((item) => item.instanceId === $("composeFirst").value);
  const second = state.items.find((item) => item.instanceId === $("composeSecond").value);
  if (!first || !second || first === second) return showToast("请选择两块不同的石板。");
  const sourceCustomIds = new Set([first.typeId, second.typeId]);
  const sourcePayload = (item, rotationId) => ({
    typeId: item.typeId,
    rotation: Number($(rotationId).value),
    ...(item.runtimeRule ? { runtimeRule: item.runtimeRule } : {}),
  });
  const button = $("composeConfirmBtn"); button.disabled = true;
  try {
    const custom = await api("/api/custom-tablet/compose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        cellCount: gridCellCount(), name: $("composeName").value.trim(),
        sources: [sourcePayload(first, "composeFirstRotation"), sourcePayload(second, "composeSecondRotation")],
        customTabletTypes: state.customTabletTypes.filter((type) => sourceCustomIds.has(type.id)),
      }),
    });
    state.customTabletTypes = state.customTabletTypes.filter((type) => type.id !== custom.id);
    state.customTabletTypes.push(custom);
    const consumed = new Set([first.instanceId, second.instanceId]);
    state.items = state.items.filter((item) => !consumed.has(item.instanceId));
    state.items.push({
      instanceId: `t-${Date.now().toString(36)}-${state.serial++}`,
      typeId: custom.id, kind: "tablet", weight: 1, baseLevel: null,
      minLevel: null, exactLevel: null, fixedCell: null, fixedRotation: null,
      specialPriority: false, specialTargetInstanceId: null,
    });
    state.result = null; $("composeDialog").close();
    persist(); renderCatalog(); renderOwned(); renderBoard(); updateStatusIdle();
    showToast(`已合成 ${custom.name}，两块源石板已从构筑中移除。`);
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
  }
}

function showResult(result, solveId) {
  state.result = result; renderBoard();
  $("primaryMetric").textContent = result.primaryObjective ?? "—"; $("secondaryMetric").textContent = result.secondaryObjective ?? "—"; $("specialMetric").textContent = result.specialObjective ?? "—"; $("tertiaryMetric").textContent = result.tertiaryObjective ?? "—"; $("emptyCellMetric").textContent = result.emptyCellObjective ?? "—";
  $("gapMetric").textContent = result.relativeGap == null ? "—" : `${(result.relativeGap * 100).toFixed(1)}%`; $("timeMetric").textContent = `${result.solveMs} ms`;
  state.finishedSolveId = result.placements?.length && state.solveUsedGameSource ? solveId : null;
  updateApplyButton();
  if (!result.placements?.length) { $("resultSection").hidden = true; setStatus("error", result.solutionStatus === "INFEASIBLE" ? "没有可行排布" : "未找到排布", result.message); return; }
  const optimalDetail = result.specialStatus === "DISABLED" ? "加权得分、等级合计、副作用惩罚与空闲格等级均已完成最优性证明" : "加权得分、等级合计、特殊效果、副作用惩罚与空闲格等级均已完成最优性证明";
  setStatus(result.solutionStatus === "OPTIMAL" ? "success" : "warning", result.solutionStatus === "OPTIMAL" ? "已证明最优" : "已找到可行排布", result.solutionStatus === "OPTIMAL" ? optimalDetail : `当前解距主目标上界 ${(result.relativeGap * 100).toFixed(1)}%`);
  const tbody = $("resultBody"); tbody.replaceChildren();
  result.artifacts.forEach((detail) => {
    const tr = document.createElement("tr"); const values = [detail.name, `${Math.floor(detail.cell / cols()) + 1} 行 ${detail.cell % cols() + 1} 列`, detail.rawBonus > 0 ? `+${detail.rawBonus}` : detail.rawBonus, `${detail.level} / ${detail.cap}`, detail.weight, detail.contribution];
    values.forEach((value) => { const td = document.createElement("td"); td.textContent = String(value); tr.append(td); });
    const status = document.createElement("td"); const chip = document.createElement("span"); chip.className = `status-chip${detail.active ? "" : " inactive"}`; chip.textContent = detail.active ? "生效" : "未启用"; status.append(chip); tr.append(status); tbody.append(tr);
  });
  const active = result.artifacts.filter((d) => d.active).length; $("activeSummary").textContent = `${active} / ${result.artifacts.length} 件生效`; $("resultSection").hidden = false;
}

function setSolving(solving) { $("solveBtn").disabled = solving; $("stopBtn").hidden = !solving; $("stopBtn").disabled = false; }
function setStatus(kind, title, detail) { const bar = $("statusBar"); bar.className = `status-bar ${kind}`; $("statusTitle").textContent = title; $("statusDetail").textContent = detail; }
function updateStatusIdle() {
  state.finishedSolveId = null; state.solveUsedGameSource = false; updateApplyButton();
  const fixed = state.items.filter((i) => i.fixedCell != null || i.fixedRotation != null || i.minLevel != null || i.exactLevel != null || i.specialPriority || (i.kind === "artifact" && (i.weight !== 5 || (i.baseLevel ?? 0) !== 0))).length;
  setStatus("idle", state.items.length ? "构筑已更新" : "等待构筑", `${state.items.length} 件物品 · ${fixed} 件带约束`);
  ["primaryMetric", "secondaryMetric", "specialMetric", "tertiaryMetric", "emptyCellMetric", "gapMetric", "timeMetric"].forEach((id) => { $(id).textContent = "—"; }); $("resultSection").hidden = true;
}
function showToast(message) { const toast = $("toast"); toast.textContent = message; toast.hidden = false; clearTimeout(showToast.timer); showToast.timer = setTimeout(() => { toast.hidden = true; }, 3200); }

function persist() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ cellCount: gridCellCount(), items: state.items, customTabletTypes: state.customTabletTypes, serial: state.serial }));
}
function restore() {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null"); if (!raw || !Array.isArray(raw.items)) return;
    $("cellCountInput").value = Math.max(1, Math.min(60, Number(raw.cellCount) || (Number(raw.rows) * Number(raw.cols)) || 30));
    state.customTabletTypes = Array.isArray(raw.customTabletTypes) ? raw.customTabletTypes.filter((type) => type && type.custom && typeof type.id === "string" && Number(type.cellCount) === gridCellCount()) : [];
    state.items = raw.items.filter((item) => item && ["artifact", "tablet"].includes(item.kind) && findType(item.kind, item.typeId)).map((item) => ({ ...item, weight: item.kind === "artifact" && item.weight == null ? 5 : item.weight, baseLevel: item.kind === "artifact" ? Math.max(0, Number(item.baseLevel) || 0) : null, specialPriority: item.kind === "artifact" && Boolean(item.specialPriority), specialTargetInstanceId: typeof item.specialTargetInstanceId === "string" ? item.specialTargetInstanceId : null }));
    const artifactIds = new Set(state.items.filter((item) => item.kind === "artifact").map((item) => item.instanceId));
    state.items.forEach((item) => { if (item.specialTargetInstanceId && !artifactIds.has(item.specialTargetInstanceId)) item.specialTargetInstanceId = null; });
    state.serial = Number(raw.serial) || state.items.length + 1;
  } catch { localStorage.removeItem(STORAGE_KEY); }
}
function bindEvents() {
  $("artifactTab").addEventListener("click", () => switchKind("artifact")); $("tabletTab").addEventListener("click", () => switchKind("tablet")); $("searchInput").addEventListener("input", renderCatalog);
  $("readGameBtn").addEventListener("click", readGameInventory);
  $("applyBtn").addEventListener("click", applyArrangement);
  $("combineTabletBtn").addEventListener("click", openComposeDialog);
  $("composeFirst").addEventListener("change", changeComposeSource);
  $("composeSecond").addEventListener("change", changeComposeSource);
  $("composeName").addEventListener("input", () => { state.composeNameDirty = true; });
  $("composeCancelBtn").addEventListener("click", () => $("composeDialog").close());
  $("composeForm").addEventListener("submit", composeSelectedTablets);
  $("cellCountInput").addEventListener("input", scheduleGridUpdate); $("cellCountInput").addEventListener("change", updateGrid); $("solveBtn").addEventListener("click", startSolve); $("stopBtn").addEventListener("click", stopSolve);
  $("resetBtn").addEventListener("click", () => { if (state.items.length && !confirm("清空当前构筑和求解结果？")) return; state.items = []; state.customTabletTypes = []; state.gameSource = null; state.result = null; persist(); renderCatalog(); renderOwned(); renderBoard(); updateStatusIdle(); });
}
function switchKind(kind) { state.kind = kind; $("artifactTab").classList.toggle("active", kind === "artifact"); $("tabletTab").classList.toggle("active", kind === "tablet"); $("artifactTab").setAttribute("aria-selected", String(kind === "artifact")); $("tabletTab").setAttribute("aria-selected", String(kind === "tablet")); renderCatalog(); }

async function init() {
  bindEvents(); renderBoard();
  try { state.catalog = await api("/api/catalog"); restore(); renderCatalog(); renderOwned(); renderBoard(); updateStatusIdle(); }
  catch (error) { setStatus("error", "目录加载失败", error.message); $("solveBtn").disabled = true; }
}
init();
