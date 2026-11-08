import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { S3Client, PutObjectCommand, GetObjectCommand } from '@aws-sdk/client-s3';
export function storage(c) {
  if (c.driver === 'local') return {
    async put(key, bytes) { const file = path.join(c.dataDir, 'objects', key); await mkdir(path.dirname(file), { recursive: true }); await writeFile(file, bytes); },
    get(key) { return readFile(path.join(c.dataDir, 'objects', key)); },
  };
  const client = new S3Client({ endpoint: c.endpoint, region: c.region, forcePathStyle: true, maxAttempts: 3,
    requestHandler: { requestTimeout: 30000, connectionTimeout: 5000 },
    requestChecksumCalculation: 'WHEN_REQUIRED', responseChecksumValidation: 'WHEN_REQUIRED',
    credentials: { accessKeyId: c.keyId, secretAccessKey: c.appKey } });
  return {
    put(key, bytes, contentType = 'image/jpeg') { return client.send(new PutObjectCommand({ Bucket: c.bucket, Key: key, Body: bytes, ContentType: contentType })); },
    async get(key) { const response = await client.send(new GetObjectCommand({ Bucket: c.bucket, Key: key })); return Buffer.from(await response.Body.transformToByteArray()); },
  };
}
