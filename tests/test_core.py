import struct
import unittest
from binascii import crc32
from unittest.mock import Mock, patch

import ap_helper
import hotspot
import server
import setup_dolphin


class DSUProtocolTests(unittest.TestCase):
    def test_packet_contains_valid_crc(self):
        packet = bytearray(server.build_packet(server.MSG_VERSION, b"payload"))
        expected = struct.unpack("<I", packet[8:12])[0]
        packet[8:12] = b"\0\0\0\0"
        self.assertEqual(expected, crc32(packet) & 0xFFFFFFFF)

    def test_pad_input_is_clamped(self):
        pad = server.PadState(0)
        pad.update_from_json({"left_x": 2, "left_y": -2, "px": 4, "py": -4})
        self.assertEqual((pad.left_x, pad.left_y), (1.0, -1.0))
        self.assertEqual((pad.right_x, pad.right_y), (1.0, -1.0))


class HotspotTests(unittest.TestCase):
    def test_wifi_qr_escapes_delimiters(self):
        self.assertEqual(
            server.wifi_qr_payload("Party;Mote", r"pass:word\\"),
            r"WIFI:T:WPA;S:Party\;Mote;P:pass\:word\\\\;;",
        )

    def test_current_channel_uses_station_frequency(self):
        access_point = hotspot.AccessPoint("wlan0", "PartyPad", "partypad")
        output = Mock(stdout="Connected to 00:00:00:00:00:00\n\tfreq: 5220\n")
        with patch.object(hotspot.subprocess, "run", return_value=output):
            self.assertEqual(access_point.current_channel(), 44)

    def test_current_channel_defaults_to_six_while_offline(self):
        access_point = hotspot.AccessPoint("wlan0", "PartyPad", "partypad")
        with patch.object(hotspot.subprocess, "run", return_value=Mock(stdout="Not connected.")):
            self.assertEqual(access_point.current_channel(), 6)

    def test_route_and_firewall_helpers(self):
        route = "default via 192.168.1.1 dev wlan0 proto dhcp metric 600\n"
        self.assertEqual(ap_helper.default_route_interface(route, "ap0"), "wlan0")
        insertion = ("-I", "FORWARD", "1", "-i", "ap0", "-o", "wlan0", "-j", "ACCEPT")
        self.assertEqual(
            ap_helper.deletion_rule(insertion),
            ("-D", "FORWARD", "-i", "ap0", "-o", "wlan0", "-j", "ACCEPT"),
        )


class DolphinConfigTests(unittest.TestCase):
    def test_parse_sections_preserves_preamble_and_sections(self):
        sections = setup_dolphin.parse_sections("note\n[One]\na=1\n[Two]\nb=2\n")
        self.assertEqual(sections[0], (None, ["note"]))
        self.assertEqual(sections[1], ("[One]", ["a=1"]))
        self.assertEqual(sections[2], ("[Two]", ["b=2"]))


if __name__ == "__main__":
    unittest.main()
