import struct
import sys
import unittest
from binascii import crc32
from unittest.mock import Mock, patch

import device_auth
import dashboard
import online_transport
import server
import setup_dolphin
import setup_online
import setup_retroarch
import systems

if sys.platform.startswith("linux"):
    import ap_helper
    import hotspot
    import uinput_backend
else:
    ap_helper = hotspot = uinput_backend = None


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

    def test_complete_input_schema_rejects_unbounded_or_ambiguous_values(self):
        self.assertTrue(
            server.valid_controller_input(
                {"t": "i", "b": {"cross": True}, "left_x": 0.5,
                 "m": {"ax": 1.0, "rg": 90.0, "accel_polarity": -1}}
            )
        )
        self.assertFalse(server.valid_controller_input({"t": "i", "b": {"cross": "yes"}}))
        self.assertFalse(server.valid_controller_input({"t": "i", "m": {"ax": 1e100}}))
        self.assertFalse(server.valid_controller_input({"t": "i", "unknown": 1}))


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
    def test_turn_urls_expand_into_udp_tcp_tls_retry_attempts(self):
        attempts = online_transport.ice_server_attempts(
            [
                {"urls": ["stun:stun.example:3478"]},
                {
                    "urls": [
                        "turns:turn.example:5349?transport=tcp",
                        "turn:turn.example:3478?transport=tcp",
                        "turn:turn.example:3478?transport=udp",
                    ],
                    "username": "user",
                    "credential": "credential",
                },
            ]
        )
        self.assertEqual(len(attempts), 3)
        self.assertIn("transport=udp", attempts[0][-1]["urls"][0])
        self.assertTrue(attempts[1][-1]["urls"][0].startswith("turn:"))
        self.assertTrue(attempts[2][-1]["urls"][0].startswith("turns:"))
        self.assertTrue(all(attempt[0]["urls"][0].startswith("stun:") for attempt in attempts))

    def test_update_check_is_nonfatal_and_reports_only_newer_versions(self):
        import io

        def response(payload):
            value = Mock()
            value.__enter__ = Mock(return_value=io.BytesIO(payload))
            value.__exit__ = Mock(return_value=False)
            return value

        with patch.object(
            online_transport.urllib.request,
            "urlopen",
            return_value=response(
                b'{"latest_client_version":"0.3.0","download_url":"https://example.test/release"}'
            ),
        ):
            self.assertEqual(
                online_transport.available_update("https://example.test", "0.2.0"),
                ("0.3.0", "https://example.test/release"),
            )
        with patch.object(online_transport.urllib.request, "urlopen", side_effect=OSError("offline")):
            self.assertIsNone(online_transport.available_update("https://example.test"))

    def test_device_credential_is_saved_with_private_permissions(self):
        import stat
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config" / "partypad" / "device_credential.json"
            store = device_auth.PrivateFileCredentialStore(path)
            credential = device_auth.DeviceCredential(
                "token", "device-id", "Test laptop", "2026-10-11T18:00:00Z"
            )
            store.save(credential)
            self.assertEqual(store.load(), credential)
            if sys.platform != "win32":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            store.delete()
            self.assertIsNone(store.load())

    def test_session_creation_uses_device_auth_and_protocol_version(self):
        import io

        response = Mock()
        response.__enter__ = Mock(return_value=io.BytesIO(b'{"id":"i","host_secret":"h",'
                                                         b'"join_url":"j","ws_url":"w",'
                                                         b'"end_url":"e","ice_servers":[]}'))
        response.__exit__ = Mock(return_value=False)
        with patch.object(online_transport.urllib.request, "urlopen", return_value=response) as call:
            result = online_transport.create_session(
                "https://example.test",
                "device-token",
                system="wii",
                system_name="Wii",
                controller_mode="wii",
                backend="dolphin",
            )
        request = call.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Device device-token")
        self.assertEqual(request.get_header("X-partypad-protocol"), "1")
        self.assertEqual(result["id"], "i")

    def test_session_creation_persists_rotated_device_token(self):
        import io

        payload = (
            b'{"id":"i","host_secret":"h","join_url":"j","ws_url":"w",'
            b'"end_url":"e","ice_servers":[],"rotated_device_token":"new-token",'
            b'"device_token_expires_at":"2027-01-01T00:00:00Z"}'
        )
        response = Mock()
        response.__enter__ = Mock(return_value=io.BytesIO(payload))
        response.__exit__ = Mock(return_value=False)
        store = Mock()
        store.load.return_value = device_auth.DeviceCredential(
            "old-token", "device-id", "Test laptop", "2026-01-01T00:00:00Z"
        )
        with (
            patch.object(online_transport.urllib.request, "urlopen", return_value=response),
            patch.object(online_transport, "default_credential_store", return_value=store),
        ):
            online_transport.create_session(
                "https://example.test",
                "old-token",
                system="wii",
                system_name="Wii",
                controller_mode="wii",
                backend="dolphin",
            )
        saved = store.save.call_args.args[0]
        self.assertEqual((saved.token, saved.expires_at), ("new-token", "2027-01-01T00:00:00Z"))

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
        self.assertEqual(peer.last_sequence, 2)

    def test_invalid_sequence_does_not_poison_later_input(self):
        system = Mock(id="wii", controller_mode="wii")
        app = {"hub": server.Hub(), "system": system, "log": None, "uinput": None}
        host = online_transport.OnlineHost(app, {})
        peer = online_transport.OnlinePeer("peer", "a" * 16, 0)
        host.peers[peer.id] = peer

        host._handle_input(peer.id, 2**53, {"t": "i", "left_x": 1})
        host._handle_input(peer.id, 1, {"t": "i", "left_x": 0.25})
        self.assertEqual(peer.last_sequence, 1)
        self.assertEqual(app["hub"].pads[0].left_x, 0.25)


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


