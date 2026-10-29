# ==============================================================================
# Dockerfile — ComfyUI "Everything-Ready" para Vast.ai (RTX 5090 / RTX 4090)
# ==============================================================================
# --- Stage 1: Compilar comfyctl (Rust 1.86.0 fijado por digest, §4.3 / §4.4) ---
FROM rust:1.86.0-slim-bookworm@sha256:57d415bbd61ce11e2d5f73de068103c7bd9f3188dc132c97cef4a8f62989e944 AS builder
WORKDIR /build/comfyctl

# Nota (R-09): No se copia rust-toolchain.toml al stage builder porque la imagen base
# ya es exactamente Rust 1.86.0 fijada por digest y copiar components = ["rustfmt", "clippy"]
# forzaría una descarga de red innecesaria de rustup durante docker build.
COPY comfyctl/Cargo.toml comfyctl/Cargo.lock ./
RUN mkdir -p src && printf 'fn main() {}\n' > src/main.rs && printf '' > src/lib.rs \
    && cargo build --release --locked \
    && rm -rf src

COPY comfyctl/src ./src
RUN touch src/main.rs src/lib.rs && cargo build --release --locked --bin comfyctl

# --- Stage 2: Imagen final de ejecución ---
# Base CUDA fijada por digest (igual que el stage builder, §4.3 / REP-01): la etiqueta
# se conserva por legibilidad, pero el digest garantiza pulls reproducibles.
FROM nvidia/cuda:12.8.0-runtime-ubuntu24.04@sha256:44e43f0e0bcca1fc6fdc775e6002c67834bf78d39eb1fd76825240fc79ba4a49

