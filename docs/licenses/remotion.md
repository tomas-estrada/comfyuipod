# Remotion 4.0.541 and dependency notices

The editor image installs the exact dependency graph in
[`studio/remotion/package-lock.json`](../../studio/remotion/package-lock.json)
with `npm ci`. The image keeps that project under `/opt/remotion`; the build
also copies Remotion's own `LICENSE.md` from the pinned `remotion` package to
`/usr/share/doc/remotion/LICENSE.md`. This file is copied to
`/usr/share/doc/remotion/THIRD-PARTY-NOTICES.md`.

## Remotion License

The pinned release identifies its terms as the **Remotion License**. Its exact
text is in the image at `/usr/share/doc/remotion/LICENSE.md` and in the source
package at `studio/remotion/node_modules/remotion/LICENSE.md` after `npm ci`.
The terms allow individuals, for-profit organizations with up to three
employees, nonprofits/not-for-profits, and evaluators to use Remotion to create
videos and images, including commercially. A for-profit organization outside
those groups needs a Company License. The license does not grant general
permission to copy or modify Remotion code to sell, rent, license, relicense,
or sublicense a Remotion derivative.

The product image therefore has a **conditional** redistribution decision.
Recipients must qualify for the Free License or obtain a Company License for
their use; ComfyUIPod does not grant a Remotion license. Do not present the
image as an unrestricted Remotion distribution.

## Package notice findings

The lock pins `kiwi-schema` 0.5.0, whose npm metadata declares MIT. Its npm
archive has no license file, so the MIT notice is reproduced below. The
copyright attribution follows the original Kiwi implementation's published
credits.

```text
MIT License

Copyright (c) 2016-2023 Evan Wallace

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

The pinned Remotion 4.0.541 Linux dependency install also exposes incomplete
license metadata in `@remotion/media`, `@remotion/sdk`,
`@remotion/studio-protocol`, and the Linux compositor packages. Remotion's
upstream [PR #13337](https://github.com/remotion-dev/remotion/pull/13337)
records that these packages should carry the existing Remotion License and
adds package-level license files/metadata. That fix is not part of the pinned
4.0.541 release. The root Remotion license is preserved alongside this notice;
the distribution decision remains conditional until the recipient's license
eligibility is established.

## Sources

- Remotion v4.0.541 license: <https://github.com/remotion-dev/remotion/blob/v4.0.541/LICENSE.md>
- Remotion v4.0.541 source revision: <https://github.com/remotion-dev/remotion/commit/e6df941441f3a2c8b4677a3dc5f1326746ac9db3>
- Upstream package-license metadata fix: <https://github.com/remotion-dev/remotion/pull/13337>
- `kiwi-schema` 0.5.0 license metadata: <https://www.npmjs.com/package/kiwi-schema/v/0.5.0>
- Kiwi project source: <https://github.com/evanw/kiwi>
- Kiwi implementation copyright credit: <https://www.npmjs.com/package/@open-pencil/kiwi>

Reviewed 2026-11-15 against the pinned lock, npm package metadata, the tagged
Remotion license, and the upstream package-license change. Recheck this record
when upgrading Remotion or changing image distribution.
