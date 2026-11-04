# Install Herness on an iPhone using Windows

You can install the native app with **AltStore Classic and a free Apple Account**. No paid Apple Developer membership is needed. You need a Windows PC, a USB cable for initial setup, and an iPhone running **iOS 17 or later**.

Use **AltStore Classic** for this `.ipa` file. AltStore PAL uses a different distribution process.

## Set up AltStore Classic

Follow the [official Windows installation guide](https://github.com/altstoreio/FAQ/blob/main/altstore-world/how-to-install-altstore-windows.md):

1. Install iTunes and iCloud using the Apple downloads linked in that guide. Its standard setup uses Apple's installers rather than the Microsoft Store versions.
2. Download and install **AltServer for Windows** from the guide. Run AltServer as administrator and allow its requested access to your private network.
3. Connect and unlock your iPhone, then accept the computer's trust request. Open iTunes, sign in, and enable **Wi-Fi sync** for the phone.
4. In the Windows system tray, choose **AltServer → Install AltStore → your iPhone**. Enter your Apple Account credentials in AltServer when prompted.
5. On the iPhone, trust the installed developer profile under **Settings → General → VPN & Device Management**. The wording can vary by iOS version.
6. Enable **Settings → Privacy & Security → Developer Mode**, then complete the restart and confirmation prompts. AltStore requires this on iOS 16 and later.

Enter Apple credentials only in the local installation flow. Do not send your password, verification codes, or signing credentials to this chat or GitHub Actions.

## Download and install Herness

1. Open the [Herness iOS workflow](https://github.com/amitwjace-creator/herness/actions/workflows/ios.yml?query=branch%3Acodex%2Fnative-iphone-app) and choose a successful run that includes the IPA packaging step.
2. Download its artifact containing **`Herness-unsigned.ipa`**. GitHub may require you to sign in. Extract the downloaded artifact `.zip`; the file you need is the `.ipa` inside, not an `.xcarchive` or the artifact ZIP itself.
3. Put `Herness-unsigned.ipa` in **Files** on your iPhone, for example through iCloud Drive. Keep AltServer running on the PC, with the iPhone connected by USB or on the same Wi-Fi network.
4. Open **AltStore → My Apps → +**, choose `Herness-unsigned.ipa`, and complete any Apple Account prompts. AltStore signs the app with your personal development certificate and installs it.
5. Open **Herness** from the Home Screen.

The downloaded IPA is unsigned and cannot be installed directly by tapping it. Personal signing happens through your AltStore/AltServer setup; the build artifact contains no Apple signing credentials. Physical-device signing and installation have not yet been verified for this project.

## Keep the app working

Free-account apps expire after **7 days**. Open **AltStore → My Apps → Refresh All** before they expire while AltServer is reachable over USB or the same Wi-Fi network. Automatic background refresh is attempted, but leaving the PC off for a week will not keep the apps renewed.

There is a limit of **3 active sideloaded apps**, including AltStore itself. AltStore plus Herness use two slots. If AltStore expires, the [official AltServer guide](https://github.com/altstoreio/FAQ/blob/main/altstore-world/altserver.md) says to reinstall it through AltServer **without deleting it first**.

## Connect your Herness server

Installation and backend setup are separate. Follow [Connect the backend](README.md#connect-the-backend) to run Herness and configure an authenticated HTTPS address your iPhone can reach. Enter that address and its pairing key in the app.

The native app has no embedded live model. Its sample conversation works locally; live chat and Knowledge use your running backend and configured models. `localhost` on the iPhone refers to the phone, not the Windows PC.

## Official references

- [Windows installation](https://github.com/altstoreio/FAQ/blob/main/altstore-world/how-to-install-altstore-windows.md)
- [Seven-day expiry and app limits](https://github.com/altstoreio/FAQ/blob/main/altstore-world/your-altstore.md)
- [AltServer connection and refresh requirements](https://github.com/altstoreio/FAQ/blob/main/altstore-world/altserver.md)
- [Free distribution with AltStore Classic](https://github.com/altstoreio/FAQ/blob/main/developers/distribute-with-altstore-classic.md)
