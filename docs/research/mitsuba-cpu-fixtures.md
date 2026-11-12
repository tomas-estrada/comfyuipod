# Mitsuba CPU inverse-rendering fixture

## Use in this repository

A12 requires a fixture that estimates an identifiable scene parameter with Mitsuba on CPU and compares it with a baseline and held-out evidence. The build smoke is `scripts/smoke_mitsuba_cpu_fit.py`; its dependency is isolated as the `inverse-rendering` Studio extra and pinned in `studio/pyproject.toml`.

The fixture uses Mitsuba's `scalar_rgb` CPU variant, a 32×32 sphere under a constant environment, and a single unknown diffuse red-albedo value. A reference image from one camera and fixed seed is the optimization objective. A second camera and independent seed are held out from the fit. Bounded golden-section search evaluates 24 candidate values. It reports both parameter-space error against the known fixture value and image-space error on the held-out view, alongside the unoptimized baseline.

On the development machine with Mitsuba 3.9.1, the fixture recovered red albedo 0.699998 from ground truth 0.7. Parameter MSE fell from 0.36 to about 3.14e-12, while held-out image MSE fell from about 0.4065 to 3.66e-12. The main-image candidate smoke reruns the same fixture against the pinned package. These numbers are deterministic-fixture checks, not real-capture reconstruction accuracy or production performance.

## Operational guidance

- Start with a synthetic scene whose parameter has known ground truth and affects rendered pixels monotonically enough to identify.
- Keep optimization data separate from validation data. Change both camera and random seed for the held-out render.
- Report parameter error and image error separately; a low image loss can still conceal an incorrect or ambiguous parameter.
- Keep resolution, samples, and iteration count bounded for image-build smoke tests.
- `scalar_rgb` is a CPU scalar renderer, not Mitsuba's differentiable `llvm_ad_rgb` backend. This fixture tests a black-box optimizer around real Mitsuba renders; it does not establish differentiable gradients.
- Do not infer hidden surfaces, camera calibration, real-material accuracy, GPU capability, or neural reconstruction quality from this fixture.

## Primary references

- [Mitsuba gradient-based inverse-rendering tutorial](https://mitsuba.readthedocs.io/en/v3.5.0/src/inverse_rendering/gradient_based_opt.html): constructs a reference render, recovers known material reflectance, and tracks parameter error.
- [Mitsuba quickstart and variants](https://mitsuba.readthedocs.io/en/stable/src/quickstart/mitsuba_quickstart.html): distinguishes scalar CPU, LLVM CPU, and CUDA variants.
- [Mitsuba on PyPI](https://pypi.org/project/mitsuba/): package metadata, supported wheel platforms, included variants, and optional LLVM requirements.
