"""Verification test suite for pywings matching Wings functionality."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from wings import create_app
from wings.config import Settings
from wings.events import bus, ConsoleOutputEvent, InstallCompletedEvent, InstallStartedEvent, StatusEvent
from wings.parser import ConfigParser
from wings.processes import OutputLineMatcher, ProcessManager, STATE_OFFLINE, STATE_RUNNING, STATE_STARTING, STATE_STOPPING
from wings.remote import PanelRemoteClient
from wings.servers import ServerRecord, ServerStore


class OutputLineMatcherTest(unittest.TestCase):
    def test_plain_string_match(self):
        matcher = OutputLineMatcher("Done (2.5s)! For help, type")
        self.assertTrue(matcher.matches("[Server thread/INFO]: Done (2.5s)! For help, type \"help\""))
        self.assertFalse(matcher.matches("Loading libraries, please wait..."))

    def test_regex_match(self):
        matcher = OutputLineMatcher(r"regex:^\[.*\]: Done \([0-9.]+s\)!.*")
        self.assertTrue(matcher.matches("[Server thread/INFO]: Done (2.54s)! For help, type \"help\""))
        self.assertFalse(matcher.matches("Done without prefix"))


class ConfigParserTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_properties_update(self):
        props_file = self.root / "server.properties"
        props_file.write_text("server-port=25565\nserver-ip=127.0.0.1\nmotd=A Minecraft Server\n", encoding="utf-8")

        config = {
            "process_configuration": {
                "configs": [
                    {
                        "file": "server.properties",
                        "parser": "properties",
                        "replace": [
                            {"match": "server-port", "replace_with": "{{SERVER_PORT}}"},
                            {"match": "server-ip", "replace_with": "0.0.0.0"},
                        ],
                    }
                ]
            }
        }
        parser = ConfigParser(self.root, config, {"SERVER_PORT": "25577"})
        parser.update_configuration_files()

        updated = props_file.read_text(encoding="utf-8")
        self.assertIn("server-port=25577", updated)
        self.assertIn("server-ip=0.0.0.0", updated)
        self.assertIn("motd=A Minecraft Server", updated)

    def test_json_update(self):
        json_file = self.root / "config.json"
        json_file.write_text(json.dumps({"server": {"port": 80, "host": "127.0.0.1"}}), encoding="utf-8")

        config = {
            "process_configuration": {
                "configs": [
                    {
                        "file": "config.json",
                        "parser": "json",
                        "replace": [
                            {"match": "server.port", "replace_with": "{{SERVER_PORT}}"},
                            {"match": "server.host", "replace_with": "0.0.0.0"},
                        ],
                    }
                ]
            }
        }
        parser = ConfigParser(self.root, config, {"SERVER_PORT": "8080"})
        parser.update_configuration_files()

        data = json.loads(json_file.read_text(encoding="utf-8"))
        self.assertEqual(data["server"]["port"], 8080)
        self.assertEqual(data["server"]["host"], "0.0.0.0")


class ProcessLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = ServerStore(self.temp_dir.name)
        self.mock_runtime = MagicMock()
        self.mock_remote = MagicMock()
        self.manager = ProcessManager(
            self.store,
            self.mock_runtime,
            allowed_mounts=(),
            remote_client=self.mock_remote,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_startup_invocation_ignores_dict_in_process_configuration(self):
        config = {
            "invocation": "java -Xms128M -jar {{SERVER_JARFILE}}",
            "process_configuration": {
                "startup": {
                    "done": [")! For help, type"],
                    "user_interaction": [],
                    "strip_ansi": False,
                }
            },
        }
        cmd = ProcessManager._startup(config, {"SERVER_JARFILE": "server.jar"})
        self.assertEqual(cmd, "java -Xms128M -jar server.jar")
        self.assertNotIn("{done:", cmd)

    def test_install_lifecycle_notifies_panel(self):
        server_uuid = "11111111-1111-4111-8111-111111111111"
        self.store.add(ServerRecord(uuid=server_uuid, configuration={"uuid": server_uuid, "image": "alpine"}))

        mock_process = MagicMock()
        mock_process.stdout = ["Cloning repo...\n", "Installing dependencies...\n"]
        mock_process.wait.return_value = 0
        self.mock_runtime.start_async.return_value = mock_process

        self.mock_remote.get_installation_script.return_value = {
            "container_image": "alpine",
            "entrypoint": "sh",
            "script": "echo install",
        }

        # Run install synchronously for test
        self.manager._run_install(
            server_uuid,
            {"uuid": server_uuid, "image": "alpine"},
            reinstall=False,
            start_on_completion=False,
        )

        # 1. Panel was notified of successful install!
        self.mock_remote.set_installation_status.assert_called_once_with(
            server_uuid, successful=True, reinstall=False
        )

        # 2. Local state is offline!
        server = self.store.get(server_uuid)
        self.assertEqual(server.state, STATE_OFFLINE)

        # 3. Verified start_async arguments: runs shell with smart fallback
        call_args = self.mock_runtime.start_async.call_args
        self.assertEqual(call_args.args[1][0], "/bin/sh")
        self.assertEqual(call_args.args[1][1], "-c")
        self.assertIn("/mnt/install/install.sh", call_args.args[1][2])
        self.assertEqual(call_args.kwargs.get("entrypoint"), "")

    def test_start_already_running_does_not_mark_offline(self):
        server_uuid = "22222222-2222-4222-8222-222222222222"
        self.store.add(ServerRecord(uuid=server_uuid, configuration={"uuid": server_uuid, "image": "alpine"}, state=STATE_RUNNING))

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # Running
        self.manager._processes[server_uuid] = mock_proc

        # Calling start again should NOT throw an error or mark it offline
        self.manager.start(server_uuid, {"uuid": server_uuid, "image": "alpine"})

        server = self.store.get(server_uuid)
        self.assertEqual(server.state, STATE_RUNNING)

    def test_state_transitions_on_boot_and_stop(self):
        server_uuid = "33333333-3333-4333-8333-333333333333"
        config = {
            "uuid": server_uuid,
            "container": {"image": "alpine"},
            "process_configuration": {
                "startup": {"done": ["Server marked as done!"]},
                "stop": {"value": "stop"},
            },
        }
        self.store.add(ServerRecord(uuid=server_uuid, configuration=config, state=STATE_OFFLINE))

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdout = ["[INFO] Starting server...\n", "[INFO] Server marked as done!\n"]
        mock_proc.wait.return_value = 0
        mock_proc.stdin = MagicMock()
        self.mock_runtime.start_async.return_value = mock_proc

        events_received = []
        bus.add_callback(server_uuid, lambda evt, args: events_received.append((evt, args)))

        # Start server
        self.manager.start(server_uuid, config)
        self.assertEqual(self.store.get(server_uuid).state, STATE_STARTING)

        # Run watch (stdout processing)
        self.manager._watch(server_uuid, mock_proc, config)

        # After done pattern, was running, and upon exit, went offline
        self.assertEqual(self.store.get(server_uuid).state, STATE_OFFLINE)

        # Verify events emitted
        status_events = [args[0] for evt, args in events_received if evt == StatusEvent]
        self.assertIn(STATE_STARTING, status_events)
        self.assertIn(STATE_RUNNING, status_events)
        self.assertIn(STATE_OFFLINE, status_events)

    def test_crash_triggers_auto_restart_worker(self):
        server_uuid = "77777777-7777-4777-8777-777777777777"
        config = {
            "uuid": server_uuid,
            "container": {"image": "alpine"},
            "invocation": "echo run",
            "crash_detection_enabled": True,
        }
        self.store.add(ServerRecord(uuid=server_uuid, configuration=config, state=STATE_RUNNING))

        mock_proc = MagicMock()
        mock_proc.stdout = []
        mock_proc.wait.return_value = 1  # Non-zero exit -> crash!
        self.manager._processes[server_uuid] = mock_proc

        with patch.object(self.manager, "_auto_restart_worker") as mock_worker:
            self.manager._watch(server_uuid, mock_proc, config)
            mock_worker.assert_called_once_with(server_uuid, config)


class SFTPInterfaceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "sub").mkdir(parents=True, exist_ok=True)
        (self.root / "file.txt").write_text("hello sftp", encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_sftp_list_and_read(self):
        from wings.sftp import PteroSFTPInterface
        sftp_iface = PteroSFTPInterface(self.root, ["file.read", "file.read-content"])
        files = sftp_iface.list_folder("/")
        self.assertIsInstance(files, list)
        names = [f.filename for f in files]
        self.assertIn("file.txt", names)
        self.assertIn("sub", names)

    def test_sftp_traversal_blocked(self):
        from wings.sftp import PteroSFTPInterface, paramiko
        sftp_iface = PteroSFTPInterface(self.root, ["*"])
        res = sftp_iface.stat("../../etc/passwd")
        self.assertEqual(res, paramiko.SFTP_PERMISSION_DENIED)


class ApiRoutesTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        settings = Settings(
            token="test-token",
            token_id="test-token-id",
            uuid="node-uuid",
            data_directory=self.temp_dir.name,
            remote="http://test-panel",
        )
        self.app = create_app(settings)
        self.client = self.app.test_client()
        self.auth_headers = {"Authorization": "Bearer test-token"}

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_create_server_triggers_install(self):
        server_uuid = "44444444-4444-4444-8444-444444444444"
        with patch.object(self.app.extensions["process_manager"], "install") as mock_install:
            res = self.client.post(
                "/api/servers",
                headers=self.auth_headers,
                json={
                    "uuid": server_uuid,
                    "start_on_completion": True,
                    "container": {"image": "ghcr.io/pterodactyl/yolks:java_17"},
                },
            )
            self.assertEqual(res.status_code, 202)
            mock_install.assert_called_once()
            args, kwargs = mock_install.call_args
            self.assertEqual(args[0], server_uuid)
            self.assertTrue(kwargs.get("start_on_completion"))

    def test_server_power_route(self):
        server_uuid = "55555555-5555-4555-8555-555555555555"
        self.app.extensions["server_store"].add(
            ServerRecord(uuid=server_uuid, configuration={"uuid": server_uuid, "image": "alpine"})
        )
        with patch.object(self.app.extensions["process_manager"], "start") as mock_start:
            res = self.client.post(
                f"/api/servers/{server_uuid}/power",
                headers=self.auth_headers,
                json={"action": "start"},
            )
            self.assertEqual(res.status_code, 202)

    def test_delete_transfer_route(self):
        server_uuid = "66666666-6666-4666-8666-666666666666"
        res = self.client.delete(
            f"/api/transfers/{server_uuid}",
            headers=self.auth_headers,
        )
        self.assertEqual(res.status_code, 204)


if __name__ == "__main__":
    unittest.main()
