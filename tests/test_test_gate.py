"""Exercise CI lane selection without executing the expensive gate commands."""

import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "support", Path(__file__).with_name("support.py")
)
support = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(support)

ROOT = support.ROOT
SCRIPT = ROOT / "scripts/test.sh"


def gates_job():
    return support.job("gates")


def lanes():
    """Each lane's matrix entry, as a dict of its keys."""
    tail = support.section(gates_job(), "        include:\n", "\0", "matrix include")
    include = []
    for line in tail.splitlines():
        if not line.startswith("          "):
            break
        include.append(line)
    include = "\n".join(include)
    entries = {}
    for entry in include.split("          - ")[1:]:
        values = dict(re.findall(r"(\w+): (.+)", entry))
        entries[values["lane"]] = values
    return entries


def function_body(name):
    source = SCRIPT.read_text()
    return support.section(source, f"\n{name}()\n{{\n", "\n}\n", f"function {name}")


def condition(text, name):
    match = re.search(r"^        if: (.+)$", support.step(text, name), re.M)
    return match[1] if match else ""


class TestGateLanes(unittest.TestCase):
    def run_lane(
        self,
        *args,
        fail="",
        extra_gate=False,
        skip="",
        ci=False,
        unset_cxx=False,
        replace=(),
        env=None,
    ):
        source = SCRIPT.read_text()
        for old, new in replace:
            self.assertEqual(source.count(old), 1, old)
            source = source.replace(old, new)
        # Keep the real dispatch and failure aggregation; substitute only the
        # gate payload so the test needs neither compilers nor downloaded assets.
        self.assertEqual(source.count('if "$@"; then'), 1)
        source = source.replace('if "$@"; then', 'if probe "$name"; then')
        source = source.replace(
            "gate()\n",
            """probe()
{
    echo "gate:$1"
    case " $PROBE_SKIP " in
        *" $1 "*) skip "$1" ;;
    esac
    case " $PROBE_FAIL " in
        *" $1 "*) return 1 ;;
    esac
}

gate()
""",
            1,
        )
        if extra_gate:
            source = source.replace("gate fmt ", "gate future-check true\ngate fmt ", 1)
        env = {**os.environ, "CXX": "unused", **(env or {})}
        if unset_cxx:
            del env["CXX"]
        result = subprocess.run(
            ["sh", "-c", source, str(SCRIPT), *args],
            env={**env, "PROBE_FAIL": fail, "PROBE_SKIP": skip, "CI": str(ci).lower()},
            capture_output=True,
            text=True,
            check=False,
        )
        gates = [
            line[len("gate:") :]
            for line in result.stdout.splitlines()
            if line.startswith("gate:")
        ]
        return result, gates

    def run_browser_tests(self, root, tier, *, body=None, path=None):
        """Run test.sh's browser_tests on root, then print what it skipped."""
        body = function_body("browser_tests") if body is None else body
        script = (
            f"ROOT={shlex.quote(str(root))}\nskipped=\n"
            f"browser_tests() {{\n{body}\n}}\n"
            f'browser_tests {tier}\nstatus=$?\nprintf "%s" "$skipped"\nexit "$status"\n'
        )
        env = {**os.environ, "CI": "false"}
        if path:
            env["PATH"] = f"{path}{os.pathsep}{env['PATH']}"
        return subprocess.run(
            ["sh", "-c", script],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def reported_skips(self, label, stderr):
        """What test.sh adds to its summary for a command printing stderr."""
        pattern = re.search(
            rf"with_reported_skips {label}\s*\\?\s*'([^']*)'", SCRIPT.read_text()
        )
        self.assertIsNotNone(pattern, label)
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "stderr"
            log.write_text(stderr)
            script = (
                f"skipped=\nwith_reported_skips() {{\n"
                f"{function_body('with_reported_skips')}\n}}\n"
                f"with_reported_skips {label} {shlex.quote(pattern[1])} "
                f"sh -c 'cat \"$1\" >&2' - {shlex.quote(str(log))} 2>/dev/null\n"
                'printf "%s" "$skipped"\n'
            )
            result = subprocess.run(
                ["sh", "-c", script], capture_output=True, text=True, check=True
            )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def test_parallel_lanes_cover_the_default_gate(self):
        names = list(lanes())
        self.assertEqual(set(names), {"rust", "portable", "browser"})
        self.assertIn("      fail-fast: false\n", gates_job())
        results = {lane: self.run_lane(lane) for lane in ("all", *names)}
        for result, gates in results.values():
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertGreater(len(gates), 0)
            self.assertEqual(len(gates), len(set(gates)))
        full, rust, portable, browser = (set(results[lane][1]) for lane in results)
        shared = {"fetch-vendor", "verify-vendor"}
        self.assertEqual(rust & portable, shared)
        self.assertEqual(
            rust - shared, {"clippy", "cargo-test", "browser-server-tests"}
        )
        self.assertEqual(rust & browser, shared)
        self.assertEqual(portable & browser, shared)
        self.assertEqual(browser - shared, {"browser-tests"})
        self.assertEqual(rust | portable | browser, full)
        self.assertEqual(self.run_lane()[1], results["all"][1])

    def test_new_gate_is_included_in_portable_lane(self):
        self.assertIn("future-check", self.run_lane("portable", extra_gate=True)[1])
        self.assertNotIn("future-check", self.run_lane("rust", extra_gate=True)[1])

    def test_failure_does_not_hide_later_gates(self):
        for lane, failures in (
            ("rust", "clippy cargo-test"),
            ("portable", "fmt indent"),
        ):
            with self.subTest(lane=lane):
                result, gates = self.run_lane(lane, fail=failures)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"failed gates: {failures}", result.stderr)
                self.assertIn(
                    "cargo-test" if lane == "rust" else "report-parity-fixtures", gates
                )

    def test_browser_suites_run_once_with_their_build_dependencies(self):
        selected = {}
        with tempfile.TemporaryDirectory() as directory:
            support.write_executable(
                Path(directory) / "node",
                "#!/bin/sh\nfor arg do\n"
                'case "$arg" in --test-timeout=*) ;; '
                '*.test.js) printf "%s\\n" "$arg" >&2 ;; esac\ndone\n',
            )
            for tier in ("server", "static"):
                result = self.run_browser_tests(ROOT, tier, path=directory)
                self.assertEqual(result.returncode, 0, result.stderr)
                selected[tier] = {Path(line) for line in result.stderr.splitlines()}
        suites = set((ROOT / "tests/browser").glob("*.test.js"))
        self.assertEqual(selected["server"] | selected["static"], suites)
        self.assertFalse(selected["server"] & selected["static"])
        self.assertTrue(selected["server"])
        for suite in suites:
            if "target/debug" in suite.read_text():
                self.assertIn(suite, selected["server"], suite.name)
        _, rust = self.run_lane("rust")
        self.assertLess(rust.index("cargo-test"), rust.index("browser-server-tests"))

    def test_ci_refuses_skipped_gates(self):
        for ci in (False, True):
            with self.subTest(ci=ci):
                result, _ = self.run_lane("rust", skip="clippy", ci=ci)
                self.assertEqual(result.returncode, 1 if ci else 0, result.stderr)
                self.assertIn("not checked by this run:", result.stderr)
                if ci:
                    self.assertIn("failed gates: skipped-gates", result.stderr)

    def run_status(self, command, **env):
        return subprocess.run(
            ["bash", "-e", "-c", command],
            env={**os.environ, **env},
            capture_output=True,
            check=False,
        ).returncode

    def test_check_waits_only_for_the_gate_lanes(self):
        check = support.job("check")
        self.assertIn("    needs: gates\n", check)
        self.assertIn("    if: ${{ !cancelled() }}\n", check)
        command = re.search(r"^        run: (.+)$", check, re.M)
        self.assertIsNotNone(command)
        for state in ("success", "failure", "cancelled", "skipped", ""):
            with self.subTest(state=state):
                self.assertEqual(
                    self.run_status(command[1], RESULT=state) == 0,
                    state == "success",
                )

    def test_mutation_status_rejects_failed_or_unexplained_skips(self):
        mutation = support.job("mutation")
        self.assertIn("    needs: [plan-mutants, mutants]\n", mutation)
        self.assertIn("    if: ${{ !cancelled() }}\n", mutation)
        command = support.run_block(mutation)
        accepted = {
            # Shards ran and passed.
            ("success", "success"),
            # The plan found no mutants, so no shard started.
            ("success", "skipped"),
            # A push to main plans nothing.
            ("skipped", "skipped"),
        }
        states = ("success", "failure", "cancelled", "skipped", "")
        for plan in states:
            for mutants in states:
                with self.subTest(plan=plan, mutants=mutants):
                    status = self.run_status(command, PLAN=plan, MUTANTS=mutants)
                    self.assertEqual(status == 0, (plan, mutants) in accepted)

    def test_each_lane_has_a_bounded_job(self):
        timeouts = {lane: int(entry["timeout"]) for lane, entry in lanes().items()}
        self.assertIn("    timeout-minutes: ${{ matrix.timeout }}\n", gates_job())
        self.assertLess(timeouts["browser"], timeouts["rust"])
        self.assertLess(timeouts["portable"], timeouts["rust"])

    def test_browser_timeout_fails_a_stalled_suite(self):
        probe = subprocess.run(
            ["node", "--test-timeout=100", "--version"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if probe.returncode:
            self.skipTest("this Node version has no test timeout option")
        body = function_body("browser_tests")
        timeout = re.search(r"--test-timeout=(\d+)", body)
        self.assertIsNotNone(timeout)
        self.assertGreater(int(timeout[1]), 0)
        # Two stalled tests must still fail by name before the job is killed.
        browser_minutes = int(lanes()["browser"]["timeout"])
        self.assertLess(2 * int(timeout[1]), browser_minutes * 60 * 1000)
        with tempfile.TemporaryDirectory() as directory:
            tests = Path(directory) / "tests/browser"
            tests.mkdir(parents=True)
            (tests / "stalled.test.js").write_text(
                'const { test } = require("node:test");\n'
                'test("stalled fixture", async () => {\n'
                "  await new Promise(resolve => setTimeout(resolve, 5000));\n"
                "});\n"
            )
            result = self.run_browser_tests(
                directory, "static", body=body.replace(timeout[0], "--test-timeout=100")
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timed out", result.stdout + result.stderr)

    def test_real_node_skips_and_todos_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            tests = Path(directory) / "tests/browser"
            tests.mkdir(parents=True)
            (tests / "mixed.test.js").write_text(
                'const { test } = require("node:test");\n'
                'test("plain pass", () => {});\n'
                'test("a title that says # SKIP is not a skip", () => {});\n'
                'test("no browser", (t) => t.skip("chromium missing"));\n'
                'test("later", { todo: "write it" }, () => {});\n'
            )
            result = self.run_browser_tests(directory, "static")
        self.assertEqual(result.returncode, 0, result.stderr)
        reported = result.stdout.split("\n")[-3:]
        self.assertEqual(
            [line.strip() for line in reported if line.strip()],
            [
                "browser tests, 1 skipped: chromium missing",
                "browser tests, 1 todo: write it",
            ],
        )

    def test_avatar_reuses_compatible_check_caches_without_saving_them(self):
        check = support.WORKFLOW.read_text()
        avatar = (ROOT / ".github/workflows/avatar.yml").read_text()

        def environment(workflow):
            block = support.section(workflow, "\nenv:\n", "\njobs:\n", "env")
            return dict(re.findall(r"^  ([A-Z_]+): (.+)$", block, re.M))

        check_env, avatar_env = environment(check), environment(avatar)
        for name in (
            "CARGO_PROFILE_DEV_DEBUG",
            "CARGO_PROFILE_TEST_DEBUG",
            "RUSTFLAGS",
        ):
            self.assertEqual(avatar_env[name], check_env[name], name)
        self.assertEqual(
            avatar_env["CARGO_INCREMENTAL"], check_env.get("CARGO_INCREMENTAL", "1")
        )

        def cache_inputs(workflow, name):
            block = support.step(workflow, name)
            return support.section(block, "        with:\n", "\n      #", name).strip()

        cargo = "uses: ./.github/actions/restore-cargo-cache"
        for workflow in (check, avatar):
            self.assertIn(cargo, support.step(workflow, "Restore cargo cache"))
        for name in ("Restore vendored bytes", "Restore Chromium"):
            self.assertEqual(cache_inputs(check, name), cache_inputs(avatar, name))
        self.assertNotIn("actions/cache/save", avatar)
        self.assertNotIn("uses: actions/cache@", avatar)

    def test_only_the_rust_lane_fetches_clang(self):
        if os.uname().sysname != "Linux" or os.uname().machine != "x86_64":
            self.skipTest("the pinned Clang is only fetched on Linux x86_64")
        fetch = 'sh "$ROOT/scripts/fetch-clang.sh" "$ROOT/target/clang"'
        self.assertEqual(SCRIPT.read_text().count(fetch), 1)
        for lane, fetched in (("all", True), ("rust", True), ("portable", False)):
            with self.subTest(lane=lane):
                result, _ = self.run_lane(
                    lane,
                    unset_cxx=True,
                    replace=[(fetch, "{ echo fetch:clang >&2; }")],
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual("fetch:clang" in result.stderr, fetched)

    def test_only_the_portable_lane_sets_up_the_packaging_python(self):
        # As if the Python found had no `cryptography`, so the gate would set
        # up the instructor tool's environment for the packaging tests.
        replace = [
            ("\"$1\" -c 'import cryptography'", "false"),
            (
                '"$PYTHON" "$ROOT/scripts/problem_bank/runtime.py"',
                "{ echo setup:task-tools >&2; }",
            ),
        ]
        for lane, setup in (
            ("all", True),
            ("portable", True),
            ("rust", False),
            ("browser", False),
        ):
            with self.subTest(lane=lane):
                result, _ = self.run_lane(lane, replace=replace)
                self.assertEqual("setup:task-tools" in result.stderr, setup)
        self.assertEqual(
            condition(gates_job(), "Place the instructor packaging environment"),
            "matrix.lane == 'portable'",
        )

    def test_a_named_packaging_environment_is_set_up_beside_a_ready_python(self):
        # A runner's system Python can already import `cryptography`; CI names
        # its own environment so the packaging tests run in a venv regardless.
        setup = (
            '"$PYTHON" "$ROOT/scripts/problem_bank/runtime.py"',
            "{ echo setup:task-tools >&2; }",
        )
        ready = ("\"$1\" -c 'import cryptography'", "true")
        for named, expected in (("/nonexistent/task-tools", True), ("", False)):
            with self.subTest(named=named):
                env = {"CODETRIAL_TASK_TOOLS": named}
                result, _ = self.run_lane("portable", replace=[ready, setup], env=env)
                self.assertEqual("setup:task-tools" in result.stderr, expected)

    def test_each_lane_installs_what_its_gates_need(self):
        gates, matrix = gates_job(), lanes()
        self.assertEqual(condition(gates, "Install system packages"), "matrix.packages")
        self.assertEqual(condition(gates, "Install Rust"), "matrix.components")
        self.assertEqual(matrix["rust"]["components"], "clippy")
        self.assertEqual(matrix["portable"]["components"], "rustfmt")
        self.assertTrue({"mold", "sqlite3"} <= set(matrix["rust"]["packages"].split()))
        self.assertEqual(
            set(matrix["portable"]["packages"].split()), {"shellcheck", "shfmt"}
        )
        self.assertEqual(set(matrix["browser"]), {"lane", "timeout"})
        for name in ("ruff", "commentflow", "actionlint", "cargo-audit"):
            with self.subTest(tool=name):
                self.assertEqual(
                    condition(gates, f"Install {name}"), "matrix.lane == 'portable'"
                )
        for name in ("Restore Chromium", "Install Chromium"):
            self.assertEqual(condition(gates, name), "matrix.lane != 'portable'")
        # After the gates pass, so the binary is this checkout's.
        for name in (
            "Prove the build names its commit",
            "Run the browser interview check",
        ):
            self.assertEqual(condition(gates, name), "matrix.lane == 'rust'")
        # Every lane restores the vendor tree and Chromium; one lane saves each,
        # after its gates, under the key the restores asked for.
        self.assertEqual(condition(gates, "Restore vendored bytes"), "")
        for name, lane, step_id in (
            ("Save vendored bytes", "portable", "vendor"),
            ("Save Chromium", "browser", "chromium"),
        ):
            with self.subTest(step=name):
                self.assertEqual(
                    condition(gates, name),
                    f"matrix.lane == '{lane}' && "
                    f"steps.{step_id}.outputs.cache-hit != 'true'",
                )
                self.assertIn(
                    f"key: ${{{{ steps.{step_id}.outputs.cache-primary-key }}}}",
                    support.step(gates, name),
                )
                self.assertLess(
                    gates.index("- name: Run gates"), gates.index(f"- name: {name}")
                )
        # GitHub reads 0 as false, so `portable && 0 || 1` would always be 1.
        self.assertIn(
            "fetch-depth: ${{ matrix.lane != 'portable' && 1 || 0 }}\n", gates
        )

    def test_formatter_skips_join_the_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "repo"
            (work / "scripts").mkdir(parents=True)
            shutil.copy(ROOT / "scripts/indent.sh", work / "scripts/indent.sh")
            (work / "example.sh").write_text("#!/bin/sh\necho ok\n")
            (work / "Cargo.toml").write_text("")
            (work / ".editorconfig").write_text("")
            subprocess.run(["git", "init", "-q"], cwd=work, check=True)
            # Only the plumbing indent.sh needs, so no formatter is on PATH.
            tools = Path(directory) / "bin"
            tools.mkdir()
            for tool in (
                "git",
                "sort",
                "tar",
                "mktemp",
                "cat",
                "cmp",
                "grep",
                "rm",
                "dirname",
                "tr",
            ):
                found = shutil.which(tool)
                self.assertIsNotNone(found, tool)
                (tools / tool).symlink_to(found)
            result = subprocess.run(
                [shutil.which("sh"), "scripts/indent.sh", "--check"],
                cwd=work,
                env={"PATH": str(tools), "HOME": directory},
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "formatting, commentflow not installed",
            self.reported_skips("formatting", result.stderr),
        )

    def test_python_skips_join_the_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            suite = Path(directory) / "test_skips.py"
            suite.write_text(
                "import unittest\n"
                "class Cases(unittest.TestCase):\n"
                "    def test_runs(self):\n"
                "        pass\n"
                "    def test_skips(self):\n"
                "        self.skipTest('tool missing')\n"
            )
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/run-python-tests.py"), str(suite)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        reported = self.reported_skips('"python tests"', result.stderr)
        self.assertEqual(len(reported), 1, reported)
        self.assertRegex(reported[0], r"^python tests, test_skips .*: tool missing$")

    def test_unknown_lane_fails_before_running_any_gate(self):
        result, gates = self.run_lane("typo")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(gates, [])
        self.assertIn("usage:", result.stderr)


if __name__ == "__main__":
    unittest.main()
