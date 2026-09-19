## Highlights

This fork-only prerelease exercises the complete publication path for a
verified IoT Operations workspace package and its matching Site Ops engine.

- Install an identified Site Ops engine through the verified bundle or
  standalone wheel.
- Acquire a complete signed workspace package without cloning the repository.
- Pin the verified workspace in a project while keeping operator Sites
  separate from published content.

The package, routing descriptor, project pin, cache, planner, and executor
contracts remain provider neutral. This rehearsal uses GitHub as the first
release provider rather than making it the only supported source or gallery.

Installed-engine qualification checks package compatibility, protected cache
use, and guarded catalog loading. It does not authorize targets, deploy Azure
resources, compare executable deployment plans, or evaluate workload health.
This is a fork publication rehearsal, not an official Scale Kit release.
