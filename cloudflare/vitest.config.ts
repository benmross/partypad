import path from "node:path";
import { fileURLToPath } from "node:url";
import { cloudflareTest, readD1Migrations } from "@cloudflare/vitest-pool-workers";
import { defineConfig } from "vitest/config";

const directory = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [
    cloudflareTest(async () => ({
      wrangler: { configPath: path.join(directory, "wrangler.jsonc") },
      miniflare: {
        bindings: {
          AUTH_HASH_KEY: "test-only-hash-key",
          ACCESS_TEAM_DOMAIN: "example",
          ACCESS_AUD: "test-audience",
          AUTH_HOST: "auth.example.test",
          TEST_MIGRATIONS: await readD1Migrations(path.join(directory, "migrations")),
        },
      },
    })),
  ],
  test: { setupFiles: ["./test/apply-migrations.ts"] },
});