LABEL org.opencontainers.image.title="ComfyUI Everything-Ready (Vast.ai RTX 5090 / 4090)"
LABEL org.opencontainers.image.description="ComfyUI (/opt/ComfyUI) + ComfyUI-Manager + ffmpeg (libx264/nvenc) + Nginx Basic Auth + Top Custom Nodes & Auto-Models"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_CONSTRAINT=/etc/comfyui/constraints.txt \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=all \
    VHS_FORCE_FFMPEG_PATH=/usr/bin/ffmpeg \
    VHS_USE_IMAGEIO_FFMPEG=0 \
    COMFY_APP_DIR=/opt/ComfyUI \
    COMFY_DATA_DIR=/workspace/comfyui-data \
    OPEN_BUTTON_PORT=8188

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# 1. Dependencias del sistema: ffmpeg completo (libx264 + nvenc), Nginx, ttyd, tini, git, openssl
#    y build-essential + python3-dev conservados (ALTA-03) para que torch.compile (Triton)
#    y la instalación en 1 clic de nodos con extensiones C/C++ desde ComfyUI-Manager funcionen en runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        unzip \
        tini \
        openssl \
        procps \
        ffmpeg \
        nginx \
        apache2-utils \
        ttyd \
        build-essential \
        cmake \
        pkg-config \
        python3 \
        python3-dev \
        python3-pip \
        python3-venv \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
        libgomp1 \
        libsndfile1 \
        libnss3 \
        libnspr4 \
        libdbus-1-3 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libxkbcommon0 \
        libasound2t64 \
        libpango-1.0-0 \
        libcairo2 \
    && ffmpeg -version \
    && ttyd --version \
    && rm -rf /var/lib/apt/lists/*

# 2. Copiar archivos de bloqueo y restricciones de versión (REP-01 / REP-02 / ALTA-05)
RUN mkdir -p /etc/comfyui /etc/comfyui/portal /etc/comfyui/probes /etc/comfyui/panel
COPY config/constraints.txt /etc/comfyui/constraints.txt
COPY config/nodes.lock /etc/comfyui/nodes.lock
RUN sed -i 's/\r$//' /etc/comfyui/constraints.txt /etc/comfyui/nodes.lock

# 3. Instalar terna fijada de PyTorch CUDA 12.8 (ALTA-05: soporta Blackwell sm_120 y Ada sm_89),
#    clonar ComfyUI y sam2 por SHA fijado, y compilar paquetes base.
# --ignore-installed: el pip 24.0 de apt en Ubuntu 24.04 no tiene fichero RECORD y el pip
# nuevo se niega a desinstalarlo ("Cannot uninstall pip 24.0, RECORD file not found"),
# así que el upgrade inicial ignora la copia de Debian (falló el primer build V21).
RUN pip3 install --upgrade --ignore-installed pip setuptools wheel \
    && pip3 install \
        torch==2.9.1+cu128 \
        torchvision==0.24.1+cu128 \
        torchaudio==2.9.1+cu128 \
        --index-url https://download.pytorch.org/whl/cu128 \
    && awk '$1 == "core" {print $3, $4}' /etc/comfyui/nodes.lock | while read -r repo_url sha; do \
        git init /opt/ComfyUI && \
        git -C /opt/ComfyUI remote add origin "$repo_url" && \
        git -C /opt/ComfyUI fetch --depth=1 origin "$sha" && \
        git -C /opt/ComfyUI checkout -B main FETCH_HEAD; \
    done \
    && pip3 install -r /opt/ComfyUI/requirements.txt \
    && awk '$1 == "dep" && $2 == "sam2" {print $3, $4}' /etc/comfyui/nodes.lock | while read -r repo_url sha; do \
        git init /tmp/sam2 && \
        git -C /tmp/sam2 remote add origin "$repo_url" && \
        git -C /tmp/sam2 fetch --depth=1 origin "$sha" && \
        git -C /tmp/sam2 checkout FETCH_HEAD && \
        SAM2_BUILD_CUDA=0 pip3 install --no-build-isolation /tmp/sam2 && \
        rm -rf /tmp/sam2; \
    done \
    && pip3 install \
        "huggingface-hub>=1.0.0,<3.0.0" \
        "onnxruntime-gpu>=1.20.0,<2.0.0" \
        insightface \
        facexlib==0.3.0 \
        filterpy \
        "opencv-python-headless>=4.10.0.84,<5.0.0" \
        imageio-ffmpeg \
        ultralytics \
        segment-anything \
        piexif \
        numba \
        gguf \
        accelerate \
        diffusers \
        pytorch-lightning

# 4. Preinstalar los 12 Custom Nodes fijados por SHA en config/nodes.lock (REP-01 / MED-03 / CRIT-02)
#    - Soporta tanto requirements.txt como requirements-no-cupy.txt (ComfyUI-Frame-Interpolation).
#    - Filtra paquetes que pisan PyTorch/OpenCV/ONNX/NumPy o sam2 (ya instalado por SHA en paso 3).
#    - Limpia todas las variantes de OpenCV/ONNX y reinstala con --force-reinstall --no-deps (CRIT-02).
WORKDIR /opt/ComfyUI/custom_nodes
RUN awk '$1 == "node" {print $2, $3, $4}' /etc/comfyui/nodes.lock | while read -r dir_name repo_url sha; do \
        echo "=== Clonando ${dir_name} (${sha}) ===" && \
        git init "${dir_name}" && \
        git -C "${dir_name}" remote add origin "${repo_url}" && \
        git -C "${dir_name}" fetch --depth=1 origin "${sha}" && \
        git -C "${dir_name}" checkout -B main FETCH_HEAD && \
        for req_file in "${dir_name}/requirements.txt" "${dir_name}/requirements-no-cupy.txt"; do \
            if [ -f "${req_file}" ]; then \
                grep -Ev '^\s*(#|$|onnxruntime(\s*[<>=!~]|$)|opencv-python(\s*[<>=!~]|$)|opencv-contrib-python(\s*[<>=!~]|$)|numpy(\s*[<>=!~]|$)|torch(\s*[<>=!~]|$)|torchvision(\s*[<>=!~]|$)|torchaudio(\s*[<>=!~]|$)|git\+https://github\.com/facebookresearch/sam2)' "${req_file}" > /tmp/req_filtered.txt || true; \
                if [ -s /tmp/req_filtered.txt ]; then \
                    pip3 install -r /tmp/req_filtered.txt; \
                fi; \
                rm -f /tmp/req_filtered.txt; \
            fi; \
        done; \
    done \
    && if [ -d /opt/ComfyUI/custom_nodes/ComfyUI-LatentSyncWrapper/.git/info ]; then \
        printf 'checkpoints\ncheckpoints/\n' >> /opt/ComfyUI/custom_nodes/ComfyUI-LatentSyncWrapper/.git/info/exclude; \
    fi \
    && touch /root/.latentsync16_dependencies_installed \
    && pip3 uninstall -y opencv-python opencv-contrib-python opencv-python-headless opencv-contrib-python-headless onnxruntime onnxruntime-gpu || true \
    && pip3 install --no-deps --force-reinstall "opencv-python-headless>=4.10.0.84,<5.0.0" "onnxruntime-gpu>=1.20.0,<2.0.0" \
    && pip3 freeze > /etc/comfyui/requirements.lock \
    && rm -rf /root/.cache \
    && find /usr/local/lib -type d \( -path "*/nvidia/*/lib" -o -path "*/torch/lib" \) > /etc/ld.so.conf.d/pytorch-cuda.conf \
    && ldconfig \
    && python3 -c "import torch, onnxruntime as ort, insightface, cv2, huggingface_hub; assert torch.version.cuda and torch.version.cuda.startswith('12.8'), f'CUDA mismatch: {torch.version.cuda}'; assert 'CUDAExecutionProvider' in ort.get_available_providers(), f'Missing CUDAExecutionProvider: {ort.get_available_providers()}'; print('Verified OK:', torch.__version__, torch.version.cuda, cv2.__version__, huggingface_hub.__version__)"

