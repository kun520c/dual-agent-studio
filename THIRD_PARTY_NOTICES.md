# Third-party notices and license review

The project itself has no selected license; all rights are reserved. Dependency licenses do not license the project's own source.

This repository contains application/test source and four project GUI screenshots, plus a typographic social preview using a real screenshot. No third-party binary, font, JAR, Python wheel, node_modules or bundled Qt distribution is tracked. The retained Java stand-in snippets are fixed project fixture code, not redistributed dependencies. Historical model outputs and external project sources are omitted.

## Dependencies installed by users

| Dependency | Version | Declared license / source |
| --- | --- | --- |
| React / React DOM | 19.1.1 | MIT; npm package metadata and LICENSE |
| Lucide React | 0.468.0 | ISC; package LICENSE also contains Feather-derived MIT notice |
| TypeScript | 5.8.3 | Apache-2.0 |
| Vite | 6.4.4 | MIT; transitive notices remain in installed package |
| @types/react, @types/react-dom | 19.1.10 / 19.1.9 | MIT |
| PySide6 / Shiboken6 | 6.12.0 | LGPL-3.0 / GPL alternatives or commercial terms; Qt modules and WebEngine have additional third-party notices |

Frontend lockfile inventory: 72 package entries (including platform-optional packages), declared licenses: Apache-2.0: 1, BSD-3-Clause: 1, ISC: 2, MIT: 68. No unknown license declaration was found in this inventory. npm packages are fetched from registry.npmjs.org and keep their upstream license files. The dependency lockfile is not a redistribution of those binaries.

Primary references: [React license](https://github.com/facebook/react/blob/main/LICENSE), [Lucide license](https://github.com/lucide-icons/lucide/blob/main/LICENSE), [TypeScript license](https://github.com/microsoft/TypeScript/blob/main/LICENSE.txt), [Vite license](https://github.com/vitejs/vite/blob/main/LICENSE), [PySide6 on PyPI](https://pypi.org/project/PySide6/6.12.0/), [Qt for Python license notices](https://doc.qt.io/qtforpython-6/licenses.html).

Qt licensing needs a separate distribution review before shipping a future binary installer. This publication supplies source and installation references only, with no Qt/browser redistribution and no claim of universal license compatibility for future packaging. GitHub Actions dependencies use official actions repositories and are not copied into the source tree.
