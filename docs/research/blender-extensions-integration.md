# Blender Extensions for the agent harness

Research snapshot: 2026-11-12. Recheck the official repository metadata before
pinning a package; marketplace versions and compatibility change.

## What the catalogue gives us

The official [Blender Extensions add-on catalogue](https://extensions.blender.org/add-ons/)
listed 1,184 add-ons in this snapshot. Blender's official API can return the
extensions compatible with a specific Blender version and platform, for example
`https://extensions.blender.org/api/v1/extensions/?blender_version=5.2.4&platform=linux-x64`.
Its entries provide package ID and version, archive size, SHA-256 archive hash,
download URL, minimum/maximum Blender versions, maintainer, SPDX license, tags,
and declared permissions. The [API schema](https://developer.blender.org/docs/features/extensions/api_listing/v1/)
defines these fields.

The [platform](https://extensions.blender.org/about/) hosts free and open-source
extensions compatible with Blender's GPL requirements. The [license policy](https://docs.blender.org/manual/en/latest/advanced/extensions/licenses.html)
requires GPL v3 or later for add-ons on the platform. The [Blender manual](https://docs.blender.org/manual/en/latest/editors/preferences/extensions.html)
documents repository-based and local ZIP installation and says repository updates
use the latest version currently available. That is convenient for artists, but
not a reproducible production dependency. Blender's [command-line extension
interface](https://docs.blender.org/manual/en/latest/advanced/command_line/extension_arguments.html)
supports listing, installing, installing a local package file, and enabling at
installation time.

## Integration policy

The agent should be able to discover hundreds of candidates without loading
hundreds of Python packages into every Blender process or enlarging every image.
Discovery and execution are separate:

1. Search a version- and platform-filtered metadata catalogue. Return package ID,
   exact version/hash, size, license, declared permissions, compatibility, tags,
   and source URL so the agent can explain why a candidate fits a task.
2. Only an explicit, reviewed production profile may make a package available to
   a Blender job. The image build downloads the exact archive and verifies its
   SHA-256 before installing from the local file. Do not install the repository's
   mutable `latest` from an agent request.
3. The Studio agent calls typed capability tools through its role-restricted
   `ToolBelt`; it never accepts an arbitrary add-on URL, module name, or Python
   snippet. Blender work remains subject to the existing subprocess timeout,
   output validation, and per-job limits.
4. Each admitted extension needs a headless smoke for the specific operation it
   is supposed to provide. Catalogue tags, download counts, and marketplace
   approval are discovery signals; they do not prove headless compatibility or
   production value.

The permissions in an extension manifest are declared reasons for access, not
proof that the operating system prevents other access. Treat extension code as
executable code inside Blender. Any future execution profile should isolate
inputs, outputs, temporary files, environment credentials, network access, and
resource limits independently of those declarations. This is an engineering
inference from the manifest's metadata role and the fact that extensions contain
Python code; the marketplace listing does not certify an OS sandbox.

This supports a large discoverable catalogue while keeping only selected,
versioned dependencies in the runtime. The default should be zero optional
extensions until their operation is covered by a smoke and an acceptance row.

## Candidates and evidence limits

| Candidate | Potential fit | Official metadata in this snapshot | Gate before use |
|---|---|---|---|
| [Rigify](https://extensions.blender.org/add-ons/rigify/versions/) | C11 rigging; Blender's manual says it is bundled with Blender | 0.6.12, Blender 4.2+, GPL-2.0-or-later, no special permissions. The marketplace marks it approved but also warns it has not been reviewed. | First verify the exact bundled version in Blender 5.2.4; test a real metarig-to-rig operation. Do not fetch a second copy if it is already bundled. |
| [Ucupaint](https://extensions.blender.org/add-ons/ucupaint/versions/) | C19 layered material authoring and texture baking | 3.0.1, Blender 4.2+, 2.0 MB, GPL-3.0-or-later, declares filesystem access for loading/saving images. | Optional material profile only; prove its Python workflow headlessly and bound texture dimensions, output bytes, and temporary storage. |
| [Editable Motion Paths](https://extensions.blender.org/add-ons/editable-motion-paths/versions/) | Candidate for animation path review/editing around C12 | 2.0.5, Blender 4.4+, tags Animation/Rigging, GPL-3.0-or-later, declares file access for saved view renders. | Treat as an interactive review aid until a tested headless API exists; it does not replace motion reconstruction or held-out trajectory validation. |
| [Amaranth](https://extensions.blender.org/add-ons/amaranth/versions/) | Diagnostics and artist productivity | Version 1.0.34 supports Blender 4.2 through versions before 5.3; 1.0.35 targets Blender 5.3+. GPL-3.0-or-later; no special permissions. | Select by the actual Blender binary. This version split is why a single unversioned add-on list is unsafe. It is not a default headless production dependency. |

These are candidates, not approved production dependencies. In particular, UI
convenience and existing Blender features should not be mistaken for a missing
Studio capability. Prioritize add-ons only when they close a named acceptance
gap and their exact operation is measurable in the production path.

## Current repository connection

Blender is exposed through `BlenderEngine` and the Studio `artist3d` role. The
shared agent harness controls the available tools, but the older engine
`install(submotor, ref=...)` shape accepts a module name and optional package
path when dynamic installation is enabled. Before connecting marketplace
packages, replace that path with an immutable lock lookup and make “not in lock”
fail closed. Do not let marketplace discovery call the install operation.

Useful follow-up implementation: an offline catalogue search tool, a
version/platform-specific extension lock with archive hashes, and a build-time
installer that consumes only the lock. Keep the catalogue refresh separate from
the immutable runtime lock. Record the chosen extension version and hash in
every Blender run's provenance.
