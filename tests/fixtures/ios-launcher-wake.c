/* SPDX-License-Identifier: GPL-2.0-only */
/* Exercise the production wake functions and main with CoreFoundation values.
 * Power scheduling, child execution and privileged identity/lease checks use
 * local fakes. The fixture never loads IOKit or starts a kernel loader.
 * test_p105_ios_wake.py extracts the production source into the included file.
 */
#include <CoreFoundation/CoreFoundation.h>
#include <assert.h>
#include <dlfcn.h>
#include <errno.h>
#include <mach/mach.h>
#include <math.h>
#include <signal.h>
#include <spawn.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
static int schedule_calls,cancel_calls,copy_calls,spawn_calls,wait_calls,lease_calls,api_calls;
static int schedule_error,cancel_error,spawn_error,wait_error,eintr_left,queue_case,child_status;
static int hash_error,lease_error,open_error,missing_api,lease_open,owned,foreign;
static int wrong_model, wrong_build, bad_length;
static double clock_now;
static CFDateRef saved_date;
static CFStringRef saved_owner,saved_type;
static void reset(void) {
 if(saved_date) CFRelease(saved_date);
 if(saved_owner) CFRelease(saved_owner);
 if(saved_type) CFRelease(saved_type);
 saved_date=NULL;saved_owner=NULL;saved_type=NULL;
 schedule_calls=cancel_calls=copy_calls=spawn_calls=wait_calls=lease_calls=api_calls=0;
 schedule_error=cancel_error=spawn_error=wait_error=eintr_left=queue_case=child_status=0;
 hash_error=lease_error=open_error=missing_api=lease_open=owned=0;foreign=1;clock_now=1000.75;wrong_model=wrong_build=bad_length=0;
}
static kern_return_t fake_schedule(CFDateRef date,CFStringRef owner,CFStringRef type) {
 assert(lease_open && !schedule_calls && !cancel_calls);
 assert(CFDateGetAbsoluteTime(date)==1060.0);
 assert(CFEqual(owner,CFSTR("org.orchardlinux.p105.start.4242")));
 assert(CFEqual(type,CFSTR("wake")));
 saved_date=CFRetain(date);saved_owner=CFRetain(owner);saved_type=CFRetain(type);
 owned=1;schedule_calls++;return schedule_error;
}
static kern_return_t fake_cancel(CFDateRef date,CFStringRef owner,CFStringRef type) {
 assert(lease_open && schedule_calls==1 && !cancel_calls);
 assert(date==saved_date && owner==saved_owner && type==saved_type);
 assert(foreign==1);cancel_calls++;
 if(!cancel_error) owned=0;
 return cancel_error;
}
static CFArrayRef fake_copy(void) {
 assert(lease_open && schedule_calls==1 && !cancel_calls);copy_calls++;
 if(queue_case==5) return NULL;
 if(queue_case==6) return (CFArrayRef)CFRetain(CFSTR("invalid queue"));
 CFMutableArrayRef array=CFArrayCreateMutable(NULL,0,&kCFTypeArrayCallBacks);
 const void *keys[]={CFSTR("time"),CFSTR("scheduledby"),CFSTR("eventtype")};
 CFDateRef other_date=CFDateCreate(NULL,1061.0);
 const void *other[]={other_date,CFSTR("some.other.application"),CFSTR("wake")};
 CFDictionaryRef other_event=CFDictionaryCreate(NULL,keys,other,3,&kCFTypeDictionaryKeyCallBacks,&kCFTypeDictionaryValueCallBacks);
 CFArrayAppendValue(array,other_event);
 if(queue_case!=1) {
  const void *values[]={queue_case==3?other_date:saved_date,queue_case==2?CFSTR("wrong owner"):saved_owner,queue_case==4?CFSTR("sleep"):saved_type};
  CFDictionaryRef event=CFDictionaryCreate(NULL,keys,values,3,&kCFTypeDictionaryKeyCallBacks,&kCFTypeDictionaryValueCallBacks);
  CFArrayAppendValue(array,event);CFRelease(event);
 }
 if(queue_case==7) for(int i=0;i<4096;i++) CFArrayAppendValue(array,other_event);
 CFRelease(other_event);CFRelease(other_date);return array;
}
static void *fake_dlopen(const char *path,int flags) {
 assert(!strcmp(path,"/System/Library/Frameworks/IOKit.framework/IOKit"));
 assert(flags==(RTLD_NOW|RTLD_LOCAL));api_calls++;
 return open_error?NULL:(void *)(uintptr_t)1;
}
static void *fake_dlsym(void *handle,const char *name) {
 assert(handle==(void *)(uintptr_t)1);
 if(!strcmp(name,"IOPMSchedulePowerEvent")) return missing_api==1?NULL:(void *)fake_schedule;
 if(!strcmp(name,"IOPMCancelScheduledPowerEvent")) return missing_api==2?NULL:(void *)fake_cancel;
 if(!strcmp(name,"IOPMCopyScheduledPowerEvents")) return missing_api==3?NULL:(void *)fake_copy;
 assert(0);return NULL;
}
static int fake_dlclose(void *handle) {assert(handle==(void *)(uintptr_t)1);return 0;}
static int fake_spawn(pid_t *pid,const char *path,const posix_spawn_file_actions_t *actions,const posix_spawnattr_t *attrs,char *const argv[],char *const env[]) {
 assert(lease_open && owned && copy_calls==1 && !cancel_calls);
 assert(!strcmp(path,"/usr/local/lib/orchard-p105/kloader"));
 assert(!strcmp(argv[0],path) && !strcmp(argv[1],"/usr/local/lib/orchard-p105/ibec-local.raw") && !argv[2]);
 assert(!actions && !attrs && !strcmp(env[1],"HOME=/var/root"));
 spawn_calls++;*pid=567;return spawn_error;
}
static pid_t fake_wait(pid_t pid,int *status,int flags) {
 assert(pid==567 && flags==0 && lease_open && owned && !cancel_calls);wait_calls++;
 if(eintr_left){eintr_left--;errno=EINTR;return -1;}
 if(wait_error){errno=ECHILD;return -1;}
 *status=child_status;return pid;
}
static int fake_close(int fd) {assert(fd==99 && lease_open);lease_open=0;return 0;}
static int fake_sysctl(const char *name,void *value,size_t *length,void *new_value,size_t new_length) {
 assert(!new_value && !new_length);
 if(bad_length){*length=bad_length==1?0:33;return 0;}
 const char *result;
 if(!strcmp(name,"hw.machine")) result=wrong_model?"other":"iPad2,5";
 else {assert(!strcmp(name,"kern.osversion"));result=wrong_build?"other":"13G36";}
 assert(*length>strlen(result));memcpy(value,result,strlen(result)+1);*length=strlen(result)+1;return 0;
}
static uid_t fake_uid(void){return 0;}
static int fake_setgid(gid_t value){assert(value==0);return 0;}
static int fake_setuid(uid_t value){assert(value==0);return 0;}
static kern_return_t fake_task(mach_port_t task,int pid,mach_port_t *port){(void)task;assert(pid==0);*port=1;return 0;}
static kern_return_t fake_deallocate(mach_port_t task,mach_port_t port){(void)task;assert(port==1);return 0;}
static int checked(const char *path,size_t length,const char *hash){(void)path;(void)length;(void)hash;assert(!lease_calls && !api_calls);return hash_error;}
static int boot_lease(void){assert(!api_calls && !schedule_calls);lease_calls++;if(lease_error)return -1;lease_open=1;return 99;}
static CFAbsoluteTime fake_now(void){return clock_now;}
static pid_t fake_pid(void){return 4242;}
#define TARGET_MACHINE "iPad2,5"
#define TARGET_IOS_BUILD "13G36"
#define ROOT_BOOT_PATH "/boot/omarchy-p105.bin"
#define IBEC_PATH "/usr/local/lib/orchard-p105/ibec-local.raw"
#define KLOADER_PATH "/usr/local/lib/orchard-p105/kloader"
#define WAKE_OWNER "org.orchardlinux.p105.start"
#define WAKE_DELAY_SECONDS 60
#define ROOT_BOOT_BYTES 1
#define ROOT_BOOT_SHA256 "stub"
#define IBEC_BYTES 1
#define IBEC_SHA256 "stub"
#define KLOADER_BYTES 1
#define KLOADER_SHA256 "stub"
#define dlopen fake_dlopen
#define dlsym fake_dlsym
#define dlclose fake_dlclose
#define posix_spawn fake_spawn
#define waitpid fake_wait
#define close fake_close
#define sysctlbyname fake_sysctl
#define geteuid fake_uid
#define setgid fake_setgid
#define setuid fake_setuid
#define task_for_pid fake_task
#define mach_port_deallocate fake_deallocate
#define CFAbsoluteTimeGetCurrent fake_now
#define getpid fake_pid
#define main inspected_main

