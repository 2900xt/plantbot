"""Inference adapter using each packaged model's preprocessing and class order."""
import base64
import hashlib
import io
import json
from pathlib import Path
import sys
import torch
from PIL import Image, ImageOps
from torch import nn
from torchvision.models import resnet18
from torchvision.transforms import v2


class LeafCNN(nn.Module):
    """Architecture reviewed against the supplied epoch-5 training source."""
    def __init__(self, classes=38):
        super().__init__()
        layers, channels = [], 3
        for width in (32, 64, 128, 256):
            for _ in range(2):
                layers += [nn.Conv2d(channels, width, 3, padding=1, bias=False),
                           nn.BatchNorm2d(width), nn.ReLU(inplace=True)]
                channels = width
            layers.append(nn.MaxPool2d(2))
        self.features = nn.Sequential(*layers)
        self.head = nn.Sequential(nn.AdaptiveAvgPool2d(1), nn.Flatten(),
                                  nn.Linear(256, 128), nn.ReLU(),
                                  nn.Dropout(0.4), nn.Linear(128, classes))

    def forward(self, images):
        return self.head(self.features(images))


def load_predictor(directory, *, cpu_threads=8, memory_format="channels_last"):
    directory = Path(directory)
    if not isinstance(cpu_threads,int) or not 1 <= cpu_threads <= 32:
        raise ValueError("cpu_threads must be between 1 and 32")
    if memory_format not in ("channels_last", "contiguous"):
        raise ValueError("Invalid inference memory format")
    config = json.loads((directory / "config.json").read_text())
    classes = config["classes"]
    architecture = config["architecture"]
    if architecture not in ("resnet18", "leaf-cnn") or len(classes) != 38 or not all(isinstance(c,str) and c for c in classes) or len(set(classes)) != 38:
        raise ValueError("Invalid model architecture or class order")
    size = config["image_size"]
    if not isinstance(size,int) or not 32 <= size <= 1024:
        raise ValueError("Invalid model image size")
    torch.set_num_threads(cpu_threads)
    if architecture == "leaf-cnn":
        model = LeafCNN(len(classes))
        weight_file = directory / "weights.pt"
    else:
        model = resnet18(weights=None)
        model.fc = nn.Linear(model.fc.in_features, len(classes))
        weight_file = directory / "resnet18_plant_fp16.pt"
    state = torch.load(weight_file, map_location="cpu", weights_only=True)
    model.load_state_dict({k:v.float() for k,v in state.items()}, strict=True)
    model.eval()
    layout = torch.channels_last if memory_format == "channels_last" else torch.contiguous_format
    model.to(memory_format=layout)

    def square(image):
        side = min(image.shape[-2:])
        return v2.functional.center_crop(image, [side,side])

    transform = v2.Compose([v2.ToImage(),v2.Lambda(square),v2.Resize((size,size),antialias=True),
                            v2.ToDtype(torch.float32,scale=True),v2.Normalize(config["mean"],config["std"])])
    fingerprint = hashlib.sha256(weight_file.read_bytes()).hexdigest()
    return model, transform, classes, size, f"{architecture}-{fingerprint[:16]}", layout


def predict(payload):
    model, transform, classes, size, version, layout = load_predictor(
        payload["model_dir"], cpu_threads=payload.get("cpu_threads",8),
        memory_format=payload.get("memory_format","channels_last"))
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(base64.b64decode(payload["image"],validate=True)))).convert("RGB")
    batch = transform(image).unsqueeze(0).contiguous(memory_format=layout)
    with torch.inference_mode():
        probabilities = torch.softmax(model(batch),1)[0]
    values, indices = probabilities.topk(3)
    top = [{"class_name":classes[i],"confidence":float(v)} for v,i in zip(values.tolist(),indices.tolist())]
    name = top[0]["class_name"]
    return {"verdict":"healthy" if name.endswith("_healthy") else "diseased", "confidence":top[0]["confidence"],
            "class_name":name,"class_index":int(indices[0]),"top_predictions":top,"input_size":[size,size],
            "score_type":"winning_class_softmax","model_version":version,"simulated":False}


if __name__ == "__main__":
    try:
        print(json.dumps(predict(json.load(sys.stdin))))
    except Exception as error:
        print(f"Local inference failed ({type(error).__name__}): {error}",file=sys.stderr)
        raise SystemExit(1)
