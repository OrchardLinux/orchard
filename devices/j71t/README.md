# iPad 5 J71t qualification

Orchestrator-observed J71t integration evidence, 2026-11-08. Kernel:
`7.3.0-rc1-orchard-apple-16k-topic-warmport-20261108`, bundle
`build/orchard-pr2/cs/out-release-main-warmport-20261108`.

Omarchy menu Restart returns to a GPU-rendered desktop after td-kexec handover.
AOP reports `AOP released warm=1`, `AOP IS READY`, four IIO sensors and working
rotation. NVMe health PASS; charging +1.23 A. Host-assisted display/normal hops
using `scripts/orchard/warm-reboot.py` and the file loader (`kexec -s`) also PASS
with HEALTH PASS. Exact full invocations and per-hop log paths were not supplied;
do not invent them. CPU1 offline/online reports `psci: CPU1 killed (polled 0 ms)`;
SMP warm hops work. The CPU sysfs toggle is the reproducible test interface;
no new command was run by this preparation task.

Mac charging: allocation 2400 mA, `Apple host current grant: extra 1900 mA`,
input limit 2400000, battery Charging +1.1 to +1.4 A on cold boots and rebind.
Known gap: one warm boot from an AOP-blacklisted outgoing kernel stayed at
500 mA. Charging hardening is excluded. Stable `BOARD:j71t ID:<hash>` serial
with PID 0x1014 avoided Allow Accessory prompts on two cold boots and a rebind
using the interim initrd's identical format. The final shared helper/RAM host
key path was not independently flown.

Fixed NCM transferred 157 MB and 164 MB without stalls; long soak pending.
GPU desktop: labwc GLES2/PowerVR; Firefox WebGL1/2 via zink and WebGL Aquarium.
Earlier smoke: vkcube/es2gears 60 fps, glmark2 324–454. These are smoke results,
not a full conformance suite. AOP cold accel/gyro/pressure/light and rotation work.
Owner validates Bluetooth pairing, buttons, logout/login, manual brightness,
rotation and touch mapping without blackout, speaker and mic noises, Wi-Fi,
and zram policy. Auto-brightness is now smooth with improved fast-up response;
bar Display toggle is device-validated. Netconsole (=m) captured outgoing warm-hop
logs. PMU line is quiet and rtc0 is present; alarms and cover wake are unqualified.


These results apply to the integration build, not the isolated PR branches.
Suspend/resume and cover wake remain untested. Other boards are untested.
