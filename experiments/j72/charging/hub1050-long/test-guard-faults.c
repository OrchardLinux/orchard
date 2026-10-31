#include <assert.h>
#include <stdio.h>
#include "module/j72-limit-core.h"
int main(void) {
 assert(j72_limit_pack_faults(1,90,357,4200000,373000)==0);
 assert(j72_limit_pack_faults(1,90,357,4201000,373000)==8);
 assert(j72_limit_pack_faults(1,90,357,4199000,1500001)==16);
 assert(j72_limit_pack_faults(1,96,381,4201000,1500001)==30);
 assert(j72_limit_pack_faults(0,9,199,3499999,-1500001)==31);
 assert(j72_limit_pack_guard(1,90,357,4201000,373000)==-ERANGE);
 assert(j72_limit_pack_guard(1,10,200,3500000,-1500000)==0);
 assert(j72_limit_pack_guard(1,95,380,4200000,1500000)==0);
 puts("PASS: exact voltage boundary and independent/multiple guard faults (8 cases)");
}
