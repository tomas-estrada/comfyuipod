# GPU Out-of-Memory (OOM)

## Symptoms
- ComfyUI crashes with `CUDA out of memory` error.
- GPU utilization reaches 100% before crash.
- Error messages in logs: `RuntimeError: CUDA out of memory`.

## Root Causes
- Insufficient GPU memory for the selected model or batch size.
- Memory leaks in ComfyUI or custom nodes.
- Multiple processes competing for GPU resources.

## Diagnosis Steps
1. **Check GPU Memory Usage**:
   ```bash
   nvidia-smi
   ```

2. **Check ComfyUI Logs**:
   ```bash
   docker logs comfyui
   ```

3. **Check Batch Size and Resolution**:
   - Verify settings in ComfyUI workflow.

4. **Check for Memory Leaks**:
   - Monitor GPU memory over time using `nvidia-smi`.

## Resolution Steps
1. **Reduce Batch Size or Resolution**:
   - Lower the batch size in ComfyUI workflow.
   - Reduce image resolution.

2. **Free Up GPU Memory**:
   - Kill unnecessary processes using GPU:
     ```bash
     kill -9 <PID>
     ```

3. **Restart ComfyUI**:
   ```bash
   docker restart comfyui
   ```

4. **Use a Smaller Model**:
   - Switch to a model with lower memory requirements.

5. **Enable Gradient Checkpointing**:
   - Add `--enable-gradient-checkpointing` to ComfyUI startup flags.

## Prevention
- Monitor GPU memory usage with `nvidia-smi`.
- Use models and batch sizes that fit within GPU memory limits.
- Regularly update ComfyUI and custom nodes to fix memory leaks.

## References
- [NVIDIA CUDA Documentation](https://docs.nvidia.com/cuda/)
- [ComfyUI Memory Optimization](https://github.com/comfyanonymous/ComfyUI/wiki/Memory-Optimization)