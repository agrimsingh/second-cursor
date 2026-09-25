// swift-tools-version:6.1
import PackageDescription

let package = Package(
    name: "second-cursor",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "SecondCursor", targets: ["SecondCursor"]),
        .executable(name: "sc-native-host", targets: ["sc-native-host"]),
        .executable(name: "sc-dev", targets: ["sc-dev"]),
        .executable(name: "sc-display-dev", targets: ["sc-display-dev"]),
        .executable(name: "sc-browser-dev", targets: ["sc-browser-dev"]),
    ],
    dependencies: [
        .package(url: "https://github.com/modelcontextprotocol/swift-sdk.git", exact: "0.12.1"),
        .package(url: "https://github.com/apple/swift-nio.git", from: "2.65.0"),
    ],
    targets: [
        .target(name: "SCShared"),

        .target(name: "SCCore", dependencies: ["SCShared"]),

        .target(name: "SCDisplayPrivate"),
        .target(name: "SCDisplay", dependencies: ["SCShared", "SCDisplayPrivate"]),

        .target(name: "SCBrowser", dependencies: ["SCShared"]),

        .target(
            name: "SCServer",
            dependencies: [
                "SCShared",
                .product(name: "MCP", package: "swift-sdk"),
                .product(name: "NIOCore", package: "swift-nio"),
                .product(name: "NIOPosix", package: "swift-nio"),
                .product(name: "NIOHTTP1", package: "swift-nio"),
            ]
        ),

        .executableTarget(
            name: "SecondCursor",
            dependencies: ["SCShared", "SCCore", "SCDisplay", "SCBrowser", "SCServer"]
        ),
        .executableTarget(name: "sc-native-host"),
        .executableTarget(name: "sc-dev", dependencies: ["SCShared", "SCCore"]),
        .executableTarget(name: "sc-display-dev", dependencies: ["SCShared", "SCDisplay"]),
        .executableTarget(name: "sc-browser-dev", dependencies: ["SCShared", "SCBrowser"]),
    ],
    swiftLanguageModes: [.v5]
)
