"""declared-target: the AGENTS.md Contracts table against systemd units, launchd plists,
package.json scripts and manifests."""
import json

from layout_fixture import LayoutCase

UNIT = """[Unit]
Description=Telemetry broker

[Service]
WorkingDirectory=/home/pi/dev/{repo}
ExecStartPre=/usr/bin/test -r /home/pi/dev/{repo}/lib/can_wake.py
ExecStart=/usr/bin/python3 -m projects.vehicle_data.broker serve --socket /run/x.sock
"""
PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>Label</key><string>com.example.panel</string>
<key>ProgramArguments</key><array>
<string>/bin/zsh</string><string>-lc</string><string>/Users/me/dev/repo/macbook/scripts/panel.zsh --serve</string>
</array></dict></plist>
"""
TABLE = "# Repo\n\n## Contracts\n\n| Path | Caller | Owner |\n|---|---|---|\n{rows}\n## Other\n"


def table(*rows):
    return TABLE.format(rows="".join(f"| {p} | {c} | {o} |\n" for p, c, o in rows))


class DeclaredTarget(LayoutCase):
    def unit(self) -> str:
        """Deployed under a folder named like the checkout, as the van's units are."""
        return UNIT.format(repo=self.root.name)

    def tree(self, agents: str = None):
        files = {"projects/__init__.py": "", "projects/vehicle_data/__init__.py": "",
                 "projects/vehicle_data/broker.py": "", "lib/can_wake.py": "",
                 "systemd/van-telemetry.service": self.unit(), "launchd/com.example.panel.plist": PLIST,
                 "macbook/scripts/panel.zsh": "#!/bin/zsh\n", "server/index.mjs": "export {}\n",
                 "package.json": json.dumps({"name": "app", "scripts": {"start": "node server/index.mjs",
                                                                         "build": "vite build",
                                                                         "test": "node --test tests/*.test.mjs"}})}
        if agents:
            files["AGENTS.md"] = agents
        self.write(files)

    def test_without_a_table_targets_are_advisory_listings(self):
        self.tree()
        data = self.check(expect=0)
        found = self.found(data, "declared-target")
        self.assertEqual(sorted(f["path"] for f in found),
                         ["lib/can_wake.py", "macbook/scripts/panel.zsh", "projects/vehicle_data/broker.py",
                          "server/index.mjs"])
        self.assertTrue(all(f["treatment"] == "advisory" for f in found))
        self.assertEqual(data["counts"]["targetFiles"], 3)

    def test_complete_table_passes(self):
        self.tree(table(("`python3 -m projects.vehicle_data.broker`", "van-telemetry.service", "broker.py"),
                        ("`lib/can_wake.py`", "van-telemetry.service ExecStartPre test -r", "lib"),
                        ("`macbook/scripts/panel.zsh`", "launchd com.example.panel", "macbook/scripts"),
                        ("`server/`", "npm start", "server/index.mjs")))
        self.assertEqual(self.found(self.check(expect=0), "declared-target"), [])

    def test_unlisted_target_and_missing_row_block_once_baselined(self):
        self.tree(table(("`projects.vehicle_data.broker`", "van-telemetry.service", "broker.py"),
                        ("`lib/`", "van-telemetry.service prechecks", "lib"),
                        ("`server/`", "npm start", "server/index.mjs"),
                        ("`tools/gone.py`", "cron", "tools")))
        self.baseline_file()
        self.commit()
        self.assertEqual(self.run_checker("rebaseline").returncode, 0)
        data = self.check(expect=1)
        failing = {f["path"]: f["status"] for f in self.found(data, "declared-target")}
        self.assertEqual(failing, {"macbook/scripts/panel.zsh": "failing", "tools/gone.py": "failing"})

    def test_unit_running_a_missing_module_and_rows_without_owner(self):
        self.write({"projects/__init__.py": "", "lib/can_wake.py": "", "systemd/x.service": self.unit(),
                    "AGENTS.md": table(("`lib/can_wake.py`", "x.service", ""))})
        found = {f["path"]: f["message"] for f in self.found(self.check(), "declared-target")}
        self.assertIn("does not exist", found["projects/vehicle_data/broker.py"])
        self.assertIn("names no caller or no owner", found["lib/can_wake.py"])

    def test_working_directory_outside_the_repo_and_deployed_copies(self):
        self.write({"pi/projects/phone-drop/phone_drop.py": "", "pi/projects/cast/src/index.mjs": "",
                    "units/a.service": "[Service]\nWorkingDirectory=/srv/homeassistant\n"
                                       "ExecStart=/srv/homeassistant/bin/hass -c /home/x\n",
                    "units/b.service": "[Service]\nWorkingDirectory=/home/pi/phone-drop\n"
                                       "ExecStart=/usr/bin/python3 -B /home/pi/phone-drop/phone_drop.py --port 1\n",
                    "units/c.service": "[Service]\nWorkingDirectory=/home/pi/cast/current\n"
                                       "ExecStart=/usr/bin/node /home/pi/cast/current/src/index.mjs\n"})
        self.assertEqual(self.paths(self.check(), "declared-target"),
                         ["pi/projects/cast/src/index.mjs", "pi/projects/phone-drop/phone_drop.py"])

    def test_prechecks_and_modules_resolve_through_deployed_copies(self):
        current = "/home/pi/scripts/python-packages/current"
        self.write({"pi/__init__.py": "", "pi/apps/__init__.py": "", "pi/apps/video/__main__.py": "",
                    "pi/apps/video/assets.py": "", "macbook/scripts/sns.sh": "",
                    "units/v.service": f"[Service]\nExecStartPre=/usr/bin/test -r {current}/pi/apps/video/assets.py\n"
                                       "ExecStartPre=/usr/bin/test -r /home/pi/sns.sh\n"
                                       "ExecStartPre=/usr/bin/test -x /usr/bin/vlc\n"
                                       "ExecStart=/usr/bin/python3 -P -m pi.apps.video\nWorkingDirectory=/home/pi\n"})
        data = self.check()
        self.assertEqual(self.paths(data, "declared-target"), ["pi/apps/video/__main__.py", "pi/apps/video/assets.py"])
        self.assertEqual(len(data["dynamicTargets"]), 1)
        self.assertIn("/home/pi/sns.sh", data["dynamicTargets"][0])

    def test_test_files_run_by_a_test_script_are_not_contracts(self):
        self.write({"tools/test-workbench.js": "", "tests/test_intake.rb": "",
                    "package.json": json.dumps({"scripts": {"test": "node tools/test-workbench.js && "
                                                                     "ruby tests/test_intake.rb"}})})
        self.assertEqual(self.found(self.check(), "declared-target"), [])

    def test_dynamic_targets_are_listed_for_a_manual_check(self):
        self.write({"systemd/y.service": "[Service]\nExecStart=${TOOL_DIR}/run.sh --now\n"})
        data = self.check()
        self.assertEqual(self.found(data, "declared-target"), [])
        self.assertEqual(data["counts"]["dynamicTargets"], 1)
        self.assertIn("${TOOL_DIR}/run.sh", data["dynamicTargets"][0])

    def test_manifest_globs_read_json_paths_and_placeholders(self):
        self.write({"tools/fuel.py": "", "lib/reports.py": "",
                    ".van-compute.json": json.dumps({"tasks": [{"source_paths": ["tools/fuel.py", "lib/reports.py"],
                                                                "argv": ["{source:tools/fuel.py}", "{input:0}"],
                                                                "description": "Offline label inventory, no access."}]}),
                    "AGENTS.md": table(("`lib/`", "van-compute tasks", "lib"))})
        self.config({"targets": ["**/*.service", ".van-compute.json"]})
        self.assertEqual(self.paths(self.check(), "declared-target"), ["tools/fuel.py"])
