#!/usr/bin/env python3
"""Run one research scenario in a fresh detached process and exit on HOLD."""

import argparse
import threading
import time

import bluesky as bs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scenario', help='Scenario name relative to scenario/, without .scn')
    args = parser.parse_args(argv)

    bs.init(mode='sim', detached=True)
    original_send = bs.net.send

    def report_stack_messages(topic, data='', to_group=b''):
        if topic in ('ECHO', b'ECHO') and isinstance(data, dict):
            message = data.get('text', '')
            if message:
                print(message, flush=True)
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
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
