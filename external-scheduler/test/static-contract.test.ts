import { describe, expect, it } from "vitest";

import controlSource from "../src/github-control.ts?raw";
import indexSource from "../src/index.ts?raw";
import wranglerSource from "../wrangler.jsonc?raw";

describe("Phase G1 static boundaries", () => {
  it("commits no active Cron Trigger", () => {
    const config = JSON.parse(wranglerSource);
    expect(config.triggers.crons).toEqual([]);
  });

  it("has no public HTTP handler and no Actions dispatch", () => {
    expect(indexSource).not.toMatch(/\bfetch\s*\(/);
    expect(indexSource).not.toContain("actions/workflows");
    expect(controlSource).not.toContain("actions/workflows");
  });

  it("has no snapshot-data or AI HOT producer client", () => {
    const source = `${indexSource}\n${controlSource}`;
    expect(source).not.toContain("snapshot-data");
    expect(source).not.toContain("/api/v1/items");
    expect(source).not.toContain("V2CandidateService");
  });
});
