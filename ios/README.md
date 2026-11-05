# TV Remote for iPhone/iPad (SwiftUI)

Native app for the hub API. Remote (d-pad, volume/channel with hold-to-repeat, media, power), Apps launcher, Keyboard, and More (numbers, color keys, HDMI inputs). Key presses go over a WebSocket with REST fallback; the hub URL and access key live in the Keychain.

## Build and install (Mac with Xcode 15+)

```bash
brew install xcodegen
cd ios && xcodegen generate
open TVRemote.xcodeproj
```

1. In Xcode, select the TVRemote target > Signing & Capabilities > Team: your Apple ID (Xcode > Settings > Accounts to add it).
2. Plug in the iPhone (or pair over Wi-Fi), pick it as the run destination, press Run.
3. On the iPhone: Settings > General > VPN & Device Management > trust your developer certificate. Turn on Developer Mode if asked.
4. Open Remote, allow Local Network, install Tailscale on the iPhone and sign in, then enter the hub address (`your-hub.your-tailnet.ts.net`) and the key from `~/.config/tvremote/hub.key`.

A free Apple ID signs the app for 7 days (rebuild to renew). A paid developer account ($99/yr) gives 1 year and TestFlight.