class DeviceAuthorizationTests(unittest.TestCase):
    def test_credential_store_falls_back_when_system_keyring_fails(self):
        primary = Mock()
        primary.load.side_effect = RuntimeError("keyring unavailable")
        primary.save.side_effect = RuntimeError("keyring unavailable")
        fallback = Mock()
        credential = device_auth.DeviceCredential("token", "id", "Laptop", "later")
        fallback.load.return_value = credential
        store = device_auth.FallbackCredentialStore(primary, fallback)
        self.assertEqual(store.load(), credential)
        store.save(credential)
        fallback.save.assert_called_once_with(credential)

    def test_verifier_hash_is_urlsafe_sha256(self):
        value = device_auth.verifier_hash("known verifier")
        self.assertNotIn("=", value)
        self.assertEqual(len(value), 43)

    def test_authorize_opens_browser_polls_and_saves(self):
        import contextlib
        import io

        store = Mock()
        flow = {
            "device_code": "device-code",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://auth.example/activate",
            "verification_uri_complete": "https://auth.example/activate?code=ABCD-EFGH",
            "expires_in": 600,
            "interval": 1,
        }
        approved = {
            "status": "authorized",
            "device_token": "token",
            "device": {
                "id": "device-id",
                "name": "Test laptop",
                "expires_at": "2026-10-11T18:00:00Z",
            },
        }
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            patch.object(device_auth, "begin_authorization", return_value=(flow, "verifier")),
            patch.object(device_auth, "poll_authorization", return_value=approved),
        ):
            credential = device_auth.authorize_device(
                "https://example.test",
                device_name="Test laptop",
                store=store,
                open_browser=store.open_browser,
                sleep=lambda _seconds: None,
            )
        store.open_browser.assert_called_once_with(flow["verification_uri_complete"])
        store.save.assert_called_once_with(credential)
        self.assertEqual(credential.token, "token")
        self.assertIn(flow["verification_uri_complete"], output.getvalue())

    def test_authorize_honors_server_slow_down(self):
        store = Mock()
        flow = {
            "device_code": "device-code", "user_code": "ABCD-EFGH",
            "verification_uri": "https://auth.example/activate",
            "expires_in": 600, "interval": 1,
        }
        approved = {
            "status": "authorized", "device_token": "token",
            "device": {"id": "id", "name": "Laptop", "expires_at": "later"},
        }
        sleeps = []
        with (
            patch.object(device_auth, "begin_authorization", return_value=(flow, "verifier")),
            patch.object(
                device_auth,
                "poll_authorization",
                side_effect=[device_auth.PollingTooFast(7), approved],
            ),
        ):
            device_auth.authorize_device(
                "https://example.test", device_name="Laptop", store=store,
                open_browser=lambda _url: None, sleep=sleeps.append,
            )
        self.assertEqual(sleeps, [1, 7])


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_requires_launch_token_and_does_not_expose_secrets(self):
        from aiohttp import CookieJar
        from aiohttp.test_utils import TestClient, TestServer

        store = Mock()
        store.load.return_value = None
        app = dashboard.create_app("https://example.test", "launch-secret")
        client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
        await client.start_server()
        try:
            self.assertEqual((await client.get("/launch/wrong")).status, 403)
            response = await client.get("/launch/launch-secret")
            self.assertEqual(response.status, 200)
            with (
                patch.object(dashboard, "default_credential_store", return_value=store),
                patch.object(dashboard, "dolphin_status", return_value=("not found", None)),
            ):
                status = await (await client.get("/api/status")).json()
                self.assertEqual(status["authorization"], "Not authorized")
                self.assertNotIn("host_secret", status)
                start = await client.post("/session/start", json={"system": "wii"})
                self.assertEqual(start.status, 400)
        finally:
            await client.close()

    async def test_runtime_state_file_is_private_and_bounded(self):
        import stat
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            app = {
                "state_file": path,
                "online_session": {
                    "join_url": "https://example.test/#/join/id/secret",
                    "end_url": "https://example.test/api/sessions/id",
                    "host_secret": "host-secret",
                    "expires_at": "soon",
                },
                "online_host": None,
            }
            server.write_runtime_state(app)
            value = __import__("json").loads(path.read_text())
            self.assertEqual(value["players"], [])
            if sys.platform != "win32":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux uinput backend")
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


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux AP backend")
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

    def test_patchers_are_idempotent_and_preserve_unrelated_sections(self):
        wiimote = "note\n[Wiimote1]\nDevice = Keyboard/0\n[Hotkeys]\nA = B\n"
        dsu = "[Other]\nValue = 1\n[Server]\nEnabled = False\nEntries = old:1:2;partypad:9:9;\n"
        patched_wiimote = setup_dolphin.patched_wiimote_ini(wiimote)
        patched_dsu = setup_dolphin.patched_dsu_ini(dsu)
        self.assertEqual(setup_dolphin.patched_wiimote_ini(patched_wiimote), patched_wiimote)
        self.assertEqual(setup_dolphin.patched_dsu_ini(patched_dsu), patched_dsu)
        self.assertIn("[Hotkeys]\nA = B", patched_wiimote)
        self.assertIn("[Other]\nValue = 1", patched_dsu)
        self.assertEqual(patched_dsu.count(setup_dolphin.SERVER_ENTRY), 1)

    def test_install_and_revert_restore_existing_and_remove_created_files(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "Config"
            config_dir.mkdir()
            wiimote = config_dir / "WiimoteNew.ini"
            wiimote.write_text("[Wiimote1]\nDevice = Keyboard/0\n")
            original = wiimote.read_text()

            setup_dolphin.install(config_dir)
            setup_dolphin.install(config_dir)
            self.assertTrue((config_dir / setup_dolphin.MANIFEST_NAME).exists())
            self.assertTrue((config_dir / "DSUClient.ini").exists())
            self.assertEqual(
                wiimote.with_suffix(".ini" + setup_dolphin.BACKUP_SUFFIX).read_text(), original
            )

            setup_dolphin.revert(config_dir)
            self.assertEqual(wiimote.read_text(), original)
            self.assertFalse((config_dir / "DSUClient.ini").exists())
            self.assertFalse((config_dir / setup_dolphin.MANIFEST_NAME).exists())

    def test_discovery_covers_windows_macos_linux_flatpak_and_custom_paths(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            paths = {
                "win32": home / "AppData" / "Roaming" / "Dolphin Emulator" / "Config",
                "darwin": home
                / "Library"
                / "Application Support"
                / "Dolphin"
                / "Config",
                "linux": home / ".config" / "dolphin-emu",
            }
            for platform, path in paths.items():
                path.mkdir(parents=True)
                found = setup_dolphin.discover_dolphin_dirs(
                    platform=platform,
                    home=home,
                    env={},
                    include_saved=False,
                )
                self.assertIn(path.resolve(), [candidate.config_dir for candidate in found])

            flatpak = (
                home
                / ".var"
                / "app"
                / "org.DolphinEmu.dolphin-emu"
                / "config"
                / "dolphin-emu"
            )
            flatpak.mkdir(parents=True)
            found = setup_dolphin.discover_dolphin_dirs(
                platform="linux", home=home, env={}, include_saved=False
            )
            self.assertIn(flatpak.resolve(), [candidate.config_dir for candidate in found])

            custom = home / "CustomUser"
            found = setup_dolphin.discover_dolphin_dirs(
                platform="linux",
                home=home,
                env={"DOLPHIN_EMU_USERPATH": str(custom)},
                include_saved=False,
            )
            self.assertNotIn(custom / "Config", [candidate.config_dir for candidate in found])
            custom.mkdir()
            (custom / "Config").mkdir()
            found = setup_dolphin.discover_dolphin_dirs(
                platform="linux",
                home=home,
                env={"DOLPHIN_EMU_USERPATH": str(custom)},
                include_saved=False,
            )
            self.assertIn((custom / "Config").resolve(), [candidate.config_dir for candidate in found])

    def test_selection_refuses_multiple_populated_directories(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            candidates = []
            for index in range(2):
                path = Path(tmp) / str(index)
                path.mkdir()
                (path / "Dolphin.ini").write_text("")
                candidates.append(setup_dolphin.DolphinCandidate(path, f"candidate {index}"))
            with self.assertRaisesRegex(ValueError, "multiple Dolphin configurations"):
                setup_dolphin.select_dolphin_dir(candidates)

    def test_process_detection_uses_exact_executable_names(self):
        with patch.object(
            setup_dolphin.subprocess,
            "run",
            return_value=Mock(stdout="notdolphin\ndolphin-emu\n"),
        ):
            self.assertTrue(setup_dolphin.dolphin_is_running("linux"))
        with patch.object(
            setup_dolphin.subprocess,
            "run",
            return_value=Mock(stdout="notdolphin\n"),
        ):
            self.assertFalse(setup_dolphin.dolphin_is_running("linux"))


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
