"""CLI-contract tests: one parseable JSON object on stdout, exit codes, env-file
auth, --state-dir isolation, schema_version, --dry-run with zero POSTs.

Runs the real argparse/main in-process with an injected stub transport and
frozen clock (the test seam); zero network. Numbered docstrings are the verbatim
test groups from plans/zai-quota-implementation-plan.md (plus a bracketed aspect
note); tests named addendum_* cover plans/quota-and-delegation-20261019.md A2.
"""

import contextlib
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import models  # noqa: E402
import zai_api  # noqa: E402
import zai_quota  # noqa: E402

NOW_MS = 1_700_000_000_000
API_KEY = "zai-secret-key-CLI-TEST-0123456789"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SKILL_PATH = pathlib.Path(__file__).resolve().parents[1] / "SKILL.md"


def clock():
    return NOW_MS


def quota_payload(used=15.4, minutes_ahead=212):
    return {
        "success": True,
        "code": 200,
        "data": {
            "limits": [
                {
                    "unit": 3,
                    "type": "TOKENS_LIMIT",
                    "percentage": used,
                    "nextResetTime": NOW_MS + minutes_ahead * 60_000,
                }
            ]
        },
    }


def cards_payload(cards):
    return {"success": True, "code": 200, "data": {"fiveHourResets": cards, "weekResets": []}}


def card(record_id=910002, expire="2026-10-19 09:18:08", available=True, **extra):
    entry = {"recordId": record_id, "available": available, "expireTime": expire}
    entry.update(extra)
    return entry


class FakeTransport:
    def __init__(self):
        self.queues = {}
        self.requests = []
        self.post_count = 0

    def queue_json(self, url, status, payload):
        self.queues.setdefault(url, []).append(("resp", status, json.dumps(payload).encode("utf-8")))

    def queue_raw(self, url, status, body):
        self.queues.setdefault(url, []).append(("resp", status, body))

    def queue_raise(self, url, exc):
        self.queues.setdefault(url, []).append(("raise", exc))

    def __call__(self, request):
        url = request.full_url
        self.requests.append((request.get_method(), url, request.get_header("Authorization")))
        if request.get_method() == "POST":
            self.post_count += 1
        queue = self.queues.get(url) or []
        if not queue:
            raise AssertionError("unexpected request: %s %s" % (request.get_method(), url))
        item = queue.pop(0)
        if item[0] == "raise":
            raise item[1]
        return item[1], item[2]

    def auth_header(self):
        return self.requests[0][2] if self.requests else None


@contextlib.contextmanager
def captured_io():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


def run_main(argv, transport=None, env=None):
    """Run zai_quota.main in-process; returns (exit_code, stdout_buffer, stderr_buffer)."""
    with captured_io() as (out, err):
        code = zai_quota.main(argv, transport=transport, now_ms=clock, env=env)
    return code, out, err


