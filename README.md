# Hard Surface Workflow

Hard Surface Workflow is an editable mesh and modifier-stack extension for Blender. Version **0.3.5** supports **Blender 5.3.0**. It adds precision mesh editing and geometry inspection and retains the native and compatibility LoopTools routes.

## Build and install

The public repository contains the project-authored extension source. The build uses a separately supplied, qualified dependency bundle containing the pinned LoopTools, EdgeFlow and Edit Mesh Tools members. The 17 dependency members from the previously qualified 0.3.1 package are byte-identical and can be reused.

```sh
python build.py --dependency-bundle /path/to/qualified-hard-surface-package.zip --output hard_surface_workflow-0.3.5.zip
```

The builder verifies every dependency member against `dependencies.json` and preserves the upstream notices. Install the resulting ZIP through **Preferences → Extensions → Install from Disk**. Windows Blender 5.3.0 was used to validate the extension and verify the saved split-face and GStretch examples.

## Modeling skill

The [hard surface modeling skill](skills/blender-hard-surface-modeling/SKILL.md) covers subdivision cages, curved surfaces, native operators, geometry checks, and save and recovery procedures. It includes the step-by-step guides, illustrated references, and distilled case studies through tier four. Install the skill by copying its complete directory to your agent's skill directory.

The native workflows and extension panel use Blender 5.3.0. Examples marked `blenderctl` and the two rewind planners require the matching workspace CLI 0.91.0. The older [public Blender CLI releases](https://github.com/tsist/blenderctl/releases) do not provide those 0.3.5 routes.

The extension source is GPL-3.0-or-later. See the [license](hard_surface_workflow/LICENSE).
