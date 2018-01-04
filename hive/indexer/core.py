import json
import logging
import glob
import time
import os

from funcy.seqs import drop
from toolz import partition_all

from hive.db.db_state import DbState
from hive.db.methods import query_one, query_all, query

from hive.indexer.accounts import Accounts
from hive.indexer.posts import Posts
from hive.indexer.cached_post import CachedPost
from hive.indexer.feed_cache import FeedCache
from hive.indexer.custom_op import CustomOp

from hive.indexer.steem_client import get_adapter

log = logging.getLogger(__name__)

# block-level routines
# --------------------

def db_last_block():
    return query_one("SELECT MAX(num) FROM hive_blocks") or 0

def db_last_block_date():
    return query_one("SELECT created_at FROM hive_blocks ORDER BY num DESC LIMIT 1")

# process a single block. always wrap in a transaction!
def process_block(block):
    date = block['timestamp']
    block_id = block['block_id']
    prev = block['previous']
    num = int(block_id[:8], base=16)
    txs = block['transactions']
    ops = sum([len(tx['operations']) for tx in txs])

    query("INSERT INTO hive_blocks (num, hash, prev, txs, ops, created_at) "
          "VALUES (:num, :hash, :prev, :txs, :ops, :date)",
          num=num, hash=block_id, prev=prev, txs=len(txs), ops=ops, date=date)

    account_names = set()
    comment_ops = []
    json_ops = []
    delete_ops = []
    dirty_urls = set()
    for tx in txs:
        for operation in tx['operations']:
            op_type, op = operation

            if op_type == 'pow':
                account_names.add(op['worker_account'])
            elif op_type == 'pow2':
                account_names.add(op['work'][1]['input']['worker_account'])
            elif op_type == 'account_create':
                account_names.add(op['new_account_name'])
            elif op_type == 'account_create_with_delegation':
                account_names.add(op['new_account_name'])
            elif op_type == 'comment':
                comment_ops.append(op)
                dirty_urls.add(op['author']+'/'+op['permlink'])
                Accounts.dirty(op['author'])
            elif op_type == 'delete_comment':
                delete_ops.append(op)
            elif op_type == 'custom_json':
                json_ops.append(op)
            elif op_type == 'vote':
                dirty_urls.add(op['author']+'/'+op['permlink'])
                Accounts.dirty(op['author'])

    Accounts.register(account_names, date) # register potentially new names
    Posts.register(comment_ops, date) # ignores edits; inserts, validates
    Posts.delete(delete_ops)  # unallocates hive_posts record, delete cache
    CustomOp.process_ops(json_ops, num, date) # follow, reblog, community ops

    # return all posts modified this block
    return dirty_urls


# batch-process blocks, wrap in a transaction
def process_blocks(blocks, is_initial_sync=False):
    dirty = set()
    query("START TRANSACTION")
    for block in blocks:
        if is_initial_sync:
            process_block(block)
        else:
            dirty |= process_block(block)
    query("COMMIT")
    return dirty



# sync routines
# -------------

def sync_from_checkpoints():
    last_block = db_last_block()

    _fn = lambda f: [int(f.split('/')[-1].split('.')[0]), f]
    mydir = os.path.dirname(os.path.realpath(__file__ + "/../.."))
    files = map(_fn, glob.glob(mydir + "/checkpoints/*.json.lst"))
    files = sorted(files, key=lambda f: f[0])

    last_read = 0
    for (num, path) in files:
        if last_block < num:
            print("[SYNC] Load {} -- last block: {}".format(path, last_block))
            skip_lines = last_block - last_read
            sync_from_file(path, skip_lines, 250)
            last_block = num
        last_read = num


def sync_from_file(file_path, skip_lines, chunk_size=250):
    with open(file_path) as f:
        # each line in file represents one block
        # we can skip the blocks we already have
        remaining = drop(skip_lines, f)
        for batch in partition_all(chunk_size, remaining):
            process_blocks(map(json.loads, batch), True)


def sync_from_steemd():
    is_initial_sync = DbState.is_initial_sync()
    steemd = get_adapter()
    dirty = set()

    lbound = db_last_block() + 1
    ubound = steemd.last_irreversible_block_num()

    if ubound > lbound:
        print("[SYNC] start block %d, +%d to sync" % (lbound, ubound-lbound+1))

    while lbound < ubound:
        to = min(lbound + 1000, ubound)

        lap_0 = time.perf_counter()
        blocks = steemd.get_blocks_range(lbound, to)
        lap_1 = time.perf_counter()
        dirty |= process_blocks(blocks, is_initial_sync)
        lap_2 = time.perf_counter()

        rate = (to - lbound) / (lap_2 - lap_0)
        rps = int((to - lbound) / (lap_1 - lap_0))
        wps = int((to - lbound) / (lap_2 - lap_1))
        print("[SYNC] Got block {} ({}/s, {}rps {}wps) -- {}m remaining".format(
            to-1, round(rate, 1), rps, wps, round((ubound-to) / rate / 60, 2)))

        lbound = to

    # batch update post cache after catching up to head block
    if not is_initial_sync:
        cache_dirty_posts(dirty, trx=True)
        Accounts.cache_dirty()
        Accounts.cache_dirty_follows()


