#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Update BlueRetro's firmware over Bluetooth LE from a terminal (no browser).

  ota_blueretro.py build/BlueRetro.bin [--address AA:BB:CC:DD:EE:FF] [--name BlueRetro]
  ota_blueretro.py --info                 # just connect and print the running version

Does what the web config's OTA page does, using the GATT service in
main/bluetooth/att_cfg.c (needs: pip install bleak):

  service       56830f56-5180-fab0-314b-2fa176799a00
  ...9a07       config command (read/write)   ...9a08  OTA firmware data (write)
  ...9a09       firmware version (read)

  1. write 0xA5 (CFG_CMD_OTA_START): esp_ota_begin() erases the next OTA slot
  2. write the image in chunks to ...9a08, each acknowledged (esp_ota_write)
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
SERVICE, CFG_CMD, OTA_DATA, FW_VER = (BASE.format(i) for i in (0x00, 0x07, 0x08, 0x09))
OTA_START, OTA_END, OTA_ABORT = 0xA5, 0x5A, 0xDE


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
            lambda d, adv: (adv.local_name or d.name or "") == args.name, timeout=args.scan)
    if dev is None:
        sys.exit(f"no BlueRetro found in {args.scan:.0f} s: console on, no controller "
                 f"connected to it, PC Bluetooth on and in range?")
    return dev


async def chunk_size(client):
    """Largest write that fits one ATT packet (MTU - 3)."""
    backend = getattr(client, "_backend", None)
    if hasattr(backend, "_acquire_mtu"):          # BlueZ: MTU isn't known until acquired
        try:
            await backend._acquire_mtu()
        except Exception:
            pass
    return max(20, min(client.mtu_size - 3, 509))


async def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("firmware", nargs="?", help="BlueRetro app image (.bin)")
    ap.add_argument("--address", help="BlueRetro BLE address (default: scan by name)")
    ap.add_argument("--name", default="BlueRetro", help="advertised name to look for")
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
        await client.read_gatt_char(CFG_CMD)        # first read also requests a 7.5-15 ms interval
        running = (await client.read_gatt_char(FW_VER)).split(b"\0")[0].decode(errors="replace")
        print(f"running firmware: {running}")
        if args.info:
            return

        size = await chunk_size(client)
        print(f"starting OTA (erases the update slot, a few seconds), {size}-byte chunks")
        await client.write_gatt_char(CFG_CMD, bytes([OTA_START]), response=True)
        t0, sent, last = time.monotonic(), 0, 0.0
        try:
            while sent < len(image):
                chunk = image[sent:sent + size]
                await client.write_gatt_char(OTA_DATA, chunk, response=True)
                sent += len(chunk)
                now = time.monotonic()
                if now - last > 0.5 or sent == len(image):
                    rate = sent / max(now - t0, 1e-3) / 1024
                    print(f"\r  {sent * 100 // len(image):3d}%  {sent}/{len(image)} bytes  "
                          f"{rate:.1f} KiB/s", end="", flush=True)
                    last = now
            print()
        except BaseException:
            print("\ntransfer failed; aborting the OTA (running firmware unchanged)")
            try:
                await client.write_gatt_char(CFG_CMD, bytes([OTA_ABORT]), response=True)
            except Exception:
                pass
            raise
        print("finishing: BlueRetro checks the image, switches to it and restarts")
        try:
            await client.write_gatt_char(CFG_CMD, bytes([OTA_END]), response=True)
        except Exception as e:                       # it may restart before acknowledging
            print(f"  (no acknowledgement: {e.__class__.__name__}; normal if it restarted)")
    print(f"done in {time.monotonic() - t0:.0f} s. Check with: {sys.argv[0]} --info")


if __name__ == "__main__":
    asyncio.run(main())
