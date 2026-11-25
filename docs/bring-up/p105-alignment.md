# P105 kernel alignment fault check

This check covers the four `addr < TASK_SIZE` branches added to
`arch/arm/mm/alignment.c` by the Cascadia import in
[issue #79](https://github.com/OrchardLinux/orchard/issues/79). The test device
was an iPad mini 1 Wi-Fi (`iPad2,5`, `apple,p105ap`).

Two instrumented ARMv7 kernels differed only in those four branch conditions.
Both used the same config, DTB, initramfs and probe module. The staged RAM
bundle hashes were:

| Kernel | Bundle SHA-256 |
| --- | --- |
| With the Cascadia conditions | `7c1a8369369a4081352d11c9ac4986b66840ed967ea2c09b6b241cf0a16245bd` |
| With the conditions removed | `a4ac68f468241019710ee58e860acec396e21bea64c21db587cc2665883d4333` |

The probe module performed a privileged read from a user buffer whose address
had remainder one when divided by four. Each instruction had an exception
table recovery entry. The module checked the loaded value against the bytes
in that buffer. Instrumented `/proc/cpu/alignment` counters showed one system
fault in the intended handler on each image. Each read matched. The `Skipped`
counter stayed at zero, and the probes added no kernel log line.

| Handler | Instruction | CPU setting during probe | With conditions | Conditions removed |
| --- | --- | --- | --- | --- |
| Halfword | `ldrh` | Alignment trap enabled for one instruction | Fault handled; value matched | Fault handled; value matched |
| Word | `ldr` | Alignment trap enabled for one instruction | Fault handled; value matched | Fault handled; value matched |
| Doubleword | `ldrd` | Normal setting | Fault handled; values matched | Fault handled; values matched |
| Multi-register | `ldmia` | Normal setting | Fault handled; values matched | Fault handled; values matched |

The normal CPU control register read `0x10c5387d`, with its alignment-trap bit
clear. Under that setting the halfword and word loads completed in hardware.
For those two handler checks, the module briefly set that bit to read
`0x10c5387f` on its current CPU, ran one instruction with interrupts and
preemption held off, then restored the original register. The doubleword and
multi-register loads faulted under the normal setting.

The clean fix is Linux topic commit `f3679ef654bbff0d67378417c2f69079618dcfc2`
on top of the kernel tested for PR #237. It removed the four branch conditions
without carrying the comparison counters. Its `zImage` SHA-256 was
`57977dbfe56caf8e42bb9039229fa75219d08d1a62d666a78a57c5a79bcdf263`.
The final RAM bundle SHA-256 was
`ffbd3783073f6b7346de8b67524dab521754876b1b06b66217f9b22ce0a69281`.
It booted with both CPUs, AIC timer events, a 768 by 1024 framebuffer, USB
serial and SSH. The same four probes each reached the intended standard
counter and returned the expected value.

Each Linux boot used a RAM root. Its device tree marked the internal flash
controller `disabled`. The host check found no MTD device and no Apple SFTL
disk. The approved boot handoff staged temporary files in iOS before Linux
started. The post-test clean kernel still had the flash controller disabled,
four system alignment faults, no skipped faults and no probe module loaded.
Its only new kernel log line reported the out-of-tree probe module taint.

The result establishes the tested P105 handler paths. The early J1 boot fault
and J1-specific device tree work in issue #79 need checks on that model.