# 5. Conservar models/configs (MED-04) y preparar estructura en /workspace/comfyui-data (CFG-01)
WORKDIR /opt/ComfyUI
RUN mkdir -p /etc/comfyui/models_configs \
    && if [ -d /opt/ComfyUI/models/configs ]; then cp -a /opt/ComfyUI/models/configs/. /etc/comfyui/models_configs/; fi \
    && mkdir -p \
        /workspace/comfyui-data/models/checkpoints \
        /workspace/comfyui-data/models/configs \
        /workspace/comfyui-data/models/diffusion_models \
        /workspace/comfyui-data/models/unet \
        /workspace/comfyui-data/models/vae \
        /workspace/comfyui-data/models/loras \
        /workspace/comfyui-data/models/clip \
        /workspace/comfyui-data/models/text_encoders \
        /workspace/comfyui-data/models/clip_vision \
        /workspace/comfyui-data/models/controlnet \
        /workspace/comfyui-data/models/upscale_models \
        /workspace/comfyui-data/models/embeddings \
        /workspace/comfyui-data/models/hypernetworks \
        /workspace/comfyui-data/models/ipadapter \
        /workspace/comfyui-data/models/insightface/models/buffalo_l \
        /workspace/comfyui-data/models/facerestore_models \
        /workspace/comfyui-data/models/facedetection \
        /workspace/comfyui-data/models/ultralytics/bbox \
        /workspace/comfyui-data/models/ultralytics/segm \
        /workspace/comfyui-data/models/sams \
        /workspace/comfyui-data/models/liveportrait \
        /workspace/comfyui-data/models/whisper/faster-whisper-large-v3 \
        /workspace/comfyui-data/models/whisper/aligners \
        /workspace/comfyui-data/models/funasr/campplus \
        /workspace/comfyui-data/models/latentsync_checkpoints \
        /workspace/comfyui-data/indextts/checkpoints \
        /workspace/comfyui-data/user/__manager \
        /workspace/comfyui-data/user/default/ComfyUI-Manager \
        /workspace/comfyui-data/user/default/workflows \
        /workspace/comfyui-data/input \
        /workspace/comfyui-data/output \
    && cp -a /etc/comfyui/models_configs/. /workspace/comfyui-data/models/configs/ \
    && rm -rf /opt/ComfyUI/models /opt/ComfyUI/input /opt/ComfyUI/output /opt/ComfyUI/user \
    && ln -sfn /workspace/comfyui-data/models /opt/ComfyUI/models \
    && ln -sfn /workspace/comfyui-data/input /opt/ComfyUI/input \
    && ln -sfn /workspace/comfyui-data/output /opt/ComfyUI/output \
    && ln -sfn /workspace/comfyui-data/user /opt/ComfyUI/user \
    && if [ -d /opt/ComfyUI/custom_nodes/ComfyUI-LatentSyncWrapper ]; then \
        rm -rf /opt/ComfyUI/custom_nodes/ComfyUI-LatentSyncWrapper/checkpoints && \
        ln -sfn /workspace/comfyui-data/models/latentsync_checkpoints /opt/ComfyUI/custom_nodes/ComfyUI-LatentSyncWrapper/checkpoints; \
    fi \
    && ln -sfn /opt/ComfyUI /workspace/ComfyUI

