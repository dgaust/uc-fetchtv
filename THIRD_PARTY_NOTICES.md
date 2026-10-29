# Third-party notices

uc-fetchtv is licensed under the [Mozilla Public License 2.0](LICENSE). It uses
and, in the install file and Docker image, includes the software and artwork
below. They keep their own licences, reproduced or linked here.

## ucapi (Unfolded Circle Integration API library)

- Licence: MPL-2.0 (the same licence as this project)
- Copyright: Unfolded Circle ApS
- Source: https://github.com/unfoldedcircle/integration-python-library

Used unmodified. Its source code is available from the address above.

## websockets

- Licence: BSD-3-Clause
- Copyright: Aymeric Augustin and contributors
- Source: https://github.com/python-websockets/websockets

```
Copyright (c) Aymeric Augustin and contributors

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

    * Redistributions of source code must retain the above copyright notice,
      this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright notice,
      this list of conditions and the following disclaimer in the documentation
      and/or other materials provided with the distribution.
    * Neither the name of the copyright holder nor the names of its contributors
      may be used to endorse or promote products derived from this software
      without specific prior written permission.

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

## Python and PyInstaller

The install file for the Remote bundles the Python runtime (licensed under the
[Python Software Foundation License](https://docs.python.org/3/license.html))
and is built with [PyInstaller](https://pyinstaller.org/) (GPL-2.0-or-later with
an exception that allows the bundled program to use any licence). The Docker
image is based on the official `python` image.

## Material Design icon (channel placeholder)

The placeholder image shown for channels without a logo (`intg-fetchtv/placeholder.py`)
is a redrawing of the Material Design "tv" icon.

- Licence: Apache-2.0 (https://www.apache.org/licenses/LICENSE-2.0)
- Copyright: Google LLC
- Source: https://github.com/google/material-design-icons

## pyfetchtv (acknowledgement)

The Fetch protocol isn't documented by Fetch. This project's client was written
from scratch, but it builds on what
[pyfetchtv](https://github.com/jinxo13/pyfetchtv) (BSD licence, by jinxo13)
worked out about the service: its endpoints, message types and key names. No
pyfetchtv code is included.

## Trademarks

"Fetch" and "FetchTV" are trademarks of their owner. "Unfolded Circle" and
"Remote 3" are trademarks of Unfolded Circle ApS. This project is not affiliated
with, endorsed by or supported by any of them. The channel logos shown on the
Remote are loaded from Fetch's own servers when needed and are not part of this
repository.
