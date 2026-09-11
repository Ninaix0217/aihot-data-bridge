export const NOW = new Date("2026-09-11T08:00:00.000Z");
export const SCHEDULED_TIME = Date.parse("2026-09-11T05:25:00.000Z");
export const OLD_BLOB_SHA = "a".repeat(40);
export const NEW_BLOB_SHA = "b".repeat(40);
export const COMMIT_SHA = "c".repeat(40);

export async function testPrivateKeyPem(): Promise<string> {
  const pair = await crypto.subtle.generateKey(
    {
      name: "RSASSA-PKCS1-v1_5",
      modulusLength: 2048,
      publicExponent: new Uint8Array([1, 0, 1]),
      hash: "SHA-256",
    },
    true,
    ["sign", "verify"],
  );
  const bytes = new Uint8Array(await crypto.subtle.exportKey("pkcs8", pair.privateKey));
  const body = toBase64(bytes).match(/.{1,64}/g)?.join("\n");
  if (!body) throw new Error("test key encoding failed");
  return `-----BEGIN PRIVATE KEY-----\n${body}\n-----END PRIVATE KEY-----`;
}

export async function testPrivateKeyPkcs1Pem(): Promise<string> {
  const pkcs8Pem = await testPrivateKeyPem();
  const body = pkcs8Pem
    .replace("-----BEGIN PRIVATE KEY-----", "")
    .replace("-----END PRIVATE KEY-----", "")
    .replace(/\s/g, "");
  const pkcs8 = Uint8Array.from(atob(body), (character) => character.charCodeAt(0));
  let offset = skipValueHeader(pkcs8, 0).valueOffset;
  offset = skipValue(pkcs8, offset);
  offset = skipValue(pkcs8, offset);
  const privateKey = skipValueHeader(pkcs8, offset);
  if (pkcs8[offset] !== 0x04) throw new Error("test PKCS#8 did not contain an octet string");
  const pkcs1 = pkcs8.slice(privateKey.valueOffset, privateKey.endOffset);
  const encoded = toBase64(pkcs1).match(/.{1,64}/g)?.join("\n");
  if (!encoded) throw new Error("test PKCS#1 encoding failed");
  return `-----BEGIN RSA PRIVATE KEY-----\n${encoded}\n-----END RSA PRIVATE KEY-----`;
}

export async function gitBlobSha(bytes: Uint8Array): Promise<string> {
  const encoder = new TextEncoder();
  const header = encoder.encode(`blob ${bytes.length}\0`);
  const input = new Uint8Array(header.length + bytes.length);
  input.set(header);
  input.set(bytes, header.length);
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-1", input));
  return [...digest].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function contentResponse(
  bytes: Uint8Array,
  sha: string,
): Response {
  return Response.json({
    type: "file",
    encoding: "base64",
    size: bytes.length,
    sha,
    content: toBase64(bytes),
  });
}

export function toBase64(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function skipValue(bytes: Uint8Array, offset: number): number {
  return skipValueHeader(bytes, offset).endOffset;
}

function skipValueHeader(
  bytes: Uint8Array,
  offset: number,
): { valueOffset: number; endOffset: number } {
  if (offset + 2 > bytes.length) throw new Error("truncated test DER");
  const firstLength = bytes[offset + 1]!;
  let length: number;
  let valueOffset: number;
  if ((firstLength & 0x80) === 0) {
    length = firstLength;
    valueOffset = offset + 2;
  } else {
    const count = firstLength & 0x7f;
    if (count === 0 || count > 4 || offset + 2 + count > bytes.length) {
      throw new Error("invalid test DER length");
    }
    length = 0;
    for (let index = 0; index < count; index += 1) {
      length = (length << 8) | bytes[offset + 2 + index]!;
    }
    valueOffset = offset + 2 + count;
  }
  return { valueOffset, endOffset: valueOffset + length };
}
