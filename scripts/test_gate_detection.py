"""Dev tool: exercise GateDetector against representative questionary outputs.

Run directly on a POSIX host (Linux/macOS/WSL), or from the Hermes plugin via:

    hermes hermes-chat test-gates

The test cases here cover the prompt shapes Hermes' classic CLI is known to emit.
Add new cases as you encounter misses in the wild, ideally with the ANSI-stripped
raw output captured while `debug: true` is enabled.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from typing import Callable, Optional, Tuple

# Make the repo root importable when running this script directly.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from hermes_chat.pty_manager import GateDetector
except ImportError as exc:
    print(f"ERROR: cannot import hermes-chat PTY code: {exc}", file=sys.stderr)
    print("The gate-detection test tool requires a POSIX host (Linux/macOS/WSL).", file=sys.stderr)
    sys.exit(2)


def _strip_ansi(tail: str) -> str:
    import re

    return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]", "", tail)


TestCase = Tuple[str, str, Optional[Tuple[str, str, Optional[list[str]]]]]


def _cases() -> list[TestCase]:
    """Return (name, raw_output, expected_detection_or_None).

    expected_detection is (kind, prompt, options).
    """
    return [
        (
            "confirm_yes_no_parens",
            "? Proceed with this action? (Y/n)",
            ("confirm", "Proceed with this action?", ["Yes", "No"]),
        ),
        (
            "confirm_yes_no_brackets",
            "? Continue? [y/n]",
            ("confirm", "Continue?", ["Yes", "No"]),
        ),
        (
            "confirm_approve_deny_parens",
            "? Approve this tool call? (Y/n)",
            ("confirm", "Approve this tool call?", ["Yes", "No"]),
        ),
        (
            "confirm_with_colon",
            "? Do you want to continue? [y/n]:",
            ("confirm", "Do you want to continue?", ["Yes", "No"]),
        ),
        (
            "confirm_after_log_noise",
            "Thinking...\n? Execute the plan? (Y/n)",
            ("confirm", "Execute the plan?", ["Yes", "No"]),
        ),
        (
            "select_single_choice_unicode_cursor",
            "? Choose a model:\n  gpt-4o\n❯ gpt-4o-mini\n  claude-3-5-sonnet",
            ("select", "Choose a model", ["gpt-4o", "gpt-4o-mini", "claude-3-5-sonnet"]),
        ),
        (
            "select_single_choice_ascii_cursor",
            "? Pick a tool:\n  web_search\n> run_shell\n  read_file",
            ("select", "Pick a tool", ["web_search", "run_shell", "read_file"]),
        ),
        (
            "select_with_ansi_color",
            "\x1b[36m?\x1b[0m \x1b[1mChoose mode:\x1b[0m\n  fast\n\x1b[32m❯\x1b[0m slow\n  auto",
            ("select", "Choose mode", ["fast", "slow", "auto"]),
        ),
        (
            "select_after_log_noise",
            "Loaded plugins...\n? Select a provider:\n  openai\n❯ anthropic\n  local",
            ("select", "Select a provider", ["openai", "anthropic", "local"]),
        ),
        (
            "plain_text_no_gate",
            "Here is the result of the command:\n  total 12\n  drwxr-xr-x 2 user user 4096 Jul  7 20:00 .",
            None,
        ),
        (
            "plain_question_no_options",
            "What would you like to do next?",
            None,
        ),
        (
            "list_without_cursor",
            "? Options:\n  one\n  two\n  three",
            None,
        ),
    ]


def run_tests(
    *,
    preprocess: Optional[Callable[[str], str]] = _strip_ansi,
    verbose: bool = False,
) -> int:
    """Run all cases and return the number of failures."""
    detector = GateDetector()
    failures = 0
    passed = 0

    for name, raw, expected in _cases():
        tail = preprocess(raw) if preprocess else raw
        result = detector.detect(tail)

        if expected is None:
            ok = result is None
            detail = f"expected no gate, got {result!r}"
        else:
            exp_kind, exp_prompt, exp_options = expected
            ok = (
                result is not None
                and result[0] == exp_kind
                and result[1] == exp_prompt
                and result[2] == exp_options
            )
            detail = f"expected {expected!r}, got {result!r}"

        if ok:
            passed += 1
            if verbose:
                print(f"PASS {name}")
        else:
            failures += 1
            print(f"FAIL {name}: {detail}")
            print(textwrap.indent(repr(tail), "       "))

    print(f"\n{passed} passed, {failures} failed, {passed + failures} total")
    return failures


if __name__ == "__main__":
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    sys.exit(run_tests(verbose=verbose))
