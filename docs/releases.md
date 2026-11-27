# Releases

Every distribution release carries its `install.sh`, which installs the
`orchard` command (OrchardLinux/tools) and runs `orchard install` for that
release.

This repository's releases (a `v*` tag, see [GitHub builds](ci.md)) publish the
component bundles, the loaders `m1n1.bin` and `pongo.bin`, source archives,
`SHA256SUMS` and the attestation bundle. They publish no program: the `orchard`
command is OrchardLinux/tools' and builds from these recipes and data.

Publish deliberately: a tag creates a **draft** release; `publish.yml` checks
the checksums and the build attestation before publishing the exact assets.
Qualify installation and cold-restart launch on supported hardware before
announcing a release.