class HelpTests(unittest.TestCase):
    def test_quota_info_help(self):
        """CLI contract: `quota_info --help` works and exits 0 (no API key needed)."""
        with self.assertRaises(SystemExit) as ctx:
            run_main(["quota_info", "--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_reset_please_help(self):
        """CLI contract: `reset_please --help` works and exits 0, documents --dry-run and --state-dir."""
        with captured_io() as (out, _err):
            with self.assertRaises(SystemExit) as ctx:
                zai_quota.main(["reset_please", "--help"], now_ms=clock, env={})
        self.assertEqual(ctx.exception.code, 0)
        text = out.getvalue()
        self.assertIn("dry-run", text)
        self.assertIn("state-dir", text)

    def test_help_does_not_require_api_key_or_state(self):
        """CLI contract: --help performs no auth resolution and no requests."""
        env = {"ZAI_QUOTA_ENV_FILE": "/nonexistent/zai-quota-test-env"}
        for argv in (["quota_info", "--help"], ["reset_please", "--help"]):
            with captured_io() as (out, _err):
                with self.assertRaises(SystemExit) as ctx:
                    zai_quota.main(argv, transport=FakeTransport(), now_ms=clock, env=env)
            self.assertEqual(ctx.exception.code, 0)


class JsonOutputAndSecretsTests(unittest.TestCase):
    def test_cli_always_emits_parseable_json_and_hides_secrets(self):
        """13. CLI always emits parseable JSON; stderr does not reveal secrets; `--dry-run` makes zero POSTs."""
        state_dir = tempfile.mkdtemp(prefix="zai-quota-cli-")
        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=96.7, minutes_ahead=84))
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([card(910002)]))
        env = {"ZAI_API_KEY": API_KEY}
        code, out, err = run_main(["quota_info", "--state-dir", state_dir], transport=transport, env=env)
        self.assertEqual(code, 0)
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["minutes"], 84)
        self.assertNotIn(API_KEY, out.getvalue())
        self.assertNotIn(API_KEY, err.getvalue())

        # Missing key: still exactly one parseable JSON object, exit 2.
        env = {"ZAI_QUOTA_ENV_FILE": "/nonexistent/zai-quota-test-env"}
        code, out, err = run_main(["quota_info"], transport=FakeTransport(), env=env)
        self.assertEqual(code, 2)
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["reason"], "missing_api_key")

        # Quota API failure: still parseable JSON, exit 2.
        transport = FakeTransport()
        transport.queue_raw(zai_api.QUOTA_URL, 500, b"boom")
        env = {"ZAI_API_KEY": API_KEY}
        code, out, err = run_main(["quota_info"], transport=transport, env=env)
        self.assertEqual(code, 2)
        payload = json.loads(out.getvalue().splitlines()[-1])
        self.assertEqual(payload["status"], "error")
        self.assertNotIn(API_KEY, out.getvalue())
        self.assertNotIn(API_KEY, err.getvalue())

    def test_dry_run_makes_zero_posts(self):
        """13. CLI always emits parseable JSON; stderr does not reveal secrets; `--dry-run` makes zero POSTs."""
        state_dir = tempfile.mkdtemp(prefix="zai-quota-cli-")
        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([card(910002)]))
        env = {"ZAI_API_KEY": API_KEY}
        code, out, err = run_main(
            ["reset_please", "--dry-run", "--state-dir", state_dir],
            transport=transport,
            env=env,
        )
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "dry_run")
        self.assertEqual(payload["would_status"], "redeem_candidate")
        self.assertEqual(payload["would_redeem_card_id"], 910002)
        self.assertEqual(transport.post_count, 0)
        self.assertFalse(os.path.exists(os.path.join(state_dir, "pending.json")))
        self.assertFalse(os.path.exists(os.path.join(state_dir, "ledger.jsonl")))

        # Dry-run with healthy quota: simulate continue; still zero POSTs.
        transport2 = FakeTransport()
        transport2.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=15.4, minutes_ahead=212))
        code, out, _err = run_main(
            ["reset_please", "--dry-run", "--state-dir", state_dir],
            transport=transport2,
            env={"ZAI_API_KEY": API_KEY},
        )
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["would_status"], "continue")
        self.assertNotIn("would_redeem_card_id", payload)
        self.assertEqual(transport2.post_count, 0)

    def test_exit_codes_by_status(self):
        """CLI contract: 0 continue/wait/no_resets_available/success + partial; 2 error; 3 uncertain."""
        env = {"ZAI_API_KEY": API_KEY}
        state_dir = tempfile.mkdtemp(prefix="zai-quota-cli-")
        # continue -> 0
        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=15.4, minutes_ahead=212))
        code, out, _ = run_main(["reset_please", "--state-dir", state_dir], transport=transport, env=env)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "continue")
        # wait -> 0
        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.4, minutes_ahead=42))
        code, out, _ = run_main(["reset_please", "--state-dir", state_dir], transport=transport, env=env)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "wait")
        # no_resets_available -> 0
        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=100.0, minutes_ahead=174))
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([]))
        code, out, _ = run_main(["reset_please", "--state-dir", state_dir], transport=transport, env=env)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "no_resets_available")
        # uncertain -> 3
        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([card(910012)]))
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport.queue_raise(zai_api.RESET_URL, TimeoutError())
        code, out, _ = run_main(["reset_please", "--state-dir", state_dir], transport=transport, env=env)
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out.getvalue())["status"], "uncertain")
        # error -> 2 (fresh state dir: earlier sub-tests legitimately left a
        # pending record, which would otherwise resolve to uncertain here)
        transport = FakeTransport()
        transport.queue_raw(zai_api.QUOTA_URL, 500, b"boom")
        error_state_dir = tempfile.mkdtemp(prefix="zai-quota-cli-")
        code, out, _ = run_main(["reset_please", "--state-dir", error_state_dir], transport=transport, env=env)
        self.assertEqual(code, 2)
        # quota_info partial -> 0
        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload())
        transport.queue_raw(zai_api.CARDS_URL, 500, b"boom")
        code, out, _ = run_main(["quota_info", "--state-dir", state_dir], transport=transport, env=env)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "partial")
        # uncertain -> 3 for a later invocation resolving pending with unchanged reads
        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([card(910012)]))
        code, out, _ = run_main(["reset_please", "--state-dir", state_dir], transport=transport, env=env)
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out.getvalue())["reason"], "pending_reset_unverified")

    def test_smoke_canned_payload_single_json_line(self):
        """Acceptance smoke (stub transport, no network): quota_info with a canned quota+cards payload prints exactly one parseable JSON line."""
        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=96.7, minutes_ahead=84))
        transport.queue_json(
            zai_api.CARDS_URL,
            200,
            cards_payload([card(910002, "2026-10-19 09:18:08")]),
        )
        code, out, err = run_main(
            ["quota_info"], transport=transport, env={"ZAI_API_KEY": API_KEY}
        )
        self.assertEqual(code, 0)
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(
            payload,
            {
                "schema_version": 1,
                "status": "ok",
                "minutes": 84,
                "reason": "quota_and_cards_available",
                "quota_used_percent": 96.7,
                "quota_remaining_percent": 3.3,
                "next_reset_at": models.iso_utc(NOW_MS + 84 * 60_000),
                "next_reset_at_epoch_ms": NOW_MS + 84 * 60_000,
                "cards_available": 1,
                "earliest_card_expires_at": "2026-10-19 09:18:08",
                "fetched_at": models.iso_utc(NOW_MS),
                "warnings": [],
            },
        )


