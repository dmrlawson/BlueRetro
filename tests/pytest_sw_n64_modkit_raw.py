''' Tests for the 8BitDo N64 Modkit with raw-stick firmware ("N64 Controller Raw"). '''
import pytest
from device_data.br import axis, bt_type, bt_subtype, system, dev_mode, bt_conn_type


DEVICE_NAME = 'N64 Controller Raw'
N64_PAD = [system.N64, dev_mode.PAD, bt_conn_type.BT_BR_EDR]

# SPI flash replies exactly as the modkit firmware answers them (its emulated SPI image):
# 0x603D factory stick cal centre 0x7F0 +-0x7F0 (right stick erased), 0x6086 stick
# params (deadzone 80), 0x6098 erased, 0x8010 no user calibration.
MODKIT_SPI = {
    0x603D: 'f0077ff0077ff0077f' + 'ff' * 9,
    0x6086: '0f705d5030f33884433884433339' '93cdd66c',
    0x6098: 'ff' * 18,
    0x8010: 'ff' * 22,
}


def spi_reply(addr, data):
    ''' Subcommand 0x10 (SPI read) reply, padded like the other Switch tests. '''
    hexstr = ('a1210280000000c49782000000089010'
              f'{addr & 0xFF:02x}{addr >> 8:02x}0000{len(data) // 2:02x}{data}')
    return hexstr + '0' * (100 - len(hexstr))


def simple_report(lx_byte, ly_byte):
    ''' 0x3F report as the modkit sends it: stick byte in the high byte of each axis. '''
    return f'a13f00000f00{lx_byte:02x}00{ly_byte:02x}00800080'


def connect_modkit(blueretro, name):
    rsp = blueretro.send_name(name)
    assert rsp['device_name']['device_type'] == bt_type.SW
    assert rsp['device_name']['device_subtype'] == bt_subtype.SW_N64
    assert rsp['device_name']['device_name'] == name
    for addr, data in MODKIT_SPI.items():
        blueretro.send_hid_report(spi_reply(addr, data))
    for _ in range(2):
        blueretro.send_hid_report(simple_report(0x7F, 0x7F))


@pytest.mark.parametrize('blueretro', [N64_PAD], indirect=True)
def test_sw_n64_modkit_raw_axes_passthrough(blueretro):
    ''' Every stick byte reaches the N64 as its encoder count: no deadzone, no scaling. '''
    connect_modkit(blueretro, DEVICE_NAME)

    for b in range(256):
        count = b - 0x7F
        n64_x = min(count, 127)
        rsp = blueretro.send_hid_report(simple_report(b, 0x7F))
        assert rsp['generic_input']['axes'][axis.LX] == count
        assert rsp['generic_input']['axes'][axis.LY] == 0
        assert rsp['wired_output']['axes'][axis.LX] == n64_x & 0xFF
        assert rsp['wired_output']['axes'][axis.LY] == 0

        # Y: up is a lower byte, as in the other 0x3F axes
        rsp = blueretro.send_hid_report(simple_report(0x7F, b))
        assert rsp['generic_input']['axes'][axis.LX] == 0
        assert rsp['generic_input']['axes'][axis.LY] == -count
        assert rsp['wired_output']['axes'][axis.LX] == 0
        assert rsp['wired_output']['axes'][axis.LY] == -count & 0xFF


@pytest.mark.parametrize('blueretro', [N64_PAD], indirect=True)
def test_sw_n64_modkit_stock_name_keeps_scaling(blueretro):
    ''' The same pad under the stock name keeps the normal calibrated scaling. '''
    connect_modkit(blueretro, 'N64 Controller')

    full_tilt = 85
    rsp = blueretro.send_hid_report(simple_report(0x7F + full_tilt, 0x7F))
    assert rsp['generic_input']['axes'][axis.LX] == full_tilt * 16
    assert 0 < rsp['wired_output']['axes'][axis.LX] < full_tilt
