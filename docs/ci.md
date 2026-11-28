# GitHub builds

`.github/workflows/build.yml` tests the checkout on Python 3.11 and 3.14 and runs a native ARM64 compile/execute smoke test for pull requests and `main`. Tags matching `v*`, manual dispatches and the nightly additionally build, beside the tests instead of after them:

- Linux ARM64: per platform (one job each) the kernel, modules and every DTB; static recovery tools and quiet m1n1.
- macOS: quiet Pongo with the pinned newlib revision.
- Assembly: one `PLATFORM-components.tar.gz` per platform, `j71b-components.tar.gz` (for the released 0.1.10 tools), the loaders `m1n1.bin` and `pongo.bin`, source archives and `SHA256SUMS`.

The standard GitHub job image is Debian Bookworm, matching the local toolchain base. There is no nested Podman invocation. Native jobs set `ORCHARD_BUILD_NATIVE=1` and `ORCHARD_BUILD_ENV_ID`, run the same `install-toolchain` files used by the Containerfiles, and execute the same compiler recipes. Shared `ORCHARD_*` recipe variables select native filesystem paths in CI and mounted paths locally. Native execution is not a security sandbox: it does not enforce read-only inputs or isolate networking. Native mode is only for trusted, disposable Linux ARM64 workers. Local builds continue to use Podman by default.

GitHub must offer the `ubuntu-24.04-arm` runner for the repository. Kernel builds with debug information need substantial disk space and can take hours; the workflow timeout is six hours. The workflow records compiler package versions in the component bundle. Signed package repositories can change between builds, as with local builds; this is not a claim of byte-for-byte reproducibility.

## Build efficiency and runner costs

There is no package to build: the tests install the checkout, and the recipe jobs run the recipes from the checkout with Debian's `python3`. No job rebuilds another job's output. Kernel and tools/loader are separate jobs, so **rerun failed jobs**, not the entire workflow, when an unchanged run can be recovered. Final assembly consumes their artifacts without recompiling. The combined tools installer takes the union of the local recipes' package lists and runs one APT transaction.

Pongo/newlib compile once on macOS. An exact-input cache includes its source pins/build script, patch, runner OS/architecture, Xcode, SDK and compiler fingerprint; unchanged Apple inputs reuse the binary **and corresponding source archive**. Compiler jobs only run for tags, manual dispatch and the nightly; documentation-only pushes do not start CI.

A kernel job first asks the newest nightly (`ci/reuse_kernel.py`). When the platform's source key (`component-keys.json`: upstream pin, config, toolchain recipes, its boards' own modules) is the nightly's and the build scripts the key leaves out (`kernel/build`, `build-modules`, `stage-modules.py`, `series.py`, `kernel/modules/`, `buildenv.py`) are the files of the nightly's `orchard-source.tar.gz`, the job takes that nightly's kernel, toolchain lock and kernel source archive, each checked against the nightly's `SHA256SUMS`, and installs no compiler. A device-tree change on one platform so compiles one kernel, not three. The dispatch input `rebuild_kernels` compiles all of them regardless.

A kernel that is compiled keeps its compiler cache (ccache, which the local build uses too) between runs: one GitHub cache per platform and source key, `kernel-ccache-v1-PLATFORM-KEY`, and a new key starts from the newest older cache of its platform. ccache keys every object by the compiler binary, its arguments and the preprocessed source, so a stale cache can only miss; after the build the job drops the objects this build did not use, which keeps a cache one build large. Caches saved on `main` (the nightly) serve every branch; one saved by a dispatch on another branch serves only that branch. To start from nothing, raise `v1` in the cache key.

The tests gate what is published, not the compile: `assets` needs the tests, the smoke test and every compile job, and the draft release and the nightly need `assets`. Bundles and platform layers are gzip level 6: level 9 took eight times as long for 0.3 % smaller files.

Standard GitHub-hosted Linux ARM64 and macOS runners are free for public repositories. Included private-repository minutes are not a limit on these public standard-runner builds; larger runners are always billed. Artifact/cache storage is separate, so intermediate artifacts expire after two days and the Pongo cache is limited to its small, exact-input outputs. See [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

Record measured job/step durations from the run API, including cache hits, rather than treating workflow wall time as the sum of concurrent runner minutes. Keep the first hosted-run validation and timings in `docs/ci-validation.md` once the build has been exercised.

## Release behavior

Manual dispatch uploads two-day workflow artifacts only. A version tag must match `pyproject.toml`; after successful builds it creates a **draft** release without overwriting existing assets. Review and publish dependencies deliberately. Builds do not access devices or qualify storage/boot behavior.

Release in this order (orchard, the Orchard package repository, then the
distributions). Each step waits until the one before is published:

1. **Sources the packages follow.** The package repository publishes a package
   only when its source tags a release (OrchardLinux/packages README, "Releases
   only"). For every changed source that should ship (Mesa, Omactivity, Folio,
   ...), tag a version release (`v1.2.3`, digits only) and let *Follow sources*
   move the pin; send `source-updated` to skip its wait. Wait until the
   package repository's build has published them.
2. **orchard.** One pull request sets `pyproject.toml`'s version and renames
   CHANGELOG.md's Unreleased section to it. Merge it, push the tag `vX.Y.Z`,
   wait for the draft release, check it and publish it.
3. **orchard's kernels as packages.** In OrchardLinux/packages,
   `tools/pkgrepo orchard-release vX.Y.Z` moves the `linux-orchard-*` recipes to
   that release; merge and let it publish. Installed systems take the new
   kernel with their next update.
4. **Distributions.** First land, in the package repository, any change the
   distribution's recipe needs for this release (for Omarchy A a new upstream
   Omarchy pin: its package step refuses a recipe that pins another upstream
   Omarchy than `desktop/upstream.lock`). Then each distribution releases with
   its own pull request (changelog section, version) and tag. A tag build uses
   orchard's latest published release (step 2), never a nightly: orchard
   removes nightlies after 14 days, and a release links its files. Check the
   draft (the orchard release it links, the package versions its root
   installed) and publish it.
5. **The distribution's own package.** Omarchy A's recipe follows its
   releases: *Follow sources* moves it to the new tag (run it with
   `package=omarchy-a` to skip the wait), and installed tablets update to it.
6. Qualify a fresh install and a cold restart on hardware before announcing.

Cross-repository inputs must be published public releases, not drafts. The default repository token cannot read another repository's drafts. No broad personal access token is required for public dependencies.

## Artifact attestations

Public tag and manual builds attest every final asset, including the checksum manifest, with GitHub build provenance. The hardware bundle is retained as `orchard-build.sigstore.jsonl` alongside workflow artifacts and draft/attached release assets.

Attestation and bundle-copy steps require repository visibility to be explicitly public. Private (or unknown-visibility) builds skip both steps; no signing token is requested and no attestation is sent to Sigstore. This follows the same public/private policy as Resize APFS. Signing permissions are scoped to the final assembly job, not tests or release-upload jobs. A public signing failure prevents the release upload.

The bundle is created **after** signing the checksum manifest, so it is deliberately not listed inside that manifest. Verify an asset against this repository's attestation API with:

```sh
gh attestation verify j71b-components.tar.gz --repo OrchardLinux/orchard
```

Use the expected repository identity, not just a matching hash. Attestation identifies the workflow/commit and bytes; it does not establish reproducibility or physical-device qualification. See [GitHub's attestation documentation](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations).
