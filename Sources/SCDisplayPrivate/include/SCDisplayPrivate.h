#import <Foundation/Foundation.h>
#import <CoreGraphics/CoreGraphics.h>

NS_ASSUME_NONNULL_BEGIN

/// A virtual monitor backed by the private CGVirtualDisplay API. The display exists until
/// `-invalidate` is called or this object is deallocated.
@interface SCVirtualDisplay : NSObject

/// `width` and `height` are in points; with `hiDPI` the backing store is twice that in pixels.
/// Returns nil when the private API is missing or the window server refuses the display.
- (nullable instancetype)initWithName:(NSString *)name
                                width:(unsigned int)width
                               height:(unsigned int)height
                                hiDPI:(BOOL)hiDPI NS_DESIGNATED_INITIALIZER;
- (instancetype)init NS_UNAVAILABLE;

/// 0 after `-invalidate`.
@property (nonatomic, readonly) CGDirectDisplayID displayID;

/// Set when the window server removed the display on its own (e.g. WindowServer restart).
@property (atomic, readonly, getter=isTerminated) BOOL terminated;

/// Removes the display. The window server finishes removing it asynchronously.
- (void)invalidate;

@end

NS_ASSUME_NONNULL_END
