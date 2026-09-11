import { githubRequest, isRecord, responseJson } from "./github-api";
import { SchedulerError, SchedulerErrorReason } from "./types";

const encoder = new TextEncoder();
const CLIENT_ID = /^[A-Za-z0-9._-]{1,100}$/;
const INSTALLATION_ID = /^[1-9]\d{0,19}$/;
const PKCS8_BEGIN = "-----BEGIN PRIVATE KEY-----";
const PKCS8_END = "-----END PRIVATE KEY-----";
const PKCS1_BEGIN = "-----BEGIN RSA PRIVATE KEY-----";
const PKCS1_END = "-----END RSA PRIVATE KEY-----";

export interface InstallationAuthConfig {
  clientId: string;
  installationId: string;
  privateKey: string;
}

export interface InstallationToken {
  token: string;
  expiresAt: string;
}

export async function createGitHubAppJwt(
  clientId: string,
  privateKey: string,
  now: Date,
): Promise<string> {
  if (!CLIENT_ID.test(clientId)) {
    throw new SchedulerError(
      SchedulerErrorReason.INVALID_CONFIGURATION,
      "GitHub App client ID is missing or malformed",
    );
  }
  if (!(now instanceof Date) || !Number.isFinite(now.getTime())) {
    throw new SchedulerError(
      SchedulerErrorReason.INVALID_CONFIGURATION,
      "JWT clock must be a valid instant",
    );
  }
  const issuedAt = Math.floor(now.getTime() / 1000) - 60;
  const expiresAt = Math.floor(now.getTime() / 1000) + 9 * 60;
  const header = base64Url(encoder.encode(JSON.stringify({ alg: "RS256", typ: "JWT" })));
  const claims = base64Url(
    encoder.encode(JSON.stringify({ iat: issuedAt, exp: expiresAt, iss: clientId })),
  );
  const signingInput = `${header}.${claims}`;
  let key: CryptoKey;
  try {
    key = await crypto.subtle.importKey(
      "pkcs8",
      privateKeyDer(privateKey),
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
      false,
      ["sign"],
    );
  } catch {
    throw new SchedulerError(
      SchedulerErrorReason.GITHUB_APP_AUTH_FAILED,
      "GitHub App private key could not be imported",
    );
  }
  const signature = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    key,
    encoder.encode(signingInput),
  );
  return `${signingInput}.${base64Url(new Uint8Array(signature))}`;
}

export async function createInstallationToken(
  config: InstallationAuthConfig,
  now: Date,
  fetcher: typeof fetch,
  sleep: (milliseconds: number) => Promise<void>,
): Promise<InstallationToken> {
  const jwt = await createGitHubAppJwt(config.clientId, config.privateKey, now);
  return exchangeInstallationToken(
    config.installationId,
    jwt,
    now,
    fetcher,
    sleep,
  );
}

export async function exchangeInstallationToken(
  installationId: string,
  jwt: string,
  now: Date,
  fetcher: typeof fetch,
  sleep: (milliseconds: number) => Promise<void>,
): Promise<InstallationToken> {
  if (!INSTALLATION_ID.test(installationId)) {
    throw new SchedulerError(
      SchedulerErrorReason.INVALID_CONFIGURATION,
      "GitHub installation ID must be a positive decimal integer",
    );
  }
  const response = await githubRequest(
    {
      token: jwt,
      method: "POST",
      path: `/app/installations/${installationId}/access_tokens`,
      expectedStatus: 201,
      body: {
        repositories: ["aihot-data-bridge"],
        permissions: { contents: "write" },
      },
    },
    fetcher,
    sleep,
  );
  const payload = await responseJson(response);
  const token = payload.token;
  const expiresAt = payload.expires_at;
  const permissions = payload.permissions;
  if (
    typeof token !== "string" ||
    token.length === 0 ||
    typeof expiresAt !== "string" ||
    !Number.isFinite(Date.parse(expiresAt)) ||
    Date.parse(expiresAt) <= now.getTime() ||
    !isRecord(permissions) ||
    permissions.contents !== "write"
  ) {
    throw new SchedulerError(
      SchedulerErrorReason.GITHUB_API_MALFORMED_RESPONSE,
      "installation token response did not satisfy the scoped token contract",
    );
  }
  return { token, expiresAt };
}

