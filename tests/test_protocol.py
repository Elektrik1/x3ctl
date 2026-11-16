"""Payload checks ported from asctl's `selftest`. Run: python3 -m unittest discover tests"""

import unittest

from x3ctl import protocol as p
from x3ctl.device import feature_report_ids


def wire(report, i):
    return report[8 + i] | (report[16 + i] << 8)


def checksum_ok(report, start, end):
    hi, lo = p.checksum(report[start:end])
    return report[end] == hi and report[end + 1] == lo


class DpiTests(unittest.TestCase):
    def test_vendor_factory(self):
        r = p.build_dpi_report(p.VENDOR_FACTORY_STAGES, p.VENDOR_FACTORY_ACTIVE, p.SensorToggles())
        self.assertEqual(len(r), 56)
        self.assertEqual((r[0], r[1], r[2]), (0x04, 56, 1))
        self.assertEqual(r[5], 0b111111)
        self.assertEqual(r[24], 2)
        self.assertEqual([wire(r, i) for i in range(8)], [15, 31, 47, 63, 99, 519, 0, 0])
        self.assertEqual(r[16 + 5], 2)
        for i, s in enumerate(p.VENDOR_FACTORY_STAGES):
            self.assertEqual(tuple(r[25 + i * 3 : 28 + i * 3]), s.colour)
        self.assertEqual(r[49], 1)
        self.assertTrue(checksum_ok(r, 3, 50))

    def test_disabled_middle_slot_keeps_numbering(self):
        stages = [p.Stage(s.dpi, s.enabled, s.colour) for s in p.VENDOR_FACTORY_STAGES]
        stages[2].enabled = False
        r = p.build_dpi_report(stages, 0, p.SensorToggles())
        self.assertEqual(r[5], 0b111011)
        self.assertEqual(r[8 + 3], 63)
        self.assertEqual(r[25 + 9], 255)
        self.assertEqual(r[8 + 2], 47)

    def test_stock(self):
        r = p.build_dpi_report(p.STOCK_STAGES, 0, p.SensorToggles())
        self.assertEqual(r[5], 0b11111)
        self.assertEqual(r[24], 1)
        self.assertEqual([wire(r, i) for i in range(5)], [15, 31, 63, 99, 519])
        self.assertEqual(bytes(r[25:28]), bytes([0, 0, 255]))
        self.assertEqual(bytes(r[37:40]), bytes([255, 0, 0]))

    def test_edges_and_toggles(self):
        self.assertEqual(p.dpi_wire_value(50), 0)
        self.assertEqual(p.dpi_from_wire(519), 26000)
        stages = [p.Stage(s.dpi, s.enabled, s.colour) for s in p.VENDOR_FACTORY_STAGES]
        stages[0].dpi = 0
        self.assertEqual(p.build_dpi_report(stages, 0, p.SensorToggles())[8], 0)
        t = p.SensorToggles(lift_off_2mm=True, ripple_control=False, angle_snap=True, motion_sync=True)
        r = p.build_dpi_report(p.STOCK_STAGES, 0, t)
        self.assertEqual((r[3], r[4], r[6], r[7]), (1, 0, 1, 1))
        self.assertTrue(checksum_ok(r, 3, 50))


