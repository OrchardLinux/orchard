/*
 * hedgewick_art.h -- HEDGEWICK's pixel art, original, drawn for this game.
 * Used by hedgewick.c and by the picker's preview (arcade.c). One letter per
 * pixel, see the palette in arcade_common.h; '.' is transparent.
 *
 * Wick, the lantern-bearer: a small figure in a teal hooded cloak carrying a
 * brass lantern. Four night creatures: a violet moth, a lime toad, a crimson
 * bat and a tawny owl. Embers to gather, glow-blooms that dazzle.
 */
#ifndef HEDGEWICK_ART_H
#define HEDGEWICK_ART_H

#define SPR 11

/* [direction: 0 up, 1 right, 2 down][frame]; left is right mirrored */
static const char *const art_wick[3][2][SPR] = {
	{ {
		"....tttt...",
		"...tTTTTt..",
		"..tTTTTTTt.",
		"..tTTTTTTt.",
		"..tTTtTTTt.",
		".Y.tTTTTt..",
		"YyYtTTTTt..",
		"YOYTTTTTTt.",
		".Y.tTTTTTt.",
		"...tTTTTt..",
		"...KK..KK..",
	}, {
		"....tttt...",
		"...tTTTTt..",
		"..tTTTTTTt.",
		"..tTTTTTTt.",
		"..tTTtTTTt.",
		".Y.tTTTTt..",
		"YyYtTTTTt..",
		"YOYTTTTTTt.",
		".Y.tTTTTTt.",
		"...tTTTTt..",
		"..KK....KK.",
	} }, { {
		"...tttt....",
		"..tTTTTt...",
		".tTTTTTTt..",
		".tTTTZZZt..",
		".tTTZZZKt..",
		"..tTZZZt.Y.",
		"..ttTTTtYyY",
		".tTTTTTTYOY",
		".tTTTTTt.Y.",
		"..tTTTTt...",
		"..KK..KK...",
	}, {
		"...tttt....",
		"..tTTTTt...",
		".tTTTTTTt..",
		".tTTTZZZt..",
		".tTTZZZKt..",
		"..tTZZZt.Y.",
		"..ttTTTtYyY",
		".tTTTTTTYOY",
		".tTTTTTt.Y.",
		"..tTTTTt...",
		".KK....KK..",
	} }, { {
		"...tttt....",
		"..tTTTTt...",
		".tTTTTTTt..",
		".tTZZZZTt..",
		".tZKZZKZt..",
		"..tZZZZt.Y.",
		"..tTTTTtYyY",
		".tTTTTTTYOY",
		".tTTTTTt.Y.",
		"..tTTTTt...",
		"..KK..KK...",
	}, {
		"...tttt....",
		"..tTTTTt...",
		".tTTTTTTt..",
		".tTZZZZTt..",
		".tZKZZKZt..",
		"..tZZZZt.Y.",
		"..tTTTTtYyY",
		".tTTTTTTYOY",
		".tTTTTTt.Y.",
		"..tTTTTt...",
		"...KK.KK...",
	} },
};

enum { K_MOTH, K_TOAD, K_BAT, K_OWL };

static const char *const art_crit[4][2][SPR] = {
	{ {     /* moth: wings open */
		"..v.....v..",
		"...v...v...",
		"V...vvv...V",
		"VV.vWvWv.VV",
		"VVVvvvvvVVV",
		"VVVVvvvVVVV",
		"VwVVvvvVVwV",
		".VVV.v.VVV.",
		"..VV.v.VV..",
		"...V...V...",
		"...........",
	}, {    /* wings raised */
		"..v.....v..",
		"...v...v...",
		"..V.vvv.V..",
		"..VvWvWvV..",
		".VVvvvvvVV.",
		".VVVvvvVVV.",
		".VwVvvvVwV.",
		"..VV.v.VV..",
		"..V..v..V..",
		"...........",
		"...........",
	} }, { {        /* toad: sitting */
		"...........",
		"..FF...FF..",
		".FWKF.FKWF.",
		".FFFFFFFFF.",
		"FFFFfffFFFF",
		"FfFFFFFFFfF",
		"FFFwwwwwFFF",
		".FFwwwwwFF.",
		"..FFFFFFF..",
		".ff.f.f.ff.",
		"...........",
	}, {    /* leaping */
		"..FF...FF..",
		".FWKF.FKWF.",
		".FFFFFFFFF.",
		"FFFFfffFFFF",
		"FfFFFFFFFfF",
		"FFFwwwwwFFF",
		".FFwwwwwFF.",
		"..FFFFFFF..",
		"..f.f.f.f..",
		".f.......f.",
		"f.........f",
	} }, { {        /* bat: wings up */
		"R.........R",
		"RR..r.r..RR",
		"RRR.rrr.RRR",
		"RRRrWrWrRRR",
		"RRRrrrrrRRR",
		".RRRrrrRRR.",
		"..R.rrr.R..",
		".....r.....",
		"...........",
		"...........",
		"...........",
	}, {    /* wings down */
		"...........",
		"....r.r....",
		"....rrr....",
		"...rWrWr...",
		"..RrrrrrR..",
		".RRRrrrRRR.",
		"RRRRrrrRRRR",
		"RR.R.r.R.RR",
		"R...R.R...R",
		"...........",
		"...........",
	} }, { {        /* owl */
		"..a.....a..",
		"..aa...aa..",
		"..aAAAAAa..",
		".aAYYAYYAa.",
		".aYKYAYKYa.",
		".aAYYOYYAa.",
		".aAAAOAAAa.",
		".aAwAwAwAa.",
		".aAAwAwAAa.",
		"..aAAAAAa..",
		"...O...O...",
	}, {    /* blinking, wings a little out */
		"..a.....a..",
		"..aa...aa..",
		"..aAAAAAa..",
		".aAAAAAAAa.",
		".aaaaAaaaa.",
		".aAAAOAAAa.",
		".aAAAOAAAa.",
		"aaAwAwAwAaa",
		".aAAwAwAAa.",
		"..aAAAAAa..",
		"...O...O...",
	} },
};

static const char *const art_poof[3][SPR] = { {
		"...........",
		"...........",
		"...........",
		"....y.y....",
		"...yCWCy...",
		"....WWW....",
		"...yCWCy...",
		"....y.y....",
		"...........",
		"...........",
		"...........",
	}, {
		"...........",
		"...y...y...",
		"....C.C....",
		".y.......y.",
		"..C.....C..",
		".....W.....",
		"..C.....C..",
		".y.......y.",
		"....C.C....",
		"...y...y...",
		"...........",
	}, {
		"y.........y",
		"...........",
		"..y.....y..",
		"...........",
		"...........",
		"y....c....y",
		"...........",
		"...........",
		"..y.....y..",
		"...........",
		"y.........y",
} };

static const char *const art_bloom[2][7] = { {
		".B...B.",
		"BWB.BWB",
		".BWYWB.",
		"..YOY..",
		".BWYWB.",
		"BWB.BWB",
		".B...B.",
	}, {
		".......",
		"..B.B..",
		".BWYWB.",
		"..YOY..",
		".BWYWB.",
		"..B.B..",
		".......",
} };

static const char *const art_lamp[6] = {
	"..Y..",
	".YyY.",
	"YyOyY",
	"YyOyY",
	".YyY.",
	"..Y..",
};

static const char *const crit_name[4] = { "MOTH", "TOAD", "BAT", "OWL" };
static const char crit_col[4] = { 'V', 'F', 'R', 'A' };

#endif
