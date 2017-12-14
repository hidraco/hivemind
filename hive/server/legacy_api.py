from hive.db.methods import query, query_one, query_col, query_row, query_all


async def get_followers(account: str, start: str, follow_type: str, limit: int):
    limit = _validate_limit(limit, 1000)
    state = _follow_type_to_int(follow_type)
    account_id = _get_account_id(account)
    seek = ''

    if start:
        seek = """
          AND hf.created_at <= (
            SELECT created_at FROM hive_follows
             WHERE following = :account_id AND follower = %d AND state = :state)
        """ % _get_account_id(start)

    sql = """
        SELECT name FROM hive_follows hf
          JOIN hive_accounts ON hf.follower = id
         WHERE hf.following = :account_id AND state = :state %s
      ORDER BY hf.created_at DESC LIMIT :limit
    """ % seek

    res = query_col(sql, account_id=account_id, state=state, limit=int(limit))
    return [dict(follower=r, following=account, what=[follow_type])
            for r in res]


async def get_following(account: str, start: str, follow_type: str, limit: int):
    limit = _validate_limit(limit, 1000)
    state = _follow_type_to_int(follow_type)
    account_id = _get_account_id(account)
    seek = ''

    if start:
        seek = """
          AND hf.created_at <= (
            SELECT created_at FROM hive_follows
             WHERE follower = :account_id AND following = %d AND state = :state)
        """ % _get_account_id(start)

    sql = """
        SELECT name FROM hive_follows hf
          JOIN hive_accounts ON hf.following = id
         WHERE hf.follower = :account_id AND state = :state %s
      ORDER BY hf.created_at DESC LIMIT :limit
    """ % seek

    res = query_col(sql, account_id=account_id, state=state, limit=int(limit))
    return [dict(follower=account, following=r, what=[follow_type])
            for r in res]


async def get_follow_count(account: str):
    sql = """
        SELECT name as account,
               following as following_count,
               followers as follower_count
          FROM hive_accounts WHERE name = :n
    """
    return dict(query_row(sql, n=account))


async def get_discussions_by_trending(start_author: str, start_permlink: str = '', limit: int = 20, tag: str = None):
    return _get_discussions('trending', start_author, start_permlink, limit, tag)

async def get_discussions_by_hot(start_author: str, start_permlink: str = '', limit: int = 20, tag: str = None):
    return _get_discussions('hot', start_author, start_permlink, limit, tag)

async def get_discussions_by_promoted(start_author: str, start_permlink: str = '', limit: int = 20, tag: str = None):
    return _get_discussions('promoted', start_author, start_permlink, limit, tag)

async def get_discussions_by_created(start_author: str, start_permlink: str = '', limit: int = 20, tag: str = None):
    return _get_discussions('created', start_author, start_permlink, limit, tag)


# author blog
async def get_discussions_by_blog(tag: str, start_author: str = '', start_permlink: str = '', limit: int = 20):
    limit = _validate_limit(limit, 20)
    account_id = _get_account_id(tag)
    seek = ''

    if start_permlink:
        start_id = _get_post_id(start_author, start_permlink)
        seek = """
          AND created_at <= (
            SELECT created_at FROM hive_feed_cache
             WHERE account_id = :account_id AND post_id = %d)
        """ % start_id

    sql = """
        SELECT post_id FROM hive_feed_cache WHERE account_id = :account_id %s
      ORDER BY created_at DESC LIMIT :limit
    """ % seek

    ids = query_col(sql, account_id=account_id, limit=limit)
    return _get_posts(ids)


# author feed
async def get_discussions_by_feed(tag: str, start_author: str = '', start_permlink: str = '', limit: int = 20):
    limit = _validate_limit(limit, 20)
    account_id = _get_account_id(tag)
    seek = ''

    if start_permlink:
        start_id = _get_post_id(start_author, start_permlink)
        seek = """
          HAVING MIN(hive_feed_cache.created_at) <= (
            SELECT MIN(created_at) FROM hive_feed_cache WHERE post_id = %d
               AND account_id IN (SELECT following FROM hive_follows
                                  WHERE follower = :account AND state = 1))
        """ % start_id

    sql = """
        SELECT post_id, string_agg(name, ',') accounts
          FROM hive_feed_cache
          JOIN hive_follows ON account_id = hive_follows.following AND state = 1
          JOIN hive_accounts ON hive_follows.following = hive_accounts.id
         WHERE hive_follows.follower = :account
      GROUP BY post_id %s
      ORDER BY MIN(hive_feed_cache.created_at) DESC LIMIT :limit
    """ % (seek)

    res = query_all(sql, account=account_id, limit=limit)
    posts = _get_posts([r[0] for r in res])

    # Merge reblogged_by data into result set
    accts = dict(res)
    for post in posts:
        rby = set(accts[post['post_id']].split(','))
        rby.discard(post['author'])
        if rby:
            post['reblogged_by'] = list(rby)

    return posts