class AddendumEnvFileTests(unittest.TestCase):
    def _env_file(self, key_value="ZAI_API_KEY=file-key-ABC"):
        fd, path = tempfile.mkstemp(prefix="zai-quota-env-", suffix=".env")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("# zai-quota env\n%s\n" % key_value)
        return path

    def test_env_file_used_when_process_env_missing(self):
        """addendum A2.1: ZAI_API_KEY falls back to the ZAI_QUOTA_ENV_FILE env file."""
        path = self._env_file()
        try:
            transport = FakeTransport()
            transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload())
            env = {"ZAI_QUOTA_ENV_FILE": path}
            code, out, err = run_main(["quota_info"], transport=transport, env=env)
            self.assertEqual(code, 0)
            self.assertEqual(transport.auth_header(), "Bearer file-key-ABC")
            self.assertNotIn("file-key-ABC", out.getvalue())
            self.assertNotIn("file-key-ABC", err.getvalue())
        finally:
            os.unlink(path)

    def test_process_env_wins_over_env_file(self):
        """addendum test: env-file loading order -- process env wins over file."""
        path = self._env_file()
        try:
            transport = FakeTransport()
            transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload())
            env = {"ZAI_API_KEY": API_KEY, "ZAI_QUOTA_ENV_FILE": path}
            code, out, _err = run_main(["quota_info"], transport=transport, env=env)
            self.assertEqual(code, 0)
            self.assertEqual(transport.auth_header(), "Bearer %s" % API_KEY)
            # The file value must never be consulted or leaked.
            self.assertNotIn("file-key-ABC", out.getvalue())
        finally:
            os.unlink(path)

    def test_missing_everywhere_is_missing_api_key_exit_2(self):
        """addendum A2.1: missing key -> status error, reason missing_api_key, exit 2."""
        path = self._env_file(key_value="OTHER_KEY=not-consumed")
        try:
            transport = FakeTransport()
            env = {"ZAI_QUOTA_ENV_FILE": path}
            code, out, _err = run_main(["quota_info"], transport=transport, env=env)
            self.assertEqual(code, 2)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["reason"], "missing_api_key")
            self.assertEqual(transport.requests, [])  # zero requests without a key
        finally:
            os.unlink(path)

    def test_only_zai_api_key_consumed_from_file(self):
        """addendum A2.1: only ZAI_API_KEY is consumed from the env file."""
        path = self._env_file(key_value="OTHER=zz\nZAI_API_KEY=file-key-OK\n")
        try:
            values = zai_quota.load_env_file(path)
            self.assertEqual(values.get("ZAI_API_KEY"), "file-key-OK")
            key = zai_quota.resolve_api_key(env={}, env_file=path)
            self.assertEqual(key, "file-key-OK")
        finally:
            os.unlink(path)


