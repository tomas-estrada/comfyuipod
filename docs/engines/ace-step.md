# ACE-Step 1.5 music workflow

Studio's `generate_music` tool submits the native ComfyUI API template
`ace_step_music`. It uses ComfyUI core classes from the revision pinned in
[`config/nodes.lock`](../../config/nodes.lock) and saves FLAC audio into the
active project gallery.

The 10,025,478,736-byte ACE-Step 1.5 Turbo AIO checkpoint is optional. Set
`STUDIO_INCLUDE_MUSIC=1` before acquisition/deployment; the derived storage
requirement reads the same model manifest as the downloader. Template listing
and submission require both the opt-in and the checkpoint to be present on
disk.

The tool supports up to three sequential variants, a requested structure,
lyrics, duration, BPM, time signature, language, key scale, and seed. Its beat
map is derived from the requested tempo grid; audio-to-beat alignment is not
measured. The workflow does not claim to produce separated stems. GPU runtime,
VRAM/RAM use, audio quality, and musical alignment still require measurements on
the target hardware.

CPU contracts live in `studio/tests/test_c26_ace_step_assets.py`,
`studio/tests/test_c26_ace_step_template.py`, and
`studio/tests/test_c26_ace_step_tool.py`. They validate pinning, opt-in
availability, the native graph, project asset registration, sequential variant
seeds, and rollback after invalid output. They do not simulate a successful
model inference.
