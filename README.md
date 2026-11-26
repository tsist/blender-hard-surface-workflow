# Hard Surface Workflow

Hard Surface Workflow is an editable mesh and modifier-stack extension for Blender. Version **0.3.7** adds surface sweeps for rectangular seals and hollow round pipes. Define a route with named stations, project to the installation side, and preview a change before committing it. The extension retains precision mesh and Face Flow inspection tools. The release was tested on **Windows Blender 5.3.0**, build `02f0899d78e3`.

## Build and install

The public repository contains the project-authored extension source. The build uses a separately supplied, qualified dependency bundle containing the pinned LoopTools, EdgeFlow and Edit Mesh Tools members. The 17 dependency members from the previously qualified 0.3.1 package are byte-identical and can be reused.

```sh
python build.py --dependency-bundle /path/to/qualified-hard-surface-package.zip --output hard_surface_workflow-0.3.7.zip
```

The builder verifies every dependency member against `dependencies.json` and preserves the upstream notices. Install the resulting ZIP through **Preferences → Extensions → Install from Disk**.

## Surface sweep

In Object Mode, select the installation surface and open **N → Hard Surface → 贴面扫掠**. Load a recipe from a Blender Text datablock. Preview the sweep, review its section dimensions and minimum clearance, then commit it. A named station Empty controls a local edit. The preview marks the affected route and the saved file retains the last committed state.

The [walkthrough](skills/blender-hard-surface-modeling/references/surface-sweep.md) starts with a flat test surface and a [paste-ready recipe](skills/blender-hard-surface-modeling/references/surface-sweep-example.json). It covers rounded bends, miter limits, and a hollow pipe with open ends.

## Modeling skill

The [hard surface modeling skill](skills/blender-hard-surface-modeling/SKILL.md) covers upfront modeling plans and five-tier part breakdown methodology, three modeling routes (full-quad Sub-D cages, mid-poly bevel + weighted normals, and hybrid assemblies), three blockout strategies, Blender 5.3 golden modifier stacks, assembly interfaces, and in-process wireframe/highlight checks aligned with G1-G4 white-model review. It ships 8 focused craft guides, 22 visual comparison plates, and 2 precision rewind planners. Install the skill by copying its complete directory to your agent's skill directory.

The extension panel runs in Blender 5.3.0. Examples explicitly marked workspace CLI 0.91.0 and the two rewind planners require that workspace distribution. See the [public blenderctl releases](https://github.com/tsist/blenderctl/releases) for their supported commands.

The extension source is GPL-3.0-or-later. See the [license](hard_surface_workflow/LICENSE).
