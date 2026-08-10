# Single-account iPhone build

The active production path is:

```text
lucius's iPhone -> https://photode.ru/tg/api/index.php -> VPS -> tg-vpn -> Telegram
```

The app contains no Telegram phone/code/password flow. It opens the already-authorized
TDLib account on the VPS using one revocable device credential. The credential is held
in the ignored `ios/DeviceSecrets.xcconfig` file and compiled into this private build.

## Current behavior

- opens directly to the existing account's chat list
- loads up to 100 chat summaries
- restores the last cached account, chats, and histories before the first frame
- refreshes cached data in place while the header displays `UPD`
- preloads the latest 30 messages for every returned chat in bounded batches
- prefetches older 30-message pages when only ten unseen rows remain above the viewport
- opens a chat only after its initial history is ready and bottom-anchored
- sends and receives multiline text through HTTPS long polling
- renders outgoing text optimistically, adding `Y.` only after Telegram confirms it
- updates read receipts, presence, and typing state
- connects only to `https://photode.ru/tg/api/index.php`
- rejects redirects away from `photode.ru`

Media controls remain disabled in this Swift checkpoint. The deployed backend already
supports photo, video, voice-note, and video-note transport.

## Build and install

1. Keep `ios/DeviceSecrets.xcconfig` on the authorized Mac. Do not commit, upload, or
   include it in a source archive.
2. Open `ios/TGPhotode.xcodeproj` in Xcode.
3. Choose the configured Apple development team.
4. Select `lucius's iPhone` as the physical run destination.
5. Press Run.

The server credential does not expire. Apple development signing may still require the
application to be rebuilt later, depending on the provisioning profile used by Xcode.

## Revoke or rotate access

If the iPhone or application bundle is lost:

1. Clear `IOS_DEVICE_ACCESS_TOKEN` in `/etc/tg-photode/tg-photode.env` on the VPS.
2. Run `ops/deploy-single-device.sh` again to generate a replacement.
3. Copy the newly generated `/root/TGPhotode-DeviceSecrets.xcconfig` to the authorized
   Mac as `ios/DeviceSecrets.xcconfig`.
4. Rebuild the app in Xcode.

The old installed build stops receiving API access as soon as the server token changes.
