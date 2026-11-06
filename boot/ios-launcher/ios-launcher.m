// SPDX-License-Identifier: GPL-2.0-only
#import <UIKit/UIKit.h>
#include <spawn.h>
#include "ios-start-config.h"
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>

@interface OrchardLauncher : UIResponder <UIApplicationDelegate>
@property(strong, nonatomic) UIWindow *window;
@property(strong, nonatomic) UILabel *message;
@property(strong, nonatomic) UIButton *start;
@end

@implementation OrchardLauncher
- (BOOL)application:(UIApplication *)application didFinishLaunchingWithOptions:(NSDictionary *)options
{
    (void)application;
    (void)options;
    self.window = [[UIWindow alloc] initWithFrame:[UIScreen mainScreen].bounds];
    UIViewController *controller = [UIViewController new];
    self.window.rootViewController = controller;
    UIView *view = controller.view;
    view.backgroundColor = [UIColor colorWithRed:0.06 green:0.09 blue:0.12 alpha:1];
    self.message = [[UILabel alloc] initWithFrame:CGRectZero];
    self.message.text = LAUNCHER_TITLE;
    self.message.textColor = [UIColor whiteColor];
    self.message.font = [UIFont systemFontOfSize:26];
    self.message.textAlignment = NSTextAlignmentCenter;
    self.message.numberOfLines = 0;
    self.message.translatesAutoresizingMaskIntoConstraints = NO;
    [view addSubview:self.message];
    self.start = [UIButton buttonWithType:UIButtonTypeSystem];
    [self.start setTitle:LAUNCHER_START_TITLE forState:UIControlStateNormal];
    self.start.titleLabel.font = [UIFont systemFontOfSize:22];
    self.start.translatesAutoresizingMaskIntoConstraints = NO;
    [self.start addTarget:self action:@selector(startLinux) forControlEvents:UIControlEventTouchUpInside];
    [view addSubview:self.start];
    [NSClassFromString(@"NSLayoutConstraint") activateConstraints:@[
        [self.message.centerXAnchor constraintEqualToAnchor:view.centerXAnchor],
        [self.message.centerYAnchor constraintEqualToAnchor:view.centerYAnchor constant:-60],
        [self.message.widthAnchor constraintEqualToAnchor:view.widthAnchor multiplier:0.85],
        [self.start.centerXAnchor constraintEqualToAnchor:view.centerXAnchor],
        [self.start.topAnchor constraintEqualToAnchor:self.message.bottomAnchor constant:30],
        [self.start.heightAnchor constraintEqualToConstant:60]
    ]];
    [self.window makeKeyAndVisible];
    return YES;
}
- (void)startLinux
{
    self.start.enabled = NO;
    self.message.text = LAUNCHER_STARTING_TITLE;
    dispatch_async(dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_DEFAULT, 0), ^{
        pid_t child;
        char *const argv[] = {HELPER_PATH, NULL};
        char *const environment[] = {"PATH=/usr/bin:/bin:/usr/sbin:/sbin", NULL};
        int status = 0;
        int error = posix_spawn(&child, argv[0], NULL, NULL, argv, environment);
        if (!error && waitpid(child, &status, 0) != child)
            error = 1;
        NSString *message = @"Startup stopped. Restart the iPad before trying again.";
        if (!error && WIFEXITED(status) && WEXITSTATUS(status) == 11)
            message = @"Enable the jailbreak, then open this app again.";
        if (!error && WIFEXITED(status) && WEXITSTATUS(status) == 12)
            message = @"The installed boot files need repair.";
        if (!error && WIFEXITED(status) && WEXITSTATUS(status) == 14)
            message = @"The startup wake timer could not be prepared. Restart the iPad before trying again.";
        if (!error && WIFEXITED(status) && WEXITSTATUS(status) == 16)
            message = @"The startup wake timer could not be cleared. Restart the iPad before trying again.";
        dispatch_async(dispatch_get_main_queue(), ^{ self.message.text = message; });
    });
}
@end

int main(int argc, char **argv)
{
    @autoreleasepool {
        if (argc == 2 && !strcmp(argv[1], "--check-runtime")) {
            NSObject *object = [[NSObject alloc] init];
            if (!object)
                return 20;
            NSString *required[] = {@"NSArray", @"UIButton", @"UIColor", @"UIFont",
                @"UILabel", @"UIResponder", @"UIScreen", @"UIViewController", @"UIWindow",
                @"NSLayoutConstraint", @"NSLayoutXAxisAnchor", @"NSLayoutYAxisAnchor",
                @"NSLayoutDimension", @"OrchardLauncher"};
            for (unsigned int index = 0; index < sizeof(required) / sizeof(required[0]); index++) {
                if (!NSClassFromString(required[index]))
                    return 21;
            }
            puts("Orchard launcher runtime verified");
            return 0;
        }
        return UIApplicationMain(argc, argv, nil, NSStringFromClass([OrchardLauncher class]));
    }
}
