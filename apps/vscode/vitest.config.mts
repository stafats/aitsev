import { defineConfig } from "vitest/config";
import * as path from "node:path";

export default defineConfig({
  resolve: {
    alias: { "@aitsev/agent-core": path.resolve(import.meta.dirname, "../../packages/agent-core/src/index.ts") },
  },
  test: {
    include: ["test/**/*.test.ts"],
    testTimeout: 30_000,
    fileParallelism: false,
  },
});
