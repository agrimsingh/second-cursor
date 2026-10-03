# Second Cursor icon

Two offset cursors represent the human and agent working independently. Warm white and vermilion orange sit on a graphite macOS tile; the transparent corners work on light and dark backgrounds.

`AppIcon.png` is the original generated artwork. `../AppIcon.icns` contains standard and Retina macOS representations from 16 to 1024 pixels for older macOS releases. The browser extension uses PNGs generated from the same master. Current macOS uses the full-bleed artwork in `../AppIcon.icon`, compiled into `../Assets.car`, so the system applies its own corners without adding a second frame. Regenerate the shipping assets on macOS with Xcode 27 (the verified toolchain):

```sh
scripts/generate-icons.sh
```

`scripts/build-app.sh` copies both icon formats before signing. Generated assets are checked in, so normal app builds do not need to regenerate artwork. The menu bar retains its state-specific symbols for activity, pause, and errors.

The full-bleed source follows [Apple's app icon guidance](https://developer.apple.com/design/human-interface-guidelines/app-icons/). Check the actual bundled app with `NSWorkspace.shared.icon(forFile:)` when changing the artwork: inspecting the PNG alone will not reveal macOS's automatic framing.

Verified the actual bundle rendering on macOS 27, the ten ICNS representations, extension icon dimensions and paths, and the signed release build. Xcode also includes generated bitmap fallbacks in `Assets.car`; selection between those and the separate ICNS on macOS 14–15 has not been tested on those systems.

## Artwork provenance

Created with the built-in image generation tool, October 4, 2026. Final generation prompt:

> Create a polished, distinctive macOS application icon for "Second Cursor", a desktop utility where a human and an AI each have their own independent cursor. This is the actual final icon asset, not a mockup or presentation.
>
> Canvas: square 1024x1024 transparent PNG. A single large macOS-style rounded square tile centered on the transparent canvas, spanning approximately x=56 to 968 and y=56 to 968, smooth continuous rounded corners. No surrounding scene, no extra border/frame. Tile is very dark warm graphite (#181713), with a subtle premium satin surface and extremely restrained top-edge lighting.
>
> Mark: exactly TWO beautiful, instantly recognizable mouse pointer arrows, both pointing toward the upper left, each a classic solid desktop arrow silhouette with a clear notch and short diagonal stem. They form a bold, balanced, interlocking diagonal composition: a slightly larger warm porcelain-white cursor in the upper-left/center, and a vivid vermilion-orange (#F54E00) second cursor offset to the lower-right. The two cursors are close and partially overlapping, with a thin dark separation where they meet so both remain readable. The orange cursor sits above the white one in the overlap. The result should feel like a clever single emblem for two people working together, not a repeated pattern. Use two simple chunky arrow silhouettes, generous negative space, subtle beveled edge catching light and minimal depth, almost flat graphic. Keep the mark large, occupying about two-thirds of the tile. Optically centered. Strong clean geometry, pristine antialiased edges, no tiny details. Recognizable at 16 and 32 pixels. Premium independent Mac software aesthetic.
>
> Constraints: no letters, no text, no numbers, no labels, no sparkle, no magic wand, no robot, no circular badge, no additional arrows or graphics, no gradients with multiple bright colors, no shiny plastic or exaggerated 3D. Transparent outside the rounded tile, no cast shadow extending beyond it. Return only the single finished icon.

Modern macOS adaptation prompt, using the original artwork as the reference:

> Adapt this exact Second Cursor app icon for Apple's modern app icon pipeline. Preserve the two cursors, their shapes, colors, size, position, overlap, and the overall visual design. The ONLY change is the background: remove the rounded tile boundary and all transparency, and extend the dark warm graphite satin background fully to all four square canvas edges. Make a full-bleed opaque square, with NO rounded corners, NO border, NO light rim, NO extra outer frame, NO margin around a tile. The operating system will apply its own rounded-square mask, so the source image must be square and opaque everywhere. Keep exactly the same bold warm white upper-left cursor and vermilion orange lower-right cursor. No text or new elements. Output a single square image.