# author comments
async def get_discussions_by_comments(start_author: str, start_permlink: str = '', limit: int = 20):
    limit = _validate_limit(limit, 20)
    seek = ''

    if start_permlink:
        start_id = _get_post_id(start_author, start_permlink)
        seek = ("AND created_at <= (SELECT created_at FROM hive_posts WHERE id = %d)"
                % start_id)

    sql = """
        SELECT id FROM hive_posts WHERE author = :account %s AND depth > 0
      ORDER BY created_at DESC LIMIT :limit
    """ % seek

    ids = query_col(sql, account=start_author, limit=limit)
    return _get_posts(ids)


# author replies
async def get_replies_by_last_update(start_author: str, start_permlink: str = '', limit: int = 20):
    limit = _validate_limit(limit, 20)
    parent = start_author
    seek = ''

    if start_permlink:
        parent_id, start_date = query_row("""
          SELECT parent_id, created_at FROM hive_posts
           WHERE author = :a AND permlink = :p
        """, a=start_author, p=start_permlink)
        parent = query_one("SELECT author FROM hive_posts WHERE id = %d" % parent_id)
        seek = "AND created_at <= '%s'" % start_date

    sql = """
      SELECT id FROM hive_posts
      WHERE parent_id IN (SELECT id FROM hive_posts WHERE author = :parent) %s
      ORDER BY created_at DESC LIMIT :limit
    """ % seek

    ids = query_col(sql, parent=parent, limit=limit)
    return _get_posts(ids)

# https://github.com/steemit/steem/blob/06e67bd4aea73391123eca99e1a22a8612b0c47e/libraries/app/database_api.cpp#L1937
async def get_state(path: str):
    if path[0] == '/':
        path = path[1:]

    if not path:
        path = 'trending'

    state = {}
    state['current_route'] = path
    state['props'] = "TODO: get_dynamic_global_properties"
    state['feed_price'] = "TODO: get_current_median_history_price"
    state['tag_idx'] = {}
    state['tag_idx']['trending'] = "TODO: array of trending tags"

    part = path.split('/')

    if len(part) > 4:
        print("INVALID PATH: {}".format(path))
        raise Exception("invalid path")

    tag = path[1].lower()

    if part[0] and part[0][0] == '@':
        account = part[0][1:]
        state['accounts'][account] = dict(name=account, misc="TODO: add steem[extended_account] props")
        #state['accounts'][account].tags_usage = ?deprecated?
        state['accounts'][account].reputation = 2555 # TODO

        if part[1] == 'transfers':
            # TODO: get_account_history and filter.
            # goes to state['accounts'][account][transfer_history/other_history]
            raise Exception("not implemented")
        elif part[1] == 'recent-replies':
            #state['accounts'][account]
            # get_replies_by_last_update
            raise Exception("not implemented")
        elif part[1] == 'comments':
            #state['accounts'][account]
            # get_discussions_by_comments
            raise Exception("not implemented")
        elif not part[1]: #'blog'
            #state['accounts'][account]
            # get_discussions_by_blog
            raise Exception("not implemented")
        elif part[1] == 'feed':
            #state['accounts'][account]
            # get_discussions_by_feed
            raise Exception("not implemented")
    elif part[1] and part[1][0] == '@':
        # pull a complete discussion (recursively fetch content)
        pass
    elif part[0] == 'witnesses':
        pass
    elif part[0] == 'trending':
        # get_discussions_by_trending
        pass
    elif part[0] == 'promoted':
        # get_discussions_by_promoted
        pass
    elif part[0] == 'hot':
        # get_discussions_by_hot
        pass
    elif part[0] == 'created':
        # get_discussions_by_created
        pass
    elif part[0] == "tags":
        # get_trending_tags
        # state['tag_idx']['trending'] << t.name
        # state['tags][t.name] << t
        pass
    else:
       raise Exception("unknown path {}".format(path))

    # iterate through state.content, pull out accounts
    # (only needed for discussion? only post.author_rep used for lists?)
    # for account in accouts:
    #   state.accounts[account.name] = extended_account

    raise Exception("unrecognized path {}".format(path))


