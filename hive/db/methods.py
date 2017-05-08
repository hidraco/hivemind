from funcy.seqs import first
from hive.db import conn
from hive.db.schema import (
    hive_follows,
)
from sqlalchemy import text, select


# generic
# -------
def query(sql):
    res = conn.execute(text(sql).execution_options(autocommit=False))
    return res


def query_one(sql):
    res = conn.execute(text(sql))
    row = first(res)
    if row:
        return first(row)


def db_last_block():
    return query_one("SELECT MAX(num) FROM hive_blocks") or 0


# api specific
# ------------
def get_followers(account: str, skip: int, limit: int, db=conn):
    q = select([hive_follows]). \
        where(hive_follows.c.following == account). \
        skip(skip).limit(limit)
    return db.execute(q)


def get_following(account: str, skip: int, limit: int, db=conn):
    q = select([hive_follows]). \
        where(hive_follows.c.follower == account). \
        skip(skip).limit(limit)
    return db.execute(q)
