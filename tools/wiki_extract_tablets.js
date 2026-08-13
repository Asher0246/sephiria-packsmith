"use strict";

const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(0, "utf8");
const captured = [];
const sandbox = { self: { webpackChunk_N_E: { push: (entry) => captured.push(entry) } } };
vm.runInNewContext(source, sandbox, { timeout: 2_000, displayErrors: true });

const modules = Object.assign({}, ...captured.map((entry) => entry[1]));

function loadModule(predicate, exportName) {
  const match = Object.values(modules).find((factory) => predicate(factory.toString()));
  if (!match) throw new Error(`Unable to locate Wiki module exporting ${exportName}`);
  const exports = {};
  const webpackRequire = () => { throw new Error("Unexpected dependency in data module"); };
  webpackRequire.d = (target, definitions) => {
    for (const [name, getter] of Object.entries(definitions)) {
      Object.defineProperty(target, name, { enumerable: true, get: getter });
    }
  };
  match({}, exports, webpackRequire);
  return exports[exportName];
}

const tablets = loadModule(
  (text) => text.includes('value:"chivalry"') && text.includes("ko_label") && text.includes("/slabs/"),
  "H",
);
const rules = loadModule(
  (text) => text.includes("approximation:") && text.includes("newDx") && text.includes("home_town:"),
  "i",
);

if (!Array.isArray(tablets) || tablets.length < 50) {
  throw new Error(`Unexpected Wiki tablet count: ${tablets?.length}`);
}
if (!rules || Object.keys(rules).length !== tablets.length) {
  throw new Error(`Wiki tablet/rule mismatch: ${tablets.length}/${Object.keys(rules || {}).length}`);
}

const candidates = {};
for (const tablet of tablets) {
  const rule = rules[tablet.value];
  if (typeof rule !== "function") throw new Error(`Missing rule for ${tablet.value}`);
  const bySize = {};
  for (let rows = 1; rows <= 10; rows += 1) {
    for (let cols = 1; cols <= 6; cols += 1) {
      const layout = Array.from({ length: rows }, (_, row) => ({ rows: row, cols }));
      const values = [];
      for (let y = 0; y < rows; y += 1) {
        for (let x = 0; x < cols; x += 1) {
          const cell = y * cols + x;
          const rotationCount = tablet.rotate ? 4 : 1;
          for (let rotation = 0; rotation < rotationCount; rotation += 1) {
            const effects = {};
            const flags = {};
            for (let row = 0; row < rows; row += 1) {
              for (let col = 0; col < cols; col += 1) {
                effects[`${row}-${col}`] = 0;
                flags[`${row}-${col}`] = null;
              }
            }
            rule(x, y, `${y}-${x}`, { ...tablet, rotation }, effects, flags, layout);
            const sparseEffects = Object.entries(effects)
              .filter(([, value]) => typeof value === "number" && value !== 0)
              .map(([key, value]) => {
                const [row, col] = key.split("-").map(Number);
                return [row * cols + col, value];
              });
            const unlocks = Object.entries(flags)
              .filter(([, value]) => value === "ignore")
              .map(([key]) => {
                const [row, col] = key.split("-").map(Number);
                return row * cols + col;
              });
            values.push([cell, rotation, sparseEffects, unlocks]);
          }
        }
      }
      bySize[`${rows}x${cols}`] = values;
    }
  }
  candidates[tablet.value] = bySize;
}

process.stdout.write(JSON.stringify({ tablets, candidates }));
