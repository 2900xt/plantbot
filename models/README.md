# Model setup

Large weight files are excluded from this source repository. The source runner,
hash-bound inference settings and original export-validation metadata are included.
Copy the weights from the supplied project model archives into these paths:

- `LeafCNN-Epoch8-GPU/selected-gpu-model.pt` — SHA256 `506ddb8621650b6af0c2804b56617830b4f01bf345b15fb900628e8b22b512cc`.
- `PlantLeafPipeline/models/segformer-b2-ade20k/pytorch_model.bin` — use the exact bundled B2 checkpoint matching the included config/preprocessor.

The first archive is the project-supplied LeafCNN epoch-8 GPU FP16 model. The second
is the project-supplied PlantLeafPipeline archive. The B2 checkpoint originates from
[NVIDIA SegFormer B2 ADE20K](https://huggingface.co/nvidia/segformer-b2-finetuned-ade-512-512).
Keep the matching metadata and consult the upstream model terms. The guarded CNN
runner requires CUDA and the exact validated artifact; it never substitutes a CPU model.
