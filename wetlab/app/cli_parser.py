"""ArgumentParser construction for the `wetlab` CLI (`--mode` on root and all subcommands)."""

import argparse
from collections.abc import Callable

Handler = Callable[[argparse.Namespace], int]


def _sub_common() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--mode", choices=["virtual", "real"], default=argparse.SUPPRESS)
    parent.add_argument("--db", default=argparse.SUPPRESS)
    parent.add_argument("--runs-dir", default=argparse.SUPPRESS)
    return parent


def build_cli_parser(handlers: dict[str, Handler]) -> argparse.ArgumentParser:
    """Build the top-level and subcommand parsers with `--mode {virtual,real}` everywhere."""
    parser = argparse.ArgumentParser(prog="wetlab", description="WetLab CLI")
    parser.add_argument("--mode", choices=["virtual", "real"], default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--runs-dir", default=None)
    common = [_sub_common()]
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", parents=common)
    p_serve.add_argument("--once", action="store_true")
    p_serve.add_argument("--rounds", type=int, default=None)
    p_serve.set_defaults(func=handlers["serve"])

    p_agents = sub.add_parser("agents", parents=common)
    p_agents.add_argument("--once", action="store_true")
    p_agents.add_argument("--poll-interval", type=float, default=0.5)
    p_agents.set_defaults(func=handlers["agents"])

    p_submit = sub.add_parser("submit", parents=common)
    p_submit.add_argument("file")
    p_submit.add_argument("--mission-id", default=None)
    p_submit.add_argument("--experiment-id", default=None)
    p_submit.add_argument("--title", default=None)
    p_submit.add_argument("--key", default=None)
    p_submit.set_defaults(func=handlers["submit"])

    p_rehearse = sub.add_parser("rehearse", parents=common)
    p_rehearse.add_argument("file")
    p_rehearse.set_defaults(func=handlers["rehearse"])

    p_approve = sub.add_parser("approve", parents=common)
    p_approve.add_argument("protocol_id")
    p_approve.add_argument("--by", required=True)
    p_approve.add_argument("--fingerprint", required=True)
    p_approve.add_argument("--key", default=None)
    p_approve.set_defaults(func=handlers["approve"])

    p_reject = sub.add_parser("reject", parents=common)
    p_reject.add_argument("protocol_id")
    p_reject.add_argument("--comment", required=True)
    p_reject.add_argument("--by", default=None)
    p_reject.add_argument("--key", default=None)
    p_reject.set_defaults(func=handlers["reject"])

    p_pause = sub.add_parser("pause", parents=common)
    p_pause.add_argument("--protocol-id", default=None)
    p_pause.add_argument("--key", default=None)
    p_pause.set_defaults(func=handlers["pause"])

    p_resume = sub.add_parser("resume", parents=common)
    p_resume.add_argument("--protocol-id", default=None)
    p_resume.add_argument("--key", default=None)
    p_resume.set_defaults(func=handlers["resume"])

    p_stop = sub.add_parser("stop", parents=common)
    p_stop.add_argument("--key", default=None)
    p_stop.set_defaults(func=handlers["stop"])

    p_unfreeze = sub.add_parser("unfreeze", parents=common)
    p_unfreeze.add_argument(
        "--conclusion",
        choices=["step_done", "step_failed", "abort"],
        required=True,
    )
    p_unfreeze.add_argument("--labware-id", default=None)
    p_unfreeze.add_argument("--position", default=None)
    p_unfreeze.add_argument("--key", default=None)
    p_unfreeze.set_defaults(func=handlers["unfreeze"])

    p_abort = sub.add_parser("abort", parents=common)
    p_abort.add_argument("--experiment-id", default=None)
    p_abort.add_argument("--protocol-id", default=None)
    p_abort.add_argument("--key", default=None)
    p_abort.set_defaults(func=handlers["abort"])

    p_lw = sub.add_parser("labware", parents=common)
    lw_sub = p_lw.add_subparsers(dest="lw_action", required=True)
    p_place = lw_sub.add_parser("place", parents=common)
    p_place.add_argument("labware_id")
    p_place.add_argument("type")
    p_place.add_argument("position")
    p_place.add_argument("--key", default=None)
    p_rem = lw_sub.add_parser("remove", parents=common)
    p_rem.add_argument("labware_id")
    p_rem.add_argument("--key", default=None)
    p_cor = lw_sub.add_parser("correct", parents=common)
    p_cor.add_argument("labware_id")
    p_cor.add_argument("position")
    p_cor.add_argument("--key", default=None)
    p_lw.set_defaults(func=handlers["labware"])

    p_status = sub.add_parser("status", parents=common)
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=handlers["status"])

    p_doctor = sub.add_parser("doctor", parents=common)
    p_doctor.add_argument("--realtime", action="store_true")
    p_doctor.set_defaults(func=handlers["doctor"])

    p_tools = sub.add_parser("tools", parents=common)
    tools_sub = p_tools.add_subparsers(dest="tools_action", required=True)
    p_export = tools_sub.add_parser("export", parents=common)
    p_export.add_argument("--output", default=None)
    p_tools.set_defaults(func=handlers["tools"])
    return parser
