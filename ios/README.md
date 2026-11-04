# Herness for iPhone

This directory contains a native SwiftUI client and a checked-in Xcode project. The Python Herness backend runs on your computer or private server; the app connects to its authenticated API. It targets iOS 17 and supports iPhone and iPad. It opens disconnected until you configure a reachable server.

The native interface covers chat with streamed text through status polling, stopping a turn, reviewing approvals, conversation history, model profiles, selected-file attachments, Knowledge document upload/configuration/indexing/search, and system read-aloud. It does not reproduce every web workspace action. The app includes a clearly labeled, read-only sample conversation. It has no embedded model or standalone offline chat engine; the backend's optional `--demo` mode runs the scripted tool demonstration.

## Build on a Mac

Use Xcode 15.4 or newer with an installed iOS simulator runtime. The project uses Swift 5 language mode and the Swift 5.9-or-newer compiler supplied by Xcode. No external Swift packages or project generator are required.

```bash
open ios/Herness.xcodeproj
```

Select the shared **Herness** scheme and an iPhone simulator, then choose **Product → Run**. The app and test targets use the placeholder bundle identifiers `app.herness.ios` and `app.herness.ios.tests`; these identifiers are not represented as registered Apple identifiers. Simulator builds do not require a signing team.

To run tests from the command line, choose an available simulator first:

```bash
xcrun simctl list devices available
xcodebuild test \
  -project ios/Herness.xcodeproj \
  -scheme Herness \
  -configuration Debug \
  -destination 'platform=iOS Simulator,id=YOUR_SIMULATOR_UDID' \
  -derivedDataPath .build/ios \
  -resultBundlePath .build/HernessTests.xcresult \
  CODE_SIGNING_ALLOWED=NO
```

Replace `YOUR_SIMULATOR_UDID` with an identifier from the first command. Use a fresh result-bundle path for subsequent runs. `.github/workflows/ios.yml` selects an available iPhone simulator on a macOS runner, builds the app, runs its unit tests, and retains the test result bundle. After passing tests, it also creates an unsigned iPhone archive and saves it as the `Herness-unsigned-iphone-archive` artifact. That archive still requires Apple signing before installation; the workflow does not sign or upload to TestFlight or the App Store.

The XCTest suite covers origin and path validation, request authentication, JSON body encoding, rejected authentication, error redaction, invalid server responses, redirect refusal, current server payload decoding, and attachment validation. URLProtocol fixtures make these tests independent of a live server or paid provider. The redirect test exercises the delegate's refusal decision; actual TLS and proxy behavior still need device testing.

