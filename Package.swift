// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "QwenHarnessDesktop",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .executable(name: "QwenHarnessDesktop", targets: ["QwenHarnessDesktop"])
    ],
    targets: [
        .executableTarget(
            name: "QwenHarnessDesktop",
            linkerSettings: [
                .linkedFramework("AppKit"),
                .linkedFramework("WebKit")
            ]
        )
    ]
)
