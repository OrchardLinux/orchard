# P105: writing a Linux entry into the LwVM table

State: code only. No device has run it, and nothing here claims that it is
safe for iOS.

## What orchard does

When the iOS disk's LwVM map has unallocated chunks stored back to back
(at least the minimum root size, and room left in LwVM's virtual space and
descriptor table), `orchard install` offers "create a Linux partition in free
space". The person has to type `create partition`. Then:

1. The host reads both header copies (offset 0 and the end of the first
   chunk), which must agree and match what was inspected.
2. The host computes the new header (`src/orchard/lwvm.py`,
   `allocate_free`): one Linux entry (`0fc63daf-…`, name `Linux`, no
   attributes) appended after the others, its virtual range after every
   other partition's, the free chunks mapped to it in order, the CRC
   recomputed, and the result parsed again. The allocation follows Shayne
   Legault's `allocate` in #94; nothing else in the header changes.
3. The add-on writes the copy at the end of the first chunk, reads it back,
   then the primary, reads it back (`dd ... conv=fsync`), and the host parses
   both copies again.
4. The kernel re-reads the table and must add the partition with the planned
   number, start and size. Only then is the root written into it.

If the install stops between the two writes, the copies disagree: the
kernel keeps the old primary, and the installer refuses the disk until
someone looks.

## Why it has not run on a device

- Today no unallocated LwVM space exists: Shayne's resize tool shrinks the
  Data filesystem but does not yet shrink Data's LwVM entry, so this option
  never appears.
- Whether iOS 9 accepts an LwVM entry it did not create is unknown; Shayne's
  notes mention a key-store step on the iOS side.

The first device test of this write happens only after Shayne's resize tool
has shown that LwVM writes are safe for iOS (iOS still boots, Data still
unlocks). Until then the code is tested only with synthetic headers
(`tests/test_lwvm_free.py`).
