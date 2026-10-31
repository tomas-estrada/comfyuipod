# Model Download Failures

## Symptoms
- Model download fails with `404 Not Found` or `Connection Error`.
- Partial or corrupted model files in `models/` directory.
- ComfyUI fails to load models.

## Root Causes
- Invalid or outdated model URLs.
- Network issues or firewall restrictions.
- Insufficient disk space.
- Rate limiting by the model host.

## Diagnosis Steps
1. **Check Download Logs**:
   ```bash
   docker logs comfyui
   ```

2. **Verify Model URL**:
   - Check the model URL in `config/models-manifest.json`.

3. **Check Disk Space**:
   ```bash
   df -h
   ```

4. **Test Network Connectivity**:
   ```bash
   ping huggingface.co
   ```
   ```bash
   curl -I <model-url>
   ```

## Resolution Steps
1. **Retry Download**:
   - Use `scripts/download-models.sh` to retry the download.

2. **Manually Download Model**:
   - Download the model using `wget` or `curl`:
     ```bash
     wget -O models/<model-name> <model-url>
     ```

3. **Free Up Disk Space**:
   - Delete unnecessary files or expand disk space.

4. **Use a Mirror or Alternative Host**:
   - Download the model from a mirror or alternative source.

5. **Update Model Manifest**:
   - Update `config/models-manifest.json` with the correct URL.

## Prevention
- Regularly update `config/models-manifest.json`.
- Monitor disk space and network connectivity.
- Use reliable model hosts and mirrors.

## References
- [Hugging Face Model Hub](https://huggingface.co/models)
- [CivitAI Models](https://civitai.com/)