async def get_content(author: str, permlink: str):
    post_id = _get_post_id(author, permlink)
    return _get_posts([post_id])[0]


async def get_content_replies(parent: str, parent_permlink: str):
    post_id = _get_post_id(parent, parent_permlink)
    post_ids = query_col("SELECT id FROM hive_posts WHERE parent_id = %d" % post_id)
    return _get_posts(post_ids)


# sort can be trending, hot, new, promoted
def _get_discussions(sort, start_author, start_permlink, limit, tag, context=None):
    limit = _validate_limit(limit, 20)

    col = ''
    where = []
    if sort == 'trending':
        col = 'sc_trend'
    elif sort == 'hot':
        col = 'sc_hot'
    elif sort == 'created':
        col = 'post_id'
        where.append('depth = 0')
    elif sort == 'promoted':
        col = 'promoted'
        where.append('is_paidout = 0')
        where.append('promoted > 0')
    else:
        raise Exception("unknown sort order {}".format(sort))

    if tag:
        tagged_posts = "SELECT post_id FROM hive_post_tags WHERE tag = :tag"
        where.append("post_id IN (%s)" % tagged_posts)

    start_id = None
    if start_permlink:
        start_id = _get_post_id(start_author, start_permlink)
        sql = ("SELECT %s FROM hive_posts_cache %s ORDER BY %s DESC LIMIT 1"
               % (col, _where([*where, "post_id = :start_id"]), col))
        where.append("%s <= (%s)" % (col, sql))

    sql = ("SELECT post_id FROM hive_posts_cache %s ORDER BY %s DESC LIMIT :limit"
           % (_where(where), col))
    ids = query_col(sql, tag=tag, start_id=start_id, limit=limit)
    return _get_posts(ids, context)


def _where(conditions):
    if not conditions:
        return ''
    return 'WHERE ' + ' AND '.join(conditions)

def _validate_limit(limit, ubound=100):
    limit = int(limit)
    if limit <= 0:
        raise Exception("invalid limit")
    if limit > ubound:
        raise Exception("limit exceeded")
    return limit

def _follow_type_to_int(follow_type: str):
    if follow_type not in ['blog', 'ignore']:
        raise Exception("Invalid follow_type")
    return 1 if follow_type == 'blog' else 2

def _get_post_id(author, permlink):
    sql = "SELECT id FROM hive_posts WHERE author = :a AND permlink = :p"
    return query_one(sql, a=author, p=permlink)

def _get_account_id(name):
    return query_one("SELECT id FROM hive_accounts WHERE name = :n", n=name)

# given an array of post ids, returns full metadata in the same order
def _get_posts(ids, context=None):
    # TODO: output format must match steemd
    sql = """
    SELECT post_id, author, permlink, title, preview, img_url, payout,
           promoted, created_at, payout_at, is_nsfw, rshares, votes, json
      FROM hive_posts_cache WHERE post_id IN :ids
    """

    reblogged_ids = []
    if context:
        reblogged_ids = query_col("SELECT post_id FROM hive_reblogs WHERE "
                                  "account = :a AND post_id IN :ids",
                                  a=context, ids=tuple(ids))

    # key by id so we can return sorted by input order
    posts_by_id = {}
    for row in query(sql, ids=tuple(ids)).fetchall():
        obj = dict(row)

        if context:
            voters = [csa.split(",")[0] for csa in obj['votes'].split("\n")]
            obj['user_state'] = {
                'reblogged': row['post_id'] in reblogged_ids,
                'voted': context in voters
            }

        # TODO: Object of type 'Decimal' is not JSON serializable
        obj['payout'] = float(obj['payout'])
        obj['promoted'] = float(obj['promoted'])

        # TODO: Object of type 'datetime' is not JSON serializable
        obj['created_at'] = str(obj['created_at'])
        obj['payout_at'] = str(obj['payout_at'])

        obj.pop('votes') # temp
        obj.pop('json')  # temp

        obj.pop('preview')

        posts_by_id[row['post_id']] = obj

    # in rare cases of cache inconsistency, recover and warn
    missed = set(ids) - posts_by_id.keys()
    if missed:
        print("WARNING: get_posts do not exist in cache: {}".format(missed))
        for _id in missed:
            ids.remove(_id)

    return [posts_by_id[_id] for _id in ids]
