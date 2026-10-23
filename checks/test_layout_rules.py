"""The ratcheted structure rules on fixture trees: contract, prefix-group, entry-heavy, kind-folder,
cycle, plus source roots, aliases and excluded output."""
import json

from layout_fixture import LayoutCase, python_module


class Contract(LayoutCase):
    def test_import_across_declared_boundary_is_found(self):
        self.write({"lib/__init__.py": "", "lib/store.py": "from app import web\n",
                    "app/__init__.py": "", "app/web.py": "from lib import store\n"})
        self.config({"contracts": [{"from": "lib", "to": ["app"]}]})
        found = self.found(self.check(expect=0), "contract")
        self.assertEqual([f["key"] for f in found], ["lib/store.py -> app/web.py"])
        self.assertEqual(found[0]["line"], 1)

    def test_allowed_direction_and_lazy_imports_with_allow_dynamic_pass(self):
        self.write({"lib/__init__.py": "", "lib/store.py": "def run():\n    from app import web\n",
                    "app/__init__.py": "", "app/web.py": "from lib import store\n"})
        self.config({"contracts": [{"from": "lib", "to": ["app"], "allowDynamic": True}]})
        self.assertEqual(self.found(self.check(), "contract"), [])

    def test_lazy_import_counts_without_allow_dynamic(self):
        self.write({"lib/__init__.py": "", "lib/store.py": "def run():\n    from app import web\n",
                    "app/__init__.py": "", "app/web.py": ""})
        self.config({"contracts": [{"from": "lib", "to": ["app"]}]})
        self.assertEqual(len(self.found(self.check(), "contract")), 1)

    def test_star_contract_and_js_alias(self):
        self.write({"server/dev/panel.ts": "export const x = 1\n",
                    "server/http.ts": "import { x } from '~/dev/panel'\nexport const y = x\n"})
        self.config({"contracts": [{"from": "*", "to": ["server/dev"]}], "aliases": {"~/": "server/"}})
        data = self.check()
        self.assertEqual([f["key"] for f in self.found(data, "contract")], ["server/http.ts -> server/dev/panel.ts"])
        self.assertEqual(data["counts"]["importsUnsupported"], 0)

    def test_unconfigured_alias_is_counted_not_guessed(self):
        self.write({"server/dev/panel.ts": "export const x = 1\n",
                    "server/http.ts": "import { x } from '~/dev/panel'\nexport const y = x\n"})
        self.config({"contracts": [{"from": "*", "to": ["server/dev"]}]})
        data = self.check()
        self.assertEqual(self.found(data, "contract"), [])
        self.assertEqual(data["counts"]["importsUnsupported"], 1)

    def test_contract_duplicating_react_structure_is_skipped_with_warning(self):
        self.write({"src/components/Card.ts": "import { p } from '../pages/Home'\n",
                    "src/pages/Home.ts": "export const p = 1\n"})
        self.config({"contracts": [{"from": "src/components", "to": ["src/pages"]}]},
                    structure={"sourceRoot": "src"})
        data = self.check()
        self.assertEqual(self.found(data, "contract"), [])
        self.assertTrue(any("duplicates a react-app structure contract" in w for w in data["warnings"]))


class PrefixGroup(LayoutCase):
    def test_three_prefixed_siblings_without_folder(self):
        self.write({"tools/radar_align.py": "", "tools/radar_steps.py": "from radar_align import *\n",
                    "tools/radar_ref.py": "", "tools/broker.py": "import radar_ref\n"})
        found = self.found(self.check(), "prefix-group")
        self.assertEqual(sorted(f["path"] for f in found),
                         ["tools/radar_align.py", "tools/radar_ref.py", "tools/radar_steps.py"])
        self.assertEqual({f["group"] for f in found}, {"tools/radar*"})
        self.assertIn("1 import(s) within the group", found[0]["message"])
        self.assertIn("1 outside caller(s)", found[0]["message"])

    def test_hyphen_prefixes_count_and_two_files_do_not(self):
        self.write({"server/autofill-salary.ts": "", "server/autofill-name.ts": "", "server/autofill-cv.ts": "",
                    "server/gmail-send.ts": "", "server/gmail-read.ts": ""})
        self.assertEqual({f["group"] for f in self.found(self.check(), "prefix-group")}, {"server/autofill*"})

    def test_existing_folder_named_for_the_prefix_passes(self):
        self.write({"tools/radar_a.py": "", "tools/radar_b.py": "", "tools/radar_c.py": "",
                    "tools/radar/__init__.py": ""})
        self.assertEqual(self.found(self.check(), "prefix-group"), [])

    def test_bare_stem_joins_and_folders_carrying_the_prefix_are_skipped(self):
        self.write({"vd/api.py": "", "vd/api_client.py": "", "vd/api_schema.py": "",
                    "pi/backup/backup_a.sh": "", "pi/backup/backup_b.sh": "", "pi/backup/backup_c.sh": "",
                    "server/task-kinds/task-a.ts": "", "server/task-kinds/task-b.ts": "",
                    "server/task-kinds/task-c.ts": ""})
        self.assertEqual(self.paths(self.check(), "prefix-group"), ["vd/api.py", "vd/api_client.py", "vd/api_schema.py"])

    def test_test_files_are_not_a_group(self):
        self.write({"tests/test_a.py": "", "tests/test_b.py": "", "tests/test_c.py": ""})
        self.assertEqual(self.found(self.check(), "prefix-group"), [])


