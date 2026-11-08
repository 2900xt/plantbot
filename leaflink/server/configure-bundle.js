// Migrate only the known previous bundle default; preserve tokens and other settings.
import {existsSync,readFileSync,writeFileSync} from 'node:fs';
if(existsSync('models/leaf-cnn-epoch6/weights.pt') && existsSync('.env')) {
  const text=readFileSync('.env','utf8');
  const previous=/^LOCAL_MODEL_DIR=.*(?:plant-resnet18|leaf-cnn-epoch5)[ \t]*\r?$/m;
  if(/^MODEL_MODE=local[ \t]*\r?$/m.test(text) && previous.test(text)) {
    writeFileSync('.env',text.replace(previous,'LOCAL_MODEL_DIR=./models/leaf-cnn-epoch6'));
    console.log('Switched the previous bundle default to the included epoch-6 LeafCNN.');
  }
}
