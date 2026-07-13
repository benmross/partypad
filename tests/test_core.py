import struct
import unittest
from binascii import crc32
from unittest.mock import Mock, patch

import ap_helper
import hotspot
import online_transport
import server
import setup_dolphin
import setup_online
import setup_retroarch
import systems
import uinput_backend


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

    def test_malformed_or_nonfinite_input_is_ignored(self):
        pad = server.PadState(0)
        pad.update_from_json(
            {
                "left_x": "not-a-number",
                "left_y": float("nan"),
                "px": float("inf"),
                "m": {"ax": "bad", "ay": None, "az": float("-inf")},
            }
        )
        self.assertEqual((pad.left_x, pad.left_y, pad.right_x), (0.0, 0.0, 0.0))
        self.assertEqual(pad.accel, (0.0, 0.0, -0.0))


class PlayerSlotTests(unittest.TestCase):
    def test_disconnected_slot_is_reserved_for_same_browser(self):
        hub = server.Hub()
        sockets = [Mock() for _ in range(3)]
        for index, client_id in enumerate(("a" * 16, "b" * 16, "c" * 16)):
            self.assertEqual(hub.claim_slot(client_id, now=0), index)
            hub.connection_by_slot[index] = sockets[index]

        self.assertTrue(hub.release_slot(1, "b" * 16, sockets[1], now=10))
        self.assertEqual(hub.claim_slot("b" * 16, now=20), 1)

    def test_new_browser_does_not_steal_slot_during_grace_period(self):
        hub = server.Hub()
        ws = Mock()
        self.assertEqual(hub.claim_slot("a" * 16, now=0), 0)
        hub.connection_by_slot[0] = ws
        hub.release_slot(0, "a" * 16, ws, now=10)
        self.assertEqual(hub.claim_slot("b" * 16, now=20), 1)
        self.assertEqual(hub.claim_slot("c" * 16, now=41), 0)

    def test_stale_socket_cannot_release_reclaimed_slot(self):
        hub = server.Hub()
        old_ws, new_ws = Mock(), Mock()
        client_id = "a" * 16
        slot = hub.claim_slot(client_id, now=0)
        hub.connection_by_slot[slot] = old_ws
        hub.release_slot(slot, client_id, old_ws, now=1)
        self.assertEqual(hub.claim_slot(client_id, now=2), slot)
        hub.connection_by_slot[slot] = new_ws
        self.assertFalse(hub.release_slot(slot, client_id, old_ws, now=3))
        self.assertTrue(hub.pads[slot].connected)

    def test_same_browser_reclaims_slot_before_old_socket_exits(self):
        hub = server.Hub()
        client_id = "a" * 16
        self.assertEqual(hub.claim_slot(client_id, now=0), 0)
        self.assertEqual(hub.claim_slot(client_id, now=1), 0)


