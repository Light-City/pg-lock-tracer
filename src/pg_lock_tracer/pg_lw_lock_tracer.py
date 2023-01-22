#!/usr/bin/env python3
#
# PostgreSQL LW lock tracer. To use this script, PostgreSQL has to be
# compiled with '--enable-dtrace'.
#
# See https://www.postgresql.org/docs/current/dynamic-trace.html
#
# List all available USDT probes
# sudo bpftrace -l "usdt:/home/jan/postgresql-sandbox/bin/REL_15_1_DEBUG/bin/postgres:*"
###############################################

import sys
import argparse

from enum import IntEnum

from bcc import BPF, USDT
from prettytable import PrettyTable

from pg_lock_tracer.helper import BPFHelper

EXAMPLES = """examples:
# Trace the LW locks of the PID 1234
pg_lw_lock_tracer -p 1234

# Trace the LW locks of the PIDs 1234 and 5678
pg_lw_lock_tracer -p 1234 -p 5678

# Trace the LW locks of the PID 1234 and be verbose
pg_lw_lock_tracer -p 1234 -v

# Trace the LW locks of the PID 1234 and collect statistics
pg_lw_lock_tracer -p 1234 -v --statistics
"""

parser = argparse.ArgumentParser(
    description="",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=EXAMPLES,
)
parser.add_argument("-v", "--verbose", action="store_true", help="Be verbose")
parser.add_argument(
    "-p",
    "--pid",
    type=int,
    nargs="+",
    dest="pids",
    metavar="PID",
    help="the pid(s) to trace",
    required=True,
)
parser.add_argument(
    "-d",
    "--dry-run",
    action="store_true",
    help="compile and load the BPF program but exit afterward",
)
parser.add_argument("--statistics", action="store_true", help="print lock statistics")


class Events(IntEnum):
    LOCK = 0
    LOCK_OR_WAIT = 1
    LOCK_OR_WAIT_FAIL = 2
    UNLOCK = 3
    WAIT_START = 4
    WAIT_DONE = 5
    COND_ACQUIRE = 6
    COND_ACQUIRE_FAIL = 7


class LockStatisticsEntry:
    def __init__(self) -> None:

        # The number of non-waited requested locks
        self._direct_lock_count = 0

        # The number of lock or wait calls
        self._direct_lock_or_wait_count = 0

        # The number of lock waits
        self._wait_lock_count = 0

        # The number of locks with condition
        self._lock_cond_count = 0

        # The number of failed lock with condition
        self._lock_cond_failed_count = 0

        # The total time spend for lock wait requests
        self._lock_wait_time_ns = 0

        # A list with the requested locks
        self._requested_locks = []

    @property
    def direct_lock_count(self):
        return self._direct_lock_count

    @direct_lock_count.setter
    def direct_lock_count(self, value):
        self._direct_lock_count = value

    @property
    def direct_lock_or_wait_count(self):
        return self._direct_lock_or_wait_count

    @direct_lock_or_wait_count.setter
    def direct_lock_or_wait_count(self, value):
        self._direct_lock_or_wait_count = value

    @property
    def wait_lock_count(self):
        return self._wait_lock_count

    @wait_lock_count.setter
    def wait_lock_count(self, value):
        self._wait_lock_count = value

    @property
    def lock_cond_count(self):
        return self._lock_cond_count

    @lock_cond_count.setter
    def lock_cond_count(self, value):
        self._lock_cond_count = value

    @property
    def lock_cond_failed_count(self):
        return self._lock_cond_failed_count

    @lock_cond_failed_count.setter
    def lock_cond_failed_count(self, value):
        self._lock_cond_failed_count = value

    @property
    def lock_wait_time_ns(self):
        return self._lock_wait_time_ns

    @lock_wait_time_ns.setter
    def lock_wait_time_ns(self, value):
        self._lock_wait_time_ns = value

    @property
    def requested_locks(self):
        return self._requested_locks

    @requested_locks.setter
    def requested_locks(self, lock_type):
        self._requested_locks.append(lock_type)


