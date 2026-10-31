# ComfyUI Crash

## Symptoms
- ComfyUI process terminates unexpectedly.
- Error messages in logs: `Segmentation fault`, `Killed`, or `Traceback (most recent call last)`.
- Web UI becomes unresponsive.

## Root Causes
- Insufficient system memory or GPU memory.
- Corrupted model files or configurations.
- Bugs in ComfyUI or custom nodes.
- Conflicts with other running processes.

## Diagnosis Steps
1. **Check Logs**:
   ```bash
   docker logs comfyui
   ```
   or
   ```bash
   journalctl -u comfyui -n 50 --no-pager
   ```

2. **Check System Resources**:
   ```bash
   nvidia-smi
   ```
   ```bash
   free -h
   ```

3. **Check for Corrupted Models**:
   - Verify model files in `models/` directory.
   - Re-download corrupted models.

4. **Check for Custom Node Issues**:
   - Disable custom nodes and restart ComfyUI.

## Resolution Steps
1. **Restart ComfyUI**:
   ```bash
   docker restart comfyui
   ```

2. **Free Up System Resources**:
   - Close unnecessary applications.
   - Reduce batch size or resolution in ComfyUI.

3. **Reinstall ComfyUI**:
   ```bash
   docker-compose down && docker-compose up -d
   ```

4. **Update ComfyUI and Custom Nodes**:
   - Pull the latest ComfyUI image.
   - Update custom nodes via ComfyUI-Manager.

## Prevention
- Monitor system resources using `nvidia-smi` and `htop`.
- Regularly update ComfyUI and custom nodes.
- Use stable versions of models and nodes.

## References
- [ComfyUI GitHub Issues](https://github.com/comfyanonymous/ComfyUI/issues)
- [NVIDIA System Management Interface](https://developer.nvidia.com/nvidia-system-management-interface)