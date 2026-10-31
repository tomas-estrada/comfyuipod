#!/bin/bash
#
# Validate GPU, storage, and networking on vast.ai.
#

# Exit on error
set -e

# GPU Validation
echo "Validating GPU..."
nvidia-smi || { echo "GPU validation failed."; exit 1; }
nvcc --version || { echo "CUDA validation failed."; exit 1; }

# Storage Validation
echo "Validating storage..."
df -h || { echo "Storage validation failed."; exit 1; }
ls -lh /models/ || { echo "Model manifest validation failed."; exit 1; }

# Networking Validation
echo "Validating networking..."
curl -s http://localhost:8188/status || { echo "ComfyUI API validation failed."; exit 1; }
curl -s http://localhost:8000/api/status || { echo "Studio API validation failed."; exit 1; }

echo "All validations passed."