class AddendumStateDirTests(unittest.TestCase):
    def test_state_dir_isolation(self):
        """addendum test: --state-dir isolation -- ledgers in different dirs do not affect each other."""
        dir_a = tempfile.mkdtemp(prefix="zai-quota-iso-a-")
        dir_b = tempfile.mkdtemp(prefix="zai-quota-iso-b-")
        zai_quota.append_ledger(
            dir_a,
            action="redeem",
            card_id=910020,
            request_id="req-a",
            result="success",
            next_reset_at_epoch_ms=NOW_MS + 60 * 60_000,
            ts=models.iso_utc(NOW_MS),
        )
        env = {"ZAI_API_KEY": API_KEY}
        # Dir A: blocked by the seeded success entry.
        transport_a = FakeTransport()
        transport_a.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        code, out, _ = run_main(
            ["reset_please", "--state-dir", dir_a], transport=transport_a, env=env
        )
        self.assertEqual(json.loads(out.getvalue())["reason"], "redemption_already_used_this_window")
        self.assertEqual(transport_a.post_count, 0)
        # Dir B: fresh state -> proceeds to inventory (empty -> no_resets_available).
        transport_b = FakeTransport()
        transport_b.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport_b.queue_json(zai_api.CARDS_URL, 200, cards_payload([]))
        code, out, _ = run_main(
            ["reset_please", "--state-dir", dir_b], transport=transport_b, env=env
        )
        self.assertEqual(json.loads(out.getvalue())["status"], "no_resets_available")
        self.assertEqual(transport_b.post_count, 0)

    def test_default_state_dir_derivation_and_permissions(self):
        """addendum A2.2: default dir is ~/.local/state/zai-quota/<sha256(key)[:16]>/, 0700, files 0600."""
        import hashlib

        home = tempfile.mkdtemp(prefix="zai-quota-home-")
        derived = zai_quota.state_dir_for("key-xyz", None, home=home)
        expected = hashlib.sha256(b"key-xyz").hexdigest()[:16]
        self.assertEqual(
            derived, pathlib.Path(home) / ".local/state/zai-quota" / expected
        )
        self.assertEqual(str(zai_quota.state_dir_for("k", "/tmp/over")), "/tmp/over")

        state_dir = tempfile.mkdtemp(prefix="zai-quota-perm-")
        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([card(910021)]))
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport.queue_json(zai_api.RESET_URL, 200, {"success": True, "code": 200})
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=10.0, minutes_ahead=290))
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([]))
        code, out, _ = run_main(
            ["reset_please", "--state-dir", state_dir],
            transport=transport,
            env={"ZAI_API_KEY": API_KEY},
        )
        self.assertEqual(code, 0)
        self.assertEqual(stat.S_IMODE(os.stat(state_dir).st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE(os.stat(os.path.join(state_dir, "ledger.jsonl")).st_mode), 0o600
        )
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(state_dir, "lock")).st_mode), 0o600)


