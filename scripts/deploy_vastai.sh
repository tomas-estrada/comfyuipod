#!/bin/bash
#
# Deploy ComfyUIPod on vast.ai and validate GPU, storage, and networking.
#

# Exit on error
set -e

# Validate GPU
echo "Validating GPU..."
nvidia-smi
nvcc --version

# Validate Storage
 echo "Validating storage..."
df -h
ls -lh /models/

# Validate Networking
 echo "Validating networking..."
curl http://localhost:8188/status
curl http://localhost:8000/api/status

# Start ComfyUI and Studio API
echo "Starting ComfyUI and Studio API..."
cd /workspace/ComfyUI
python main.py --listen &

cd /workspace/studio
uvicorn comfy_studio.api:app --host 0.0.0.0 --port 8000 &

# Wait for services to start
sleep 10

# Validate ComfyUI and Studio API
echo "Validating ComfyUI and Studio API..."
curl http://localhost:8188/status
curl http://localhost:8000/api/status

echo "Validation complete."