**Verification status:** on November 4, 2026, [GitHub Actions run 41882933868](https://github.com/amitwjace-creator/herness/actions/runs/41882933868) successfully built the app, passed its iPhone simulator unit tests, and produced an unsigned Release archive for commit `2854d96752a616b52e353d6020ebbb4e33070eb4` on a Mac runner. The run saved `Herness-unsigned-iphone-archive` and `Herness-iOS-tests` artifacts with seven-day retention. Apple signing, TestFlight distribution, and physical-device testing have not been completed; the unsigned archive cannot be installed directly on an iPhone.

## Connect the backend

Install the Python project and create a private pairing key in an existing directory:

```bash
python -m pip install -e .
python -m herness mobile-key --output /private/path/herness-mobile.key
python -m herness --workspace /path/to/project mobile \
  --public-origin https://your-private-server.example \
  --key-file /private/path/herness-mobile.key
```

Replace the paths and example hostname. `mobile-key` creates a new owner-only key file without printing its contents. Open that file privately when pairing the app. Its contents are the pairing key; the file path is not the key. Add `--demo` to the `mobile` command to exercise the backend's scripted demo instead of a live model.

For live chat, configure the backend's model and provider credentials as described in [Connect a provider](../README.md#connect-a-provider). The phone can select profiles already saved on that backend. Knowledge's embedding settings are separate and can be configured in the native app.

The mobile process listens on loopback port **8766**. Configure your own private HTTPS reverse proxy or VPN gateway to forward the chosen HTTPS origin to that local port, preserving the origin's Host header and the Authorization header. The project does not create or deploy a proxy. Use the same origin, without a path or query, in the app's connection settings and paste the pairing key. A real iPhone must be able to reach that origin and trust its TLS certificate. When the configured server is on your local network, iOS may request Local Network access. `localhost` on an iPhone refers to the phone, not your development computer.

Release builds accept HTTPS origins. Debug builds additionally allow HTTP loopback origins for local development; this does not expose the backend or make it reachable from another device. Redirects are refused instead of forwarding the pairing key to another request. The app stores a successful pairing key in the device's Keychain under that server origin and does not synchronize it through iCloud Keychain. Model-provider keys remain backend configuration, separate from the app's pairing key.

Run **one backend process per workspace**. Connected devices share its active conversation and agent turn. Chat, stop, approval, and conversation-switch requests include the conversation the app observed (`expected_session`). If another client switches conversations, the server rejects a stale action; refresh and review the current conversation before continuing. This is not a multi-user collaboration service with independent sessions or roles.

## Verify on an iPhone

In Xcode, select your own signing team and an available registered bundle identifier, connect your iPhone, enable Developer Mode if prompted, and run the app. A free personal team may support local device testing subject to Apple's current restrictions; TestFlight requires Apple Developer Program membership.

Before distributing a build, verify these real-device flows against your own server:

- Connect with a valid pairing key; confirm an invalid or revoked key is rejected.
- Send a message, watch updates, stop a run, and reopen its saved conversation.
- Review and deny an action, then approve a separately reviewed action and verify its actual backend result.
- Switch conversations from another client and confirm stale actions are rejected.
- Select a model profile and exercise Knowledge indexing/search with your configured provider.
- Select an attachment through Files, background and foreground the app, disconnect, and reconnect.
- Check Dynamic Type, VoiceOver, keyboard behavior, read-aloud, and iPad layout on the devices you intend to support.

These checks require your network and provider configuration. They have not been completed by the Linux build environment.

## Signing and TestFlight

No Apple account, team, certificate, provisioning profile, registered app identifier, or App Store Connect record is supplied. If you are unsure whether you have Developer Program membership, check your account in Xcode's **Settings → Accounts** and Apple's developer account page before planning TestFlight distribution.

Use the Xcode and iOS SDK versions currently required by Apple for submissions. The project's minimum development version above does not guarantee that Apple accepts an archive produced by that older toolchain.

1. Choose your registered bundle identifier and signing team in **Herness → Signing & Capabilities**. Update the test target's identifier consistently. The project uses automatic signing but leaves the team empty.
2. Set the version and a new build number in the target settings. Create the corresponding iOS app record in App Store Connect using your own account.
3. Run the simulator suite and physical-device checks. Select **Any iOS Device** and use **Product → Archive**.
4. Review the archive in Organizer. Choose **Distribute App → App Store Connect** and perform the upload manually when you are ready. Complete TestFlight's required app information and any review requirements before inviting testers.

An equivalent archive command, after configuring your signing identity and profiles, is:

```bash
xcodebuild archive \
  -project ios/Herness.xcodeproj \
  -scheme Herness \
  -configuration Release \
  -destination 'generic/platform=iOS' \
  -archivePath .build/Herness.xcarchive \
  DEVELOPMENT_TEAM=YOUR_TEAM_ID \
  PRODUCT_BUNDLE_IDENTIFIER=your.registered.bundle.identifier
```

The team and identifier above are placeholders. This command builds a signed archive only when your Mac has valid signing configuration; it does not upload it. The repository includes no automatic release or store-upload trigger.

## Privacy and resources

The app uses its configured backend for prompts, attachments, conversation state, and Knowledge operations; that backend may send data to your configured model and embedding providers. Read-aloud uses Apple's speech synthesis. The client does not request microphone, speech-recognition, camera, or broad photo-library permissions. Files are selected explicitly through the system picker.

`PrivacyInfo.xcprivacy` declares the UserDefaults required-reason API for app settings (`CA92.1`). It is not a completed App Store privacy questionnaire. Before submission, review the final app, backend hosting, and provider behavior and complete Apple's privacy disclosures accurately. The app icon and named colors are bundled original resources; the app does not download remote artwork for its interface.
