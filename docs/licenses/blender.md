# Blender runtime artifact

The image includes the official Blender 5.2.4 LTS Linux x86_64 archive. Its
upstream source revision is `cd5dc428cce1993a4b35986f227c6a24e42a365c`; the
downloaded archive is pinned by SHA-256
`2fd14c50987426897ba60237ee996edf2b4151f4b1a72a460ca20be58c1d2d86`.

Redistribution is allowed under Blender's GPL terms. The image retains the full
official distribution under `/opt/blender`, including `license/license.md`,
`license/licenses.json`, and bundled component notices. No separately sourced
add-ons, assets, or model weights are included by this change. The official
archive is fetched and hash-verified in the Docker build; the build fails if
the runtime or its bundled license index is missing.

Sources:

- [Blender 5.2 LTS status](https://www.blender.org/download/lts/)
- [Blender license terms](https://www.blender.org/about/license/)
- [Blender 5.2.4 source revision](https://projects.blender.org/blender/blender/src/commit/cd5dc428cce1993a4b35986f227c6a24e42a365c)
- [Official Linux archive SHA-256 list](https://download.blender.org/release/Blender5.2/blender-5.2.4.sha256)
- [Blender release license bundle guidance](https://developer.blender.org/docs/handbook/release_process/release_checklist/third_party_licenses/)
