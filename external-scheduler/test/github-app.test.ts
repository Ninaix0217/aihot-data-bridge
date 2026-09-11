import { beforeAll, describe, expect, it, vi } from "vitest";

import {
  createGitHubAppJwt,
  createInstallationToken,
  normalizePrivateKey,
} from "../src/github-app";
import { SchedulerErrorReason } from "../src/types";
import { NOW, testPrivateKeyPem, testPrivateKeyPkcs1Pem } from "./helpers";

let privateKey: string;

beforeAll(async () => {
  privateKey = await testPrivateKeyPem();
});

describe("GitHub App authentication", () => {
  it("creates a client-ID RS256 JWT with current GitHub bounds", async () => {
    const jwt = await createGitHubAppJwt("Iv1.test-client", privateKey, NOW);
    const [header, claims, signature] = jwt.split(".");
    expect(JSON.parse(decodeBase64Url(header!))).toEqual({ alg: "RS256", typ: "JWT" });
    expect(JSON.parse(decodeBase64Url(claims!))).toEqual({
      iat: Math.floor(NOW.getTime() / 1000) - 60,
      exp: Math.floor(NOW.getTime() / 1000) + 540,
      iss: "Iv1.test-client",
    });
    expect(signature).toBeTruthy();
  });

  it("normalizes real and escaped PEM newlines without exposing content", async () => {
    expect(normalizePrivateKey(privateKey)).toBe(privateKey);
    const escaped = privateKey.replace(/\n/g, "\\n");
    await expect(createGitHubAppJwt("Iv1.test-client", escaped, NOW)).resolves.toMatch(/^[^.]+\.[^.]+\.[^.]+$/);
    await expect(createGitHubAppJwt("Iv1.test-client", "not-a-key", NOW)).rejects.toMatchObject({
      reason: SchedulerErrorReason.GITHUB_APP_AUTH_FAILED,
    });
  });

  it("accepts the RSA PKCS#1 PEM form issued by GitHub Apps", async () => {
    const pkcs1 = await testPrivateKeyPkcs1Pem();
    await expect(createGitHubAppJwt("Iv1.test-client", pkcs1, NOW)).resolves.toMatch(
      /^[^.]+\.[^.]+\.[^.]+$/,
    );
  });

  it("requests one repository-scoped short-lived installation token", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      Response.json(
        {
          token: "ghs_APPID_JWT_non_fixed_format",
          expires_at: "2026-09-11T09:00:00Z",
          permissions: { contents: "write" },
        },
        { status: 201 },
      ),
    );
    const result = await createInstallationToken(
      { clientId: "Iv1.test-client", installationId: "12345", privateKey },
      NOW,
      fetcher,
      async () => undefined,
    );
    expect(result).toEqual({
      token: "ghs_APPID_JWT_non_fixed_format",
      expiresAt: "2026-09-11T09:00:00Z",
    });
    const [url, init] = fetcher.mock.calls[0]!;
    expect(url).toBe("https://api.github.com/app/installations/12345/access_tokens");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({
      repositories: ["aihot-data-bridge"],
      permissions: { contents: "write" },
    });
  });

  it("rejects malformed installation token responses", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      Response.json({ token: "", expires_at: "never", permissions: {} }, { status: 201 }),
    );
    await expect(
      createInstallationToken(
        { clientId: "Iv1.test-client", installationId: "12345", privateKey },
        NOW,
        fetcher,
        async () => undefined,
      ),
    ).rejects.toMatchObject({ reason: SchedulerErrorReason.GITHUB_API_MALFORMED_RESPONSE });
  });
});

function decodeBase64Url(value: string): string {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
  return atob(padded);
}