class OtherReportTests(unittest.TestCase):
    def test_polling(self):
        for hz, div in [(125, 8), (250, 4), (500, 2), (1000, 1)]:
            r = p.build_polling_report(hz)
            self.assertEqual(r[:3], bytes([6, 9, 1]))
            self.assertEqual(r[3], div)
            self.assertEqual(r[4], (~div) & 0xFF)
        with self.assertRaises(ValueError):
            p.build_polling_report(8000)

    def test_preamble(self):
        self.assertEqual(p.hexs(p.PREAMBLE), "0C 0A 01 FE 01 FE 00 00 00 00")

    def test_buttons_factory(self):
        r = p.build_button_report(p.FACTORY_BUTTONS)
        self.assertEqual((r[0], r[1], len(r)), (0x08, 59, 59))
        self.assertEqual(r[3], 0x02)
        self.assertEqual(r[3 + 9], 0x0D)
        self.assertEqual(r[3 + 12], 0x3C)
        self.assertEqual(r[3 + 18], 0x06)
        self.assertEqual(r[3 + 21], 0x05)
        self.assertTrue(checksum_ok(r, 3, 57))

    def test_action_parsing(self):
        self.assertEqual(p.parse_action("key:ctrl+c"), (0x11, 0x01, 0x06))
        self.assertEqual(p.parse_action("macro:2"), (0x12, 0, 2))
        self.assertEqual(p.parse_action("easy_aim"), (0x10, 0, 3))
        self.assertEqual(p.parse_action("button_off")[0], 0x01)
        self.assertEqual(p.parse_action("raw:11.01.06"), (0x11, 1, 6))
        self.assertTrue(p.has_left_click(p.FACTORY_BUTTONS))
        self.assertFalse(p.has_left_click(["right"] * 18))

    def test_power(self):
        r = p.build_power_report(10, 10, 10)
        self.assertEqual((r[0], r[1]), (0x05, 15))
        self.assertEqual((r[9], r[10]), (10, 10))
        self.assertEqual(r[4] & 0xF0, 0)
        self.assertEqual((r[5] & 0xF0) >> 4, 10)
        self.assertTrue(checksum_ok(r, 3, 11))
        r = p.build_power_report(5, 60, 2)
        self.assertEqual(((r[4] & 0xF0) | (r[5] >> 4)), 60)

    def test_macro(self):
        events = [p.MacroEvent(0x04, True, 20), p.MacroEvent(0x04, False, 20)]
        buf = p.build_macro_buffer(3, events, repeat=2, loop_mode=p.LOOP_WHILE_HELD)
        self.assertEqual(len(buf), 131)
        self.assertEqual((buf[0], buf[1], buf[2], buf[3], buf[7], buf[28]), (9, 131, 3, 2, 2, 2))
        self.assertEqual(bytes(buf[29:33]), bytes([0x03, 0x04, 0x83, 0x04]))
        self.assertTrue(checksum_ok(buf, 3, 129))
        chunks = p.build_macro_reports(3, events)
        self.assertEqual([c[1] for c in chunks], [64, 64, 12])
        self.assertEqual([c[3] for c in chunks], [0, 1, 2])
        self.assertTrue(all(len(c) == 64 and c[0] == 9 and c[2] == 3 for c in chunks))
        self.assertEqual(p.split_delay(1800), [1] + [200] * 9)
        self.assertEqual(p.split_delay(1300), [100] + [200] * 6)

    def test_status(self):
        e = p.parse_status(bytes([0x03, 0x00, 0x10, 0x02, 0x00]))
        self.assertEqual((e.code, e.value), (0x1000, 2))
        e = p.parse_status(bytes([0x03, 0x10, 0x50, 0x00, 0x04]))
        self.assertEqual(e.describe(), "device acknowledged report 0x04")
        self.assertEqual(p.StatusEvent(0x4010, 0x0501).battery_percent, 50)
        self.assertIsNone(p.StatusEvent(0x4010, 0x0B01).battery_percent)

    def test_settings_round_trip(self):
        s = p.Settings()
        s.buttons[6] = "key:ctrl+shift+t"
        s.toggles.motion_sync = True
        again = p.Settings.from_dict(s.to_dict())
        self.assertEqual(again.dpi_report(), s.dpi_report())
        self.assertEqual(again.button_report(), s.button_report())
        self.assertEqual(again.power_report(), s.power_report())


class DescriptorTests(unittest.TestCase):
    def test_finds_feature_reports(self):
        # Fragment of the X3 receiver's config interface descriptor.
        desc = bytes.fromhex("050b0900a10185049533b1018505950cb1018506 9508b101c0".replace(" ", ""))
        self.assertEqual(feature_report_ids(desc), {4, 5, 6})


if __name__ == "__main__":
    unittest.main()
