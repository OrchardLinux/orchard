/*
 * fake_alsa.c -- a stand-in for the ALSA kernel interface, for the headless
 * tests only (src/test.sh). voidmoths.c includes it when it is built with
 * -DVM_FAKE_ALSA; the phone binary never contains it.
 *
 * The device files under $FAKE_ALSA_ROOT are plain files; this code answers
 * the ioctls the game sends to them, like the n66 speaker card would:
 * "Speaker Volume" 0..204 with a -102 dB + 0.5 dB/step scale, 48 kHz S16_LE
 * mono, at least 512-byte periods, 2..16 periods. The "hardware" plays one
 * game tick of frames (1600) per tick.
 *
 * FAKE_ALSA_MODE: ok (default), hwpick (512-frame periods refused), hwfail,
 * notlv, loud (scale starts at 0 dB), readback (volume reads back wrong),
 * wrongcard (CARD_INFO names another card), xrun (plays 3x too fast now and
 * then), dead (writes fail with EIO).
 * FAKE_ALSA_DUMP: file that receives every frame written (raw S16_LE mono).
 *
 * Any open of a file that is not the speaker card's control or playback
 * device aborts the test.
 */
static struct {
	int vol, prepared, running, xrun, reported;
	long queued, last_tick, frames, writes, eagain, epipe;
	unsigned long period, bufsize, start;
	int peak;
	double sumsq;
	FILE *dump;
} fk;

static int fk_is(const char *m)
{
	const char *e = getenv("FAKE_ALSA_MODE");
	return !strcmp(e ? e : "ok", m);
}

static int fk_err(int e)
{
	errno = e;
	return -1;
}

static void fk_report(void)
{
	if (fk.reported) return;
	fk.reported = 1;
	fprintf(stderr, "fake: frames=%ld writes=%ld eagain=%ld epipe=%ld peak=%d (%.1f dBFS) rms=%.1f dBFS vol=%d\n",
		fk.frames, fk.writes, fk.eagain, fk.epipe, fk.peak,
		fk.peak ? 20.0 * log10((double)fk.peak / 32768.0) : -999.0,
		fk.frames ? 10.0 * log10(fk.sumsq / (double)fk.frames / (32768.0 * 32768.0) + 1e-30) : -999.0,
		fk.vol);
	if (fk.dump) fclose(fk.dump);
	fk.dump = NULL;
}

static int fake_open(const char *path, int flags)
{
	char ctl[200], pcm[200];
	static int once;
	if (!once) {
		const char *d = getenv("FAKE_ALSA_DUMP");
		once = 1;
		atexit(fk_report);
		if (d) fk.dump = fopen(d, "wb");
	}
	snprintf(ctl, sizeof ctl, "%s/dev/snd/controlC%d", snd_root, snd_card);
	snprintf(pcm, sizeof pcm, "%s/dev/snd/pcmC%dD0p", snd_root, snd_card);
	fprintf(stderr, "fake: open %s\n", path);
	if (strcmp(path, ctl) && strcmp(path, pcm)) {
		fprintf(stderr, "fake: FORBIDDEN open of %s\n", path);
		abort();
	}
	return open(path, flags);
}

