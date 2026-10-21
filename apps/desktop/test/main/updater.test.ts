import { describe, expect, it } from "vitest";
import { shouldCheckForUpdates } from "../../src/main/updater";

describe("auto updates", () => {
  it("only run in installed builds, outside smoke tests, unless disabled", () => {
    expect(shouldCheckForUpdates({ isPackaged: true }, {}, false)).toBe(true);
    expect(shouldCheckForUpdates({ isPackaged: false }, {}, false)).toBe(false);
    expect(shouldCheckForUpdates({ isPackaged: true }, {}, true)).toBe(false);
    expect(shouldCheckForUpdates({ isPackaged: true }, { AITSEV_DISABLE_UPDATES: "1" }, false)).toBe(false);
  });
});
