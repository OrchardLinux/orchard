# J71t 0.4 qualification

The Nov 8 integration observations predate the rebased 0.4 topic branches.
They do not qualify these revisions. No feature is promoted to working here.

Merge device/chip changes with their implementing topics. Keep suspend/resume
untested until real wake, peripheral resume, long sleep and stress tests pass.
Camera and Touch ID remain missing and outside 0.4.

Vocabulary follow-ups: GPU/GL, warm reboot, RTC alarm, cover wake, pressure,
and hardware video decode need distinct definitions. RTC presence is not alarm
qualification; cover polling is not wake; pairing is not Bluetooth input/audio.

Required receipts: exact Orchard/kernel/Mesa/loader revisions, artifact hashes,
boot invocation, boot ID, logs, observed successes and failures, and the other
board maintainer's J71b regression result for shared changes.
