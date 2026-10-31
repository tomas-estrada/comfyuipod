#!/bin/bash

# Deployment Script for vast.ai

set -e

# Configuration
DOCKER_REGISTRY="your_dockerhub_username"
INSTANCE_IMAGE="pytorch/pytorch"
DISK_SIZE=50
ONSTART_CMD="bash /workspace/start.sh"

# Build and Push Docker Images
echo "Building Docker images..."
docker-compose build

echo "Pushing Docker images to registry..."
docker tag comfyuipod-comfyctl $DOCKER_REGISTRY/comfyuipod-comfyctl:latest
docker tag comfyuipod-studio $DOCKER_REGISTRY/comfyuipod-studio:latest
docker tag comfyuipod-panel $DOCKER_REGISTRY/comfyuipod-panel:latest

docker push $DOCKER_REGISTRY/comfyuipod-comfyctl:latest
docker push $DOCKER_REGISTRY/comfyuipod-studio:latest
docker push $DOCKER_REGISTRY/comfyuipod-panel:latest

# Create vast.ai Instance
echo "Creating vast.ai instance..."
INSTANCE_ID=$(vastai create instance --image $INSTANCE_IMAGE --env "-e COMFY_PASS=password" --disk $DISK_SIZE --onstart-cmd "$ONSTART_CMD" --raw | jq -r '.new_contract')

echo "Instance ID: $INSTANCE_ID"

# Wait for Instance to Start
echo "Waiting for instance to start..."
while true; do
    STATUS=$(vastai show instance $INSTANCE_ID --raw | jq -r '.actual_status')
    if [ "$STATUS" == "running" ]; then
        break
    fi
    sleep 10
done

# Deploy ComfyUIPod
echo "Deploying ComfyUIPod to instance $INSTANCE_ID..."
vastai runscript $INSTANCE_ID << 'EOF'
#!/bin/bash
cd /workspace
git clone https://github.com/your_username/ComfyUIPod.git
cd ComfyUIPod
docker-compose pull
docker-compose up -d
EOF

echo "Deployment complete!"