import { describe, expect, it } from "vitest";

import { resolveApiUrl, type EngineConnection } from "./api";

const engine: EngineConnection = {
  baseUrl: "http://127.0.0.1:43210",
  token: "test-token"
};

describe("resolveApiUrl", () => {
  it("routes API calls through the desktop engine", () => {
    expect(resolveApiUrl("/api/state", engine)).toBe("http://127.0.0.1:43210/api/state");
  });

  it("keeps browser assets and debug-mode API calls unchanged", () => {
    expect(resolveApiUrl("/assets/app.js", engine)).toBe("/assets/app.js");
    expect(resolveApiUrl("/api/state", null)).toBe("/api/state");
  });
});
