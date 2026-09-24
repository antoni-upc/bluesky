#!/usr/bin/env python3
"""Run one research scenario in a fresh detached process and exit on HOLD.

The exit status is 1 when the HOLD followed a strict model failure rather
than a scenario command or a planned envelope ABORT.
"""

import argparse
import sys
import threading
import time

import bluesky as bs


# Echoed by PYBADATEM immediately before it holds on a failed evaluation.
STRICT_FAILURE_MARKER = 'strict evaluation failure'


def unplanned_hold(messages):
    """Return the first echoed message that reports a strict-failure HOLD."""
    return next((message for message in messages if STRICT_FAILURE_MARKER in message), None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scenario', help='Scenario name relative to scenario/, without .scn')
    parser.add_argument('--pybada-nonstrict', action='store_true',
                        help='Allow kinematic baselines to continue through infeasible BADA requests')
    args = parser.parse_args(argv)

    bs.init(mode='sim', detached=True)
    if args.pybada_nonstrict:
        bs.settings.pybada_strict = False
    original_send = bs.net.send
    messages = []

    def report_stack_messages(topic, data='', to_group=b''):
        if topic in ('ECHO', b'ECHO') and isinstance(data, dict):
            message = data.get('text', '')
            if message:
                print(message, flush=True)
                messages.append(message)
        return original_send(topic, data, to_group)

    bs.net.send = report_stack_messages
    # IC resets every simulation object. Reloading immutable navdata in the same
    # fresh process is redundant and dominates the licensed test runtime.
    bs.navdb.reset = lambda: None
    bs.stack.stack(f'IC {args.scenario}')

    def supervise():
        seen_running = False
        while bs.sim.state != bs.END:
            if bs.sim.state == bs.OP and not seen_running:
                seen_running = True
                bs.sim.fastforward()
            if seen_running and bs.sim.state == bs.HOLD:
                bs.sim.quit()
                return
            time.sleep(0.01)

    watcher = threading.Thread(target=supervise, daemon=True)
    watcher.start()
    bs.sim.run()
    watcher.join(timeout=1.0)
    failure = unplanned_hold(messages)
    if failure:
        print(f'UNPLANNED HOLD: {failure}', file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
