"use strict";

(function expose(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.SephiriaGameReadState = api;
}(typeof globalThis === "object" ? globalThis : this, function createApi() {
  const SAME_RUN_THRESHOLD = 0.7;

  function isGameItem(item) {
    if (!item || !["artifact", "tablet"].includes(item.kind)) return false;
    if (typeof item.instanceId !== "string" || typeof item.typeId !== "string") return false;
    const prefix = item.kind === "artifact" ? "game-a-" : "game-t-";
    return item.instanceId.startsWith(prefix);
  }

  function identityKey(item) {
    // The game instance id is the stable identity.  Type resolution is a
    // separate concern and can briefly differ while the bridge is reading a
    // changing inventory (for example while an item is being created).
    return `${item.kind}\u0000${item.instanceId}`;
  }

  function captureGameRead(items) {
    if (!Array.isArray(items)) return null;
    const captured = items.filter(isGameItem).map((item) => ({
      instanceId: item.instanceId,
      typeId: item.typeId,
      kind: item.kind,
      ...(item.kind === "artifact" ? {
        weight: Number.isInteger(item.weight) && item.weight >= 1 && item.weight <= 10
          ? item.weight : 5,
        specialPriority: item.specialPriority === true,
        specialTargetInstanceId: typeof item.specialTargetInstanceId === "string"
          ? item.specialTargetInstanceId : null,
        // 最低等级与固定等级互斥；历史数据若同时存在，保留约束更强的固定等级。
        minLevel: Number.isInteger(item.exactLevel)
          ? null : (Number.isInteger(item.minLevel) ? item.minLevel : null),
        exactLevel: Number.isInteger(item.exactLevel) ? item.exactLevel : null,
      } : {}),
    }));
    return captured.length ? { items: captured } : null;
  }

  function inventorySimilarity(previous, nextItems) {
    const oldRead = captureGameRead(previous?.items);
    const newRead = captureGameRead(nextItems);
    if (!oldRead || !newRead) return 0;
    const oldKeys = new Set(oldRead.items.map(identityKey));
    const newKeys = new Set(newRead.items.map(identityKey));
    let intersection = 0;
    oldKeys.forEach((key) => { if (newKeys.has(key)) intersection += 1; });
    return intersection / Math.max(oldKeys.size, newKeys.size);
  }

  // Same-run detection must tolerate in-run churn: picking items up or
  // dropping them between two reads changes the key sets even though the
  // run is the same.  Dividing by the smaller side keeps added or removed
  // items from diluting the ratio, while the 0.7 bar (and a two-item
  // minimum) still rejects a fresh run whose renumbered instances happen
  // to collide with the previous read.
  function isSameRun(previous, nextItems) {
    const oldRead = captureGameRead(previous?.items);
    const newRead = captureGameRead(nextItems);
    if (!oldRead || !newRead) return false;
    const oldKeys = new Set(oldRead.items.map(identityKey));
    const newKeys = new Set(newRead.items.map(identityKey));
    let intersection = 0;
    oldKeys.forEach((key) => { if (newKeys.has(key)) intersection += 1; });
    if (!intersection) return false;
    if (intersection === oldKeys.size && intersection === newKeys.size) return true;
    return intersection >= 2
      && intersection / Math.min(oldKeys.size, newKeys.size) >= SAME_RUN_THRESHOLD;
  }

  // Refreshes stored settings after user edits: keys still present take the
  // current (possibly edited) settings, keys whose items are temporarily
  // absent keep their remembered settings so a later read can inherit them.
  function mergeGameRead(previous, current) {
    const oldRead = captureGameRead(previous?.items);
    const currentRead = captureGameRead(current?.items);
    if (!currentRead) return oldRead;
    if (!oldRead) return currentRead;
    const merged = new Map(oldRead.items.map((item) => [identityKey(item), item]));
    currentRead.items.forEach((item) => merged.set(identityKey(item), item));
    return {
      items: [...merged.values()],
    };
  }

  function inheritArtifactSettings(previous, nextItems) {
    const items = Array.isArray(nextItems) ? nextItems.map((item) => ({ ...item })) : [];
    const similarity = inventorySimilarity(previous, items);
    if (!isSameRun(previous, items)) {
      return { items, similarity, sameRun: false, inheritedCount: 0 };
    }
    const oldRead = captureGameRead(previous?.items);
    const oldArtifacts = new Map(
      oldRead.items.filter((item) => item.kind === "artifact")
        .map((item) => [identityKey(item), item]),
    );
    const newArtifactIds = new Set(
      items.filter((item) => item.kind === "artifact").map((item) => item.instanceId),
    );
    let inheritedCount = 0;
    items.forEach((item) => {
      if (item.kind !== "artifact") return;
      const previousItem = oldArtifacts.get(identityKey(item));
      if (!previousItem) return;
      item.weight = previousItem.weight;
      item.specialPriority = previousItem.specialPriority;
      item.specialTargetInstanceId = newArtifactIds.has(previousItem.specialTargetInstanceId)
        ? previousItem.specialTargetInstanceId : null;
      item.minLevel = previousItem.minLevel ?? null;
      item.exactLevel = previousItem.exactLevel ?? null;
      inheritedCount += 1;
    });
    return { items, similarity, sameRun: true, inheritedCount };
  }

  return {
    SAME_RUN_THRESHOLD,
    captureGameRead,
    inventorySimilarity,
    isSameRun,
    mergeGameRead,
    inheritArtifactSettings,
  };
}));
