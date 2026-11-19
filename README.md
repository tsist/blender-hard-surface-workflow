# Hard Surface Workflow

This repository contains the project-authored Hard Surface Workflow Blender extension. It provides editable modeling tools, command recipes and panels. Blender 5.3 routes qualified Curve and GStretch configurations to native operators. Other LoopTools configurations use the compatibility implementation supplied at build time.

## Source release 0.3.1

The public release contains the project's source. A local installable archive requires a separately supplied, qualified dependency bundle. The dependency members are from LoopTools 4.7.7, EdgeFlow and Edit Mesh Tools, including the compatibility changes present in the original local distribution. The recorded digests select the exact members.

```sh
python build.py --dependency-bundle /path/to/qualified-hard-surface-package.zip --output hard_surface_workflow-0.3.1.zip
```

The builder checks every dependency SHA-256 before starting the archive and keeps the upstream notices from the dependency bundle. Install the resulting ZIP via **Preferences → Get Extensions → Install from Disk**. The assembled extension was checked on Windows Blender 5.3.0 and 5.2.1 LTS. Six native configurations and ten compatibility configurations were checked on 5.3. The public repository does not distribute the dependency bundle.

Project source is GPL-3.0-or-later. See the retained licence in the extension directory.
