# D2365 test fixtures

`t8010-j71b.dts` is the unmodified upstream file from the commit pinned in
`kernel/upstream.lock`, `cee9395acd8043be0644b25c34bfa86623f2b935`:

https://github.com/torvalds/linux/blob/cee9395acd8043be0644b25c34bfa86623f2b935/arch/arm64/boot/dts/apple/t8010-j71b.dts

Its Git blob is `534eb8413e08fece2595e5a1109e6646823e8802`; the original
license and copyright notices are retained. The device-tree test applies
every matching patch in the real J71b series with `git apply`, then checks
the resulting file. This keeps the test offline without mistaking the last
patch's added lines for the final device tree. When the upstream pin moves,
refresh this fixture and `J71B_UPSTREAM` together.

`t8010-ipad6.dtsi` is the unmodified upstream file from the same commit
(Git blob `1e46e4a3a7f4ad9f00d3dc0724aec2928c54b332`), for the PMIC power-off node, which sits in the
shared iPad 6 file that every iPad 6 and iPad 7 board includes.
