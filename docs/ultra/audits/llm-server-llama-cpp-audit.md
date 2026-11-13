# `llm-server` / llama.cpp implementation audit

**Scope:** read-only source, image, runtime, and license audit using the local
repository and public upstream records. This is not GPU validation and does not
authorize paid inference or model-weight downloads.

## Findings

The repository has an outbound HTTP adapter for `llama-server`, but the
container currently has no build/download/install step for that executable and
does not start a local server process. The adapter's request to
`127.0.0.1:8080` therefore has no in-repository producer unless an operator
starts one separately.

The official `v0.9.0` release is source-only. Its sole release asset is a
7-byte `nightly-tag.txt` pointer containing `b12110`; it is not a llama.cpp
binary. The annotated tag resolves to commit
`cd959bae99ff4bed22bef2604f45241c09bbb8c8`. The `b12110` nightly metadata
points to that commit and includes CUDA archives, but the audit did not
cryptographically establish an Actions build-to-asset provenance chain. Do not
label the nightly archive as a `v0.9.0` release binary.

llama.cpp is MIT licensed, but a complete binary notice review must also cover
compiled vendored code and final linked libraries. The pinned cpp-httplib and
nlohmann headers contain copyright attributions beyond the separately copied
license files. The generated Unicode normalization tables require a Unicode
notice, now added by the follow-up below. The CPU lock disables CUDA, examples,
and the prebuilt UI; the image's final linked system-library notice coverage
still needs verification. The engine therefore remains conditional and does
not claim complete notice review.

## Implementation decision

Keep `llm-server` marked unimplemented until the repository can demonstrate all
of the following with tracked evidence:

1. A reproducible build from a pinned immutable source revision, with the
   intended CPU/CUDA configuration stated explicitly.
2. A passing binary smoke (`llama-server --version`) and an explicit image
   inventory check.
3. A supervised launch/lifecycle path that binds to loopback and works with
   the existing adapter, or a deliberately documented externally-managed
   process contract with a health probe.
4. A complete notice bundle for the actual compiled configuration.
5. A local, non-billable HTTP contract test. No weights are needed for this
   contract test, and model inference quality remains a separate measurement.

An upstream source build is more reproducible than consuming the nightly
binary. A CUDA build would require a CUDA development stage and a runtime
compatibility check; the read-only audit did not build either configuration.
No binary, model weights, or inference run were produced by this audit.

## Implementation follow-up (2026-11-13)

A follow-up branch adds a pinned, CPU-only source build for the image, records
the source/CMake options and explicit upstream license-file inventory in a
tracked lock, bounds compiler parallelism from cgroup/host resources, and
updates the adapter's packaged-binary status. The process remains externally
managed and model weights remain user-provided. A supplemental, hash-verified
Unicode License V3 notice accompanies the generated Unicode 16.0.0
normalization tables in the image. This resolves that attribution gap only;
the additional cpp-httplib/nlohmann attributions and final linked
system-library notice coverage remain open, so the manifest keeps
redistribution conditional and `notices_reviewed` false. Image CI must build
the candidate before packaging can be claimed as verified; this follow-up does
not claim inference quality or GPU validation.

## Upstream records

- [llama.cpp v0.9.0 release](https://github.com/ggml-org/llama.cpp/releases/tag/v0.9.0)
- [llama.cpp v0.9.0 license](https://raw.githubusercontent.com/ggml-org/llama.cpp/v0.9.0/LICENSE)
- [llama.cpp server build and usage documentation at v0.9.0](https://raw.githubusercontent.com/ggml-org/llama.cpp/v0.9.0/tools/server/README.md)
- [Unicode 16.0.0 Character Database ReadMe](https://www.unicode.org/Public/16.0.0/ReadMe.txt)
- [Unicode License V3](https://www.unicode.org/license.txt)
- [Unicode Terms of Use](https://www.unicode.org/terms_of_use.html)
- [Pinned llama.cpp Unicode table generator](https://github.com/ggml-org/llama.cpp/blob/cd959bae99ff4bed22bef2604f45241c09bbb8c8/scripts/gen-unicode-data.py)
- [Pinned cpp-httplib header attributions](https://github.com/ggml-org/llama.cpp/blob/cd959bae99ff4bed22bef2604f45241c09bbb8c8/vendor/cpp-httplib/httplib.h)
- [Pinned nlohmann/json.hpp attributions](https://github.com/ggml-org/llama.cpp/blob/cd959bae99ff4bed22bef2604f45241c09bbb8c8/vendor/nlohmann/json.hpp)

The upstream files and metadata are evidence for pin and license observations;
they do not substitute for a repository build, notice bundle, or runtime
acceptance test.
