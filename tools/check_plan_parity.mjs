// 票据 03 实现侧对照自检：把 .scratch 下原型（allocation-demo.html 里的纯算法模块）
// 当基准，与仓库实现（bestseller_monitor/weekly_plan.py，经 tools/dump_plan_parity.py
// 导出）跑同样的六组场景，逐格比对。原型自身的 JS↔Python 对照见
// .scratch/multi-machine-collection/prototype/check_parity.mjs。
//
// 用法：node tools/check_plan_parity.mjs

import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const html = readFileSync(
  join(root, ".scratch", "multi-machine-collection", "prototype", "allocation-demo.html"),
  "utf8",
);
const begin = html.indexOf("/* ==MODULE-BEGIN==");
const end = html.indexOf("/* ==MODULE-END== */");
if (begin < 0 || end < 0) throw new Error("HTML 里找不到模块标记");

const moduleSrc = html.slice(begin, end) +
  "\nexport { createRun, weekLabel, weekSpan };\n";
const tmp = mkdtempSync(join(tmpdir(), "alloc-parity-"));
const modPath = join(tmp, "allocator.mjs");
writeFileSync(modPath, moduleSrc, "utf8");
const { createRun } = await import(pathToFileURL(modPath).href);

// —— 与原型 dump_plans.py 相同的六个场景 ——
const SHOPS = [
  { key: "A01", pages: 23 }, { key: "A02", pages: 8 }, { key: "A03", pages: 14 },
  { key: "A04", pages: 7 }, { key: "A05", pages: 15 }, { key: "A06", pages: 5 },
  { key: "A07", pages: 16 }, { key: "A08", pages: 16 }, { key: "A09", pages: 15 },
  { key: "A10", pages: 10 }, { key: "A11", pages: 8 }, { key: "A12", pages: 14 },
];
const A13 = { key: "A13", pages: 9 };
const START = new Date(Date.UTC(2026, 8, 21));
const W = "week";

const SCENARIOS = [
  ["s1_full_c1_b4", SHOPS, ["m1", "m2", "m3"], 1, 4, Array(4).fill(W)],
  ["s2_full_c1_b1", SHOPS, ["m1", "m2", "m3"], 1, 1, Array(4).fill(W)],
  ["s3_roster_c1", SHOPS, ["m1", "m2", "m3"], 1, 4, [W, W, "add", W, "remove", W]],
  ["s4_roster_c2", SHOPS, ["m1", "m2", "m3"], 2, 4, [W, W, "add", W, "remove", W]],
  ["s5_two_shops", SHOPS.filter((s) => s.key === "A01" || s.key === "A06"), ["m1", "m2", "m3"], 1, 4, Array(3).fill(W)],
  ["s6_two_machines_c2", SHOPS.slice(0, 3), ["m1", "m2"], 2, 4, Array(3).fill(W)],
];

function runScenario(shops, machines, constraintWeeks, balanceWeeks, steps) {
  const run = createRun({ shops, machines, constraintWeeks, balanceWeeks, startMonday: START });
  for (const step of steps) {
    if (step === "add") run.addShop(A13);
    else if (step === "remove") run.removeShop("A02");
    else run.nextWeek();
  }
  return run.plans.map((p) => ({
    week: p.week,
    assignments: Object.entries(p.assignments).sort(),
    relaxed: Object.entries(p.relaxed).sort(),
    eligible: Object.entries(p.eligibleCounts).sort(),
  }));
}

const js = {};
for (const [name, shops, machines, c, b, steps] of SCENARIOS) {
  js[name] = runScenario(shops, machines, c, b, steps);
}

const py = JSON.parse(
  execFileSync("python", [join(here, "dump_plan_parity.py")], { encoding: "utf8", cwd: root })
);

const canon = (o) => JSON.stringify(o, Object.keys(o).sort());
let bad = 0;
for (const name of Object.keys(py)) {
  if (canon(js[name]) === canon(py[name])) {
    console.log("一致 ✓  " + name);
  } else {
    bad++;
    console.log("不一致 ✗  " + name);
    console.log("  js      :", JSON.stringify(js[name]));
    console.log("  python  :", JSON.stringify(py[name]));
  }
}
console.log(bad ? "共 " + bad + " 个场景不一致" : "全部场景逐格一致");
process.exit(bad ? 1 : 0);