class EntryHeavy(LayoutCase):
    def test_long_module_with_main_guard(self):
        self.write({"tools/scan.py": python_module(230, main_guard=True), "tools/lib.py": python_module(230)})
        self.assertEqual(self.paths(self.check(), "entry-heavy"), ["tools/scan.py"])

    def test_js_and_go_entries(self):
        guard = "if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main()\n"
        self.write({"scripts/serve.mjs": "const a = 1\n" * 210 + guard,
                    "cmd/tool/main.go": "package main\n" + "// line\n" * 210})
        self.assertEqual(self.paths(self.check(), "entry-heavy"), ["cmd/tool/main.go", "scripts/serve.mjs"])

    def test_thin_wrapper_and_listed_standalone_tool_pass(self):
        self.write({"historian.py": '"""Compatibility path: owner history/service.py; permanent."""\n'
                                    "from history.service import main\n\nif __name__ == '__main__':\n"
                                    "    raise SystemExit(main())\n",
                    "tools/standalone.py": python_module(300, main_guard=True)})
        self.config({"entries": ["tools/standalone.py"]})
        self.assertEqual(self.found(self.check(), "entry-heavy"), [])


class KindFolder(LayoutCase):
    def test_kind_named_folders_and_sh_holding_python(self):
        self.write({"lib/utils/text.py": "", "app/helpers/x.ts": "export {}\n", "shared/sh/tool.py": "",
                    "shared/sh/run.sh": "#!/bin/sh\n", "scripts/sh/only.sh": "#!/bin/sh\n", "lib/text/ok.py": ""})
        self.assertEqual(self.paths(self.check(), "kind-folder"), ["app/helpers", "lib/utils", "shared/sh"])

    def test_documented_compatibility_tree_as_exception(self):
        self.write({"shared/sh/tool.py": ""})
        self.config({"exceptions": [{"rule": "kind-folder", "path": "shared/sh",
                                     "reason": "documented compatibility layout; extended through its owners"}]})
        data = self.check()
        self.assertEqual([f["status"] for f in self.found(data, "kind-folder")], ["exempt"])
        self.assertEqual(data["counts"]["exceptionsApplied"], 1)


class Cycle(LayoutCase):
    def test_python_cycle_ratchets_and_js_cycle_is_advisory(self):
        self.write({"pkg/__init__.py": "", "pkg/a.py": "from . import b\n", "pkg/b.py": "from . import a\n",
                    "web/a.mjs": "import './b.mjs'\n", "web/b.mjs": "import './a.mjs'\n"})
        found = {f["key"]: f["treatment"] for f in self.found(self.check(), "cycle")}
        self.assertEqual(found, {"pkg/a.py | pkg/b.py": "ratchet", "web/a.mjs | web/b.mjs": "advisory"})

    def test_function_level_and_type_checking_imports_break_no_cycle(self):
        self.write({"pkg/__init__.py": "", "pkg/a.py": "from . import b\n",
                    "pkg/b.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from . import a\n"
                                "def f():\n    from . import a\n"})
        self.assertEqual(self.found(self.check(), "cycle"), [])


class Scope(LayoutCase):
    def test_nested_source_roots_scan_each_file_once(self):
        self.write({"services/api/server.py": "", "services/api/workers/job.py": "", "other/x.py": ""})
        self.config({"sourceRoots": ["services/api", "services/api/workers"]})
        self.assertEqual(self.check()["counts"]["filesScanned"], 2)

    def test_build_output_vendor_and_configured_exclusions_are_skipped(self):
        self.write({"app/main.py": "", "dist/bundle.js": "", "node_modules/x/index.js": "",
                    "build/lib/y.py": "", "vendor/z.py": "", "tmp/scratch.py": ""})
        self.config({"exclude": ["tmp"]})
        self.assertEqual(self.check()["counts"]["filesScanned"], 1)

    def test_nested_react_app_is_left_to_react_app(self):
        self.write({"server/main.py": "", "dashboard/package.json": '{"dependencies": {"preact": "10"}}',
                    "dashboard/src/utils/format.js": "export const f = 1\n", "web/App.tsx": "export {}\n"})
        data = self.check()
        self.assertEqual(self.found(data, "kind-folder"), [])
        self.assertEqual((data["counts"]["filesScanned"], data["counts"]["skippedUi"]), (1, 2))

    def test_extensionless_scripts_are_classified_by_shebang(self):
        self.write({"bin/diskctl": "#!/bin/bash\necho hi\n", "bin/notes": "plain text\n"})
        self.assertEqual(self.check()["counts"]["byKind"], {"shell": 1})


class FixtureIsolation(LayoutCase):
    def test_gitless_fixture_inside_ignored_parent_repo_is_scanned(self):
        self.write({".gitignore": "tmp/\n", "tmp/fixture/app.py": "value = 1\n"})
        self.git("init", "-q")
        result = self.run_checker("--json", root=self.root / "tmp/fixture")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["counts"]["filesScanned"], 1)
