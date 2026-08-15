"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  SAME_RUN_THRESHOLD,
  captureGameRead,
  inventorySimilarity,
  inheritArtifactSettings,
} = require("../app/static/game_read_state.js");

function artifact(id, options = {}) {
  return {
    instanceId: `game-a-${id}`,
    typeId: `artifact-${id}`,
    kind: "artifact",
    weight: 5,
    specialPriority: false,
    specialTargetInstanceId: null,
    ...options,
  };
}

function tablet(id) {
  return { instanceId: `game-t-${id}`, typeId: `tablet-${id}`, kind: "tablet" };
}

test("inherits artifact settings at exactly seventy percent similarity", () => {
  const previousItems = [
    artifact(1, { weight: 9, specialPriority: true, specialTargetInstanceId: "game-a-2" }),
    artifact(2), artifact(3), artifact(4),
    tablet(1), tablet(2), tablet(3), tablet(4), tablet(5), tablet(6),
  ];
  const nextItems = [
    artifact(1), artifact(2), artifact(3),
    tablet(1), tablet(2), tablet(3), tablet(4),
    artifact(10), tablet(10), tablet(11),
  ];
  const previous = captureGameRead(previousItems);

  const result = inheritArtifactSettings(previous, nextItems);

  assert.equal(SAME_RUN_THRESHOLD, 0.7);
  assert.equal(result.similarity, 0.7);
  assert.equal(result.sameRun, true);
  assert.equal(result.inheritedCount, 3);
  assert.deepEqual(
    result.items.find((item) => item.instanceId === "game-a-1"),
    artifact(1, { weight: 9, specialPriority: true, specialTargetInstanceId: "game-a-2" }),
  );
  assert.equal(result.items.find((item) => item.instanceId === "game-a-10").weight, 5);
});

test("does not inherit settings below seventy percent similarity", () => {
  const previousItems = [artifact(1, { weight: 10 }), artifact(2), ...Array.from({ length: 8 }, (_, index) => tablet(index + 1))];
  const nextItems = [artifact(1), artifact(2), tablet(1), tablet(2), tablet(3), tablet(4), artifact(10), artifact(11), tablet(10), tablet(11)];

  const result = inheritArtifactSettings(captureGameRead(previousItems), nextItems);

  assert.equal(inventorySimilarity(captureGameRead(previousItems), nextItems), 0.6);
  assert.equal(result.sameRun, false);
  assert.equal(result.items[0].weight, 5);
});

test("clears a special target that is no longer present", () => {
  const previousItems = [
    artifact(1, { weight: 8, specialPriority: true, specialTargetInstanceId: "game-a-2" }),
    artifact(2), artifact(3), artifact(4), artifact(5),
    tablet(1), tablet(2), tablet(3), tablet(4), tablet(5),
  ];
  const nextItems = [
    artifact(1), artifact(3), artifact(4), artifact(5), artifact(6),
    tablet(1), tablet(2), tablet(3), tablet(4), tablet(5),
  ];

  const result = inheritArtifactSettings(captureGameRead(previousItems), nextItems);
  const inherited = result.items.find((item) => item.instanceId === "game-a-1");

  assert.equal(result.similarity, 0.9);
  assert.equal(inherited.weight, 8);
  assert.equal(inherited.specialPriority, true);
  assert.equal(inherited.specialTargetInstanceId, null);
});

test("capture ignores manually added items and sanitizes artifact settings", () => {
  const captured = captureGameRead([
    artifact(1, { weight: 99, specialPriority: "true", specialTargetInstanceId: 2 }),
    { instanceId: "a-manual", typeId: "artifact-manual", kind: "artifact", weight: 10 },
  ]);

  assert.deepEqual(captured, { items: [{
    instanceId: "game-a-1",
    typeId: "artifact-1",
    kind: "artifact",
    weight: 5,
    specialPriority: false,
    specialTargetInstanceId: null,
  }] });
});
