# Release distribution

## Failure cases and acceptance criteria

- An explicitly requested signing identity that is absent must fail before a build. The release packager never creates a certificate, key, or notarization credential.
- `--check` inspects readiness without building, signing, copying, making directories, or changing credentials. Missing requirements produce a nonzero exit status.
- An app without its executable, native host, browser extension, or version metadata cannot be packaged. Binaries must not depend on a developer checkout or unbundled non-system libraries.
- `--skip-build --notarize` must reject a prepared app that lacks the requested Developer ID signature, a secure timestamp, or the hardened runtime on either executable.
- A failed signature check, rejected notarization submission, failed staple, or Gatekeeper rejection must stop a notarized release. Submission receipt alone is not success.
- The final ZIP must contain the stapled app. The DMG must be independently signed, notarized, stapled, and validated. Both artifacts receive SHA-256 checksums and explicit trust metadata.
- A locally signed package must describe itself as unnotarized. Local signing preserves development permissions but does not make a download trusted on another Mac.
- Packaging never installs, launches, publishes, or replaces a previously completed artifact. `scripts/build_and_run.sh` remains the developer build/install/run entry point.

## Local distribution

Run `scripts/package-release.sh --check` to check local prerequisites. An existing `Second Cursor Local Signing` certificate is required; create it through the usual developer build flow if this is a fresh machine. Run `scripts/package-release.sh` to build a release app and package a ZIP and DMG under `build/release`.

Use `--skip-build` to package the existing `build/SecondCursor.app`, and `--output /absolute/directory` to choose an output directory. A prepared app is always validated before packaging. Output filenames include the app version and build number from `Resources/Info.plist` (or the prepared app's own plist with `--skip-build`). A completed artifact with the same name causes a failure rather than being overwritten.

The bundle includes `Contents/MacOS/sc-native-host`, `Contents/Resources/extension`, and any required Swift compatibility libraries under `Contents/Frameworks`, so the downloaded app does not need the source checkout or Xcode. The build removes absolute toolchain search paths and signs nested libraries before the main bundle. Browser extension installation and native-host registration are handled by the app's setup flow. Browser developer mode is still necessary until an extension store distribution is available.

## Developer ID and notarization

Provision an existing **Developer ID Application** signing identity and a `notarytool` keychain profile outside these scripts. Set `SC_SIGNING_IDENTITY` to the certificate's exact name or SHA-1 fingerprint, and `SC_NOTARY_PROFILE` to that profile's name. Then run:

```sh
SC_SIGNING_IDENTITY='Developer ID Application: Your Organization (TEAMID)' \
SC_NOTARY_PROFILE='second-cursor-notary' \
scripts/package-release.sh --notarize --check

SC_SIGNING_IDENTITY='Developer ID Application: Your Organization (TEAMID)' \
SC_NOTARY_PROFILE='second-cursor-notary' \
scripts/package-release.sh --notarize
```

The check verifies local tooling, identity, and presence of the profile name. It does not contact Apple or read keychain secrets, so it cannot prove that the stored profile is usable. The actual submission validates the credentials and service access. Missing Developer ID credentials are a release prerequisite, not a reason to block local development.

The notarized path signs both executables with the hardened runtime and secure timestamps, verifies the bundle, submits the app archive, waits for acceptance, staples and validates the app, and assesses it with Gatekeeper. It then builds the final ZIP and DMG, signs and submits the DMG, staples it, and assesses that artifact separately. No entitlements are added speculatively; this app is not sandboxed and obtains its Accessibility and Screen Recording permissions through macOS.

Each successful release includes a `.release.json` manifest describing its signing mode, app/version/build/architectures, notarization state, and artifact checksums, plus a `.sha256` checksum file. Apple's submission responses are retained for notarized releases. A failed submission retains its diagnostic response and, when available, Apple's log under a uniquely named failure directory in the selected output folder. No final release manifest is emitted on failure.

## Verification

```sh
bash -n scripts/build-app.sh scripts/package-release.sh scripts/build_and_run.sh
scripts/package-release.sh --check
scripts/package-release.sh --skip-build
(cd build/release && shasum -a 256 -c SecondCursor-*.sha256)
codesign --verify --deep --strict --verbose=2 build/SecondCursor.app
```

Run the checksum command from the output directory (the checksum file contains portable basenames). On a notarized release, also inspect the retained responses and manifest, and test the actual download on a separate Mac/user account with quarantine intact. Packaging verifies signatures and trust policy; it cannot by itself prove the fresh-account permission and browser setup experience.