def listen_steemd(trail_blocks=2):
    assert trail_blocks >= 0
    assert trail_blocks < 25
    steemd = get_adapter()
    curr_block = db_last_block()
    last_hash = False

    while True:
        curr_block = curr_block + 1

        # if trailing too close, take a pause
        while trail_blocks:
            gap = steemd.head_block() - curr_block
            if gap >= 25:
                print("[HIVE] gap too large: %d -- abort listen mode" % gap)
                return
            if gap >= trail_blocks:
                break
            time.sleep(0.5)

        # get the target block; if DNE, pause and retry
        block = steemd.get_block(curr_block)
        while not block:
            time.sleep(0.5)
            block = steemd.get_block(curr_block)

        # ensure the block we received links to our last
        if last_hash and last_hash != block['previous']:
            # this condition is very rare unless trail_blocks is 0 and fork is
            # encountered; to handle gracefully, implement a pop_block method
            raise Exception("Unlinkable block: have {}, got {} -> {})".format(
                last_hash, block['previous'], block['block_id']))
        last_hash = block['block_id']

        start_time = time.perf_counter()
        query("START TRANSACTION")

        dirty = process_block(block)
        edits = cache_dirty_posts(dirty, trx=False, date=block['timestamp'])
        paids = cache_paidout_posts(trx=False, date=block['timestamp'])
        accts = Accounts.cache_dirty()
        follows = Accounts.cache_dirty_follows()

        query("COMMIT")
        secs = time.perf_counter() - start_time

        print("[LIVE] Got block %d at %s with %d txs -- %d posts, %d payouts, %d accounts, %d follows -- %dms%s"
              % (curr_block, block['timestamp'], len(block['transactions']),
                 edits, paids, accts, follows, int(secs * 1e3), ' SLOW' if secs > 1 else ''))

        # once a minute, update chain props
        if curr_block % 20 == 0:
            update_chain_state()

        # approx once per hour, update accounts
        if curr_block % 1200 == 0:
            print("[HIVE] Performing account maintenance...")
            Accounts.cache_oldest(10000)
            Accounts.update_ranks()


def select_missing_tuples(start_id, limit=1000000):
    sql = """SELECT id, author, permlink FROM hive_posts
              WHERE is_deleted = '0' AND id > :id
           ORDER BY id LIMIT :limit"""
    return query_all(sql, id=start_id, limit=limit)

def cache_missing_posts():
    # cached posts inserted sequentially, so compare MAX(id)'s
    last_cached_id = CachedPost.last_id()
    last_post_id = Posts.last_id()
    gap = last_post_id - last_cached_id
    print("[INIT] {} missing post cache entries".format(gap))
    if not gap:
        return

    missing = select_missing_tuples(last_cached_id)
    CachedPost.update_batch(missing, get_adapter())

    # repeat until no gap
    cache_missing_posts()


def cache_paidout_posts(trx=True, date=None):
    steemd = get_adapter()
    if not date:
        date = db_last_block_date()
    paidout = CachedPost.select_paidout_tuples(date)
    for (_id, author, permlink) in paidout:
        Accounts.dirty(author)
    if trx or len(paidout) > 1000:
        print("[PREP] Process {} payouts since {}".format(len(paidout), date))
    CachedPost.update_batch(paidout, steemd, date, trx)
    return len(paidout)


def cache_dirty_posts(dirty, trx=True, date=None):
    steemd = get_adapter()
    if not date:
        date = steemd.head_time()
    tups = Posts.urls_to_tuples(dirty)
    if trx or len(tups) > 1000:
        print("[PREP] Update {} edited posts".format(len(dirty)))
    CachedPost.update_batch(tups, steemd, date, trx)
    return len(tups)


# refetch dynamic_global_properties, feed price, etc
def update_chain_state():
    state = get_adapter().gdgp_extended()
    query("""UPDATE hive_state SET block_num = :block_num,
             steem_per_mvest = :spm, usd_per_steem = :ups,
             sbd_per_steem = :sps, dgpo = :dgpo""",
          block_num=state['dgpo']['head_block_number'],
          spm=state['steem_per_mvest'],
          ups=state['usd_per_steem'],
          sps=state['sbd_per_steem'],
          dgpo=json.dumps(state['dgpo']))


def run():

    # make sure db schema is up to date, perform checks
    DbState.initialize()

    # prefetch id->name memory map
    Accounts.load_ids()

    if DbState.is_initial_sync():
        print("[INIT] *** Initial fast sync ***")
        sync_from_checkpoints()
        sync_from_steemd()

        print("[INIT] *** Initial cache build ***")
        # todo: disable indexes during this process
        cache_missing_posts()
        FeedCache.rebuild()

        DbState.finish_initial_sync()

    else:
        # perform cleanup in case process did not exit cleanly
        cache_missing_posts()

    while True:
        sync_from_steemd()
        cache_paidout_posts()
        listen_steemd()


def head_state(*args):
    _ = args  # JSONRPC injects 4 arguments here
    steemd_head = get_adapter().head_block()
    hive_head = db_last_block()
    diff = steemd_head - hive_head
    return dict(steemd=steemd_head, hive=hive_head, diff=diff)


if __name__ == '__main__':
    run()