export function normalizePrivateKey(privateKey: string): string {
  if (typeof privateKey !== "string" || privateKey.trim() === "") {
    throw new SchedulerError(
      SchedulerErrorReason.INVALID_CONFIGURATION,
      "GitHub App private key secret is missing",
    );
  }
  const normalized = privateKey.includes("\\n")
    ? privateKey.replace(/\\n/g, "\n")
    : privateKey;
  return normalized.trim().replace(/\r\n/g, "\n");
}

function privateKeyDer(privateKey: string): ArrayBuffer {
  const pem = normalizePrivateKey(privateKey);
  if (pem.startsWith(PKCS8_BEGIN) && pem.endsWith(PKCS8_END)) {
    return pemBody(pem, PKCS8_BEGIN, PKCS8_END).buffer as ArrayBuffer;
  }
  if (pem.startsWith(PKCS1_BEGIN) && pem.endsWith(PKCS1_END)) {
    const pkcs1 = pemBody(pem, PKCS1_BEGIN, PKCS1_END);
    return wrapPkcs1AsPkcs8(pkcs1).buffer as ArrayBuffer;
  }
  throw new SchedulerError(
    SchedulerErrorReason.GITHUB_APP_AUTH_FAILED,
    "GitHub App private key must be PKCS#8 or RSA PKCS#1 PEM",
  );
}

function pemBody(pem: string, begin: string, end: string): Uint8Array {
  const body = pem.slice(begin.length, -end.length).replace(/\s/g, "");
  if (body === "" || !/^[A-Za-z0-9+/]+={0,2}$/.test(body)) {
    throw new SchedulerError(
      SchedulerErrorReason.GITHUB_APP_AUTH_FAILED,
      "GitHub App private key PEM body is malformed",
    );
  }
  try {
    return Uint8Array.from(atob(body), (character) => character.charCodeAt(0));
  } catch {
    throw new SchedulerError(
      SchedulerErrorReason.GITHUB_APP_AUTH_FAILED,
      "GitHub App private key PEM body is malformed",
    );
  }
}

function wrapPkcs1AsPkcs8(pkcs1: Uint8Array): Uint8Array {
  const version = Uint8Array.of(0x02, 0x01, 0x00);
  const rsaAlgorithm = Uint8Array.of(
    0x30, 0x0d, 0x06, 0x09, 0x2a, 0x86, 0x48, 0x86,
    0xf7, 0x0d, 0x01, 0x01, 0x01, 0x05, 0x00,
  );
  const privateKey = derValue(0x04, pkcs1);
  return derValue(0x30, concat(version, rsaAlgorithm, privateKey));
}

function derValue(tag: number, value: Uint8Array): Uint8Array {
  return concat(Uint8Array.of(tag), derLength(value.length), value);
}

function derLength(length: number): Uint8Array {
  if (length < 0x80) return Uint8Array.of(length);
  const bytes: number[] = [];
  for (let remaining = length; remaining > 0; remaining >>>= 8) {
    bytes.unshift(remaining & 0xff);
  }
  return Uint8Array.of(0x80 | bytes.length, ...bytes);
}

function concat(...arrays: Uint8Array[]): Uint8Array {
  const output = new Uint8Array(arrays.reduce((total, value) => total + value.length, 0));
  let offset = 0;
  for (const value of arrays) {
    output.set(value, offset);
    offset += value.length;
  }
  return output;
}

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/=/g, "").replace(/\+/g, "-").replace(/\//g, "_");
}
