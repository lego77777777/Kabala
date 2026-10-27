/**
 * Where receipt PDFs live. Nothing here is public: files are read back only by this server, behind
 * a signed, expiring link (see signed-links.ts).
 *   STORAGE=local  → a folder on disk (development, single server)
 *   STORAGE=s3     → any S3-compatible bucket: Amazon S3, Cloudflare R2, MinIO (private bucket)
 */
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { GetObjectCommand, PutObjectCommand, S3Client } from "@aws-sdk/client-s3";

export interface Storage {
  readonly kind: string;
  put(key: string, data: Buffer, contentType: string): Promise<void>;
  get(key: string): Promise<Buffer | null>;
}

const SAFE_KEY = /^[a-zA-Z0-9][a-zA-Z0-9/_.-]{0,200}$/;
function checkKey(key: string): string {
  if (!SAFE_KEY.test(key) || key.includes("..")) throw new Error(`invalid storage key ${key}`);
  return key;
}

export class LocalStorage implements Storage {
  readonly kind = "local";
  constructor(private readonly root: string) {}

  async put(key: string, data: Buffer): Promise<void> {
    const file = path.join(this.root, checkKey(key));
    await mkdir(path.dirname(file), { recursive: true });
    await writeFile(file, data);
  }

  async get(key: string): Promise<Buffer | null> {
    try {
      return await readFile(path.join(this.root, checkKey(key)));
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code === "ENOENT") return null;
      throw e;
    }
  }
}

export class S3Storage implements Storage {
  readonly kind = "s3";
  private readonly client: S3Client;
  private readonly sse: "AES256" | undefined;

  /**
   * endpoint is needed for R2 (https://<account>.r2.cloudflarestorage.com) and MinIO; omit for AWS.
   * sse: ask the bucket to encrypt each receipt at rest. Default: on for AWS; R2 already encrypts every object
   * and rejects the header, so it is off when an endpoint is set unless S3_SSE=AES256.
   */
  constructor(private readonly bucket: string, opts: { region: string; endpoint?: string; accessKeyId: string; secretAccessKey: string; sse?: "AES256" | "none" }) {
    this.sse = (opts.sse ?? (opts.endpoint ? "none" : "AES256")) === "AES256" ? "AES256" : undefined;
    this.client = new S3Client({
      region: opts.region,
      endpoint: opts.endpoint,
      forcePathStyle: Boolean(opts.endpoint),
      credentials: { accessKeyId: opts.accessKeyId, secretAccessKey: opts.secretAccessKey },
    });
  }

  async put(key: string, data: Buffer, contentType: string): Promise<void> {
    await this.client.send(new PutObjectCommand({ Bucket: this.bucket, Key: checkKey(key), Body: data, ContentType: contentType, ServerSideEncryption: this.sse }));
  }

  async get(key: string): Promise<Buffer | null> {
    try {
      const r = await this.client.send(new GetObjectCommand({ Bucket: this.bucket, Key: checkKey(key) }));
      return r.Body ? Buffer.from(await r.Body.transformToByteArray()) : null;
    } catch (e) {
      if ((e as { name?: string }).name === "NoSuchKey") return null;
      throw e;
    }
  }
}

/** In-memory storage for tests. */
export class MemoryStorage implements Storage {
  readonly kind = "memory";
  readonly files = new Map<string, Buffer>();
  async put(key: string, data: Buffer): Promise<void> {
    this.files.set(checkKey(key), data);
  }
  async get(key: string): Promise<Buffer | null> {
    return this.files.get(key) ?? null;
  }
}
