/* SPDX-License-Identifier: GPL-2.0-only */
/* Derived from Project Cascadia; see NOTICE. */
#include "cascadia-generated.h"

	.arch	armv7-a
	.syntax	unified
	.arm

	.equ	KERNEL_ENTRY,	LOAD_ADDRESS + KERNEL_OFFSET

	.section .text.start, "ax"
	.global	_start
	.global	cascadia_main
	.global	linux_boot_jump
_start:
	cpsid	if
	msr	cpsr_c, #0xd3

	ldr	sp, =LOAD_ADDRESS + 0x7000
	bl	cascadia_main
	/* main validates the image and returns DTB physical address. */
	cmp	r0, #0
	beq	1f
	b	linux_boot_jump
1:	wfi
	b	1b

/*
 * r0 = DTB physical address
 * Tear down MMU/caches, then bx KERNEL_ENTRY with
 *   r0=0, r1=~0, r2=DTB
 */
linux_boot_jump:
	mov	r4, r0			/* save DTB */

	bl	v7_flush_dcache_all
	bl	l2x0_flush_all

	mrc	p15, 0, r0, c1, c0, 0
	bic	r0, r0, #(1 << 0)	/* MMU */
	bic	r0, r0, #(1 << 2)	/* D-cache */
	bic	r0, r0, #(1 << 12)	/* I-cache */
	bic	r0, r0, #(1 << 11)	/* Z */
	mcr	p15, 0, r0, c1, c0, 0
	isb

	mov	r0, #0
	mcr	p15, 0, r0, c7, c5, 0
	mcr	p15, 0, r0, c7, c5, 6
	mcr	p15, 0, r0, c8, c7, 0
	dsb
	isb

	mov	r0, #0
	mvn	r1, #0
	mov	r2, r4
	ldr	r3, =KERNEL_ENTRY
	bx	r3

1:	wfi
	b	1b

v7_flush_dcache_all:
	push	{r4-r11, lr}
	dmb
	mrc	p15, 1, r0, c0, c0, 1
	ands	r3, r0, #0x7000000
	mov	r3, r3, lsr #23
	beq	.Lfinished
	mov	r10, #0
.Lloop_level:
	add	r2, r10, r10, lsr #1
	mov	r1, r0, lsr r2
	and	r1, r1, #7
	cmp	r1, #2
	blt	.Lskip_level
	mcr	p15, 2, r10, c0, c0, 0
	isb
	mrc	p15, 1, r1, c0, c0, 0
	and	r2, r1, #7
	add	r2, r2, #4
	movw	r4, #0x3ff
	ands	r4, r4, r1, lsr #3
	clz	r5, r4
	movw	r7, #0x7fff
	ands	r7, r7, r1, lsr #13
.Lloop_way:
	mov	r9, r7
.Lloop_set:
	orr	r11, r10, r4, lsl r5
	orr	r11, r11, r9, lsl r2
	mcr	p15, 0, r11, c7, c14, 2
	subs	r9, r9, #1
	bge	.Lloop_set
	subs	r4, r4, #1
	bge	.Lloop_way
.Lskip_level:
	add	r10, r10, #2
	cmp	r3, r10
	bgt	.Lloop_level
.Lfinished:
	mov	r10, #0
	mcr	p15, 2, r10, c0, c0, 0
	dsb
	isb
	pop	{r4-r11, pc}

l2x0_flush_all:
	push	{r0, r1, r2, lr}
	ldr	r0, =L2_ADDRESS
	cmp	r0, #0
	beq	2f
	ldr	r1, [r0]
	lsr	r2, r1, #6
	and	r2, r2, #0x3f
	cmp	r2, #3
	cmpne	r2, #2
	cmpne	r2, #1
	bne	2f
	ldr	r1, [r0, #0x100]
	tst	r1, #1
	beq	2f
	mov	r1, #0xff00
	orr	r1, r1, #0xff
	str	r1, [r0, #0x7fc]
	ldr	r2, =1000000
1:	ldr	r1, [r0, #0x7fc]
	tst	r1, #1
	beq	2f
	subs	r2, r2, #1
	bne	1b
3:	wfi
	b	3b
2:	dsb
	pop	{r0, r1, r2, pc}
