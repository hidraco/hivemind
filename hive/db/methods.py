from funcy.seqs import first
from hive.db import conn
from hive.db.schema import (
    hive_follows,
)
from sqlalchemy import text, select, func
from decimal import Decimal

import time
import re
import atexit


class QueryStats:
    stats = {}
    ttltime = 0.0

    @classmethod
    def log(cls, sql, ms):
        nsql = re.sub('\s+', ' ', sql).strip()[0:256] #normalize
        nsql = re.sub('VALUES (\s*\([^\)]+\),?)+', 'VALUES (...)', nsql)
        if nsql not in cls.stats:
            cls.stats[nsql] = [0, 0]
        cls.stats[nsql][0] += ms
        cls.stats[nsql][1] += 1
        cls.ttltime += ms
        if cls.ttltime > 30 * 60 * 1000:
            cls.print()

    @classmethod
    def print(cls):
        ttl = cls.ttltime
        print("[DEBUG] total SQL time: {}s".format(int(ttl / 1000)))
        for arr in sorted(cls.stats.items(), key=lambda x:-x[1][0])[0:40]:
            sql, vals = arr
            ms, calls = vals
            print("% 5.1f%% % 10.2fms % 8.2favg % 5dx -- %s" % (100 * ms/ttl, ms, ms/calls, calls, sql[0:180]))
        cls.stats = {}
        cls.ttltime = 0

atexit.register(QueryStats.print)

# generic
# -------
def query(sql, **kwargs):
    ti = time.time()
    query = text(sql).execution_options(autocommit=False)
    res = conn.execute(query, **kwargs)
    ms = (time.time() - ti) * 1000
    QueryStats.log(sql, ms)
    if ms > 100:
        disp = re.sub('\s+', ' ', sql).strip()[:200]
        print("\033[93m[SQL][{}ms] {}\033[0m".format(int(ms), disp))
    return res

# n*m
def query_all(sql, **kwargs):
    res = query(sql, **kwargs)
    return res.fetchall()

# 1*m
def query_row(sql, **kwargs):
    res = query(sql, **kwargs)
    return first(res)

# n*1
def query_col(sql, **kwargs):
    res = query(sql, **kwargs).fetchall()
    return [r[0] for r in res]

# 1*1
def query_one(sql, **kwargs):
    row = query_row(sql, **kwargs)
    if row:
        return first(row)


def db_head_state():
    sql = "SELECT num,created_at,UNIX_TIMESTAMP(CONVERT_TZ(created_at, '+00:00', 'SYSTEM')) ts FROM hive_blocks ORDER BY num DESC LIMIT 1"
    row = query_row(sql)
    return dict(db_head_block = row['num'],
                db_head_time = row['created_at'],
                db_head_age = int(time.time() - row['ts']))

def db_last_block():
    return query_one("SELECT MAX(num) FROM hive_blocks") or 0


# api specific
# ------------
def get_followers(account: str, skip: int, limit: int):
    sql = """
    SELECT follower, created_at FROM hive_follows WHERE following = :account
    AND state = 1 ORDER BY created_at DESC LIMIT :limit OFFSET :skip
    """
    res = query(sql, account=account, skip=int(skip), limit=int(limit))
    return [[r[0],r[1]] for r in res.fetchall()]


def get_following(account: str, skip: int, limit: int):
    sql = """
    SELECT following, created_at FROM hive_follows WHERE follower = :account
    AND state = 1 ORDER BY created_at DESC LIMIT :limit OFFSET :skip
    """
    res = query(sql, account=account, skip=int(skip), limit=int(limit))
    return [[r[0],r[1]] for r in res.fetchall()]


def following_count(account: str):
    sql = "SELECT COUNT(*) FROM hive_follows WHERE follower = :a AND state = 1"
    return query_one(sql, a=account)


def follower_count(account: str):
    sql = "SELECT COUNT(*) FROM hive_follows WHERE following = :a AND state = 1"
    return query_one(sql, a=account)


# evaluate replacing two above methods with this
def follow_stats(account: str):
    sql = """
    SELECT SUM(IF(follower  = :account, 1, 0)) following,
           SUM(IF(following = :account, 1, 0)) followers
      FROM hive_follows
     WHERE state = 1
    """
    return first(query(sql))

# all completed payouts
def payouts_total():
    # memoized historical sum. To update:
    #  SELECT SUM(payout) FROM hive_posts_cache
    #  WHERE is_paidout = 1 AND payout_at <= precalc_date
    precalc_date = '2017-08-30 00:00:00'
    precalc_sum = Decimal('19358777.541')

    # sum all payouts since `precalc_date`
    sql = """
      SELECT SUM(payout) FROM hive_posts_cache
      WHERE is_paidout = 1 AND payout_at > '%s'
    """ % (precalc_date)

    return precalc_sum + query_one(sql)

# sum of completed payouts last 24 hrs
def payouts_last_24h():
    sql = """
      SELECT SUM(payout) FROM hive_posts_cache
      WHERE is_paidout = 1 AND payout_at > DATE_SUB(NOW(), INTERVAL 24 HOUR)
    """
    return query_one(sql)

