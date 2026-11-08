#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Update BlueRetro's firmware over Bluetooth LE from a terminal (no browser).

  ota_blueretro.py build/BlueRetro.bin [--address AA:BB:CC:DD:EE:FF] [--name BlueRetro]
  ota_blueretro.py --info                 # just connect and print the running version

Mirrors the web config's OTA page (BlueRetroWebCfg ota.js, utils/otaWriteFirmware.js,
otaWriteFwRecursive.js, getAppVersion.js, getStringCmd.js, getBdAddr.js) step for step,
using the GATT service in main/bluetooth/att_cfg.c (needs: pip install bleak):

  service       56830f56-5180-fab0-314b-2fa176799a00    (brUuid[0])
  ...9a07       config command (write, then read)      (brUuid[7])
  ...9a08       OTA firmware data (write)              (brUuid[8])
  ...9a09       firmware version (read)                (brUuid[9])
  ...9a0c       Bluetooth address (read)               (brUuid[12])

  connect: device whose name starts with "BlueRetro"; read the address, the version,
           and the firmware name (write 0x07 to ...9a07, read it back)
  check:   as the web config, the running firmware and the image must agree on "hw2"
           (version/name vs the image's first 256 bytes). Stricter here: if both carry a
           BlueRetro_<hw>_<system> name they must be equal (--force to override)
  1. write 0xA5 (CFG_CMD_OTA_START): esp_ota_begin() erases the next OTA slot
  2. write the image to ...9a08 in 244-byte chunks (the web config's fixed `mtu`), each
     a write with response (esp_ota_write)
  3. write 0x5A (CFG_CMD_OTA_END): image checked, boot slot switched, adapter restarts

The running firmware is untouched until step 3 succeeds; on an error before that the
script sends 0xDE (CFG_CMD_OTA_ABORT). The factory firmware is never written: holding
the adapter's button ~30 s (with a firmware that boots) returns to it.

BlueRetro must be powered (console on) and advertising, i.e. not in use by a
controller, like when the web config connects.
"""
import argparse, asyncio, struct, sys, time
from bleak import BleakClient, BleakScanner

BASE = "56830f56-5180-fab0-314b-2fa176799a{:02x}"
SERVICE, CFG_CMD, OTA_DATA, FW_VER, BD_ADDR = (BASE.format(i) for i in (0x00, 0x07, 0x08, 0x09, 0x0c))
CMD_GET_FW_NAME, OTA_START, OTA_END, OTA_ABORT = 0x07, 0xA5, 0x5A, 0xDE
CHUNK = 244                                   # web config utils/constants.js `mtu`


def check_image(data):
    """ESP32 app image: header magic 0xE9, esp_app_desc_t (magic 0xABCD5432) at 0x20."""
    if len(data) < 0x100 or data[0] != 0xE9:
        sys.exit("not an ESP32 app image (first byte must be 0xE9) -- use the app .bin, "
                 "not bootloader/partition files")
    magic, = struct.unpack_from("<I", data, 0x20)
    if magic != 0xABCD5432:
        sys.exit("no ESP-IDF app description in the image")
    text = lambda off, n: data[off:off + n].split(b"\0")[0].decode(errors="replace")
    return {"version": text(0x30, 32), "project": text(0x50, 32),
            "time": text(0x70, 16), "date": text(0x80, 16)}


async def find(args):
    if args.address:
        dev = await BleakScanner.find_device_by_address(args.address, timeout=args.scan)
    else:
        dev = await BleakScanner.find_device_by_filter(
            lambda d, adv: (adv.local_name or d.name or "").startswith(args.name), timeout=args.scan)
    if dev is None:
        sys.exit(f"no BlueRetro found in {args.scan:.0f} s: console on, no controller "
                 f"connected to it, PC Bluetooth on and in range?")
    return dev


def text(b):
    return bytes(b).split(b"\0")[0].decode(errors="replace")


def hw_sys(name):
    """'BlueRetro_hw1_n64' -> 'hw1_n64'; None for a plain 'BlueRetro'."""
    return name.split("_", 1)[1] if name.startswith("BlueRetro_") else None


async def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("firmware", nargs="?", help="BlueRetro app image (.bin)")
    ap.add_argument("--address", help="BlueRetro BLE address (default: scan by name)")
    ap.add_argument("--name", default="BlueRetro", help="advertised name prefix to look for")
    ap.add_argument("--force", action="store_true",
                    help="skip the stricter hardware/system name check (not the hw2 one)")
    ap.add_argument("--scan", type=float, default=20.0, help="scan timeout, s")
    ap.add_argument("--info", action="store_true", help="only print the running version")
    args = ap.parse_args()

    image = None
    if not args.info:
        if not args.firmware:
            ap.error("firmware .bin required (or --info)")
        image = open(args.firmware, "rb").read()
        desc = check_image(image)
        print(f"image: {args.firmware}  {len(image)} bytes  {desc['project']} "
              f"{desc['version']}  built {desc['date']} {desc['time']}")

    dev = await find(args)
    print(f"found {dev.name} {dev.address}")
    async with BleakClient(dev) as client:
        if client.services.get_service(SERVICE) is None:
            sys.exit("BlueRetro config service not found on this device")
        bd = await client.read_gatt_char(BD_ADDR)
        app_ver = text(await client.read_gatt_char(FW_VER))
        await client.write_gatt_char(CFG_CMD, bytes([CMD_GET_FW_NAME]), response=True)
        app_name = text(await client.read_gatt_char(CFG_CMD))
        print(f"BlueRetro {':'.join(f'{x:02x}' for x in reversed(bd[:6]))}: "
              f"running {app_name} {app_ver}")
        if args.info:
            return

        # the web config's check, then a stricter one
        cur_hw2 = "hw2" in app_ver or "hw2" in app_name
        new_hw2 = b"hw2" in image[:256]
        if cur_hw2 != new_hw2:
            sys.exit(f"hardware mismatch: running firmware is {'hw2' if cur_hw2 else 'hw1'}, "
                     f"image is {'hw2' if new_hw2 else 'hw1'} (the web config refuses this too)")
        cur, new = hw_sys(app_name), hw_sys(desc["project"])
        if cur != new and not args.force:
            sys.exit(f"name mismatch: running {app_name!r}, image {desc['project']!r} "
                     f"(different hardware/console build?); --force to flash anyway")

        # As otaWriteFirmware.js: start, chunks, end in one chain; any failure -> abort.
        t0, sent, last, step = time.monotonic(), 0, 0.0, "start"
        try:
            print(f"starting OTA (erases the update slot, a few seconds), {CHUNK}-byte chunks")
            await client.write_gatt_char(CFG_CMD, bytes([OTA_START]), response=True)
            step = "transfer"
            while sent < len(image):
                chunk = image[sent:sent + CHUNK]
                await client.write_gatt_char(OTA_DATA, chunk, response=True)
                sent += len(chunk)
                now = time.monotonic()
                if now - last > 0.5 or sent == len(image):
                    rate = sent / max(now - t0, 1e-3) / 1024
                    print(f"\r  {sent * 100 // len(image):3d}%  {sent}/{len(image)} bytes  "
                          f"{rate:.1f} KiB/s", end="", flush=True)
                    last = now
            print()
            step = "end"
            print("finishing: BlueRetro checks the image, switches to it and restarts")
            await client.write_gatt_char(CFG_CMD, bytes([OTA_END]), response=True)
        except BaseException as e:
            print(f"\nOTA failed at step '{step}' ({e.__class__.__name__}: {e}); sending abort")
            try:
                await client.write_gatt_char(CFG_CMD, bytes([OTA_ABORT]), response=True)
            except Exception:
                pass
            if step == "end":
                print("the adapter may already have restarted into the new firmware: "
                      f"check with {sys.argv[0]} --info")
            else:
                print("the running firmware is unchanged")
            raise
    print(f"done in {time.monotonic() - t0:.0f} s. Check with: {sys.argv[0]} --info")


if __name__ == "__main__":
    asyncio.run(main())