# 5b. Motor editor (M5): Node 20 LTS + proyecto Remotion horneado.
#     El binario `npx` queda en PATH y el proyecto (composición 'Reel') se instala
#     en /opt/remotion con sus deps FIJADAS (package.json). El Chrome Headless Shell
#     de Remotion se baja al build con `browser ensure` para que el primer render no
#     espere. La ruta del proyecto se expone en STUDIO_REMOTION_PROJECT (la lee
#     EditorEngine._render_backend). Un pin de versión equivocado rompe ESTE build.
ENV STUDIO_REMOTION_PROJECT=/opt/remotion \
    REMOTION_BROWSER_EXECUTABLE="" \
    PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=1
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && node --version && npm --version \
    && rm -rf /var/lib/apt/lists/*
COPY studio/remotion/ /opt/remotion/
WORKDIR /opt/remotion
RUN npm install --no-audit --no-fund --loglevel=error \
    && npx remotion browser ensure \
    && npm cache clean --force
# Restaurar el WORKDIR a /opt/ComfyUI: el warm-up posterior ejecuta `python3 main.py`
# relativo a ese directorio (no a /). (fix: el build V-editor dejó CWD en / y el
# warm-up falló con "can't open file '//main.py'").
WORKDIR /opt/ComfyUI

# 5c. Motor upscaler (M5): Real-ESRGAN ncnn-vulkan (binario prebuilt + modelos
#     incluidos en el release; CPU/Vulkan, no necesita torch). Se baja el zip del
#     release fijado, el binario va a /usr/local/bin y los modelos a /opt/realesrgan.
#     Las rutas se exponen en STUDIO_UPSCALER_BIN / STUDIO_UPSCALER_MODEL_DIR
#     (las lee UpscalerEngine._run_model). Un release/URL equivocado rompe ESTE build.
ENV STUDIO_UPSCALER_BIN=/usr/local/bin/realesrgan-ncnn-vulkan \
    STUDIO_UPSCALER_MODEL_DIR=/opt/realesrgan/models
RUN curl -fsSL -o /tmp/realesrgan.zip \
        https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-ubuntu.zip \
    && mkdir -p /opt/realesrgan \
    && unzip -o /tmp/realesrgan.zip -d /opt/realesrgan \
    && install -m 0755 /opt/realesrgan/realesrgan-ncnn-vulkan /usr/local/bin/realesrgan-ncnn-vulkan \
    && test -d /opt/realesrgan/models \
    && rm -f /tmp/realesrgan.zip

# 5d. Motor lipsync (M5): Wav2Lip (Rudrabha) horneado en /opt/wav2lip por SHA fijado.
#     NO se instala su requirements.txt (pide torch==1.1.0 / numpy 1.17 de 2020, roto
#     con el stack torch 2.9.1 + Py3.12 del pod): instalamos solo las deps que
#     inference.py importa, en versiones compatibles, SIN tocar torch. El checkpoint
#     wav2lip_gan.pth NO se hornea: se baja en runtime a STUDIO_LIPSYNC_MODEL_DIR
#     (cacheado en el volumen). El adaptador CLI `wav2lip` (studio/engine_bin) traduce
#     la interfaz del motor a inference.py. Un SHA/dep equivocado rompe ESTE build.
ENV STUDIO_LIPSYNC_BIN=/usr/local/bin/wav2lip \
    STUDIO_LIPSYNC_MODEL_DIR=/workspace/comfyui-data/models/wav2lip \
    WAV2LIP_HOME=/opt/wav2lip
COPY studio/engine_bin/wav2lip /usr/local/bin/wav2lip
RUN git init /opt/wav2lip \
    && git -C /opt/wav2lip remote add origin https://github.com/Rudrabha/Wav2Lip \
    && git -C /opt/wav2lip fetch --depth=1 origin bac9a81e63ecc153202353372e5724b83d9e6322 \
    && git -C /opt/wav2lip checkout -B main FETCH_HEAD \
    && test -f /opt/wav2lip/inference.py \
    && chmod +x /usr/local/bin/wav2lip \
    && pip3 install --no-cache-dir "librosa>=0.10,<0.11" "numba>=0.59" \
    && mkdir -p /workspace/comfyui-data/models/wav2lip \
    && pip3 freeze > /etc/comfyui/requirements.lock

# 5e. Motor voice_clone (M5): RVC (RVC-Project/Retrieval-based-Voice-Conversion)
#     horneado por SHA fijado. ESTRATEGIA (opción A): el engine queda WARM en la
#     imagen (paquete `rvc` + deps puras), pero fairseq NO se hornea. fairseq es
#     compile-pesado y arrastra torch 2.15/numpy 2.4 (destruiría el torch 2.9.1
#     del pod); además RVC solo lo necesita al CARGAR un modelo hubert, que ocurre
#     en runtime sobre la GPU con pesos reales. Por eso fairseq + los pesos
#     (modelo .pth, hubert_base.pt, rmvpe.pt, index) se bajan/instalan en runtime
#     a STUDIO_VOICE_CLONE_MODEL_DIR (cacheados en el volumen), fuera de la ruta
#     crítica del arranque. El import del CLI de RVC NO toca fairseq (la ruta `vc`
#     lo importa de forma perezosa dentro de get_vc), así que el guard de import
#     del build pasa sin fairseq. El adaptador CLI `rvc` (studio/engine_bin)
#     traduce la interfaz del motor a `rvc infer` y falla TIPADO si falta fairseq
#     o los pesos. Un SHA/dep equivocado o un import roto rompe ESTE build.
ENV STUDIO_VOICE_CLONE_BIN=/usr/local/bin/rvc \
    STUDIO_VOICE_CLONE_MODEL_DIR=/workspace/comfyui-data/models/rvc \
    RVC_REAL_BIN=rvc-real
COPY studio/engine_bin/rvc /opt/engine_bin/rvc
RUN git init /opt/rvc \
    && git -C /opt/rvc remote add origin https://github.com/RVC-Project/Retrieval-based-Voice-Conversion \
    && git -C /opt/rvc fetch --depth=1 origin 7b284a634667c34103eaaeed972b48ccdb4b893e \
    && git -C /opt/rvc checkout -B develop FETCH_HEAD \
    && test -f /opt/rvc/pyproject.toml \
    && pip3 install --no-cache-dir --no-deps /opt/rvc \
    && pip3 install --no-cache-dir "click>=8.1" "torchcrepe>=0.0.22,<0.0.23" "torchfcpe" \
        "faiss-cpu>=1.7" "praat-parselmouth>=0.4" "pyworld>=0.3" "python-dotenv>=1.0" \
        "ffmpeg-python>=0.2" "pydub>=0.25" \
    && python3 -c "import rvc; from rvc.wrapper.cli.cli import main" \
    && printf '#!/usr/bin/env bash\nexec python3 -m rvc.wrapper.cli.cli "$@"\n' > /usr/local/bin/rvc-real \
    && chmod +x /usr/local/bin/rvc-real \
    && install -m 0755 /opt/engine_bin/rvc /usr/local/bin/rvc \
    && mkdir -p /workspace/comfyui-data/models/rvc \
    && pip3 freeze > /etc/comfyui/requirements.lock

# 5f. Motor music_gen (M5): MusicGen via audiocraft (facebookresearch/audiocraft)
#     horneado por SHA fijado. ESTRATEGIA (opción A, igual que RVC): el paquete
#     `audiocraft` queda WARM en la imagen (--no-deps), pero sus deps pesadas y
#     fragiles (xformers, demucs, spacy, flashy) + los pesos NO se hornean:
#     audiocraft pinea torch==2.1/torchvision==0.16/numpy<2/xformers<0.0.23, que
#     clavarían el torch 2.9.1+cu128 del pod, y son compile-pesadas. Se instalan
#     en runtime a STUDIO_MUSIC_GEN_MODEL_DIR (cacheado en el volumen). audiocraft.
#     models importa todo el zoo de forma EAGER (toca xformers), así que el guard
#     del build NO importa audiocraft: solo verifica que el paquete está presente
#     y que el shim compila. El adaptador `musicgen` importa MusicGen de forma
#     perezosa y falla TIPADO sin las deps o el modelo. Un SHA equivocado rompe
#     ESTE build (el pip --no-deps y el test de presencia).
ENV STUDIO_MUSIC_GEN_BIN=/usr/local/bin/musicgen \
    STUDIO_MUSIC_GEN_MODEL_DIR=/workspace/comfyui-data/models/musicgen
COPY studio/engine_bin/musicgen /opt/engine_bin/musicgen
RUN git init /opt/audiocraft \
    && git -C /opt/audiocraft remote add origin https://github.com/facebookresearch/audiocraft \
    && git -C /opt/audiocraft fetch --depth=1 origin 896ec7c47f5e5d1e5aa1e4b260c4405328bf009d \
    && git -C /opt/audiocraft checkout -B main FETCH_HEAD \
    && test -f /opt/audiocraft/audiocraft/models/musicgen.py \
    && pip3 install --no-cache-dir --no-deps /opt/audiocraft \
    && pip3 install --no-cache-dir "einops" "julius" "num2words" "sentencepiece" \
        "encodec" "torchmetrics" "huggingface_hub" \
    && python3 -c "import importlib.util; assert importlib.util.find_spec('audiocraft'), 'audiocraft no instalado'" \
    && install -m 0755 /opt/engine_bin/musicgen /usr/local/bin/musicgen \
    && mkdir -p /workspace/comfyui-data/models/musicgen \
    && pip3 freeze > /etc/comfyui/requirements.lock
ARG BUILD_DATE=unknown

COPY --from=builder --chmod=0755 /build/comfyctl/target/release/comfyctl /usr/local/bin/comfyctl
COPY config/extra_model_paths.yaml /etc/comfyui/extra_model_paths.yaml
COPY config/manager-config.ini /etc/comfyui/manager-config.ini
COPY config/_starting.html /etc/comfyui/_starting.html
COPY config/portal/index.html /etc/comfyui/portal/index.html
COPY config/probes/ /etc/comfyui/probes/
COPY config/nginx.conf /etc/nginx/nginx.conf
COPY config/models-studio.json /etc/comfyui/models-studio.json
COPY scripts/triage_patterns.json /etc/comfyui/triage_patterns.json
COPY scripts/download-models.sh /usr/local/bin/download-models.sh
COPY scripts/start.sh /start.sh
COPY studio/ /opt/studio/
COPY panel/ /etc/comfyui/panel/

RUN sed -i 's/\r$//' \
        /usr/local/bin/download-models.sh \
        /start.sh \
        /etc/comfyui/extra_model_paths.yaml \
        /etc/comfyui/manager-config.ini \
        /etc/comfyui/_starting.html \
        /etc/comfyui/portal/index.html \
        /etc/comfyui/panel/index.html \
        /etc/comfyui/panel/style.css \
        /etc/comfyui/panel/app.js \
        /etc/comfyui/probes/*.py \
        /etc/comfyui/models-studio.json \
        /etc/comfyui/triage_patterns.json \
        /etc/nginx/nginx.conf && \
    find /opt/studio -type f \( -name "*.py" -o -name "*.toml" -o -name "*.json" \) -exec sed -i 's/\r$//' {} + && \
    chmod +x /usr/local/bin/download-models.sh /start.sh && \
    /usr/local/bin/comfyctl version && \
    python3 -m py_compile /etc/comfyui/probes/*.py && \
    python3 -m venv /opt/venvs/studio && \
    /opt/venvs/studio/bin/pip install --no-cache-dir --upgrade pip setuptools wheel && \
    /opt/venvs/studio/bin/pip install --no-cache-dir /opt/studio && \
    /opt/venvs/studio/bin/python -m compileall -q /opt/venvs/studio/lib && \
    /opt/venvs/studio/bin/python -m comfy_studio.templates && \
    IMAGE_TAG="${IMAGE_TAG}" BUILD_DATE="${BUILD_DATE}" python3 -c "import json, os, subprocess, torch; sha = subprocess.check_output(['git', '-C', '/opt/ComfyUI', 'rev-parse', 'HEAD'], text=True).strip(); json.dump({'tag': os.environ.get('IMAGE_TAG', 'dev'), 'built_at': os.environ.get('BUILD_DATE', 'unknown'), 'comfyui_sha': sha, 'pytorch': torch.__version__, 'cuda': str(torch.version.cuda), 'ubuntu': '24.04'}, open('/etc/comfyui/image.json', 'w', encoding='utf-8'), indent=2); pats = json.load(open('/etc/comfyui/triage_patterns.json', encoding='utf-8')); assert len(pats) >= 17; ms = json.load(open('/etc/comfyui/models-studio.json', encoding='utf-8')); mods = ms.get('models') or []; meta = ms.get('meta') or {}; default_n = meta.get('default_entries', -1); optin_n = meta.get('opt_in_entries', -1); assert default_n >= 0 and optin_n >= 0, 'models-studio.json sin meta de conteos'; assert len(mods) == default_n + optin_n, 'Conteo de models-studio.json inconsistente con su meta (entradas=%d, default=%d, opt_in=%d)' % (len(mods), default_n, optin_n); assert sum(m['bytes'] for m in mods if not m.get('opt_in')) == meta.get('default_bytes'), 'meta.default_bytes no cuadra con las entradas'; assert sum(m['bytes'] for m in mods if m.get('opt_in')) == meta.get('opt_in_bytes'), 'meta.opt_in_bytes no cuadra con las entradas'" && \
    cp /etc/comfyui/extra_model_paths.yaml /opt/ComfyUI/extra_model_paths.yaml && \
    cp /etc/comfyui/manager-config.ini /workspace/comfyui-data/user/__manager/config.ini && \
    cp /etc/comfyui/manager-config.ini /workspace/comfyui-data/user/default/ComfyUI-Manager/config.ini && \
    nginx -t && \
    ( \
        rc=0; \
        timeout 600 python3 main.py --cpu --quick-test-for-ci > /tmp/comfy_warmup.log 2>&1 || rc=$?; \
        cat /tmp/comfy_warmup.log; \
        if [ "$rc" -ne 0 ] || grep -q "IMPORT FAILED" /tmp/comfy_warmup.log; then \
            echo "[FATAL] Warm-up falló (rc=${rc}) o al menos un custom node tuvo IMPORT FAILED:"; \
            grep -B 2 -A 5 "IMPORT FAILED" /tmp/comfy_warmup.log || true; \
            exit 1; \
        fi \
    ) && \
    rm -rf /tmp/comfy_warmup.log /opt/ComfyUI/temp /workspace/comfyui-data/temp /workspace/comfyui-data/user/default/comfy.settings.json /workspace/comfyui-data/user/__manager /workspace/comfyui-data/user/default/ComfyUI-Manager /root/.config /root/.cache && \
    mkdir -p /workspace/comfyui-data/user/__manager /workspace/comfyui-data/user/default/ComfyUI-Manager && \
    cp /etc/comfyui/manager-config.ini /workspace/comfyui-data/user/__manager/config.ini && \
    cp /etc/comfyui/manager-config.ini /workspace/comfyui-data/user/default/ComfyUI-Manager/config.ini

EXPOSE 8188

# BAJA-02: start-period=900s superior al timeout de 600s de la Fase 1.
# El 900s solo es válido mientras la Fase 1 siga acotada con `timeout 600` en el supervisor
# (+ strict/diag ~1-2 min + arranque): si el tope de la Fase 1 cambia, start-period debe
# reajustarse en consecuencia.
# HEAD (-I) es obligatorio: /healthz hace proxy_method HEAD a /system_stats, asi que la
# respuesta 200 trae Content-Length pero SIN cuerpo; un GET colgaria hasta --timeout y el
# contenedor marcaria "unhealthy" (~900s tras arrancar) estando perfectamente sano.
# timeout=10s > proxy_connect/read 5s de nginx; el probe HEAD sano completa en <1s.
HEALTHCHECK --interval=30s --timeout=10s --start-period=900s --retries=3 \
    CMD ["curl", "-fsSI", "http://127.0.0.1:8188/healthz"]

ENTRYPOINT ["/usr/bin/tini", "--", "/start.sh"]

