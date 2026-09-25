#import "SCDisplayPrivate.h"

// Private CoreGraphics classes (macOS 11+). Classes are looked up at runtime so a macOS
// release that drops them makes init return nil instead of failing to load.

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

@implementation SCVirtualDisplay {
    CGVirtualDisplay *_display;
    dispatch_queue_t _queue;
}

- (nullable instancetype)initWithName:(NSString *)name
                                width:(unsigned int)width
                               height:(unsigned int)height
                                hiDPI:(BOOL)hiDPI {
    Class descriptorClass = NSClassFromString(@"CGVirtualDisplayDescriptor");
    Class modeClass = NSClassFromString(@"CGVirtualDisplayMode");
    Class settingsClass = NSClassFromString(@"CGVirtualDisplaySettings");
    Class displayClass = NSClassFromString(@"CGVirtualDisplay");
    if (!descriptorClass || !modeClass || !settingsClass || !displayClass || width == 0 || height == 0) {
        return nil;
    }
    if (!(self = [super init])) {
        return nil;
    }

    unsigned int scale = hiDPI ? 2 : 1;
    _queue = dispatch_queue_create("dev.secondcursor.virtual-display", DISPATCH_QUEUE_SERIAL);

    CGVirtualDisplayDescriptor *descriptor = [[descriptorClass alloc] init];
    descriptor.queue = _queue;
    descriptor.name = name;
    descriptor.maxPixelsWide = width * scale;
    descriptor.maxPixelsHigh = height * scale;
    descriptor.sizeInMillimeters = CGSizeMake(width * 25.4 / 110.0, height * 25.4 / 110.0);
    descriptor.vendorID = 0xA6E7;
    descriptor.productID = 0x0001;
    descriptor.serialNum = 0x0001;
    __weak SCVirtualDisplay *weakSelf = self;
    descriptor.terminationHandler = ^(id display, id reason) {
        SCVirtualDisplay *strongSelf = weakSelf;
        if (strongSelf) {
            strongSelf->_terminated = YES;
        }
    };

    CGVirtualDisplay *display = [[displayClass alloc] initWithDescriptor:descriptor];
    if (!display || display.displayID == 0) {
        return nil;
    }

    CGVirtualDisplaySettings *settings = [[settingsClass alloc] init];
    settings.hiDPI = hiDPI ? 1 : 0;
    settings.modes = @[ [[modeClass alloc] initWithWidth:width height:height refreshRate:60] ];
    if (![display applySettings:settings]) {
        return nil;
    }

    _display = display;
    _displayID = display.displayID;
    return self;
}

- (void)invalidate {
    _display = nil;
    _displayID = 0;
}

@end
