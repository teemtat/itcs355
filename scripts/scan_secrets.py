"""Scan Git history for credentials that are actually credentials.

    python scripts/scan_secrets.py          # whole history, every branch
    python scripts/scan_secrets.py --quiet  # exit code only

Exits 0 when clean, 1 when something looks real, 2 when it cannot run.

The check this replaces was:

    git log -p | grep -i -E "secret|password|AKIA|BEGIN PRIVATE"

which matches the *words*, so it fired 27 times on a clean checkout of this
repository — on documentation about not committing secrets, on `${{ secrets.* }}`
in a workflow, on `SECRET_STORE_PATH` in `cloud.env.example`, on the checklist line
telling students to run it, and on the grading script's own pattern. A check that
cannot pass on a correct repository teaches students to ignore it, which is worse
than not having it.

So these patterns match the *shape of a value*, not the name beside it. An AWS key
id is twenty characters with a fixed prefix; a PEM block has its dashes; a GCP
service-account key has a forty-hex `private_key_id`. Where a value has no
distinctive shape, the pattern demands a quoted literal and then rejects the
placeholders and environment lookups that make up almost all real-world matches.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

# Each pattern must match something that IS a credential, never a word that
# describes one. Group 1, where present, is the value to mask in the report.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws-access-key-id", re.compile(r"\b(AKIA[0-9A-Z]{16})\b")),
    ("aws-secret-access-key", re.compile(
        r"(?i)aws_secret_access_key\s*[:=]\s*[\"']?([A-Za-z0-9/+=]{40})\b")),
    ("private-key-block", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("gcp-service-account-key", re.compile(r"\"private_key_id\"\s*:\s*\"([0-9a-f]{40})\"")),
    ("azure-storage-account-key", re.compile(r"(?i)AccountKey\s*=\s*([A-Za-z0-9+/]{86}==)")),
    ("github-token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36})\b")),
    ("slack-token", re.compile(r"\b(xox[abprs]-[A-Za-z0-9-]{10,})")),
    ("generic-assigned-secret", re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\b"
        r"\s*[:=]\s*[\"']([^\"'\s]{8,})[\"']")),
]

# A quoted value that looks like one of these is documentation, not a leak.
PLACEHOLDER = re.compile(
    r"(?i)^(?:"
    r"[<{\[].*|"                                  # <your-key>, {{ token }}, [redacted]
    r".*\$\{?[A-Za-z_(].*|"                       # $VAR, ${VAR}, ${{ secrets.X }}, $(cmd)
    r"(?:your|my|the)[-_.].*|"
    r"(?:change|replace|fill)[-_.]?me.*|"
    r"(?:placeholder|example|sample|dummy|fake|test|todo|tbd|none|null|nil|unset"
    r"|redacted|removed|hidden|xxx+|\*+|\.+|-+|_+)[-_.a-z0-9]*|"
    r".*(?:os\.environ|getenv|process\.env|secretmanager|keyvault).*"
    r")$"
)


def refuse_if_shallow(allow: bool) -> None:
    """A shallow clone reports CLEAN on a repository full of secrets.

    `actions/checkout` fetches depth 1 by default, so a scan wired into CI without
    `fetch-depth: 0` sees one commit and passes. Verified: a depth-1 clone of a
    repository with eight planted credentials exits 0. "Cannot verify" must never
    be reported as "verified", so this is a hard stop rather than a warning.
    """
    probe = subprocess.run(["git", "rev-parse", "--is-shallow-repository"],
                           capture_output=True, text=True)
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return
    if allow:
        print("scan_secrets: WARNING shallow clone — history before the graft is "
              "unscanned", file=sys.stderr)
        return
    print("scan_secrets: refusing to scan a shallow clone — it would report CLEAN\n"
          "  whatever is in the truncated history.\n"
          "  GitHub Actions:  actions/checkout@v4 with `fetch-depth: 0`\n"
          "  Locally:         git fetch --unshallow\n"
          "  Override with --allow-shallow if you know what you are giving up.",
          file=sys.stderr)
    raise SystemExit(2)


def added_lines(rev_range: str | None) -> list[tuple[str, str, str]]:
    """Yield (commit, path, text) for every line a commit ADDED.

    History, not the working tree: a credential that was committed and then deleted
    is still a leaked credential, and the deletion is what makes people believe
    otherwise.
    """
    cmd = ["git", "log", "-p", "--no-color", "--no-textconv", "--format=%H"]
    cmd.append(rev_range) if rev_range else cmd.append("--all")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", check=True)
    except FileNotFoundError:
        print("scan_secrets: git not found", file=sys.stderr)
        raise SystemExit(2) from None
    except subprocess.CalledProcessError as exc:
        print(f"scan_secrets: git failed — {exc.stderr.strip()[:200]}", file=sys.stderr)
        raise SystemExit(2) from None

    out, commit, path = [], "", ""
    for line in proc.stdout.splitlines():
        if re.fullmatch(r"[0-9a-f]{40}", line):
            commit = line
        elif line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            out.append((commit, path, line[1:]))
    return out


def findings(lines: list[tuple[str, str, str]]) -> list[tuple[str, str, str, str]]:
    hits = []
    for commit, path, text in lines:
        for name, pattern in PATTERNS:
            m = pattern.search(text)
            if not m:
                continue
            value = m.group(1) if m.groups() else ""
            if value and PLACEHOLDER.match(value):
                continue
            hits.append((commit, path, name, value))
            break
    return hits


def mask(value: str) -> str:
    if not value:
        return ""
    return f"  {value[:4]}{'*' * min(len(value) - 4, 12)}" if len(value) > 4 else "  ****"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rev-range", help="limit the scan, e.g. origin/main..HEAD")
    ap.add_argument("--quiet", action="store_true", help="exit code only")
    ap.add_argument("--allow-shallow", action="store_true",
                    help="scan anyway on a shallow clone (scans less than it appears to)")
    args = ap.parse_args()

    refuse_if_shallow(args.allow_shallow)
    hits = findings(added_lines(args.rev_range))
    if not hits:
        if not args.quiet:
            print("CLEAN  no credential-shaped value found in history")
        return 0

    if not args.quiet:
        print(f"FOUND  {len(hits)} credential-shaped value(s) in history\n")
        for commit, path, name, value in hits:
            print(f"  {commit[:8]}  {path}\n      {name}{mask(value)}")
        print("\nRemoving the commit does not unpublish the credential. Rotate it first,\n"
              "then rewrite history. In that order.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
