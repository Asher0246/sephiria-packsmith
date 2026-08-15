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
    return `${item.kind}\u0000${item.instanceId}\u0000${item.typeId}`;
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

  function inheritArtifactSettings(previous, nextItems, threshold = SAME_RUN_THRESHOLD) {
    const items = Array.isArray(nextItems) ? nextItems.map((item) => ({ ...item })) : [];
    const similarity = inventorySimilarity(previous, items);
    if (similarity < threshold) {
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
      inheritedCount += 1;
    });
    return { items, similarity, sameRun: true, inheritedCount };
  }

  return {
    SAME_RUN_THRESHOLD,
    captureGameRead,
    inventorySimilarity,
    inheritArtifactSettings,
  };
}));
