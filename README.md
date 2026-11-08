# LeafLink

Field-monitoring web app: leaf infection screening, VOC/GPS observations and a mapped image library.

## Fresh download: Windows

1. Download this repository with **Code → Download ZIP**, then **extract the entire ZIP**.
2. Open **Start-LeafLink.cmd** in the extracted folder. Keep its window open.
3. When setup finishes, open the localhost address shown and paste the displayed local API token into Connection.

No manual Node.js, Python, pip or model-weight installation is needed. The launcher installs missing Node24 locally, creates an isolated Python3.12 environment, installs matching CUDA PyTorch and image dependencies, downloads/checks model SHA256s, builds the frontend, and executes a synthetic GPU/model check before serving. It does not change system Python or PATH. Windows may request approval to install the official Microsoft Visual C++ x64 runtime if it is missing.

First setup downloads several GB and may take several minutes. Allow at least **10GB free space**, internet access to GitHub, nodejs.org, PyPI, python downloads and download.pytorch.org, and a writable extracted folder. Keep the folder on a local drive; do not run inside the ZIP viewer. Later launches reuse the local runtime and cached models, then verify model files and GPU operation again.

## Fresh download: Linux

On a supported x64 Linux machine:

```bash
bash start-leaflink.sh
```

This installs project-local Node24 when needed, then uses the same managed setup. The base system needs Bash, curl, tar/xz, sha256sum, ca-certificates and a working NVIDIA driver (standard on Ubuntu22.04/24.04 installations). No global Python packages or CUDA toolkit installation is required: the PyTorch wheel supplies its CUDA runtime. Server address/token appear in the terminal.

## Supported hardware

Automatic setup targets **x64 Windows10/11 and modern glibc x64 Linux**, with a **Turing or newer NVIDIA GPU** (compute capability7.5+, such as T4, GTX16-series and RTX20-series or newer).

- NVIDIA driver **580+**: pinned PyTorch2.11 / torchvision0.26 CUDA13.0 wheels.
- Driver **570–579**: matching CUDA12.8 wheels, followed by actual kernel/model checks.
- Older drivers: setup stops with an update link. Install the appropriate [NVIDIA driver](https://www.nvidia.com/Download/index.aspx), restart, and rerun the launcher.

A GPU's presence alone cannot guarantee compatibility. ARM/Jetson, macOS, pre-Turing cards and obsolete/missing drivers are outside this installer’s supported matrix. CUDA12.8 is a compatibility path checked at launch; the release artifact’s original GPU audit used CUDA13.0. Multi-GPU systems use the default CUDA device; custom CUDA visibility/device overrides need their own verification.

The original epoch-8 classifier remains the normal default. **Start-Crop-Experts.cmd** / `npm run start:experts` selects the separately labeled experimental experts; installation convenience does not improve or certify model accuracy.

## Commands

If Node24+ is already installed:

```bash
npm start                 # install/check/build/start, including on a fresh clone
npm run setup             # install/check/build without serving
npm run doctor            # check existing models and run GPU/B2/CNN probes; no downloads
npm run start:experts      # separate experimental model
npm test                  # setup/config regression tests
```

Setup-only launchers: `powershell -File start-field.ps1 -SetupOnly` on Windows, or `bash start-leaflink.sh --setup-only` on Linux. If setup stops, fix the named issue and run the same launcher; interrupted downloads are not installed. Existing model files with a wrong checksum are reported, not silently overwritten. Move an intentionally different model aside before reinstalling the published artifact.

Runtime/cache files live in `.leaflink/` and `.venv-field/`; weights are downloaded into `models/` from the manifest in `model-assets.json`. None is committed. Tokens/storage/event rules remain in `.env.field` (or `.env.experts`); setup updates only the managed Python paths in existing configuration. Python pins are in `requirements-runtime.txt`; model files retain their published hashes. Full runtime setup verification is recorded in `.leaflink/setup-state.json` after checks pass.

For a fresh clone you do **not** need to manually run `npm ci`, `setup-models.js`, pip or the frontend build; the normal launcher does those steps. Advanced legacy/http/demo server configurations remain available separately and are not validated by the normal GPU installer.

## What the check establishes

Startup checks execute CUDA and torchvision kernels, load the actual B2 ground checkpoint, and run the selected real classifier on a synthetic image. They establish runtime compatibility, not infected/healthy accuracy on field photos. A failed check prevents this launcher from starting an unready server. They do not install NVIDIA drivers or perform reboots.

Official runtime references: [PyTorch2.11 matching CUDA wheels](https://pytorch.org/get-started/previous-versions/), [NVIDIA CUDA driver compatibility](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html), [uv managed Python](https://docs.astral.sh/uv/concepts/python-versions/). Portable tools are fetched from official Node.js/Astral releases with SHA256 verification; the Windows Microsoft runtime is checked with Authenticode.

## Fresh-install verification

A clean GitHub clone in a folder with spaces, without weights, node_modules or a Python environment, passed on a Kaggle Tesla T4: portable Node and managed Python installation, all three weight checks, CUDA kernels, both classifier runtime probes, frontend/server health and a real HTTP image upload producing40 classified candidates. First setup took66.7s on Kaggle's fast network; cached setup14.2s. These are observations, not promised download times. Five setup/config tests and the frontend build pass locally. [Exact environment and verification scope](docs/setup-validation.json).

Windows launcher syntax/config handling was tested locally; a Windows NVIDIA GPU machine was not available for a full installation test. The clean GPU run used CUDA13.0; the driver570 compatibility path is verified on the user's device at launch. This is why the launcher checks real execution and reports failures instead of promising every NVIDIA device will work.