# unused
def get_reblogs_since(account: str, since: str):
    sql = """
      SELECT r.* FROM hive_reblogs r JOIN hive_posts p ON r.post_id = p.id
       WHERE p.author = :account AND r.created_at > :since
    ORDER BY r.created_at DESC
    """
    return [dict(r) for r in query_all(sql, account=account, since=since)]


# given an array of post ids, returns full metadata in the same order
def get_posts(ids, context = None):
    sql = """
    SELECT post_id, author, permlink, title, preview, img_url, payout,
           promoted, created_at, payout_at, is_nsfw, rshares, votes, json
      FROM hive_posts_cache WHERE post_id IN :ids
    """

    reblogged_ids = []
    if context:
        reblogged_ids = query_col("SELECT post_id FROM hive_reblogs WHERE account = :a AND post_id IN :ids", a=context, ids=ids)

    # key by id so we can return sorted by input order
    posts_by_id = {}
    for row in query(sql, ids=ids).fetchall():
        obj = dict(row)

        if context:
            voters = [csa.split(",")[0] for csa in obj['votes'].split("\n")]
            obj['user_state'] = {
                'reblogged': row['post_id'] in reblogged_ids,
                'voted': context in voters
            }

        obj.pop('votes') # temp
        obj.pop('json')  # temp
        posts_by_id[row['post_id']] = obj

    # in rare cases of cache inconsistency, recover and warn
    missed = set(ids) - posts_by_id.keys()
    if missed:
        print("WARNING: get_posts do not exist in cache: {}".format(missed))
        for id in missed:
            ids.remove(id)

    return [posts_by_id[id] for id in ids]


# builds SQL query to pull a list of posts for any sort order or tag
# sort can be: trending hot new promoted
def get_discussions_by_sort_and_tag(sort, tag, skip, limit, context = None):
    if skip > 5000:
        raise Exception("cannot skip {} results".format(skip))
    if limit > 100:
        raise Exception("cannot limit {} results".format(limit))

    order = ''
    where = []
    table = 'hive_posts_cache'
    col   = 'post_id'

    # TODO: all discussions need a depth == 0 condition?
    if sort == 'trending':
        order = 'sc_trend DESC'
    elif sort == 'hot':
        order = 'sc_hot DESC'
    elif sort == 'new':
        order = 'id DESC'
        where.append('depth = 0')
        table = 'hive_posts'
        col = 'id'
    elif sort == 'promoted':
        order = 'promoted DESC'
        where.append('is_paidout = 0')
        where.append('promoted > 0')
    else:
        raise Exception("unknown sort order {}".format(sort))

    if tag:
        id_col = 'post_id'
        if table == 'hive_posts':
            id_col = 'id'
        where.append('%s IN (SELECT post_id FROM hive_post_tags WHERE tag = :tag)' % (id_col))

    if where:
        where = 'WHERE ' + ' AND '.join(where)
    else:
        where = ''

    sql = "SELECT %s FROM %s %s ORDER BY %s LIMIT :limit OFFSET :skip" % (col, table, where, order)
    ids = [r[0] for r in query(sql, tag=tag, limit=limit, skip=skip).fetchall()]
    return get_posts(ids, context)


# returns "homepage" feed for specified account
def get_user_feed(account: str, skip: int, limit: int, context: str = None):
    sql = """
      SELECT post_id, GROUP_CONCAT(account) accounts
        FROM hive_feed_cache
       WHERE account IN (SELECT following FROM hive_follows
                          WHERE follower = :account AND state = 1)
    GROUP BY post_id
    ORDER BY MIN(created_at) DESC LIMIT :limit OFFSET :skip
    """
    res = query_all(sql, account = account, skip = skip, limit = limit)
    posts = get_posts([r[0] for r in res], context)

    # Merge reblogged_by data into result set
    accts = dict(res)
    for post in posts:
        rby = set(accts[post['post_id']].split(','))
        rby.discard(post['author'])
        if rby:
            post['reblogged_by'] = list(rby)

    return posts


# returns a blog feed (posts and reblogs from the specified account)
def get_blog_feed(account: str, skip: int, limit: int, context: str = None):
    #sql = """
    #    SELECT id, created_at
    #      FROM hive_posts
    #     WHERE depth = 0 AND is_deleted = 0 AND author = :account
    # UNION ALL
    #    SELECT post_id, created_at
    #      FROM hive_reblogs
    #     WHERE account = :account AND (SELECT is_deleted FROM hive_posts
    #                                   WHERE id = post_id) = 0
    #  ORDER BY created_at DESC
    #     LIMIT :limit OFFSET :skip
    #"""
    sql = ("SELECT post_id FROM hive_feed_cache WHERE account = :account "
            "ORDER BY created_at DESC LIMIT :limit OFFSET :skip")
    post_ids = query_col(sql, account = account, skip = skip, limit = limit)
    return get_posts(post_ids, context)


def get_related_posts(account: str, permlink: str):
    sql = """
      SELECT p2.id
        FROM hive_posts p1
        JOIN hive_posts p2 ON p1.category = p2.category
        JOIN hive_posts_cache pc ON p2.id = pc.post_id
       WHERE p1.author = :a AND p1.permlink = :p
         AND sc_trend > :t AND p1.id != p2.id
    ORDER BY sc_trend DESC LIMIT 5
    """
    thresh = time.time() / 480000
    post_ids = query_col(sql, a=account, p=permlink, t=thresh)
    return get_posts(post_ids)


