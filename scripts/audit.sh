#!/bin/bash

# Run Trivy filesystem scan
echo "Running Trivy filesystem scan..."
trivy fs . --severity CRITICAL,HIGH --exit-code 1

# Run Trivy container scan
echo "Running Trivy container scan..."
trivy image comfyui:latest --severity CRITICAL,HIGH --exit-code 1

# Run Grype dependency scan
echo "Running Grype dependency scan..."
grype . --fail-on high

# Check exit status
echo "Audit completed."