class PGLWLockTracer:
    def __init__(self, prog_args):
        self.bpf_instance = None
        self.usdts = None
        self.prog_args = prog_args
        self.statistics = {}

        # Variables for lock timing
        self.last_lock_request_time = {}

    def update_statistics(self, event, tranche, lock_mode):
        """
        Update the statistics
        """

        if tranche not in self.statistics:
            self.statistics[tranche] = LockStatisticsEntry()

        statistics_entry = self.statistics.get(tranche)

        # Lock directly requested
        if event.event_type == Events.LOCK:
            statistics_entry.direct_lock_count += 1
            statistics_entry.requested_locks = lock_mode
            return

        # Direct lock or wait
        if event.event_type == Events.LOCK_OR_WAIT:
            statistics_entry.direct_lock_or_wait_count += 1
            statistics_entry.requested_locks = lock_mode
            return

        # Wait for lock
        if event.event_type == Events.WAIT_START:
            statistics_entry.wait_lock_count += 1
            self.last_lock_request_time[event.pid] = event.timestamp
            return

        # Wait for lock done
        if event.event_type == Events.WAIT_DONE:
            wait_time = self.get_lock_wait_time(event)
            statistics_entry.lock_wait_time_ns += wait_time
            return

        # Acquire with condition
        if event.event_type == Events.COND_ACQUIRE:
            statistics_entry.lock_cond_count += 1
            statistics_entry.requested_locks = lock_mode
            return

        # Acquire with condition not possible
        if event.event_type == Events.COND_ACQUIRE_FAIL:
            statistics_entry.lock_cond_failed_count += 1
            statistics_entry.requested_locks = lock_mode
            return

    def get_lock_wait_time(self, event):
        """
        Get the last lock wait time (WAIT_START updates
        last_lock_request_time).
        """
        if event.event_type != Events.WAIT_DONE:
            return None

        return event.timestamp - self.last_lock_request_time[event.pid]

    @staticmethod
    def resolve_lock_mode(event):
        """
        Resolve the LW Lock modes
        """
        if event.mode == 0:  # LW_EXCLUSIVE,
            return "LW_EXCLUSIVE"

        if event.mode == 1:  # LW_SHARED
            return "LW_SHARED"

        if event.mode == 2:  # LW_WAIT_UNTIL_FREE
            return "LW_WAIT_UNTIL_FREE"

        raise Exception(f"Unknown event type {event.event_type}")

    def print_lock_event(self, _cpu, data, _size):
        """
        Print a new lock event.

        Developer note:
        Wait events can be tested with second PostgreSQL process and gdb
        b LWLockAcquireOrWait
        """
        event = self.bpf_instance["lockevents"].event(data)
        tranche = event.tranche.decode("utf-8")

        print_prefix = f"{event.timestamp} [Pid {event.pid}]"
        lock_mode = PGLWLockTracer.resolve_lock_mode(event)

        self.update_statistics(event, tranche, lock_mode)

        if event.event_type == Events.LOCK:
            print(f"{print_prefix} Lock {tranche} / mode {lock_mode}")
        elif event.event_type == Events.LOCK_OR_WAIT:
            print(f"{print_prefix} Lock or wait {tranche} / mode {lock_mode}")
        elif event.event_type == Events.LOCK_OR_WAIT_FAIL:
            print(f"{print_prefix} Lock or wait FAILED {tranche} / mode {lock_mode}")
        elif event.event_type == Events.UNLOCK:
            print(f"{print_prefix} Unlock {tranche}")
        elif event.event_type == Events.WAIT_START:
            print(f"{print_prefix} Wait for {tranche}")
        elif event.event_type == Events.WAIT_DONE:
            lock_time = self.get_lock_wait_time(event)
            print(f"{print_prefix} Lock for {tranche} was acquired in {lock_time} ns")
        elif event.event_type == Events.COND_ACQUIRE:
            print(f"{print_prefix} Lock {tranche} with condition / mode {lock_mode}")
        elif event.event_type == Events.COND_ACQUIRE_FAIL:
            print(
                f"{print_prefix} Lock {tranche} with condition failed / mode {lock_mode}"
            )
        else:
            raise Exception(f"Unknown event type {event.event_type}")

    def init(self):
        """
        Compile and load the BPF program
        """
        print(f"==> Attaching to PIDs {self.prog_args.pids}")
        self.usdts = list(map(lambda pid: USDT(pid=pid), self.prog_args.pids))

        # See https://www.postgresql.org/docs/15/dynamic-trace.html
        for usdt in self.usdts:
            usdt.enable_probe("lwlock__acquire", "lwlock_acquire")
            usdt.enable_probe("lwlock__acquire__or__wait", "lwlock_acquire_or_wait")
            usdt.enable_probe(
                "lwlock__acquire__or__wait__fail", "lwlock_acquire_or_wait_fail"
            )
            usdt.enable_probe("lwlock__release", "lwlock_release")
            usdt.enable_probe("lwlock__wait__start", "lwlock_wait_start")
            usdt.enable_probe("lwlock__wait__done", "lwlock_wait_done")
            usdt.enable_probe("lwlock__condacquire", "lwlock_condacquire")
            usdt.enable_probe("lwlock__condacquire__fail", "lwlock_condacquire_fail")

        if self.prog_args.verbose:
            print("=======")
            print("\n".join(map(lambda u: u.get_text(), self.usdts)))
            print("=======")

        enum_defines = BPFHelper.enum_to_defines(Events, "EVENT")
        bpf_program = BPFHelper.read_bpf_program("pg_lw_lock_tracer.c")
        bpf_program_final = bpf_program.replace("__DEFINES__", enum_defines)

        if self.prog_args.verbose:
            print(bpf_program_final)

        self.bpf_instance = BPF(text=bpf_program_final, usdt_contexts=self.usdts)

        self.bpf_instance["lockevents"].open_perf_buffer(
            self.print_lock_event, page_cnt=64
        )

    def print_statistics(self):
        """
        Print lock statistics
        """
        print("\nLock statistics:\n================")

        # Tranche lock statistics
        print("\nLocks per tranche")
        table = PrettyTable(
            [
                "Tranche Name",
                "Direct Grant",
                "Grant or wait until free",
                "With condition",
                "Failed conditions",
                "Waits",
                "Wait time (ns)",
            ]
        )

        for key in sorted(self.statistics):
            statistics = self.statistics[key]
            table.add_row(
                [
                    key,
                    statistics.direct_lock_count,
                    statistics.direct_lock_or_wait_count,
                    statistics.lock_cond_count,
                    statistics.lock_cond_failed_count,
                    statistics.wait_lock_count,
                    statistics.lock_wait_time_ns,
                ]
            )

        print(table)

        # Type lock statistics
        print("\nLocks per type")
        table = PrettyTable(["Lock type", "Requests"])

        # Map: Key = Lock type, Value = Number of requested locks
        requested_locks = {}

        for statistics in self.statistics.values():
            for lock_type in statistics.requested_locks:
                locks = requested_locks.get(lock_type, 0) + 1
                requested_locks[lock_type] = locks

        for lock_type in sorted(requested_locks):
            locks = requested_locks[lock_type]
            table.add_row([lock_type, locks])

        print(table)

    def run(self):
        """
        Run the BPF program and read results
        """
        print("===> Ready to trace")
        while True:
            try:
                self.bpf_instance.perf_buffer_poll()
            except KeyboardInterrupt:
                if self.prog_args.statistics:
                    self.print_statistics()
                sys.exit(0)


def main():
    """
    Entry point for the BPF based PostgreSQL LW lock tracer.
    """
    args = parser.parse_args()

    pg_lock_tracer = PGLWLockTracer(args)
    pg_lock_tracer.init()

    if not args.dry_run:
        pg_lock_tracer.run()


if __name__ == "__main__":
    main()