#include "p105-ios-wake-source.inc"

#undef main
static int run(int inspect){char *argv[]={"helper","--check-installed",NULL};return inspected_main(inspect?2:1,argv);}
static unsigned cases;
static void expect(int inspect,int result,int schedules,int cancels,int spawns) {
 assert(run(inspect)==result);
 assert(schedule_calls==schedules && cancel_calls==cancels && spawn_calls==spawns);
 assert(foreign==1 && !lease_open);cases++;
}
int main(void) {
 reset();bad_length=1;expect(0,10,0,0,0);assert(!lease_calls && !api_calls);
 reset();bad_length=2;expect(0,10,0,0,0);assert(!lease_calls && !api_calls);
 reset();wrong_model=1;expect(0,10,0,0,0);assert(!lease_calls && !api_calls);
 reset();wrong_build=1;expect(0,10,0,0,0);assert(!lease_calls && !api_calls);
 reset();expect(1,0,0,0,0);assert(!lease_calls && api_calls==1);
 for(int i=1;i<=3;i++){reset();missing_api=i;expect(1,14,0,0,0);assert(!lease_calls);}
 reset();hash_error=1;expect(0,12,0,0,0);assert(!lease_calls && !api_calls);
 reset();lease_error=1;expect(0,13,0,0,0);assert(!api_calls);
 reset();open_error=1;expect(0,14,0,0,0);
 for(int i=1;i<=3;i++){reset();missing_api=i;expect(0,14,0,0,0);}
 reset();clock_now=0;expect(0,14,0,0,0);
 reset();schedule_error=1;expect(0,14,1,1,0);assert(!owned);
 reset();schedule_error=1;cancel_error=1;expect(0,16,1,1,0);assert(owned);
 for(int i=1;i<=7;i++){reset();queue_case=i;expect(0,14,1,1,0);assert(!owned);}
 reset();spawn_error=EACCES;expect(0,15,1,1,1);assert(!wait_calls && !owned);
 reset();child_status=37<<8;expect(0,37,1,1,1);assert(!owned);
 reset();expect(0,15,1,1,1);assert(!owned);
 reset();child_status=SIGTERM;expect(0,128+SIGTERM,1,1,1);assert(!owned);
 reset();eintr_left=3;child_status=7<<8;expect(0,7,1,1,1);assert(wait_calls==4 && !owned);
 reset();wait_error=1;expect(0,15,1,1,1);assert(!owned);
 reset();child_status=37<<8;cancel_error=1;expect(0,16,1,1,1);assert(owned);
 reset();printf("owned_wake_cases=%u\n",cases);return 0;
}