class OnlineTransportTests(unittest.TestCase):
    def test_online_token_is_saved_with_private_permissions(self):
        import stat
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config" / "partypad" / "host_token"
            setup_online.save_token(path, "secret")
            self.assertEqual(path.read_text(), "secret\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_relay_input_drops_duplicates_and_stale_packets(self):
        system = Mock(id="wii", controller_mode="wii")
        app = {"hub": server.Hub(), "system": system, "log": None, "uinput": None}
        host = online_transport.OnlineHost(app, {})
        peer = online_transport.OnlinePeer("peer", "a" * 16, 0)
        host.peers[peer.id] = peer

        host._handle_input(peer.id, 2, {"t": "i", "b": {"cross": True}})
        host._handle_input(peer.id, 1, {"t": "i", "b": {"cross": False}})
        self.assertTrue(app["hub"].pads[0].buttons["cross"])
        self.assertEqual(peer.last_sequence, 2)

    def test_malformed_relay_input_does_not_break_later_input(self):
        system = Mock(id="wii", controller_mode="wii")
        app = {"hub": server.Hub(), "system": system, "log": None, "uinput": None}
        host = online_transport.OnlineHost(app, {})
        peer = online_transport.OnlinePeer("peer", "a" * 16, 0)
        host.peers[peer.id] = peer

        host._handle_input(peer.id, 1, {"t": "i", "left_x": object()})
        host._handle_input(peer.id, 2, {"t": "i", "left_x": 0.5})
        self.assertEqual(app["hub"].pads[0].left_x, 0.5)


class OnlineCandidateTests(unittest.IsolatedAsyncioTestCase):
    async def test_candidate_is_queued_until_peer_connection_exists(self):
        host = online_transport.OnlineHost({}, {})
        peer = online_transport.OnlinePeer("peer", "a" * 16, 0)
        host.peers[peer.id] = peer
        candidate = {
            "candidate": "candidate:1 1 UDP 1 192.0.2.1 1234 typ host",
            "sdpMid": "0",
            "sdpMLineIndex": 0,
        }

        await host._handle_candidate(peer.id, candidate)

        self.assertEqual(peer.pending_candidates, [candidate])

    async def test_malformed_candidate_is_not_queued(self):
        host = online_transport.OnlineHost({}, {})
        peer = online_transport.OnlinePeer("peer", "a" * 16, 0)
        host.peers[peer.id] = peer

        await host._handle_candidate(peer.id, "not-an-object")

        self.assertEqual(peer.pending_candidates, [])


class UInputBackendTests(unittest.TestCase):
    def test_axis_value_is_clamped_and_signed(self):
        self.assertEqual(uinput_backend.axis_value(-2), uinput_backend.AXIS_MIN)
        self.assertEqual(uinput_backend.axis_value(0), 0)
        self.assertEqual(uinput_backend.axis_value(2), uinput_backend.AXIS_MAX)

    def test_pad_events_map_nes_buttons_and_hat(self):
        pad = server.PadState(0)
        pad.update_from_json(
            {"b": {"cross": True, "square": True, "dpad_up": True, "dpad_left": True}}
        )
        events = uinput_backend.pad_events(pad)
        e = uinput_backend.ecodes
        self.assertEqual(events[(e.EV_KEY, e.BTN_SOUTH)], 1)
        self.assertEqual(events[(e.EV_KEY, e.BTN_WEST)], 1)
        self.assertEqual(events[(e.EV_ABS, e.ABS_HAT0X)], -1)
        self.assertEqual(events[(e.EV_ABS, e.ABS_HAT0Y)], -1)

    def test_uinput_pad_only_emits_changes_and_neutralizes(self):
        device = Mock()
        output = uinput_backend.UInputPad(0, device)
        pad = server.PadState(0)
        output.update(pad)
        first_count = device.write.call_count
        output.update(pad)
        self.assertEqual(device.write.call_count, first_count)
        pad.update_from_json({"b": {"cross": True}})
        output.update(pad)
        self.assertEqual(device.write.call_count, first_count + 1)
        output.neutralize()
        self.assertEqual(device.write.call_args.args[-1], 0)
        self.assertGreaterEqual(device.syn.call_count, 3)


class SystemSelectionTests(unittest.TestCase):
    def setUp(self):
        self.parser = server.build_parser()

    def test_supported_systems_select_their_backend_and_controller_mode(self):
        nes, nes_backend = server.resolve_system(self.parser.parse_args(["--system", "nes"]))
        wii, wii_backend = server.resolve_system(self.parser.parse_args(["--system", "wii"]))
        self.assertEqual((nes.controller_mode, nes_backend), ("nes", "retroarch"))
        self.assertEqual((wii.controller_mode, wii_backend), ("wii", "dolphin"))

    def test_interactive_setup_accepts_number_or_id(self):
        output = []
        selected = server.resolve_system(
            self.parser.parse_args([]), input_func=lambda _: "1", output=output.append
        )[0]
        self.assertIn(selected, systems.SUPPORTED_SYSTEMS)
        self.assertTrue(any("choose the system" in line for line in output))

    def test_planned_system_is_known_but_rejected_honestly(self):
        args = self.parser.parse_args(["--system", "snes"])
        with self.assertRaisesRegex(ValueError, "registered for future support"):
            server.resolve_system(args)

    def test_legacy_backend_still_selects_corresponding_system(self):
        system, backend = server.resolve_system(self.parser.parse_args(["--backend", "retroarch"]))
        self.assertEqual((system.id, backend), ("nes", "retroarch"))

    def test_every_requested_future_system_is_registered(self):
        requested = {
            "amiga",
            "amstradcpc",
            "arcade",
            "atari2600",
            "atari5200",
            "atari7800",
            "atarilynx",
            "bbcmicro",
            "c64",
            "coleco",
            "cps",
            "daphne",
            "doom",
            "dosbox",
            "fba",
            "fds",
            "gamegear",
            "gb",
            "gba",
            "gbc",
            "gw",
            "intelli",
            "mastersystem",
            "megadrive",
            "msx",
            "neogeo",
            "ngp",
            "pcecd",
            "pcengine",
            "pico8",
            "pokemini",
            "psx",
            "quake",
            "scummvm",
            "sega32x",
            "segacd",
            "sg-1000",
            "snes",
            "supervision",
            "test",
            "tic80",
            "vb",
            "wsc",
            "zx",
        }
        self.assertTrue(requested.issubset(systems.SYSTEMS))


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


class RetroArchConfigTests(unittest.TestCase):
    def test_installer_is_reversible(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "udev" / "PartyPad Controller.cfg"
            setup_retroarch.install(destination)
            self.assertEqual(destination.read_bytes(), setup_retroarch.SOURCE.read_bytes())
            setup_retroarch.revert(destination)
            self.assertFalse(destination.exists())

    def test_installer_preserves_preexisting_profile(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "PartyPad Controller.cfg"
            destination.write_text("user profile\n")
            setup_retroarch.install(destination)
            setup_retroarch.revert(destination)
            self.assertEqual(destination.read_text(), "user profile\n")


if __name__ == "__main__":
    unittest.main()
