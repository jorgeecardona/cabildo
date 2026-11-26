/** Fails when the Python records the extension mirrors change. */

import * as assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";

import { HUMAN } from "../src/client";

const root = path.resolve(__dirname, "../../../..");
const read = (file: string): string => fs.readFileSync(path.join(root, file), "utf8");

test("the person's identity matches store.py", () => {
  const store = read("src/cabildo/store.py");
  assert.equal(/^HUMAN_SESSION = "(.*)"$/m.exec(store)?.[1], HUMAN.session);
  assert.equal(/^HUMAN_HANDLE = "(.*)"$/m.exec(store)?.[1], HUMAN.handle);
});

test("HostSession has the fields of host.Record", () => {
  const record = read("src/cabildo/host.py").split("class Record:")[1]!.split("@staticmethod")[0]!;
  const python = [...record.matchAll(/^ {4}(\w+): /gm)].map((m) => m[1]);
  const body = read("packages/vscode/src/types.ts").split("interface HostSession {")[1]!;
  const typescript = [...body.split("}")[0]!.matchAll(/readonly (\w+):/g)].map((m) => m[1]);
  assert.deepEqual(typescript, python);
});

test("StatRow and StatGroup have the fields of stats.Row and stats.Group", () => {
  const python = read("src/cabildo/stats.py");
  const typescript = read("packages/vscode/src/types.ts");
  for (const [dataclass, record] of [["Row", "StatRow"], ["Group", "StatGroup"]]) {
    const body = python.split(`class ${dataclass}:`)[1]!.split("\n\n\n")[0]!;
    const fields = [...body.matchAll(/^ {4}(\w+): /gm)].map((m) => m[1]);
    const mirror = typescript.split(`interface ${record} {`)[1]!.split("}")[0]!;
    assert.deepEqual([...mirror.matchAll(/readonly (\w+):/g)].map((m) => m[1]), fields);
  }
});
