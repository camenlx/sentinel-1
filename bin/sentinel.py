#!/usr/bin/env python
import sys
import os
sys.path.append(os.path.normpath(os.path.join(os.path.dirname(__file__), '../lib')))
import init
import config
import misc
from absoluted import AbsoluteDaemon
from models import Superblock, Proposal, GovernanceObject, Watchdog
from models import VoteSignals, VoteOutcomes, Transient
import socket
from misc import printdbg
import time
from bitcoinrpc.authproxy import JSONRPCException
import signal
import atexit
import random
from scheduler import Scheduler
import argparse


# sync absoluted gobject list with our local relational DB backend
def perform_absoluted_object_sync(absoluted):
    GovernanceObject.sync(absoluted)


# delete old watchdog objects, create new when necessary
def watchdog_check(absoluted):
    printdbg("in watchdog_check")

    # delete expired watchdogs
    for wd in Watchdog.expired(absoluted):
        printdbg("\tFound expired watchdog [%s], voting to delete" % wd.object_hash)
        wd.vote(absoluted, VoteSignals.delete, VoteOutcomes.yes)

    # now, get all the active ones...
    active_wd = Watchdog.active(absoluted)
    active_count = active_wd.count()

    # none exist, submit a new one to the network
    if 0 == active_count:
        # create/submit one
        printdbg("\tNo watchdogs exist... submitting new one.")
        wd = Watchdog(created_at=int(time.time()))
        wd.submit(absoluted)

    else:
        wd_list = sorted(active_wd, key=lambda wd: wd.object_hash)

        # highest hash wins
        winner = wd_list.pop()
        printdbg("\tFound winning watchdog [%s], voting VALID" % winner.object_hash)
        winner.vote(absoluted, VoteSignals.valid, VoteOutcomes.yes)

        # if remaining Watchdogs exist in the list, vote delete
        for wd in wd_list:
            printdbg("\tFound losing watchdog [%s], voting DELETE" % wd.object_hash)
            wd.vote(absoluted, VoteSignals.delete, VoteOutcomes.yes)

    printdbg("leaving watchdog_check")


def prune_expired_proposals(absoluted):
    # vote delete for old proposals
    for proposal in Proposal.expired(absoluted.superblockcycle()):
        proposal.vote(absoluted, VoteSignals.delete, VoteOutcomes.yes)


# ping absoluted
def sentinel_ping(absoluted):
    printdbg("in sentinel_ping")

    absoluted.ping()

    printdbg("leaving sentinel_ping")


def attempt_superblock_creation(absoluted):
    import absolutelib

    if not absoluted.is_masternode():
        print("We are not a Masternode... can't submit superblocks!")
        return

    # query votes for this specific ebh... if we have voted for this specific
    # ebh, then it's voted on. since we track votes this is all done using joins
    # against the votes table
    #
    # has this masternode voted on *any* superblocks at the given event_block_height?
    # have we voted FUNDING=YES for a superblock for this specific event_block_height?

    event_block_height = absoluted.next_superblock_height()

    if Superblock.is_voted_funding(event_block_height):
        # printdbg("ALREADY VOTED! 'til next time!")

        # vote down any new SBs because we've already chosen a winner
        for sb in Superblock.at_height(event_block_height):
            if not sb.voted_on(signal=VoteSignals.funding):
                sb.vote(absoluted, VoteSignals.funding, VoteOutcomes.no)

        # now return, we're done
        return

    if not absoluted.is_govobj_maturity_phase():
        printdbg("Not in maturity phase yet -- will not attempt Superblock")
        return

    proposals = Proposal.approved_and_ranked(proposal_quorum=absoluted.governance_quorum(), next_superblock_max_budget=absoluted.next_superblock_max_budget())
    budget_max = absoluted.get_superblock_budget_allocation(event_block_height)
    sb_epoch_time = absoluted.block_height_to_epoch(event_block_height)

    sb = absolutelib.create_superblock(proposals, event_block_height, budget_max, sb_epoch_time)
    if not sb:
        printdbg("No superblock created, sorry. Returning.")
        return

    # find the deterministic SB w/highest object_hash in the DB
    dbrec = Superblock.find_highest_deterministic(sb.hex_hash())
    if dbrec:
        dbrec.vote(absoluted, VoteSignals.funding, VoteOutcomes.yes)

        # any other blocks which match the sb_hash are duplicates, delete them
        for sb in Superblock.select().where(Superblock.sb_hash == sb.hex_hash()):
            if not sb.voted_on(signal=VoteSignals.funding):
                sb.vote(absoluted, VoteSignals.delete, VoteOutcomes.yes)

        printdbg("VOTED FUNDING FOR SB! We're done here 'til next superblock cycle.")
        return
    else:
        printdbg("The correct superblock wasn't found on the network...")

    # if we are the elected masternode...
    if (absoluted.we_are_the_winner()):
        printdbg("we are the winner! Submit SB to network")
        sb.submit(absoluted)


