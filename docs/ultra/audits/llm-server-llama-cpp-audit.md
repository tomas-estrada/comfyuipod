# `llm-server` / llama.cpp implementation audit

**Scope:** read-only source, image, runtime, and license audit using the local
repository and public upstream records. This is not GPU validation and does not
authorize paid inference or model-weight downloads.

## Findings

The repository has an outbound HTTP adapter for `llama-server`, but the
container currently has no build/download/install step for that executable and
does not start a local server process. The adapter's request to
`127.0.0.1:8080` therefore has no in-repository producer unless an operator
starts one separately. Existing preparedness text says the binary is baked,
which overstates the current image behavior.

The official `v0.9.0` release is source-only. Its sole release asset is a
7-byte `nightly-tag.txt` pointer containing `b12110`; it is not a llama.cpp
binary. The annotated tag resolves to commit
`cd959bae99ff4bed22bef2604f45241c09bbb8c8`. The `b12110` nightly metadata
points to that commit and includes CUDA archives, but the audit did not
cryptographically establish an Actions build-to-asset provenance chain. Do not
label the nightly archive as a `v0.9.0` release binary.

llama.cpp is MIT licensed, but a complete binary notice review must also cover
compiled vendored code. The audit verified the xxHash BSD-2-Clause notice
requirement, cpp-httplib and nlohmann MIT notices, and permissive/public-domain
notices for other inspected vendored components. The embedded UI assets,
Unicode-derived data, sha1 header, selected CUDA/NCCL backend, and final linked
system libraries still need review. The current engine `terms_review` is
therefore insufficient to mark notices fully reviewed.

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

## Upstream records

- [llama.cpp v0.9.0 release](https://github.com/ggml-org/llama.cpp/releases/tag/v0.9.0)
- [llama.cpp v0.9.0 license](https://raw.githubusercontent.com/ggml-org/llama.cpp/v0.9.0/LICENSE)
- [llama.cpp server build and usage documentation at v0.9.0](https://raw.githubusercontent.com/ggml-org/llama.cpp/v0.9.0/tools/server/README.md)

The upstream files and metadata are evidence for pin and license observations;
they do not substitute for a repository build, notice bundle, or runtime
acceptance test.
