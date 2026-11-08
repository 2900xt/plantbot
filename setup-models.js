// Download the published runtime weights, checking every SHA256 before install.
import {createHash,randomUUID} from 'node:crypto';
import {createReadStream,createWriteStream} from 'node:fs';
import {mkdir,readFile,rename,rm,stat} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {Readable,Transform} from 'node:stream';
import {pipeline} from 'node:stream/promises';

const root=path.dirname(fileURLToPath(import.meta.url));
async function digest(file) {
  const hash=createHash('sha256');
  for await (const chunk of createReadStream(file)) hash.update(chunk);
  return hash.digest('hex');
}
try {
  const manifest=JSON.parse(await readFile(path.join(root,'model-assets.json'),'utf8'));
  if (manifest.schema!==1 || !Array.isArray(manifest.assets) || !manifest.assets.length) throw new Error('Invalid model-assets.json');
  for (const asset of manifest.assets) {
    const target=path.resolve(root,asset.path);
    const url=new URL(asset.url);
    if (!target.startsWith(root+path.sep) || !asset.path.startsWith('models/') || !/^[a-f0-9]{64}$/.test(asset.sha256) || !Number.isSafeInteger(asset.bytes) || asset.bytes<1 || url.protocol!=='https:' || url.hostname!=='github.com' || !url.pathname.startsWith('/NarwhaLIX/LeafLink/releases/download/')) throw new Error('Invalid model asset entry');
    const existing=await stat(target).catch(error=>{if(error.code==='ENOENT') return null;throw error;});
    if (existing) {
      if (existing.size!==asset.bytes || await digest(target)!==asset.sha256) throw new Error(`Existing model differs from release: ${asset.path}. Move it aside before installing.`);
      console.log(`Verified ${asset.path}`);continue;
    }
    await mkdir(path.dirname(target),{recursive:true});
    const temporary=target+'.download-'+randomUUID();
    try {
      console.log(`Downloading ${asset.path}`);
      const response=await fetch(url,{signal:AbortSignal.timeout(15*60*1000)});
      if (!response.ok || !response.body) throw new Error(`Model download failed (${response.status}): ${asset.path}`);
      let bytes=0;const hash=createHash('sha256');
      const check=new Transform({transform(chunk,encoding,callback){
        bytes+=chunk.length;
        if (bytes>asset.bytes) {callback(new Error('Download exceeds expected size'));return;}
        hash.update(chunk);callback(null,chunk);
      }});
      await pipeline(Readable.fromWeb(response.body),check,createWriteStream(temporary,{flags:'wx'}));
      if (bytes!==asset.bytes || hash.digest('hex')!==asset.sha256) throw new Error(`Model checksum mismatch: ${asset.path}`);
      await rename(temporary,target);
      console.log(`Installed ${asset.path}`);
    } finally {
      await rm(temporary,{force:true});
    }
  }
  console.log('All runtime model weights are installed and verified.');
} catch (error) {
  console.error(error.message);process.exitCode=1;
}