def check_object_validity(absoluted):
    # vote (in)valid objects
    for gov_class in [Proposal, Superblock]:
        for obj in gov_class.select():
            obj.vote_validity(absoluted)


def is_absoluted_port_open(absoluted):
    # test socket open before beginning, display instructive message to MN
    # operators if it's not
    port_open = False
    try:
        info = absoluted.rpc_command('getgovernanceinfo')
        port_open = True
    except (socket.error, JSONRPCException) as e:
        print("%s" % e)

    return port_open


def main():
    absoluted = AbsoluteDaemon.from_absolute_conf(config.absolute_conf)
    options = process_args()
    
    # check absoluted connectivity
    if not is_absoluted_port_open(absoluted):
        print("Cannot connect to absoluted. Please ensure absoluted is running and the JSONRPC port is open to Sentinel.")
        return

    # check absoluted sync
    if not absoluted.is_synced():
        print("absoluted not synced with network! Awaiting full sync before running Sentinel.")
        return

    # ensure valid masternode
    if not absoluted.is_masternode():
        print("Invalid Masternode Status, cannot continue.")
        return

    # register a handler if SENTINEL_DEBUG is set
    if os.environ.get('SENTINEL_DEBUG', None):
        import logging
        logger = logging.getLogger('peewee')
        logger.setLevel(logging.DEBUG)
        logger.addHandler(logging.StreamHandler())

    if options.bypass:
        # bypassing scheduler, remove the scheduled event
        printdbg("--bypass-schedule option used, clearing schedule")
        Scheduler.clear_schedule()

    if not Scheduler.is_run_time():
        printdbg("Not yet time for an object sync/vote, moving on.")
        return

    if not options.bypass:
        # delay to account for cron minute sync
        Scheduler.delay()

    # running now, so remove the scheduled event
    Scheduler.clear_schedule()

    # ========================================================================
    # general flow:
    # ========================================================================
    #
    # load "gobject list" rpc command data, sync objects into internal database
    perform_absoluted_object_sync(absoluted)

    if absoluted.has_sentinel_ping:
        sentinel_ping(absoluted)
    else:
        # delete old watchdog objects, create a new if necessary
        watchdog_check(absoluted)

    # auto vote network objects as valid/invalid
    # check_object_validity(absoluted)

    # vote to delete expired proposals
    prune_expired_proposals(absoluted)

    # create a Superblock if necessary
    attempt_superblock_creation(absoluted)

    # schedule the next run
    Scheduler.schedule_next_run()


def signal_handler(signum, frame):
    print("Got a signal [%d], cleaning up..." % (signum))
    Transient.delete('SENTINEL_RUNNING')
    sys.exit(1)


def cleanup(mutex_key):
    Transient.delete(mutex_key)


def process_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-b', '--bypass-scheduler',
                        action='store_true',
                        help='Bypass scheduler and sync/vote immediately',
                        dest='bypass',
                        default=False, required=False)
    args = parser.parse_known_args()

    return args


def entrypoint():
    # ensure another instance of Sentinel pointing at the same config
    # is not currently running
    mutex_key = 'SENTINEL_RUNNING_' + config.absolute_conf
    
    atexit.register(cleanup, mutex_key)
    signal.signal(signal.SIGINT, signal_handler)

    
    # assume that all processes expire after 'timeout_seconds' seconds
    timeout_seconds = 90

    is_running = Transient.get(mutex_key)
    if is_running:
        printdbg("An instance of Sentinel is already running -- aborting.")
        sys.exit(1)
    else:
        Transient.set(mutex_key, misc.now(), timeout_seconds)

    # locked to this instance -- perform main logic here
    main()

    Transient.delete(mutex_key)

if __name__ == '__main__':
    entrypoint()
