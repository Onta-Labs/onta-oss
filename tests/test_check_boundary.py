"""Self-tests for scripts/check_boundary.sh.

This repo is public and its sdist ships tests/ and docs/, so the boundary guard
is the last thing standing between a pasted credential and a one-way door. It
previously had no test at all, which is how it came to report "no proprietary
leaks detected" while an internal ALB hostname sat in eval_holdout_v2/.

Every case below plants a leak, runs the REAL script, and asserts it is
rejected. Add a case here before relaxing any pattern.

Most cases run the script inside a throwaway git repo holding only the script
and the planted file: it exercises the same code path, keeps the developer's
working tree untouched, and takes milliseconds instead of the ~13s a full sweep
of the 69MB eval corpus costs. One test does run over the real repo, to assert
the committed tree is actually clean.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_boundary.sh"

pytestmark = pytest.mark.skipif(
    not shutil.which("bash") or not shutil.which("git") or not SCRIPT.exists(),
    reason="needs bash, git, and the boundary script",
)


def _run(cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(cwd / "scripts" / "check_boundary.sh")],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.fixture
def sandbox(tmp_path):
    """A throwaway git repo with the real guard installed.

    Returns ``plant(relpath, content) -> CompletedProcess``: write a file, run
    the guard, hand back the result. Untracked files count (the guard passes
    ``--others``), so no commit is needed.
    """
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "check_boundary.sh")
    subprocess.run(
        ["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True
    )

    def _plant(relpath: str, content: str) -> subprocess.CompletedProcess:
        target = tmp_path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return _run(tmp_path)

    # Exposed so a test can plant something other than a regular file.
    _plant.repo = tmp_path
    return _plant


def test_real_repo_is_clean():
    """The committed tree must pass. Slow (~13s): sweeps the whole eval corpus."""
    result = _run(REPO)
    assert result.returncode == 0, result.stdout + result.stderr


def test_sandbox_without_a_leak_passes(sandbox):
    """Guards the guard: the sandbox must not fail for incidental reasons."""
    result = sandbox("harmless.py", "X = 1\n")
    assert result.returncode == 0, result.stdout + result.stderr


# --- Leak classes -----------------------------------------------------------

LEAKS = [
    pytest.param(
        "eval_runs/probe.json",
        '{"api_url": "http://omnix-demo-tenant-dev-1707774776.us-east-1.elb.amazonaws.com"}',  # boundary-ok: fake infra string planted for this self-test
        id="alb-host-in-eval-artifact",
    ),
    pytest.param(
        "probe.json",
        '{"api_url": "http://OMNIX-DEMO-TENANT-DEV-1234.us-east-1.ELB.amazonaws.com"}',  # boundary-ok: fake infra string planted for this self-test
        id="alb-host-uppercased",
    ),
    pytest.param(
        "probe.md",
        "123456789012.dkr.ecr.us-east-1.amazonaws.com/omnix-demo-tenant:latest",  # boundary-ok: fake infra string planted for this self-test
        id="ecr-uri-with-account-id",
    ),
    pytest.param(
        "probe.py",
        'client = boto3.client("AWS::SecretsManager::Secret")',  # boundary-ok: fake infra string planted for this self-test
        id="secrets-manager-cfn-spelling",
    ),
    pytest.param(
        "probe.txt",
        "ANTHROPIC_API_KEY=sk-ant-api03-9xQvT2mKp4Lw8Rn6Bz1Yc3Hd5Fg7Jk9Mq2St4Uv6Wx8Za",  # boundary-ok: fake credential planted for this self-test
        id="anthropic-key-real-format",
    ),
    pytest.param(
        "probe.env",
        "key = sk-or-v1-9f3a2b7c8d9e0f1a2b3c4d5e6f7a8b9c",  # boundary-ok: fake credential planted for this self-test
        id="openrouter-key",
    ),
    pytest.param(
        "probe.cfg",
        "aws_access_key_id = AKIAQ7WXYZ12ABCD34EF",  # boundary-ok: fake infra string planted for this self-test
        id="aws-access-key",
    ),
    pytest.param(
        "probe_sts.cfg",
        "aws_access_key_id = ASIAQ7WXYZ12ABCD34EF",  # boundary-ok: fake infra string planted for this self-test
        id="aws-sts-temporary-key",
    ),
    pytest.param(
        "probe_clerk.env",
        # Assembled at runtime, not written as a literal: Clerk and Stripe share
        # the `sk_live_` prefix, so a literal here trips GitHub's push
        # protection on this very file. The guard still sees the joined string.
        "CLERK_SECRET_KEY=" + "sk_" + "live_" + ("NOTAREALKEY" * 3),
        id="clerk-secret-key",
    ),
    pytest.param(
        "probe_neptune.py",
        'ENDPOINT = "mycluster.abc.us-east-1.neptune.amazonaws.com"',  # boundary-ok: fake infra string planted for this self-test
        id="neptune-writer-endpoint",
    ),
    pytest.param(
        "probe_rds.py",
        'DSN = "mydb.abc.us-east-1.rds.amazonaws.com"',  # boundary-ok: fake infra string planted for this self-test
        id="rds-endpoint",
    ),
    pytest.param(
        "probe_pem.txt",
        "-----BEGIN RSA PRIVATE KEY-----",  # boundary-ok: fake credential planted for this self-test
        id="private-key-pem",
    ),
    pytest.param(
        "probe_dsn.py",
        'DSN = "postgresql://svc:Hunter2CorrectHorse@db.internal:5432/app"',  # boundary-ok: fake credential planted for this self-test
        id="postgres-dsn-with-real-password",
    ),
    pytest.param(
        "probe_contact.py",
        'CONTACT = "notarealperson@gmail.com"',  # boundary-ok: fabricated address planted for this self-test
        id="personal-email",
    ),
    pytest.param(
        "probe_case.py",
        'CONTACT = "Notarealperson@Gmail.com"',  # boundary-ok: fabricated address planted for this self-test
        id="personal-email-capitalized",
    ),
    pytest.param(
        "probe_beside.json",
        '{"reviewer": "test@example.com", "owner": "notarealperson@gmail.com"}',  # boundary-ok: fabricated address planted for this self-test
        id="personal-email-beside-placeholder",
    ),
    pytest.param(
        "probe_substring.py",
        'CONTACT = "contact.test@gmail.com"',  # boundary-ok: fabricated address planted for this self-test
        id="personal-email-containing-placeholder-token",
    ),
    pytest.param(
        "probe_local.py",
        'CONTACT = "poweruser@gmail.com"',  # boundary-ok: fabricated address planted for this self-test
        id="personal-email-with-placeholder-suffix",
    ),
]


@pytest.mark.parametrize("relpath,content", LEAKS)
def test_leak_is_rejected(sandbox, relpath, content):
    result = sandbox(relpath, content)
    assert result.returncode == 1, (
        f"guard MISSED a planted leak in {relpath}\ncontent: {content}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.parametrize(
    "relpath",
    ["docs/probe.md", "tests/probe.py", "ARCHITECTURE.md", "CONTRIBUTING.md"],
)
def test_no_directory_is_exempt_from_the_secret_check(sandbox, relpath):
    """docs/ and tests/ ship in the sdist; a credential there is just as public.

    Only the CONTACT check exempts tests/ (its ER fixtures use stock free-mail
    addresses). Infra and secret checks must reach everywhere.
    """
    result = sandbox(relpath, "key AKIAQ7WXYZ12ABCD34EF")  # boundary-ok: fake credential planted for this self-test
    assert result.returncode == 1, (
        f"guard MISSED a planted secret in {relpath}\n{result.stdout}{result.stderr}"
    )


def test_contact_exemption_is_scoped_to_named_er_fixture_files(sandbox):
    """The one sanctioned path exemption, kept to NAMED files on purpose.

    A tree-wide `^tests/` exemption let a real personal address be committed as
    a "fixture" in a brand-new test file, invisible to the contact check. The
    ER suite genuinely needs it (it tests email normalization, so its fixtures
    are realistic free-mail addresses no placeholder list can enumerate), but
    only those files get it.
    """
    exempt = "tests/resolver/er/test_normalize.py"
    assert sandbox(exempt, 'EMAIL = "areal.person@gmail.com"').returncode == 0  # boundary-ok: fabricated address planted for this self-test

    # A NEW test file is not exempt, which is the regression this guards.
    assert sandbox("tests/test_brand_new.py", 'EMAIL = "areal.person@gmail.com"').returncode == 1  # boundary-ok: fabricated address planted for this self-test

    # And the exemption covers the CONTACT check only, never secrets.
    assert sandbox(exempt, "KEY = 'AKIAQ7WXYZ12ABCD34EF'").returncode == 1  # boundary-ok: fake credential planted for this self-test


# --- Escape hatch and known-good cases --------------------------------------


def test_marker_suppresses_a_hit(sandbox):
    result = sandbox(
        "probe.py", 'KEY = "AKIAQ7WXYZ12ABCD34EF"  # boundary-ok: planted fixture'
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_marker_is_line_scoped_not_file_scoped(sandbox):
    """An exempted line must not launder the rest of the file."""
    result = sandbox(
        "probe.py",
        'A = "AKIAQ7WXYZ12ABCD34EF"  # boundary-ok: fixture\n'
        'B = "AKIAZZ99YYXX88WWVV11"\n',  # boundary-ok: fake credential planted for this self-test
    )
    assert result.returncode == 1, result.stdout + result.stderr


def test_stock_placeholder_email_is_allowed(sandbox):
    result = sandbox("probe.py", "# e.g. jane.doe@gmail.com vs jane.doe@company.com")
    assert result.returncode == 0, result.stdout + result.stderr


def test_stock_placeholder_email_is_allowed_capitalized(sandbox):
    """Extraction runs -i, so the placeholder subtraction must too."""
    result = sandbox("probe.py", "# e.g. Jane.Doe@Gmail.com")
    assert result.returncode == 0, result.stdout + result.stderr


def test_local_dummy_postgres_dsn_is_allowed(sandbox):
    """`postgres:postgres` and `u:p` test DSNs must not make this check noisy.

    A check people learn to ignore is a check that gets weakened; the pattern
    requires a 12+ character password so only plausible credentials fire.
    """
    result = sandbox(
        "probe.yml",
        'URL: "postgresql://postgres:postgres@localhost:5432/postgres"\n'
        'ALT: "postgresql://u:p@localhost/db"\n',
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_docs_placeholder_keys_are_not_secrets(sandbox):
    """`sk-or-...` style placeholders in .env.example / README must stay green."""
    result = sandbox(
        "probe.txt", "OPENROUTER_API_KEY=sk-or-...\nOMNIX_ANTHROPIC_API_KEY=sk-ant-...\n"
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_role_address_on_company_domain_is_allowed(sandbox):
    """The sanctioned contact shape for outbound User-Agents."""
    result = sandbox(
        "probe.py", 'UA = "onta-client/0.1 (+https://example.com; ops@onta.team)"'
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_prose_about_secrets_manager_is_allowed(sandbox):
    """The service NAME in deploy docs is not an ARN."""
    result = sandbox("probe.md", "| Secrets | AWS Secrets Manager | API keys |")
    assert result.returncode == 0, result.stdout + result.stderr


# --- Fail-closed ------------------------------------------------------------


def test_marker_is_ignored_on_a_minified_single_line_file(sandbox, tmp_path):
    """One marker must not disarm a whole machine-generated artifact.

    Every match in a single-line JSON blob shares line 1, so honoring a marker
    there would suppress the file entirely — and these artifacts are generated
    from scraped web data, so the string could land in one without any human
    choosing it. This is exactly the file class that caused the incident.
    """
    blob = (
        '{"a":"http://omnix-demo-tenant-dev-9.us-east-1.elb.amazonaws.com",'  # boundary-ok: fake infra string planted for this self-test
        '"k":"sk-ant-api03-9xQvT2mKp4Lw8Rn6Bz1Yc3Hd5Fg7Jk9Mq",'  # boundary-ok: fake credential planted for this self-test
        '"note":"boundary-ok: generated artifact",'
        '"pad":"' + "x" * 600 + '"}'
    )
    result = sandbox("artifact.json", blob)
    assert result.returncode == 1, (
        "a marker inside a minified artifact disarmed the whole file\n"
        f"{result.stdout}{result.stderr}"
    )


def test_marker_does_not_suppress_a_suffix_colliding_path(sandbox):
    """A marker on `notes.md:3` must not silence `docs/notes.md:3`.

    Suppression keys were once matched as an unanchored substring, so any file
    whose path is a suffix of another's could launder the other's leak on the
    same line. The tree already holds a dozen such pairs (README.md,
    package.json, LICENSE), and the failure was completely silent.
    """
    marked = "x\ny\nAKIAQ7WXYZ12ABCD34EF  boundary-ok: legit fixture\n"  # boundary-ok: fake infra string planted for this self-test
    victim = "a\nb\nhost http://omnix-demo-tenant-dev-9.us-east-1.elb.amazonaws.com\n"  # boundary-ok: fake infra string planted for this self-test

    sandbox("notes.md", marked)
    result = sandbox("docs/notes.md", victim)

    assert result.returncode == 1, (
        "a marker on a suffix-colliding path suppressed a real leak\n"
        f"{result.stdout}{result.stderr}"
    )
    assert "docs/notes.md:3" in result.stdout


def test_marker_still_suppresses_its_own_file(sandbox):
    """The exact-key fix must not break the escape hatch itself."""
    result = sandbox(
        "solo.py", 'KEY = "AKIAQ7WXYZ12ABCD34EF"  # boundary-ok: fixture'
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_dangling_symlink_warns_but_does_not_abort(sandbox):
    """A dangling symlink must not turn a clean run into "COULD NOT RUN".

    `--others` lists untracked files, so a dev tree or a concurrent build can
    leave one behind. Warn and keep scanning rather than hard-failing.
    """
    (sandbox.repo / "dangling.txt").symlink_to("/nonexistent/target")

    result = sandbox("harmless.py", "X = 1\n")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "skipped unreadable path" in result.stdout


def test_dangling_symlink_does_not_hide_a_leak(sandbox):
    """Skipping an unreadable path is per-file, never a reason to stop."""
    (sandbox.repo / "dangling.txt").symlink_to("/nonexistent/target")

    result = sandbox("leak.txt", "KEY=AKIAQ7WXYZ12ABCD34EF")  # boundary-ok: fake infra string planted for this self-test

    assert result.returncode == 1, result.stdout + result.stderr


def test_secret_in_the_sibling_guard_script_is_caught(sandbox):
    """The guard scripts are exempt from the INFRA check only, never secrets."""
    result = sandbox(
        "scripts/check_npm_bundle.sh",
        'KEY="sk-ant-api03-9xQvT2mKp4Lw8Rn6Bz1Yc3Hd5Fg7Jk9Mq"',  # boundary-ok: fake credential planted for this self-test
    )
    assert result.returncode == 1, result.stdout + result.stderr


def test_colon_in_a_tracked_path_is_refused(sandbox):
    """Hits are parsed positionally as path:line:match.

    A file named `notes:3` could otherwise impersonate line 3 of `notes` and
    borrow its escape-hatch marker. Refuse to run rather than guess.
    """
    result = sandbox("weird:name.py", "X = 1")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "ambiguous" in result.stdout


@pytest.mark.parametrize(
    "shim,label",
    [
        ("exit 2\n", "silent-error"),
        ("exit 127\n", "missing-binary"),
    ],
)
def test_fails_loudly_when_grep_itself_fails(tmp_path, shim, label):
    """A grep that cannot RUN must not read as "nothing found".

    xargs collapses grep's "no match" (1) and "error" (2) into its own 123, so
    the script runs each batch under a shell that maps them apart.
    """
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "check_boundary.sh")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "leak.txt").write_text("KEY=AKIAQ7WXYZ12ABCD34EF")  # boundary-ok: fake infra string planted for this self-test

    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    fake_grep = shim_dir / "grep"
    fake_grep.write_text("#!/bin/sh\n" + shim)
    fake_grep.chmod(0o755)

    import os

    env = dict(os.environ, PATH=f"{shim_dir}:{os.environ['PATH']}")
    result = subprocess.run(
        ["bash", str(tmp_path / "scripts" / "check_boundary.sh")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )

    assert result.returncode == 2, (
        f"[{label}] guard reported success with a broken grep: "
        f"{result.returncode}: {result.stdout}{result.stderr}"
    )
    assert "passed" not in result.stdout.lower()


def test_fails_loudly_outside_a_git_repo(tmp_path):
    """A guard that fails OPEN is worse than no guard.

    `git ls-files` errors used to be swallowed by `|| true`, so every
    repo-scope check silently became a no-op and the script printed success.
    """
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "check_boundary.sh")
    (tmp_path / "cograph_client").mkdir()
    (tmp_path / "cograph_client" / "leak.py").write_text('K = "AKIAQ7WXYZ12ABCD34EF"')  # boundary-ok: fake credential planted for this self-test

    result = _run(tmp_path)

    assert result.returncode == 2, (
        f"expected a hard 'could not run' exit, got {result.returncode}: "
        f"{result.stdout}{result.stderr}"
    )
    assert "COULD NOT RUN" in result.stdout
    assert "passed" not in result.stdout.lower()
