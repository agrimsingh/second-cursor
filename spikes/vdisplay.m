#import <Foundation/Foundation.h>
#import <CoreGraphics/CoreGraphics.h>

@interface CGVirtualDisplayDescriptor : NSObject
@property (retain, nonatomic) dispatch_queue_t queue;
@property (retain, nonatomic) NSString *name;
@property (nonatomic) unsigned int maxPixelsHigh;
@property (nonatomic) unsigned int maxPixelsWide;
@property (nonatomic) CGSize sizeInMillimeters;
@property (nonatomic) unsigned int serialNum;
@property (nonatomic) unsigned int productID;
@property (nonatomic) unsigned int vendorID;
@property (copy, nonatomic) void (^terminationHandler)(id, id);
@end

@interface CGVirtualDisplayMode : NSObject
- (instancetype)initWithWidth:(unsigned int)width height:(unsigned int)height refreshRate:(double)refreshRate;
@end

@interface CGVirtualDisplaySettings : NSObject
@property (retain, nonatomic) NSArray *modes;
@property (nonatomic) unsigned int hiDPI;
@end

@interface CGVirtualDisplay : NSObject
@property (readonly, nonatomic) unsigned int displayID;
- (instancetype)initWithDescriptor:(CGVirtualDisplayDescriptor *)descriptor;
- (BOOL)applySettings:(CGVirtualDisplaySettings *)settings;
@end

static void printDisplays(void) {
    CGDirectDisplayID ids[16];
    uint32_t count = 0;
    CGGetActiveDisplayList(16, ids, &count);
    for (uint32_t i = 0; i < count; i++) {
        CGRect b = CGDisplayBounds(ids[i]);
        printf("display %u main=%d bounds=(%.0f,%.0f %.0fx%.0f)\n", ids[i], CGDisplayIsMain(ids[i]), b.origin.x, b.origin.y, b.size.width, b.size.height);
    }
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        unsigned int width = 1440, height = 900;

        CGVirtualDisplayDescriptor *descriptor = [[CGVirtualDisplayDescriptor alloc] init];
        descriptor.queue = dispatch_get_main_queue();
        descriptor.name = @"Agent Display";
        descriptor.maxPixelsWide = width * 2;
        descriptor.maxPixelsHigh = height * 2;
        descriptor.sizeInMillimeters = CGSizeMake(width * 25.4 / 110.0, height * 25.4 / 110.0);
        descriptor.vendorID = 0xA6E7;
        descriptor.productID = 0x0001;
        descriptor.serialNum = 0x0001;
        descriptor.terminationHandler = ^(id a, id b) { printf("virtual display terminated\n"); };

        CGVirtualDisplay *display = [[CGVirtualDisplay alloc] initWithDescriptor:descriptor];
        if (!display) { printf("failed to create virtual display\n"); return 1; }

        CGVirtualDisplaySettings *settings = [[CGVirtualDisplaySettings alloc] init];
        settings.hiDPI = 1;
        settings.modes = @[ [[CGVirtualDisplayMode alloc] initWithWidth:width height:height refreshRate:60] ];
        if (![display applySettings:settings]) { printf("applySettings failed\n"); return 1; }

        CGDirectDisplayID vid = display.displayID;
        printf("created virtual display id=%u\n", vid);
        usleep(1500000);

        // Only a corner touches the main display, so the user's cursor almost never wanders into it.
        CGRect main = CGDisplayBounds(CGMainDisplayID());
        CGDisplayConfigRef config;
        CGBeginDisplayConfiguration(&config);
        CGConfigureDisplayOrigin(config, vid, (int32_t)CGRectGetMaxX(main), (int32_t)CGRectGetMaxY(main));
        CGError err = CGCompleteDisplayConfiguration(config, kCGConfigureForSession);
        printf("placed diagonally (err=%d)\n", err);
        usleep(1000000);
        printDisplays();
        fflush(stdout);

        signal(SIGTERM, SIG_DFL);
        [[NSRunLoop mainRunLoop] run];
        (void)display;
    }
    return 0;
}
