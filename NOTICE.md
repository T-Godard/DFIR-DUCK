# NOTICE

DFIR-DUCK
Copyright 2026 Titouan Godard

This product is licensed under the Apache License, Version 2.0 (see `LICENSE`).

## Third-party components

DFIR-DUCK does not include the tools below in its source tree. The
`Dockerfile` downloads them at build time, pinned to the versions set in its
`ARG` declarations, and the application runs them as separate processes. Each
keeps its own license and copyright.

DFIR-ORC itself is not listed: it runs on the machine being collected, not
here, and DFIR-DUCK only reads the archives it produces.

| Component | Version (Dockerfile ARG) | License | Source |
|---|---|---|---|
| DFIR-OGRE | `OGRE_REF` | Apache-2.0 | <https://github.com/ANSSI-FR/dfir-ogre> |
| DFIR-OGRE Windows plugins | `OGRE_PLUGINS_REF` | LGPL-3.0 | <https://github.com/ANSSI-FR/dfir-ogre-plugin-windows> |
| Chainsaw | `CHAINSAW_REF` | GPL-3.0 | <https://github.com/WithSecureLabs/chainsaw> |
| Hayabusa | `HAYABUSA_REF` | AGPL-3.0 | <https://github.com/Yamato-Security/hayabusa> |
| Zircolite | `ZIRCOLITE_REF` | LGPL-3.0-or-later | <https://github.com/wagga40/Zircolite> |
| SigmaHQ rules | `SIGMA_REF` | DRL 1.1 (rules), public domain (specification) | <https://github.com/SigmaHQ/sigma> |
| rust-libesedb | `LIBESEDB_SYS_REF` | MIT | <https://github.com/sunsetkookaburra/rust-libesedb> |
| libesedb-python (libyal libesedb) | `LIBESEDB_PY_REF` | LGPL-3.0-or-later | <https://github.com/libyal/libesedb> |

Python dependencies are listed in `requirements.txt` and installed from PyPI;
they keep their own licenses.

## Distributing a built image

**Build the image locally; do not publish it.** `./init.sh` is the supported
path, and the default setup expects each operator to build their own.

Publishing a prebuilt image redistributes the components above, and the
**GPL-3.0, AGPL-3.0 and LGPL-3.0** ones (Chainsaw, Hayabusa, Zircolite, the
OGRE plugins) require you to provide the corresponding source of those works to
whoever receives the image — an obligation this project cannot discharge on
your behalf.

Sharing the source tree — including the `Dockerfile`, which merely fetches the
tools — carries none of these obligations.

## Bundled in this repository

Unlike the tools above, these are **part of the source tree**, so sharing it
redistributes them. Both licences ask only that their notice be kept.

[htmx](https://htmx.org) 1.9.12, in `app/static/vendor/htmx.min.js`, unmodified,
under the BSD 2-Clause licence:

```
Copyright (c) 2020, Big Sky Software
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## Icons

The settings icon in the web interface is the `settings` glyph from
[Feather](https://github.com/feathericons/feather), used unmodified under the
MIT license. Its copyright notice:

```
Copyright (c) 2013-2023 Cole Bemis

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

## Detection rules

Sigma rules are used unmodified, under the Detection Rule License (DRL) 1.1,
which requires attribution to SigmaHQ and its rule authors. Findings produced
from those rules should keep that attribution.