class AddendumSchemaVersionTests(unittest.TestCase):
    def test_schema_version_on_every_response(self):
        """addendum A2.4: every stdout JSON object includes "schema_version": 1."""
        env = {"ZAI_API_KEY": API_KEY}
        state_dir = tempfile.mkdtemp(prefix="zai-quota-schema-")
        responses = []

        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload())
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([card()]))
        _code, out, _ = run_main(["quota_info", "--state-dir", state_dir], transport=transport, env=env)
        responses.append(json.loads(out.getvalue()))

        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload())
        transport.queue_raw(zai_api.CARDS_URL, 500, b"boom")
        _code, out, _ = run_main(["quota_info", "--state-dir", state_dir], transport=transport, env=env)
        responses.append(json.loads(out.getvalue()))  # partial

        transport = FakeTransport()
        transport.queue_raw(zai_api.QUOTA_URL, 500, b"boom")
        _code, out, _ = run_main(["quota_info", "--state-dir", state_dir], transport=transport, env=env)
        responses.append(json.loads(out.getvalue()))  # error

        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=15.4, minutes_ahead=212))
        _code, out, _ = run_main(["reset_please", "--state-dir", state_dir], transport=transport, env=env)
        responses.append(json.loads(out.getvalue()))  # continue

        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.4, minutes_ahead=42))
        _code, out, _ = run_main(["reset_please", "--state-dir", state_dir], transport=transport, env=env)
        responses.append(json.loads(out.getvalue()))  # wait

        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=100.0, minutes_ahead=174))
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([]))
        _code, out, _ = run_main(["reset_please", "--state-dir", state_dir], transport=transport, env=env)
        responses.append(json.loads(out.getvalue()))  # no_resets_available

        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([card(910030)]))
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport.queue_json(zai_api.RESET_URL, 200, {"success": True, "code": 200})
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=10.0, minutes_ahead=290))
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([]))
        _code, out, _ = run_main(["reset_please", "--state-dir", state_dir], transport=transport, env=env)
        responses.append(json.loads(out.getvalue()))  # success

        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([card(910031)]))
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=99.5, minutes_ahead=300))
        transport.queue_raise(zai_api.RESET_URL, TimeoutError())
        _code, out, _ = run_main(["reset_please", "--state-dir", state_dir], transport=transport, env=env)
        responses.append(json.loads(out.getvalue()))  # uncertain

        _code, out, _ = run_main(
            ["quota_info"], transport=FakeTransport(), env={"ZAI_QUOTA_ENV_FILE": "/nonexistent/x"}
        )
        responses.append(json.loads(out.getvalue()))  # error missing key

        _code, out, _ = run_main(
            ["reset_please", "--dry-run", "--state-dir", state_dir],
            transport=FakeTransport(),
            env=env,
        )
        # The last dry-run has no queued quota response -> error envelope; still schema 1.
        responses.append(json.loads(out.getvalue()))

        for resp in responses:
            self.assertEqual(resp.get("schema_version"), 1, resp.get("reason"))

    def test_quota_info_includes_next_reset_epoch_and_iso(self):
        """addendum A2.5: quota_info includes next_reset_at epoch-ms int alongside the ISO string."""
        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload(used=96.7, minutes_ahead=84))
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([]))
        _code, out, _ = run_main(
            ["quota_info"], transport=transport, env={"ZAI_API_KEY": API_KEY}
        )
        payload = json.loads(out.getvalue())
        expected_ms = NOW_MS + 84 * 60_000
        self.assertEqual(payload["next_reset_at_epoch_ms"], expected_ms)
        self.assertIsInstance(payload["next_reset_at_epoch_ms"], int)
        self.assertEqual(payload["next_reset_at"], models.iso_utc(expected_ms))


@unittest.skipUnless(
    SKILL_PATH.exists(), "SKILL.md is authored in a later checkpoint (CK6); test activates then"
)
class SkillInstructionsTests(unittest.TestCase):
    def test_skill_references_real_cli_and_sleep_statuses(self):
        """14. Skill instructions refer to the real CLI paths and make agents schedule wake-ups only on statuses requiring sleep."""
        text = SKILL_PATH.read_text(encoding="utf-8")
        self.assertIn("scripts/zai_quota.py", text)
        self.assertIn("quota_info", text)
        self.assertIn("reset_please", text)
        for wake_status in ("wait", "no_resets_available"):
            self.assertIn(wake_status, text)
        # continue/success must not be described as wake-up triggers.
        self.assertNotIn("schedule a wake-up on `continue`", text.lower())


if __name__ == "__main__":
    unittest.main()


class ApiKeyCommandTests(unittest.TestCase):
    """addendum A2.7: ZAI_API_KEY_CMD supplies the key when env/file don't."""

    @staticmethod
    def _ok_transport():
        transport = FakeTransport()
        transport.queue_json(zai_api.QUOTA_URL, 200, quota_payload())
        transport.queue_json(zai_api.CARDS_URL, 200, cards_payload([card()]))
        return transport

    def test_command_source_used_when_env_and_file_absent(self):
        import shlex
        cmd = shlex.join([sys.executable, "-c", "print('cmd-sourced-key')"])
        code, out, _ = run_main(["quota_info"], transport=self._ok_transport(),
                                env={"ZAI_API_KEY_CMD": cmd,
                                     "ZAI_QUOTA_ENV_FILE":
                                         "/nonexistent/zq-test-env"})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "ok")

    def test_env_var_wins_over_command(self):
        import shlex
        cmd = shlex.join([sys.executable, "-c", "print('wrong')"])
        code, out, _ = run_main(
            ["quota_info"], transport=self._ok_transport(),
            env={"ZAI_API_KEY": API_KEY, "ZAI_API_KEY_CMD": cmd,
                                     "ZAI_QUOTA_ENV_FILE":
                                         "/nonexistent/zq-test-env"})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "ok")

    def test_failing_command_is_missing_api_key(self):
        import shlex
        cmd = shlex.join([sys.executable, "-c", "import sys; sys.exit(3)"])
        code, out, _ = run_main(["quota_info"], transport=FakeTransport(),
                                env={"ZAI_API_KEY_CMD": cmd,
                                     "ZAI_QUOTA_ENV_FILE":
                                         "/nonexistent/zq-test-env"})
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())["reason"], "missing_api_key")