static int fake_ioctl(int fd, unsigned long req, void *arg)
{
	(void)fd;
	if (req == SNDRV_CTL_IOCTL_CARD_INFO) {
		struct snd_ctl_card_info *ci = arg;
		int wrong = fk_is("wrongcard");
		memset(ci, 0, sizeof *ci);
		ci->card = snd_card;
		snprintf((char *)ci->id, sizeof ci->id, "%s", wrong ? "Headset" : "Speaker");
		snprintf((char *)ci->name, sizeof ci->name, "%s", wrong ? "iPhone Headset" : "iPhone Speaker");
		return 0;
	}
	if (req == SNDRV_CTL_IOCTL_ELEM_INFO) {
		struct snd_ctl_elem_info *in = arg;
		if (in->id.numid || in->id.iface != SNDRV_CTL_ELEM_IFACE_MIXER ||
		    strcmp((const char *)in->id.name, "Speaker Volume"))
			return fk_err(ENOENT);
		in->id.numid = 7;
		in->type = SNDRV_CTL_ELEM_TYPE_INTEGER;
		in->count = 1;
		in->access = SNDRV_CTL_ELEM_ACCESS_READWRITE | SNDRV_CTL_ELEM_ACCESS_TLV_READ;
		in->value.integer.min = 0;
		in->value.integer.max = 204;
		return 0;
	}
	if (req == SNDRV_CTL_IOCTL_TLV_READ) {
		struct snd_ctl_tlv *t = arg;
		if (fk_is("notlv")) return fk_err(ENXIO);
		if (t->numid != 7 || t->length < 4 * sizeof(unsigned)) return fk_err(EINVAL);
		t->tlv[0] = SNDRV_CTL_TLVT_DB_SCALE;
		t->tlv[1] = 2 * sizeof(unsigned);
		t->tlv[2] = (unsigned)(fk_is("loud") ? 0 : -10200);
		t->tlv[3] = 50;
		return 0;
	}
	if (req == SNDRV_CTL_IOCTL_ELEM_WRITE) {
		struct snd_ctl_elem_value *v = arg;
		if (v->id.numid != 7 || v->value.integer.value[0] < 0 || v->value.integer.value[0] > 204)
			return fk_err(EINVAL);
		fk.vol = (int)v->value.integer.value[0];
		fprintf(stderr, "fake: Speaker Volume := %d (%.1f dB)\n", fk.vol, -102.0 + 0.5 * fk.vol);
		if (fk.vol > 192) { fprintf(stderr, "fake: VOLUME ABOVE -6 dB\n"); abort(); }
		return 1;
	}
	if (req == SNDRV_CTL_IOCTL_ELEM_READ) {
		struct snd_ctl_elem_value *v = arg;
		if (v->id.numid != 7) return fk_err(EINVAL);
		v->value.integer.value[0] = fk.vol + (fk_is("readback") ? 1 : 0);
		return 0;
	}
	if (req == SNDRV_PCM_IOCTL_INFO) {
		struct snd_pcm_info *pi = arg;
		memset(pi, 0, sizeof *pi);
		pi->card = snd_card;
		pi->stream = SNDRV_PCM_STREAM_PLAYBACK;
		return 0;
	}
	if (req == SNDRV_PCM_IOCTL_HW_PARAMS) {
		struct snd_pcm_hw_params *p = arg;
		unsigned per = hw_iv(p, SNDRV_PCM_HW_PARAM_PERIOD_SIZE)->min;
		unsigned n = hw_iv(p, SNDRV_PCM_HW_PARAM_PERIODS)->min;
		if (hw_mask(p, SNDRV_PCM_HW_PARAM_ACCESS)->bits[0] != 1u << SNDRV_PCM_ACCESS_RW_INTERLEAVED ||
		    hw_mask(p, SNDRV_PCM_HW_PARAM_FORMAT)->bits[0] != 1u << SNDRV_PCM_FORMAT_S16_LE ||
		    hw_iv(p, SNDRV_PCM_HW_PARAM_CHANNELS)->min != 1 || hw_iv(p, SNDRV_PCM_HW_PARAM_CHANNELS)->max != 1 ||
		    hw_iv(p, SNDRV_PCM_HW_PARAM_RATE)->min != 48000 || hw_iv(p, SNDRV_PCM_HW_PARAM_RATE)->max != 48000 ||
		    hw_iv(p, SNDRV_PCM_HW_PARAM_PERIOD_SIZE)->max != per || hw_iv(p, SNDRV_PCM_HW_PARAM_PERIODS)->max != n) {
			fprintf(stderr, "fake: malformed HW_PARAMS\n");
			abort();
		}
		if (fk_is("hwfail") || (fk_is("hwpick") && per == 512)) return fk_err(EINVAL);
		if (per * 2 < 512 || per * 2 > 32768 || n < 2 || n > 16) return fk_err(EINVAL);
		hw_iv(p, SNDRV_PCM_HW_PARAM_BUFFER_SIZE)->min = hw_iv(p, SNDRV_PCM_HW_PARAM_BUFFER_SIZE)->max = per * n;
		fk.period = per;
		fk.bufsize = (unsigned long)per * n;
		fprintf(stderr, "fake: HW_PARAMS %u x %u\n", per, n);
		return 0;
	}
	if (req == SNDRV_PCM_IOCTL_SW_PARAMS) {
		struct snd_pcm_sw_params *sw = arg;
		if (!fk.bufsize || sw->avail_min == 0 || sw->start_threshold > fk.bufsize ||
		    sw->stop_threshold != fk.bufsize || sw->silence_size || sw->silence_threshold)
			return fk_err(EINVAL);
		fk.start = sw->start_threshold;
		return 0;
	}
	if (req == SNDRV_PCM_IOCTL_PREPARE) {
		if (!fk.bufsize) return fk_err(EBADFD);
		fk.prepared = 1;
		fk.running = fk.xrun = 0;
		fk.queued = 0;
		fk.last_tick = tick;
		return 0;
	}
	if (req == SNDRV_PCM_IOCTL_RESUME) return fk_err(ENOSYS);
	if (req == SNDRV_PCM_IOCTL_WRITEI_FRAMES) {
		struct snd_xferi *x = arg;
		const int16_t *b = x->buf;
		long played, room, n;
		if (fk_is("dead")) return fk_err(EIO);
		if (!fk.prepared) return fk_err(EBADFD);
		if (!b || x->frames < 1 || x->frames > SND_CHUNK) { fprintf(stderr, "fake: bad xferi\n"); abort(); }
		played = fk.running ? (tick - fk.last_tick) * (SR / HZ) * (fk_is("xrun") && tick % 300 < 30 ? 3 : 1) : 0;
		fk.last_tick = tick;
		fk.queued -= played;
		if (fk.queued < 0) { fk.queued = 0; fk.xrun = 1; }
		if (fk.xrun) { fk.epipe++; return fk_err(EPIPE); }
		room = (long)fk.bufsize - fk.queued;
		if (room <= 0) { fk.eagain++; return fk_err(EAGAIN); }
		n = (long)x->frames < room ? (long)x->frames : room;
		for (long i = 0; i < n; i++) {
			int a = b[i] < 0 ? -b[i] : b[i];
			if (a > fk.peak) fk.peak = a;
			fk.sumsq += (double)b[i] * (double)b[i];
		}
		if (fk.dump) fwrite(b, sizeof *b, (size_t)n, fk.dump);
		fk.queued += n;
		fk.frames += n;
		fk.writes++;
		if (fk.queued >= (long)fk.start) fk.running = 1;
		x->result = n;
		return 0;
	}
	fprintf(stderr, "fake: unexpected ioctl %#lx\n", req);
	return fk_err(ENOTTY);
}
