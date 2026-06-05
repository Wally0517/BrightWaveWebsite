# Brightwave Habitat — Mobile App

Capacitor wrapper that turns [www.brightwavehabitat.com](https://www.brightwavehabitat.com) into installable Android + iOS apps.

- **App name:** Brightwave Habitat
- **Bundle ID:** `com.brightwavehabitat.app`
- **Loads:** `https://www.brightwavehabitat.com` (the live site)

The app is a thin native shell — every code change you push to the website appears in the app instantly. No need to resubmit the app to update content.

---

## How to get an APK (free, no install required)

The recommended path: let GitHub Actions build the APK in the cloud.

1. Push this repo to GitHub (any repo, can be private).
2. Go to **Actions** tab → **Build Android APK** → **Run workflow**.
3. When it finishes (~5 min), open the run → scroll to **Artifacts** → download `brightwave-habitat-debug.apk`.
4. Email or AirDrop the `.apk` to your Android phone.
5. On the phone: tap the APK, allow "Install from unknown sources," install. Done.

You can also host the APK on `brightwavehabitat.com/download` so customers can install without the Play Store. Totally free, no Google account needed.

## How to get on the Google Play Store ($25 one-time)

1. Pay $25 once at [play.google.com/console](https://play.google.com/console).
2. Build a **release** APK (we'll update the workflow to sign it when you're ready).
3. Upload through Play Console, fill out store listing (description, screenshots, privacy policy).
4. Review takes 1–7 days first time.

## How to get on the Apple App Store

Apple requires:
- **$99/year** Apple Developer account ([developer.apple.com](https://developer.apple.com/programs/))
- **No free path exists** — this is a hard Apple requirement

What we have ready for you:
- The full iOS Xcode project is already generated in `ios/`
- GitHub Actions workflow that builds an unsigned iOS archive (free, runs on Apple's macOS runners)
- The day you pay $99, we sign the archive and submit through TestFlight → App Store

In the meantime, the **free distribution channel for iOS** is:
- TestFlight (still needs the $99 account, but distributes to up to 10,000 beta testers for free)
- Sideload via Xcode to your own iPhone (free, expires every 7 days)

---

## Local development (optional)

Requires Node 20+. Android dev also needs Android Studio + JDK 21.

```bash
cd brightwave-app
npm install
npx cap sync             # after changing capacitor.config.json
npx cap open android     # opens Android Studio
npx cap open ios         # opens Xcode (Mac only)
```

To regenerate icons/splash after updating the logo:
```bash
node scripts/build-assets.js
npx capacitor-assets generate
```

---

## What changes when the website changes?

Nothing on the app side. The app loads `https://www.brightwavehabitat.com` every time it opens, so:
- New properties, new dashboards, bug fixes → all appear automatically
- Only need to rebuild + resubmit the app if you change the **native shell** (icon, splash, app name, native plugins)

---

## File layout

```
brightwave-app/
├── capacitor.config.json     # appId, appName, server URL
├── assets/                   # source icon + splash (1024 / 2732)
├── scripts/build-assets.js   # regenerates icon/splash from logo
├── www/index.html            # fallback loader (shows while site loads)
├── android/                  # Android Studio project
├── ios/                      # Xcode project
└── .github/workflows/        # cloud builds (free)
    ├── android-build.yml
    └── ios-build.yml
```
